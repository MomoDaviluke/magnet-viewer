"""闭环验证：本地 .torrent 文件 → 解析 → 边下边播 → 流服务按分块供给。

与 local_magnet_test.py 的区别：那条走「磁力链 + ut_metadata」，本条走
**本地 .torrent 文件**入口。两条入口必须各测一遍——此前正是本地种子入口漏了
`ParseResult.cache_dir` 注入，导致主窗口的「磁盘路径 → 文件」映射键退化成相对路径，
`_pieces_map` / `_demand_range` 全部查不到，预览静默退化为「按完整静态文件服务」
（把未下载的稀疏零数据喂给播放器）。

本测试在 127.0.0.1 上同时启动做种端与解析端，无需外网。
用法：python local_torrent_test.py
"""
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import libtorrent as lt  # noqa: E402

from core.fetcher import SessionManager  # noqa: E402
from core.models import PieceMap, file_disk_path, human_size  # noqa: E402
from core.stream_server import StreamServer  # noqa: E402

SEED_PORT, PEER_PORT = 6911, 6912
TIMEOUT_META, TIMEOUT_DATA = 60, 60
OK, FAIL = [], []


def check(cond, msg):
    (OK if cond else FAIL).append(msg)
    print(("  [OK] " if cond else "  [FAIL] ") + msg)


def build_payload(root: str) -> str:
    data = {
        "movie/demo.mp4": 900 * 1024,
        "pics/a.jpg": 120 * 1024,
        "pics/b.png": 60 * 1024,
        "readme.txt": 512,
    }
    for rel, n in data.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(os.urandom(n))
    return root


def write_torrent(payload: str, out: str) -> lt.torrent_info:
    fs = lt.file_storage()
    lt.add_files(fs, payload)
    ct = lt.create_torrent(fs)
    lt.set_piece_hashes(ct, os.path.dirname(payload))
    ti = lt.torrent_info(ct.generate())
    with open(out, "wb") as f:
        f.write(lt.bencode(ct.generate()))
    return ti


def start_seeder(ti: lt.torrent_info, payload_parent: str):
    ses = lt.session({
        "listen_interfaces": f"0.0.0.0:{SEED_PORT}",
        "enable_dht": False, "enable_lsd": False, "enable_upnp": False,
        "enable_natpmp": False,
    })
    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = payload_parent
    atp.flags |= lt.torrent_flags.seed_mode
    ses.add_torrent(atp).resume()
    return ses


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mv_torr_")
    payload = build_payload(os.path.join(tmp, "payload"))
    tpath = os.path.join(tmp, "local.torrent")
    ti = write_torrent(payload, tpath)
    info_hash = str(ti.info_hash())
    print(f"[1] 数据与本地种子已就绪：{tpath}")
    print(f"    info_hash = {info_hash}")

    seed_ses = start_seeder(ti, os.path.dirname(payload))
    time.sleep(1)
    print(f"[2] 做种端启动：127.0.0.1:{SEED_PORT}")

    got, err = [], []
    cache = os.path.join(tmp, "cache")
    mgr = SessionManager(cache, listen_port=PEER_PORT)
    mgr.on_metadata = got.append
    mgr.on_error = err.append
    mgr.start()
    # 关键：走本地 .torrent 文件入口（而非磁力链）
    mgr.resolve(tpath)
    mgr.connect_peer("127.0.0.1", SEED_PORT)
    print("[3] 解析端已加入（来源为本地 .torrent 文件）")

    t0 = time.time()
    while time.time() - t0 < TIMEOUT_META and not got and not err:
        st = mgr.status()
        print(f"\r    等待元数据 {int(time.time() - t0):2d}s  状态={st['state']} "
              f"连接={st['num_peers']}   ", end="", flush=True)
        time.sleep(1)
    print()

    if not got:
        print(f"\n✗ 元数据获取失败：{err[0] if err else '超时'}")
        mgr.shutdown()
        return 1

    r = got[0]
    print(f"\n[4] 元数据获取成功（{time.time() - t0:.1f}s）：可见文件 "
          f"{len(r.view_files)} 个")

    # ---- 核心断言：cache_dir 必须已注入，映射键必须是绝对路径 ----
    print("\n[5] 路径映射（此前失效的环节）")
    check(r.cache_dir == os.path.abspath(cache),
          f"cache_dir 已注入且一致：{r.cache_dir}")
    # 目录隔离（决策 D7）：解析/预览落盘 cache_dir/.preview/<ih>/
    # （r.save_subdir 由 SessionManager 注入；接口未变，仅断言路径随布局更新）
    save_dir = os.path.join(mgr.cache_dir, *r.save_subdir.split("/")) \
        if getattr(r, "save_subdir", "") else mgr.cache_dir
    check(save_dir != mgr.cache_dir and save_dir.startswith(os.path.join(
        mgr.cache_dir, ".preview")),
        f"save_subdir 已注入（.preview/<ih> 隔离）：{r.save_subdir}")
    path_map = {os.path.normpath(file_disk_path(save_dir, f)): f
                for f in r.files}
    check(all(os.path.isabs(k) for k in path_map),
          f"全部 {len(path_map)} 个映射键均为绝对路径")
    vid = next((f for f in r.view_files if f.is_video), None)
    check(vid is not None, "找到视频文件")
    if vid is None:
        mgr.shutdown()
        return 1
    vid_disk = os.path.normpath(file_disk_path(save_dir, vid))
    check(vid_disk in path_map, f"可按绝对路径命中预览目标（{vid.name}）")

    # ---- 边下边播 ----
    mgr.start_preview(vid)
    print(f"\n[6] 已开始预览下载：{vid.name}")
    t1 = time.time()
    done = 0
    while time.time() - t1 < TIMEOUT_DATA:
        st = mgr.status()
        prog = (st["file_progress"][vid.index]
                if len(st["file_progress"]) > vid.index else 0)
        done = prog
        print(f"\r    缓冲 {st['buffer'] * 100:5.1f}%  {human_size(prog)}/"
              f"{human_size(vid.size)}  速度 {human_size(st['download_rate'])}/s   ",
              end="", flush=True)
        if prog >= vid.size:
            break
        time.sleep(1)
    print()
    check(done >= vid.size, f"预览文件下载完成：{human_size(done)}/{human_size(vid.size)}")
    check(os.path.isfile(vid_disk) and os.path.getsize(vid_disk) == vid.size,
          f"磁盘文件一致（{os.path.basename(vid_disk)}）")

    # ---- 缓冲分段（进度条着色数据源）：全部下载后应覆盖 ≥99% ----
    segs = mgr.buffered_segments_of_preview()
    check(bool(segs), "预览缓冲分段非空（buffered_segments_of_preview）")
    covered = sum(e - s for s, e in (segs or []))
    check(covered >= vid.size * 0.99,
          f"分段覆盖 ≥99%（{human_size(covered)}/{human_size(vid.size)}，"
          f"{len(segs or [])} 段）")

    # ---- 流服务按分块供给：证明 _pieces_map 链路打通 ----
    print("\n[7] 流服务 + 路径映射联动")
    hits = []

    def pieces_cb(disk_path):
        f = path_map.get(os.path.normpath(disk_path))
        hits.append(f is not None)
        if f is None:
            return None
        pl = mgr.piece_length()
        if not pl:
            return None
        return PieceMap(piece_length=pl, offset=f.offset,
                        start_piece=f.start_piece, end_piece=f.end_piece,
                        size=f.size, have=mgr.have_piece)

    srv = StreamServer(mgr.cache_dir, pieces_cb=pieces_cb)
    srv.start()
    # 目录隔离（D7）：服务相对路径要带上 save_subdir 前缀才能命中真实文件
    rel = "/".join([s for s in r.save_subdir.split("/") if s] + [vid.path]) \
        if getattr(r, "save_subdir", "") else vid.path
    url = srv.url_for(rel)
    req = urllib.request.Request(url, headers={"Range": "bytes=0-65535"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read()
        status = resp.status
    with open(vid_disk, "rb") as f:
        f.seek(0)
        expect = f.read(65536)
    srv.shutdown()

    check(bool(hits) and all(hits),
          f"流服务回调全部命中路径映射（{sum(hits)}/{len(hits)} 次）")
    check(status == 206 and len(body) == 65536,
          f"Range 请求返回 206 且长度正确（status={status}, {len(body)}B）")
    check(body == expect, "返回内容与磁盘字节完全一致（未夹带稀疏零数据）")

    mgr.shutdown()
    del seed_ses
    shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{'=' * 56}")
    print(f"通过 {len(OK)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  FAIL:", f)
    print("=== 本地 .torrent 闭环验证通过 ===" if not FAIL
          else "=== 本地 .torrent 闭环验证未通过 ===")
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
