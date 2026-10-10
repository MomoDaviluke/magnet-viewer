"""单文件种子的端到端验证（磁力链与本地种子两条入口各跑一遍）。

单文件种子（单个 .mkv/.mp4，BT 场景极常见）的落盘路径与多文件不同：
libtorrent 直接存为 ``save_path/name``，而多文件种子是 ``save_path/root/inner``。
本项目此前统一构造成 ``root/root``，单文件时会多套一层目录，导致：
  - file_disk_path 指向不存在的路径；
  - 主窗口「磁盘路径 → 文件」映射键错位，_pieces_map / _demand_range 全部落空；
  - 流服务 url_for(f.path) 请求 404，预览彻底打不开。

用法：python tests/single_file_test.py
"""
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import libtorrent as lt  # noqa: E402

from core.fetcher import SessionManager  # noqa: E402
from core.models import file_disk_path, human_size  # noqa: E402
from core.stream_server import StreamServer  # noqa: E402

SEED_PORT, PEER_PORT = 6921, 6922
SIZE = 600 * 1024
OK, FAIL = [], []


def check(cond, msg):
    (OK if cond else FAIL).append(msg)
    print(("  [OK] " if cond else "  [FAIL] ") + msg)


def make_single(tmp: str, name: str = "BigMovie.mkv") -> tuple[str, str, str]:
    """返回 (源文件路径, 种子文件路径, payload 父目录)。"""
    src = os.path.join(tmp, "src")
    os.makedirs(src, exist_ok=True)
    p = os.path.join(src, name)
    with open(p, "wb") as f:
        f.write(os.urandom(SIZE))
    fs = lt.file_storage()
    lt.add_files(fs, p)
    ct = lt.create_torrent(fs, 16 * 1024)
    lt.set_piece_hashes(ct, src)
    tp = os.path.join(tmp, "single.torrent")
    with open(tp, "wb") as f:
        f.write(lt.bencode(ct.generate()))
    return p, tp, src


def start_seeder(tp: str, src: str, port: int):
    ses = lt.session({
        "listen_interfaces": f"0.0.0.0:{port}",
        "enable_dht": False, "enable_lsd": False,
        "enable_upnp": False, "enable_natpmp": False,
    })
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(tp)
    atp.save_path = src
    atp.flags |= lt.torrent_flags.seed_mode
    ses.add_torrent(atp).resume()
    return ses


def run_case(label: str, tmp: str, source: str, info_hash: str,
             seed_src: str, seed_tp: str) -> None:
    print(f"\n----- {label} -----")
    seed_ses = start_seeder(seed_tp, seed_src, SEED_PORT)
    time.sleep(1)

    cache = os.path.join(tmp, "cache")
    got, err = [], []
    mgr = SessionManager(cache, listen_port=PEER_PORT)
    mgr.on_metadata = got.append
    mgr.on_error = err.append
    mgr.start()
    mgr.resolve(source)
    mgr.connect_peer("127.0.0.1", SEED_PORT)

    t0 = time.time()
    while time.time() - t0 < 60 and not got and not err:
        time.sleep(0.2)
    if not got:
        check(False, f"元数据获取失败：{err[0] if err else '超时'}")
        mgr.shutdown()
        return

    r = got[0]
    check(r.info_hash == info_hash, "info_hash 一致")
    check(len(r.view_files) == 1, f"可见文件 1 个（实际 {len(r.view_files)}）")
    vid = r.view_files[0]

    # 关键：构造的 path 必须与 libtorrent 真实落盘位置一致
    expect_rel = os.path.basename(seed_src)  # 占位
    # 目录隔离（决策 D7）：预览落盘 cache_dir/.preview/<ih>/
    # （r.save_subdir 由 SessionManager 注入；接口未变，仅断言路径随布局更新）
    save_dir = os.path.join(mgr.cache_dir, *r.save_subdir.split("/")) \
        if getattr(r, "save_subdir", "") else mgr.cache_dir
    disk = file_disk_path(save_dir, vid)
    check(len(vid.path.split("/")) == 1,
          f"单文件 path 不含多余目录层：{vid.path!r}")

    mgr.start_preview(vid)
    t1 = time.time()
    done = 0
    while time.time() - t1 < 60:
        st = mgr.status()
        prog = (st["file_progress"][vid.index]
                if len(st["file_progress"]) > vid.index else 0)
        done = prog
        if prog >= vid.size:
            break
        time.sleep(0.5)
    check(done >= vid.size,
          f"下载完成：{human_size(done)}/{human_size(vid.size)}")
    check(os.path.isfile(disk), f"file_disk_path 指向真实文件：{disk}")
    if not os.path.isfile(disk):
        found = [os.path.relpath(os.path.join(d, n), mgr.cache_dir)
                 for d, _, ns in os.walk(mgr.cache_dir) for n in ns]
        print(f"        缓存目录实际内容：{found}")

    # 流服务必须能按 f.path 提供数据（此前会 404）
    srv = StreamServer(mgr.cache_dir)
    srv.start()
    try:
        # 目录隔离（D7）：服务相对路径要带上 save_subdir 前缀才能命中真实文件
        rel = "/".join([s for s in r.save_subdir.split("/") if s] + [vid.path]) \
            if getattr(r, "save_subdir", "") else vid.path
        req = urllib.request.Request(srv.url_for(rel),
                                     headers={"Range": "bytes=0-1023"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            status = resp.status
        check(status == 206 and len(body) == 1024,
              f"流服务可按 f.path 供给（status={status}, {len(body)}B）")
    except urllib.error.HTTPError as e:
        check(False, f"流服务请求失败：HTTP {e.code}（url={srv.url_for(rel)}）")
    finally:
        srv.shutdown()

    mgr.shutdown()
    del seed_ses


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mv_single_")
    src_path, tp, src = make_single(tmp)
    ti = lt.torrent_info(tp)
    info_hash = str(ti.info_hash())
    print(f"[0] 单文件种子：{os.path.basename(src_path)}"
          f"（{human_size(SIZE)}），info_hash={info_hash}")

    run_case("入口 A：本地 .torrent 文件", os.path.join(tmp, "a"),
             tp, info_hash, src, tp)
    run_case("入口 B：磁力链", os.path.join(tmp, "b"),
             f"magnet:?xt=urn:btih:{info_hash}", info_hash, src, tp)

    shutil.rmtree(tmp, ignore_errors=True)
    print(f"\n{'=' * 56}")
    print(f"通过 {len(OK)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  FAIL:", f)
    print("=== 单文件种子验证通过 ===" if not FAIL
          else "=== 单文件种子验证未通过 ===")
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
