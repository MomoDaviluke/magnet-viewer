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
BIG_MIN_BYTES = 8 * 1024 * 1024   # 大夹具至少这么大，否则限速下撑不到分钟级

# 图片夹具（画廊/按需下载测试用：一张大图看进度 + 一张小图秒开 + 一张中图翻页）
PAYLOAD_IMG = ROOT / "payload_img"
TORRENT_IMG = ROOT / "fixture_img.torrent"
MAGNET_IMG = ROOT / "magnet_img.txt"
# 图片夹具**专用端口**（与另两套的 16881 分开 → 三套夹具可以同时做种，互不抢端口）
SEED_PORT_IMG = 16920
IMG_PIECE_SIZE = 64 * 1024        # 64KB 分块：3.8MB 的大图 ≈ 60 块，进度有可观的中途态
IMG_BIG_MIN_BYTES = 2 * 1024 * 1024   # 大图至少 2MB（否则「观察下载进度」看不到几帧）
IMG_BIG_MAX_BYTES = 5 * 1024 * 1024   # 也不超过 5MB（模拟器上别把等待拉太长）


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


def make_images() -> None:
    """图片夹具（画廊用）：一张大图（2~5MB，观察下载进度）+ 中图 + 小图（秒开）。

    为什么用 ffmpeg 而不是 PIL：本仓库已依赖 imageio_ffmpeg（视频夹具在用），
    不引入新依赖就能生成**确定性**的图片（testsrc 图案固定，加噪点只为把 PNG 抬到
    2MB 以上——纯 testsrc 图案压缩率极高，1920x1080 的 PNG 只有 44KB）。
    """
    PAYLOAD_IMG.mkdir(parents=True, exist_ok=True)
    specs = [
        # (文件名, lavfi 滤镜图, 编码参数, 大小下限, 说明)
        (PAYLOAD_IMG / "photo_big.png",
         "testsrc=size=1920x1080,noise=alls=35:allf=t", [], IMG_BIG_MIN_BYTES,
         "大图（噪点 PNG ≈3.8MB）：用来观察画廊里的下载进度逐格上涨"),
        (PAYLOAD_IMG / "photo_mid.jpg",
         "testsrc=size=1280x720,noise=alls=25:allf=t", ["-q:v", "3"], 100 * 1024,
         "中图（≈380KB）：翻页用，几秒下完"),
        (PAYLOAD_IMG / "photo_small.jpg",
         "testsrc=size=320x240", ["-q:v", "6"], 1024,
         "小图（≈8KB）：秒开，用来验证「已下载直接出图」"),
    ]
    for out, pattern, extra, min_bytes, note in specs:
        if out.exists() and out.stat().st_size >= min_bytes:
            print(f"已存在，跳过：{out.name} ({out.stat().st_size} 字节)")
            continue
        cmd = [ffmpeg(), "-y", "-f", "lavfi", "-i", pattern,
               "-frames:v", "1", *extra, str(out)]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"生成图片：{out.name} ({out.stat().st_size} 字节) — {note}")


def make_image_torrent() -> "lt.torrent_info":
    """图片夹具的种子（64KB 分块）：单独一份 payload/种子/磁力链，不动前两套。"""
    fs = lt.file_storage()
    lt.add_files(fs, str(PAYLOAD_IMG))
    t = lt.create_torrent(fs, piece_size=IMG_PIECE_SIZE)
    lt.set_piece_hashes(t, str(PAYLOAD_IMG.parent))
    TORRENT_IMG.write_bytes(lt.bencode(t.generate()))
    ti = lt.torrent_info(str(TORRENT_IMG))
    print(f"图片种子：{TORRENT_IMG.name}（{TORRENT_IMG.stat().st_size} 字节）")
    print(f"  piece_length={ti.piece_length()}  num_pieces={ti.num_pieces()}  "
          f"files={ti.num_files()}  total={ti.total_size()}")
    return ti


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

    # ---- 大夹具：单独一个 payload 目录/种子/磁力链 ----
    # 必须是**视频**：UI 只对视频扩展名开放预览，随机字节 .bin 根本点不开预览，
    # 灭屏/后台保活测试就走不到"下载中"状态（下一版 UI 接通后会用到）。
    PAYLOAD_BIG.mkdir(parents=True, exist_ok=True)
    big = PAYLOAD_BIG / "big.mp4"
    if not big.exists() or big.stat().st_size < BIG_MIN_BYTES:
        cmd = [ffmpeg(), "-y", "-f", "lavfi",
               # 合成测试图案压缩率极高（90s 720p 才 1MB），加噪点让码率落到 2~4 Mbps，
               # 这样在 300KB/s 限速下才撑得到分钟级，灭屏测试才有可观测的进度增长。
               "-i", "testsrc=size=1280x720:rate=30:duration=90,noise=alls=35:allf=t",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
               "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", str(big)]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"生成大夹具：{big.name}（{big.stat().st_size} 字节）")
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

    # ---- 图片夹具：单独一份 payload 目录/种子/磁力链（画廊「按需下载 → 缩略图 → 大图」用）----
    make_images()
    ti3 = make_image_torrent()
    ih3 = str(ti3.info_hashes().v1)
    magnet3 = (f"magnet:?xt=urn:btih:{ih3}&dn=payload_img"
               f"&x.pe=10.0.2.2:{SEED_PORT_IMG}")
    MAGNET_IMG.write_text(magnet3 + "\n", encoding="utf-8")
    print(f"图片夹具 infohash (v1): {ih3}｜总 {ti3.total_size()} 字节｜"
          f"分块 {ti3.piece_length()}｜文件 {ti3.num_files()}")
    print(f"图片夹具磁力链（模拟器里直接粘贴，端口 {SEED_PORT_IMG}）：\n{magnet3}")
    print(f"图片夹具做种：./.venv/Scripts/python android/tools/testrig/seed.py "
          f"--torrent fixture_img.torrent --payload payload_img --port {SEED_PORT_IMG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
