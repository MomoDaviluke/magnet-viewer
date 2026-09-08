"""moov 尾部优先流式修复的端到端验证（无 GUI，可离线运行）。

背景：普通 MP4/MOV 的 moov 索引块在文件尾部；边下边播时若只下载了文件头，
FFmpeg 探测会报 `moov atom not found` / `Invalid data found when processing input`。

本脚本：
1) 用 ffmpeg（PATH / imageio-ffmpeg 自带二进制）生成一个**真实且 moov 在尾部**
   的 MP4（testsrc → mpeg4，约 6 MB）；
2) 以本地 HTTP 流服务模拟三种下载状态并调用 ffprobe/ffmpeg 实际探测：
   - 仅头部 64KB（旧行为）        → 必须失败（复现用户报错）；
   - 头部 + 尾部 4MB 窗口（修复后）→ 必须成功（可边下边播）；
   - 全部下载                     → 必须成功（基线）。
用法：python moov_stream_test.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.models import PieceMap  # noqa: E402
from core.stream_server import StreamServer  # noqa: E402

PL = 16 * 1024
HEAD_BYTES = 64 * 1024
TAIL_BYTES = 4 * 1024 * 1024


def find_ffmpeg_exe() -> str | None:
    """定位可用的 ffmpeg.exe（不含 ffprobe）。支持 MV_FFMPEG 环境变量覆盖。"""
    env = os.environ.get("MV_FFMPEG")
    if env and os.path.isfile(env) and "ffprobe" not in os.path.basename(env).lower():
        return env
    if shutil.which("ffmpeg"):
        return shutil.which("ffmpeg")
    try:
        import imageio_ffmpeg  # pip install imageio-ffmpeg，自带静态 ffmpeg.exe
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def _subprocess_env() -> dict:
    """子进程环境：剥掉代理变量。

    FFmpeg 对 127.0.0.1 流服务的请求绝不应走代理——沙箱/公司网络设了
    HTTP_PROXY 时，FFmpeg 会把 localhost 请求转给代理，代理断连后报
    「Error reading HTTP response: End of file」（D 用例假失败的根因，
    2026-09-08 实测：verbose 日志显示连接的是代理端口而非流服务端口）。
    """
    env = dict(os.environ)
    for k in list(env):
        if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy"):
            env.pop(k, None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    return env


def find_probe_tool() -> tuple[str, str] | None:
    """返回 (类型, 可执行路径)：优先 ffprobe，其次 ffmpeg。"""
    env = os.environ.get("MV_FFMPEG")
    if env and os.path.isfile(env):
        name = os.path.basename(env).lower()
        return ("ffprobe" if "ffprobe" in name else "ffmpeg", env)
    if shutil.which("ffprobe"):
        return ("ffprobe", shutil.which("ffprobe"))
    exe = find_ffmpeg_exe()
    return ("ffmpeg", exe) if exe else None


def make_tail_moov_mp4(path: str) -> bool:
    """用 ffmpeg 生成 moov 在尾部（默认布局）的真实 MP4，大小 > 5MB。

    testsrc 合成画面本身码率极低，需用 CBR（minrate/maxrate/bufsize）
    强制到 ~2.5 Mbps 才能产出 9MB 量级的文件，保证测试里
    “头部 64KB + 尾部 4MB”两个窗口之间存在真实空洞。
    """
    exe = find_ffmpeg_exe()
    if exe is None:
        return False
    cmd = [exe, "-y", "-f", "lavfi", "-i",
           "testsrc=size=640x360:rate=15:duration=30",
           "-c:v", "mpeg4", "-b:v", "2500k",
           "-minrate", "2500k", "-maxrate", "2500k", "-bufsize", "5000k",
           "-an", path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180,
                           env=_subprocess_env())
    except subprocess.TimeoutExpired:
        return False
    return (r.returncode == 0
            and os.path.isfile(path)
            and os.path.getsize(path) > 5 * 1024 * 1024)


def probe(url: str, timeout: float = 20.0) -> tuple[int, str]:
    """用 ffprobe / ffmpeg 探测媒体，返回 (退出码, 输出文本)。"""
    tool = find_probe_tool()
    if tool is None:
        return -1, "no ffprobe/ffmpeg available"
    kind, exe = tool
    if kind == "ffprobe":
        cmd = [exe, "-v", "error", "-show_entries",
               "format=duration", "-of", "default=nw=1", url]
    else:
        # ffmpeg 7.x 无输出文件会报 “At least one output file must be specified”；
        # 用 null muxer + 0 帧输出：只验证输入能否打开，不触发解码
        cmd = [exe, "-v", "error", "-i", url,
               "-f", "null", "-frames:v", "0", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=_subprocess_env())
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "probe timeout"


def probe_seek(url: str, seek_sec: float, timeout: float = 60.0) -> tuple[int, str]:
    """模拟「拖动进度条」：从 seek_sec 处开始解码 1 帧。

    客户端（FFmpeg）发起 seek 后，服务端必须**渐进**服务就绪前缀，否则剩余
    区间不可能在超时前下完。

    定位说明：**正向验证**（渐进服务下 seek 能解码成功），不是旧行为的回归
    判据 —— 实测命令行 FFmpeg 收到 416 后会回退为顺序读取，因此关闭渐进
    服务本用例仍会通过。真实差异取决于下载速度与文件大小（见 qt 用例说明）。
    """
    exe = find_ffmpeg_exe()
    if exe is None:
        return -1, "no ffmpeg available"
    cmd = [exe, "-v", "error", "-ss", f"{seek_sec}", "-i", url,
           "-frames:v", "1", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=_subprocess_env())
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "seek probe timeout"


def run_seek_case(tmp: str, name: str, have: set[int], seek_sec: float,
                  expect_ok: bool, demand_max: int = 60) -> tuple[bool, str]:
    """拖动到未下载位置：点播按真实调度器语义**有块数上限**。

    上限很关键：若 demand 一次把请求区间（到文件尾）全部补上，旧实现
    「等整个区间就绪」也能通过，用例就失去区分度；真实
    PreviewScheduler.request_range 单次最多预约 LOOKAHEAD_PIECES 块。
    """
    disk = os.path.join(tmp, "demo.mp4")
    size = os.path.getsize(disk)
    pm = PieceMap(PL, 0, 0, (size - 1) // PL, size, have.__contains__)

    def demand(path, start, end_excl):
        first = start // PL
        last = min(max(0, end_excl - 1) // PL, first + demand_max - 1)

        def fill():
            time.sleep(0.15)          # 模拟分块陆续到达
            have.update(range(first, last + 1))
        threading.Thread(target=fill, daemon=True).start()

    srv = StreamServer(tmp, pieces_cb=lambda p: pm if p == disk else None,
                       demand_cb=demand, wait_timeout=8.0)
    srv.start()
    try:
        rc, out = probe_seek(srv.url_for("demo.mp4"), seek_sec)
    finally:
        srv.shutdown()
    ok = (rc == 0) == expect_ok
    tag = "通过" if ok else "失败"
    print(f"[{name}] {'期望可解码' if expect_ok else '期望失败'} → "
          f"退出码 {rc}：{out[:120]}  [{tag}]")
    return ok, out


def run_case(tmp: str, name: str, have: set[int],
             expect_ok: bool, fill_on_demand: bool = False) -> tuple[bool, str]:
    """以给定分块可用性启动流服务并探测。

    fill_on_demand=True 时模拟真实调度器：播放器（FFmpeg）请求哪段，
    demand_cb 就把那段的缺失分块补上（对应 PreviewScheduler.request_range），
    验证「点播 + 等待」能否让探测成功。
    """
    disk = os.path.join(tmp, "demo.mp4")
    size = os.path.getsize(disk)
    pm = PieceMap(PL, 0, 0, (size - 1) // PL, size, have.__contains__)

    def demand(path, start, end_excl):
        if not fill_on_demand:
            return
        first, last = start // PL, max(0, end_excl - 1) // PL

        def fill():
            time.sleep(0.15)          # 模拟分块陆续到达
            have.update(range(first, last + 1))
        threading.Thread(target=fill, daemon=True).start()

    srv = StreamServer(tmp, pieces_cb=lambda p: pm if p == disk else None,
                       demand_cb=demand, wait_timeout=8.0)
    srv.start()
    try:
        rc, out = probe(srv.url_for("demo.mp4"))
    finally:
        srv.shutdown()
    ok = (rc == 0) == expect_ok
    tag = "通过" if ok else "失败"
    print(f"[{name}] {'期望可开' if expect_ok else '期望打不开'} → "
          f"退出码 {rc}：{out[:120]}  [{tag}]")
    return ok, out


def main():
    if find_probe_tool() is None:
        print("[SKIP] 未找到 ffprobe/ffmpeg（含 imageio-ffmpeg），"
              "探测验证跳过（HTTP 行为仍由 smoke_test 覆盖）")
        return 2    # 退出码 2 = 显式跳过（区别于「通过=0 / 失败=1」假绿）
    tmp = tempfile.mkdtemp(prefix="mv_moov_")
    try:
        disk = os.path.join(tmp, "demo.mp4")
        if not make_tail_moov_mp4(disk):
            print("[SKIP] 无法用 ffmpeg 生成测试视频，探测验证跳过"
                  "（HTTP 行为仍由 smoke_test 覆盖）")
            return 2    # 退出码 2 = 显式跳过，避免「跳过=通过」假绿
        size = os.path.getsize(disk)
        with open(disk, "rb") as f:
            raw = f.read()
        moov_pos = raw.find(b"moov")
        print(f"[0] 真实尾部-moov MP4：{size / 1024 / 1024:.1f} MB，"
              f"moov 偏移 {moov_pos}（{size - moov_pos} B 距末尾）→ "
              f"尾部 {TAIL_BYTES // 1024 // 1024} MB 窗口应覆盖")
        assert moov_pos >= 0 and size - moov_pos <= TAIL_BYTES, \
            "moov 未落在尾部 4MB 窗口内"

        # 任务 A：仅头部 64KB（旧行为）→ 必须失败（复现 moov atom not found）
        head_only = set(range(0, HEAD_BYTES // PL))
        ok_a, _ = run_case(tmp, "A 仅头部", head_only, expect_ok=False)

        # 任务 B：头部 + 尾部窗口（修复后）→ 必须成功，可边下边播
        tail_first = (max(0, (size - TAIL_BYTES)) + PL - 1) // PL
        have_b = set(range(0, HEAD_BYTES // PL))
        have_b |= set(range(tail_first, (size - 1) // PL + 1))
        # 中间是空洞，靠 demand_cb 模拟调度器按需补拉（真实链路即此行为）
        ok_b, _ = run_case(tmp, "B 头+尾窗口+按需补拉", have_b,
                           expect_ok=True, fill_on_demand=True)

        # 任务 C：全部下载（基线）→ 必须成功
        all_pieces = set(range(0, (size - 1) // PL + 1))
        ok_c, _ = run_case(tmp, "C 全部下载", all_pieces, expect_ok=True)

        # 任务 D：拖动进度条到**未下载**位置 → 必须能继续解码。
        # seek 目标落在「头部 64KB」与「尾部 4MB 窗口」之间的空洞里；
        # FFmpeg 会发 `bytes=X-`（到文件尾），服务端只能渐进服务就绪前缀，
        # 点播还受 60 块上限约束 —— 旧实现等整个区间就绪必然超时 416。
        have_d = set(range(0, HEAD_BYTES // PL))
        have_d |= set(range(tail_first, (size - 1) // PL + 1))
        hole_mid = (HEAD_BYTES + max(0, size - TAIL_BYTES)) // 2
        seek_sec = hole_mid / size * 30.0
        print(f"[D] 空洞中段 {hole_mid} B（约 {seek_sec:.1f}s），"
              f"点播上限 60 块 = {60 * PL // 1024} KB")
        ok_d, _ = run_seek_case(tmp, "D 拖动到未下载位置", have_d,
                                seek_sec, expect_ok=True)

        ok = ok_a and ok_b and ok_c and ok_d
        print("\n=== moov 尾部优先验证" + ("全部通过" if ok else "未通过") + " ===")
        return 0 if ok else 1
    finally:
        time.sleep(0.2)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())