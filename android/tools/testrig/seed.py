"""在宿主机上做种，供模拟器里的 App 通过 x.pe 直连（10.0.2.2 = 宿主机回环）。

用法：
    ./.venv/Scripts/python android/tools/testrig/seed.py                       # 小夹具（fixture.torrent）
    ./.venv/Scripts/python android/tools/testrig/seed.py --torrent fixture_big.torrent \
        --payload payload_big --limit-kbps 300                                # 大夹具 + 限速（灭屏/后台测试）

为什么要有 --limit-kbps：小夹具 88KB 一眨眼就下完，测不出"灭屏后是否继续下载"。给做种端限上传，
把传输拉长到分钟级，才有可观测的进度增长。

行为：加载种子、做种、每 5 秒打印 peer 数/上传量/状态；Ctrl-C 退出。这个进程要一直活着。
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

import libtorrent as lt

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_PORT = 16881


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--torrent", default="fixture.torrent", help="种子文件名（相对本目录）")
    ap.add_argument("--payload", default="payload", help="内容目录名（相对本目录）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--limit-kbps", type=int, default=0, help="上传限速 KB/s，0=不限")
    ap.add_argument("--interval", type=float, default=5.0, help="状态打印间隔秒")
    args = ap.parse_args(argv[1:])

    torrent = ROOT / args.torrent
    payload = ROOT / args.payload
    if not torrent.exists():
        print(f"缺少 {torrent}，先跑 mkfixture.py", file=sys.stderr)
        return 2

    ses = lt.session({
        # 端口写死：用 :0 时 libtorrent 会按网卡各绑不同随机端口，listen_port() 只返回其中一个，
        # 对端会拨错端口（实测连接被拒 10061）。
        "listen_interfaces": f"0.0.0.0:{args.port}",
        "enable_dht": True,          # 打开，方便观察 DHT 侧是否也能互通
        "enable_lsd": True,
        "enable_upnp": False,
        "enable_natpmp": False,
        "alert_mask": lt.alert.category_t.all_categories,
    })
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(str(torrent))
    # save_path 必须是"种子内容根目录"的父目录：种子里文件路径是 <种子名>/xxx，
    # 指向内容目录本身会让做种端永远停在 downloading（它在找 <内容目录>/<种子名>/…）。
    atp.save_path = str(ROOT)
    handle = ses.add_torrent(atp)
    handle.set_flags(lt.torrent_flags.seed_mode)
    if args.limit_kbps > 0:
        handle.set_upload_limit(args.limit_kbps * 1024)
    # 注意：libtorrent 2.1 的 info_hash() 对混合种子返回 v2 截断哈希；磁力链的 btih 是 v1。
    print(f"做种中：{atp.ti.name()}  {atp.ti.total_size()} 字节  "
          f"infohash(v1)={atp.ti.info_hashes().v1}  监听 0.0.0.0:{args.port}"
          + (f"  上传限速 {args.limit_kbps} KB/s" if args.limit_kbps else ""), flush=True)
    print(f"内容目录：{payload}", flush=True)

    try:
        while True:
            time.sleep(args.interval)
            st = handle.status()
            print(f"[seed] peers={st.num_peers} seeds={st.num_seeds} "
                  f"uploaded={st.total_upload / 1024:.1f} KB "
                  f"rate={st.upload_rate / 1024:.0f} KB/s "
                  f"state={st.state}", flush=True)
    except KeyboardInterrupt:
        print("\n停止做种")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
