"""在宿主机上做种，供模拟器里的 App 通过 x.pe 直连（10.0.2.2 = 宿主机回环）。

用法：
    ./.venv/Scripts/python android/tools/testrig/seed.py [端口，默认 16881]

行为：加载 fixture.torrent，把 payload/ 目录做种；每 10 秒打印一次 peer 连接与上传量，
Ctrl-C 退出。这个进程要一直活着，模拟器那边的解析/下载才有 peer。
"""
from __future__ import annotations

import pathlib
import sys
import time

import libtorrent as lt

ROOT = pathlib.Path(__file__).resolve().parent
TORRENT = ROOT / "fixture.torrent"
PAYLOAD = ROOT / "payload"
DEFAULT_PORT = 16881


def main(argv: list[str]) -> int:
    port = int(argv[1]) if len(argv) > 1 else DEFAULT_PORT
    if not TORRENT.exists():
        print(f"缺少 {TORRENT}，先跑 mkfixture.py", file=sys.stderr)
        return 2

    ses = lt.session({
        "listen_interfaces": f"0.0.0.0:{port}",
        "enable_dht": True,          # 打开，方便观察 DHT 侧是否也能互通
        "enable_lsd": True,
        "enable_upnp": False,
        "enable_natpmp": False,
        "alert_mask": lt.alert.category_t.all_categories,
    })
    atp = lt.add_torrent_params()
    atp.ti = lt.torrent_info(str(TORRENT))
    atp.save_path = str(ROOT)      # 注意：种子内部根目录名就是 payload，save_path 必须是它的父目录
    handle = ses.add_torrent(atp)
    handle.set_flags(lt.torrent_flags.seed_mode)
    # 注意：libtorrent 2.1 的 info_hash() 返回 v2 截断哈希（混合种子时），
    # 磁力链里的 btih 是 v1，必须用 info_hashes().v1 对比，否则会误判"hash 不匹配"。
    print(f"做种中：{atp.ti.name()}  {atp.ti.total_size()} 字节  "
          f"infohash(v1)={atp.ti.info_hashes().v1}  监听 0.0.0.0:{port}")
    print("（模拟器里的磁力链已带 x.pe=10.0.2.2:%d，直接粘贴即可）" % port)

    try:
        while True:
            time.sleep(10)
            st = handle.status()
            print(f"[seed] peers={st.num_peers} seeds={st.num_seeds} "
                  f"uploaded={st.total_upload / 1024:.1f} KB "
                  f"state={st.state}", flush=True)
    except KeyboardInterrupt:
        print("\n停止做种")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
