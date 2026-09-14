"""生成安卓端到端测试用的固定夹具：一段小视频 + 对应 .torrent + 带 x.pe 的磁力链。

为什么需要它：模拟器里如果只靠公网 DHT 找 peer，测试会是「有时通有时不通」的薛定谔状态，
无法判定失败到底是 App 的问题还是网络的问题。这里用 BEP-9 的 x.pe（peer 提示）参数，
让磁力链直接指向本机做种端 —— 模拟器访问 10.0.2.2 等价于宿主机的 127.0.0.1。

用法：
    ./.venv/Scripts/python android/tools/testrig/mkfixture.py
输出：
    android/tools/testrig/payload/fixture.mp4         （faststart，moov 在前）
    android/tools/testrig/payload/fixture-tail.mp4    （moov 在尾部，用于验证尾部窗口调度）
    android/tools/testrig/fixture.torrent
    android/tools/testrig/magnet.txt                  （含 x.pe=10.0.2.2:<SEED_PORT>）
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import libtorrent as lt

SEED_PORT = 16881
ROOT = pathlib.Path(__file__).resolve().parent          # android/tools/testrig
PAYLOAD = ROOT / "payload"
TORRENT = ROOT / "fixture.torrent"
MAGNET = ROOT / "magnet.txt"


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
    print(f"\n做种端启动：./.venv/Scripts/python android/tools/testrig/seed.py  (端口 {SEED_PORT})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
