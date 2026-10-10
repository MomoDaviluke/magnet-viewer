"""闭环验证（无需外网）：本机做种 + 磁力链解析 + 边下边播调度。

沙箱/内网环境往往屏蔽 BT 与 DHT 出站，导致真实磁力链无法验证。
本脚本在 127.0.0.1 上同时启动：
  A) 做种端（seed_mode，提供真实数据）
  B) 解析端（SessionManager，只用磁力链，手动 connect_peer 直连 A）

从而完整验证：磁力链 → ut_metadata 获取元数据 → 单文件优先级 + 分块顺序下载。
用法：python tests/local_magnet_test.py
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import libtorrent as lt  # noqa: E402

from core.fetcher import SessionManager  # noqa: E402
from core.models import file_disk_path, human_size  # noqa: E402

SEED_PORT, PEER_PORT = 6901, 6902
TIMEOUT_META, TIMEOUT_DATA = 60, 40


def build_payload(root: str) -> str:
    """构造一个含视频/图片/文本的目录，返回其路径。"""
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


def make_torrent(payload: str) -> lt.torrent_info:
    fs = lt.file_storage()
    lt.add_files(fs, payload)
    ct = lt.create_torrent(fs)
    lt.set_piece_hashes(ct, os.path.dirname(payload))
    return lt.torrent_info(ct.generate())


def start_seeder(ti: lt.torrent_info, payload_parent: str):
    ses = lt.session({
        "listen_interfaces": f"0.0.0.0:{SEED_PORT}",
        "enable_dht": False, "enable_lsd": False, "enable_upnp": False,
        "enable_natpmp": False,
    })
    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = payload_parent
    atp.flags |= lt.torrent_flags.seed_mode  # 数据已完整，跳过校验
    h = ses.add_torrent(atp)
    h.resume()
    return ses, h


def main():
    tmp = tempfile.mkdtemp(prefix="mv_loop_")
    payload = build_payload(os.path.join(tmp, "payload"))
    ti = make_torrent(payload)
    info_hash = str(ti.info_hash())
    total = sum(os.path.getsize(os.path.join(d, n))
                for d, _, ns in os.walk(payload) for n in ns)
    print(f"[1] 数据已就绪：{payload}（{human_size(total)}）")
    print(f"    info_hash = {info_hash}")

    seed_ses, seed_h = start_seeder(ti, os.path.dirname(payload))
    time.sleep(1)
    print(f"[2] 做种端启动：127.0.0.1:{SEED_PORT}")

    got, err = [], []
    mgr = SessionManager(os.path.join(tmp, "cache"), listen_port=PEER_PORT)
    mgr.on_metadata = lambda r: got.append(r)
    mgr.on_error = lambda m: err.append(m)
    mgr.start()
    mgr.resolve(f"magnet:?xt=urn:btih:{info_hash}&dn=demo-payload")
    mgr.connect_peer("127.0.0.1", SEED_PORT)
    print(f"[3] 解析端已加入磁力链（来源仅磁力链，未给本地种子文件）")

    t0 = time.time()
    while time.time() - t0 < TIMEOUT_META and not got and not err:
        st = mgr.status()
        print(f"\r    等待元数据 {int(time.time() - t0):2d}s  状态={st['state']} "
              f"连接={st['num_peers']} 做种={st['num_seeds']}   ", end="", flush=True)
        time.sleep(1)
    print()

    if not got:
        print(f"\n✗ 元数据获取失败：{err[0] if err else '超时'}")
        mgr.shutdown()
        seed_ses = None
        return 1

    r = got[0]
    ok_hash = r.info_hash == info_hash
    n_vis = len(r.view_files)
    print(f"\n[4] 元数据获取成功（{time.time() - t0:.1f}s）：info_hash 一致={ok_hash}，"
          f"可见文件 {n_vis} 个")
    for f in r.view_files:
        print(f"    - {f.name}  {human_size(f.size)}")

    vid = next((f for f in r.view_files if f.is_video), None)
    if vid is None:
        print("✗ 未找到视频文件")
        return 1

    mgr.start_preview(vid)
    print(f"[5] 已开始预览下载：{vid.name}（piece {vid.start_piece}~{vid.end_piece}）")
    t1 = time.time()
    done = 0
    while time.time() - t1 < TIMEOUT_DATA:
        st = mgr.status()
        prog = st["file_progress"][vid.index] if len(st["file_progress"]) > vid.index else 0
        done = prog
        print(f"\r    缓冲 {st['buffer'] * 100:5.1f}%  {human_size(prog)}/"
              f"{human_size(vid.size)}  速度 {human_size(st['download_rate'])}/s   ",
              end="", flush=True)
        if prog >= vid.size:
            break
        time.sleep(1)
    print()

    ok_data = done >= vid.size
    print(f"\n[6] 预览文件下载{'完成' if ok_data else '未达预期'}：{human_size(done)}/{human_size(vid.size)}")
    # 目录隔离（决策 D7）：review/预览落盘 cache_dir/.preview/<ih>/
    # （r.save_subdir 由 SessionManager 注入；接口未变，仅断言路径随布局更新）
    save_dir = os.path.join(mgr.cache_dir, *r.save_subdir.split("/")) \
        if getattr(r, "save_subdir", "") else mgr.cache_dir
    disk = file_disk_path(save_dir, vid)
    ok_disk = os.path.isfile(disk) and os.path.getsize(disk) == vid.size
    print(f"    磁盘文件一致：{ok_disk}（{disk}）")

    mgr.shutdown()
    del seed_ses
    shutil.rmtree(tmp, ignore_errors=True)

    ok = ok_hash and n_vis == 4 and ok_data and ok_disk
    print("\n=== 闭环验证通过：磁力链解析 + 边下边播调度全部生效 ===" if ok
          else "\n=== 闭环验证未通过，见上方输出 ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
