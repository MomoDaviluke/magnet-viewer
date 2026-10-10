"""真实链路验证：用磁力链（非 .torrent）从 DHT 网络获取元数据。

用法：
    python tests/live_test.py <torrent文件的info_hash或torrent路径>

不传参数时默认使用 Ubuntu 24.04 官方种子（合法分发）生成磁力链做验证。
仅获取元数据（文件清单），不下载资源本体。
"""
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.fetcher import SessionManager  # noqa: E402
from core.models import human_size  # noqa: E402
from core.parser import parse_torrent_file  # noqa: E402

UBUNTU_URL = "https://releases.ubuntu.com/24.04/ubuntu-24.04.3-desktop-amd64.iso.torrent"
TIMEOUT = 120


def build_magnet(result) -> str:
    params = [f"xt=urn:btih:{result.info_hash}",
              f"dn={urllib.parse.quote(result.name)}"]
    for tr in result.trackers[:8]:
        params.append(f"tr={urllib.parse.quote(tr)}")
    return "magnet:?" + "&".join(params)


def main():
    if len(sys.argv) > 1:
        src = sys.argv[1]
    else:
        src = os.path.join(tempfile.gettempdir(), "ubuntu24.torrent")
        if not os.path.isfile(src):
            print("下载 Ubuntu 官方种子…")
            urllib.request.urlretrieve(UBUNTU_URL, src)

    base = parse_torrent_file(src)
    magnet = build_magnet(base)
    print(f"种子：{base.name}")
    print(f"info_hash：{base.info_hash}")
    print(f"磁力链：{magnet[:110]}…")
    print(f"期望：{len(base.view_files)} 个文件 / {human_size(base.total_size)}\n")

    mgr = SessionManager(os.path.join(tempfile.gettempdir(), "mv_live_cache"))
    out, err = [], []
    mgr.on_metadata = lambda r: out.append(r)
    mgr.on_error = lambda m: err.append(m)
    mgr.start()
    mgr.resolve(magnet)

    t0 = time.time()
    while time.time() - t0 < TIMEOUT and not out and not err:
        st = mgr.status()
        if st:
            print(f"\r[{int(time.time() - t0):3d}s] 状态={st['state']} "
                  f"做种={st['num_seeds']} 连接={st['num_peers']}   ", end="", flush=True)
        time.sleep(1)

    print()
    if out:
        r = out[0]
        ok_i = r.info_hash == base.info_hash
        ok_n = len(r.view_files) == len(base.view_files)
        print(f"\n✓ 元数据获取成功（用时 {time.time() - t0:.1f}s）")
        print(f"  info_hash 一致：{ok_i}   文件数一致：{ok_n}（{len(r.view_files)}）")
        for f in r.view_files[:5]:
            print(f"   - {f.name}  {human_size(f.size)}")
        if len(r.view_files) > 5:
            print(f"   … 其余 {len(r.view_files) - 5} 个文件")
        print("\n=== 真实链路验证通过 ===" if (ok_i and ok_n) else "\n=== 结果不一致，需检查 ===")
    else:
        print(f"\n✗ 获取失败：{err[0] if err else '超时'}（当前网络可能屏蔽 BT/DHT 出站）")
    mgr.shutdown()


if __name__ == "__main__":
    main()
