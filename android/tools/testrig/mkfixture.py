"""生成安卓端到端测试用的固定夹具：一段小视频 + 对应 .torrent + 带 x.pe 的磁力链。

为什么需要它：模拟器里如果只靠公网 DHT 找 peer，测试会是「有时通有时不通」的薛定谔状态，
无法判定失败到底是 App 的问题还是网络的问题。这里用 BEP-9 的 x.pe（peer 提示）参数，
让磁力链直接指向本机做种端 —— 模拟器访问 10.0.2.2 等价于宿主机的 127.0.0.1。

用法：
    ./.venv/Scripts/python .hermes/android-test/mkfixture.py
输出：
    .hermes/android-test/payload/fixture.mp4         （faststart，moov 在前）
    .hermes/android-test/payload/fixture-tail.mp4    （moov 在尾部，用于验证尾部窗口调度）
    .hermes/android-test/fixture.torrent
    .hermes/android-test/magnet.txt                  （含 x.pe=10.0.2.2:<SEED_PORT>）
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import libtorrent as lt

SEED_PORT = 16881
ROOT = pathlib.Path(__file__).resolve().parent          # .hermes/android-test
PAYLOAD = ROOT / "payload"
TORRENT = ROOT / "fixture.torrent"
MAGNET = ROOT / "magnet.txt"
# 大夹具（灭屏/后台保活测试用：文件够大，配合做种端限速才测得出来）
PAYLOAD_BIG = ROOT / "payload_big"
TORRENT_BIG = ROOT / "fixture_big.torrent"
MAGNET_BIG = ROOT / "magnet_big.txt"
BIG_BYTES = 24 * 1024 * 1024


def ffmpeg() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def make_videos() -> None:
    PAYLOAD.mkdir(parents=True, exist_ok=True)
    testsrc = "testsrc=size=640x360:rate=24:duration=20"
    # 720KB 量级的小文件：本地传输瞬时完成，测试聚焦在「调度/流服务/播放」而非带宽
    jobs = [
        (PAYLOAD / "fixture.mp4", ["-movflags", "+faststart"]),
        (PAYLOAD / "fixture-tail.mp4", []),          # 不加 faststart → moov 落尾部
    ]
    for out, extra in jobs:
        if out.exists() and out.stat().st_size > 0:
            print(f"已存在，跳过：{out.name} ({out.stat().st_size} 字节)")
            continue
        cmd = [ffmpeg(), "-y", "-f", "lavfi", "-i", testsrc,
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "34",
               "-pix_fmt", "yuv420p", "-an", *extra, str(out)]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"生成：{out.name} ({out.stat().st_size} 字节)")


def make_torrent() -> "lt.torrent_info":
    fs = lt.file_storage()
    lt.add_files(fs, str(PAYLOAD))
    t = lt.create_torrent(fs, piece_size=16 * 1024)
    # 故意不加公网 tracker：本地确定性测试里，外部 tracker 会拉进无关 peer，
    # 让"失败到底是 App 的问题还是网络的问题"无法判定。peer 只靠磁力链的 x.pe 提示。
    lt.set_piece_hashes(t, str(PAYLOAD.parent))
    TORRENT.write_bytes(lt.bencode(t.generate()))
    ti = lt.torrent_info(str(TORRENT))
    print(f"种子文件：{TORRENT.name}（{TORRENT.stat().st_size} 字节）")
    print(f"  piece_length={ti.piece_length()}  num_pieces={ti.num_pieces()}  "
          f"files={ti.num_files()}  total={ti.total_size()}")
    return ti


def main() -> int:
    make_videos()
    ti = make_torrent()
    ih = str(ti.info_hashes().v1)
    magnet = (f"magnet:?xt=urn:btih:{ih}&dn=fixture"
              f"&x.pe=10.0.2.2:{SEED_PORT}")
    MAGNET.write_text(magnet + "\n", encoding="utf-8")
    print(f"\ninfohash (v1): {ih}")
    print(f"磁力链（模拟器里直接粘贴）：\n{magnet}")

    # ---- 大夹具：单独一个 payload 目录/种子/磁力链，避免污染上面的小夹具 ----
    PAYLOAD_BIG.mkdir(parents=True, exist_ok=True)
    big = PAYLOAD_BIG / "big.bin"
    if not big.exists() or big.stat().st_size != BIG_BYTES:
        import os
        with open(big, "wb") as f:
            f.write(os.urandom(BIG_BYTES))
        print(f"\n生成大夹具：{big.name}（{BIG_BYTES} 字节）")
    fs2 = lt.file_storage()
    lt.add_files(fs2, str(PAYLOAD_BIG))
    t2 = lt.create_torrent(fs2, piece_size=256 * 1024)
    lt.set_piece_hashes(t2, str(PAYLOAD_BIG.parent))
    TORRENT_BIG.write_bytes(lt.bencode(t2.generate()))
    ti2 = lt.torrent_info(str(TORRENT_BIG))
    ih2 = str(ti2.info_hashes().v1)
    magnet2 = (f"magnet:?xt=urn:btih:{ih2}&dn=fixture_big"
               f"&x.pe=10.0.2.2:{SEED_PORT}")
    MAGNET_BIG.write_text(magnet2 + "\n", encoding="utf-8")
    print(f"大夹具 infohash (v1): {ih2}｜总 {ti2.total_size()} 字节｜分块 {ti2.piece_length()}")
    print(f"大夹具磁力链（配合做种端限速用，见 seed.py --limit-kbps）：\n{magnet2}")
    print(f"\n做种端启动：./.venv/Scripts/python android/tools/testrig/seed.py  (端口 {SEED_PORT})")
    print(f"大夹具做种（限速 300KB/s，灭屏测试用）："
          f"./.venv/Scripts/python android/tools/testrig/seed.py --torrent fixture_big.torrent "
          f"--payload payload_big --limit-kbps 300")
    return 0


if __name__ == "__main__":
    sys.exit(main())
