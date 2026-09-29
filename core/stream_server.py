"""本地 HTTP 流服务：把磁盘上的（下载中的）文件以支持 Range 的方式供播放器拉流。

关键机制：**只暴露已下载分块**。libtorrent 以稀疏文件方式落盘，未下载区间
读取结果为全零，若按逻辑大小响应，播放器会读到零数据导致解码失败。
与“仅暴露已下载前缀”的旧实现不同，本服务按**分块（piece）可用性**判定：
MP4/MOV 等文件的 moov 索引块在尾部时，先补拉尾部窗口后，播放器探测时
发起的后缀 Range 请求也能得到正确数据（Content-Range 总长始终为逻辑大小）。

可用性来源（二选一，均可不提供）：
- pieces_cb(path) -> PieceMap：按分块精确判定（预览中的种子文件，推荐）；
- avail_cb(path) -> int：已下载前缀字节数（旧接口，视为“从头连续可用”，兼容测试）。
两者都不提供时按完整静态文件服务。
"""
from __future__ import annotations

import mimetypes
import os
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .logutil import log_warning
from .models import (contiguous_bytes_of, range_available_of, ready_until_of)

CHUNK = 256 * 1024

# 渐进服务：拖动进度条后 FFmpeg 发 `bytes=X-`（X 到文件尾），要求整个区间
# 就绪必然超时（20s 下不完几百 MB）→ 416 被 FFmpeg 判为致命错误、播放器
# 进入 ErrorState（症状：拖动后卡死、进度条失灵）。因此只要 start 起就绪了
# PROGRESSIVE_MIN 字节就立即回 206，由 FFmpeg 读尽后续发 Range 补齐。
# 渐进阈值取 256KB：seek 目标落在已缓存区边缘时（就绪前缀 <1MB）不必
# 多等一整块 piece（真实种子 1~4MB/块），FFmpeg 读尽后自动续拉。
PROGRESSIVE_MIN = 256 * 1024
DEMAND_RENEW_SECS = 2.0              # 等待期间重新「点播」的间隔（幂等续期）
# 就绪前缀连续 STALL_POLLS 次轮询不再增长就提前返回（名义 10 × 0.05s = 0.5s）：
# 分块粒度较小时，单次点播（有块数上限）能补到的数据本就不足 PROGRESSIVE_MIN，
# 再等也不会更多，空耗到超时只会让播放器读超时。
# 注：旧注释写「约 1.5s」——那是按每次轮询额外 ~100ms 执行开销估的，而那个
# 开销本身就是缺陷（每次轮询一次 registry 锁 + O(任务×文件) 路径反查 +
# 一次 status()/位图物化）。2026-09-17 已把路径反查索引化（PreviewCore.
# _dir_index），轮询成本回到微秒级，实测停滞退出现在就是 ~0.5s。
STALL_POLLS = 10

TOKEN_PARAM = "t"          # URL 鉴权参数名（每会话随机，防 DNS rebinding/同机进程）
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "::1")


def _is_within(root: str, path: str) -> bool:
    """path 是否严格位于 root 之内（防目录穿越，兼容 Windows 大小写差异）。

    内部先 normpath 再比前缀：调用方即使忘了规范化，含 `..` 的输入
    （commonpath 会把 `..` 当普通段匹配到 root 自身）也无法绕过。
    """
    try:
        root_n = os.path.normcase(os.path.normpath(root))
        return os.path.commonpath([root_n,
                                   os.path.normcase(os.path.normpath(path))]) == root_n
    except ValueError:  # 不同盘符 → commonpath 抛错，视为越界
        return False


class _StreamHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 keep-alive：播放器（FFmpeg/QMediaPlayer）一次播放会对同一
    # URL 连续发起多个 Range 请求（探测 moov → 读头部 → seek 目标渐进段…）。
    # 短连接模式下每段一个 TCP 连接，高频开合会被网络防护软件判为扫描
    # 而中止连接（实测 -10053，FFmpeg 报 'Error reading HTTP response'）；
    # 复用连接从根上规避。所有响应都带精确 Content-Length，满足 1.1 要求。
    protocol_version = "HTTP/1.1"
    # 空闲/读请求头阶段的 socket 超时（S5，2026-09-18）。
    #
    # 为什么必须有：`ThreadingHTTPServer` 每连接一线程，而 `timeout` 未设时
    # 「连上却不发请求」的连接会**永久**占住一个线程 —— 同机进程开几十条这样的
    # 连接就能让线程数无界增长，**并发上限形同虚设**（限额是在 `_serve` 里抢的，
    # 而这些连接根本走不到那里）。`_QuietHTTPServer.handle_error` 早就在专门吞
    # `TimeoutError`，说明设计意图本来就有超时，只是没设值。
    #
    # 取值 15s：既要能回收空闲 keep-alive 连接，又不能太激进 —— 播放器暂停
    # 一段时间后会处于空闲 keep-alive 状态，过早断开会让它重连（虽可控但不必要）。
    # 注意：**写 body 阶段会临时取消本超时**（见 `_send_file`），否则播放器缓冲/
    # 暂停时长时间不读，带超时的 send 会把传输打断。
    timeout = 15.0
    base_dir = ""       # 由 StreamServer 注入：第一根目录（相对路径落点）
    base_dirs = ()      # 由 StreamServer 注入：全部根目录（规范化的元组）
    avail_cb = None     # 由 StreamServer 注入：path -> 已下载前缀字节数（可选）
    pieces_cb = None    # 由 StreamServer 注入：path -> PieceMap | None（可选）
    demand_cb = None    # 由 StreamServer 注入：(path, start, end_excl) -> None（可选）
    wait_timeout = 20.0  # 未就绪区间的等待上限（秒）；0 = 不等待（测试用）
    token = ""          # 由 StreamServer 注入：每会话随机鉴权 token
    last_note = None    # 由 StreamServer 注入：最近一次应答摘要（C4，见 _note）
    _last_ready = 0     # 最近一次等待得到的就绪前缀（字节）
    _last_wait_ms = 0   # 最近一次等待实际耗时（毫秒）

    def log_message(self, *args):  # 静默访问日志
        pass

    def do_HEAD(self):
        self._serve(send_body=False)

    def do_GET(self):
        self._serve(send_body=True)

    # ---------- 鉴权 ----------

    def _authorized(self) -> bool:
        """token + Host 双重校验（防 DNS rebinding / 同机进程直连）。"""
        try:
            parsed = urllib.parse.urlparse(self.path)
            got = (urllib.parse.parse_qs(parsed.query).get(TOKEN_PARAM) or [""])[0]
        except Exception:
            got = ""
        if not secrets.compare_digest(got or "", self.token or ""):
            return False
        # Host 头白名单：仅接受本机回环名（解析掉端口，兼容 [::1]:port）
        host_raw = self.headers.get("Host", "") or ""
        hn = urllib.parse.urlsplit("//" + host_raw).hostname or ""
        return hn.lower() in ALLOWED_HOSTS

    # ---------- 可用性 ----------

    def _availability(self, fp: str, logical: int):
        """返回 (连续前缀字节, PieceMap|None, avail前缀|None, 可用性可靠?)。

        回调异常时视为「无法判定可用性」（可靠=False），**绝不降级为整文件
        可用**——那会把稀疏零数据喂给播放器（与历史「静默降级」缺陷同型）。

        plan/09：连续前缀走 ``contiguous_bytes_of`` —— ``PieceMap`` 挂有增量
        视图时按需查询（摊销 O(1)、不物化位图），没有则以纯函数逐字等价。
        """
        if self.pieces_cb is not None:
            try:
                pm = self.pieces_cb(fp)
            except Exception as e:
                log_warning("stream.availability.pieces_cb", f"{fp}: {e}")
                return 0, None, None, False
            if pm is not None:
                return contiguous_bytes_of(pm), pm, None, True
            # pieces_cb 在场但不认识该文件：交给 avail_cb 兜底；两路都给不出
            # 可判定依据时必须判「不可用」——「反查不命中」不等于「已下载完」，
            # 下载中文件反查失败后被当整文件服务，就是稀疏零数据（2026-09-08
            # 实证：pieces_cb 恒返 None 时，旧逻辑对未下载文件回 206+全零）。
            if self.avail_cb is None:
                return 0, None, None, False
        if self.avail_cb is not None:
            try:
                avail = max(0, min(int(self.avail_cb(fp)), logical))
            except Exception as e:
                log_warning("stream.availability.avail_cb", f"{fp}: {e}")
                return 0, None, None, False
            return avail, None, avail, True
        return logical, None, None, True   # 纯静态服务：两个回调都未提供

    def _range_available(self, pm, avail, start: int, end_excl: int) -> bool:
        """[start, end_excl) 是否全部可读（有视图时走区间判定快路径）。"""
        if pm is not None:
            return range_available_of(pm, start, end_excl)
        if avail is not None:
            return end_excl <= avail
        return True

    # ---------- 响应 ----------

    def _note(self, code: int, start=None, end=None, why=None):
        """记录最近一次应答（**含失败路径**），供 UI 归因（C4，2026-09-18）。

        为什么放在服务端：播放失败时用户看到的只有播放器的原始报错
        （FFmpeg 的 `moov atom not found` / ExoPlayer 的 5xx 文本），
        既不知道服务端到底回了什么码，也不知道等了多久、就绪了多少 —— 
        排查只能靠猜。「谁最清楚发生了什么」就把记录放在谁那里。

        写在 handler **类**属性上：`StreamServer` 为每个实例生成独立的
        handler 子类，因此这份记录天然按服务器实例隔离；读取侧走
        :meth:`StreamServer.last_response_note`（**新增方法不触契约** ——
        `contract_check` 只冻结 `__init__` / `url_for` 的参数表）。
        """
        try:
            type(self).last_note = {
                "code": int(code),
                "range": (f"{start}-{end}" if start is not None else ""),
                "ready": int(getattr(self, "_last_ready", 0) or 0),
                "wait_ms": int(getattr(self, "_last_wait_ms", 0) or 0),
                "why": str(why or ""),
            }
        except Exception as e:      # 记录失败绝不影响应答
            log_warning("stream.note", f"{e}")

    def _security_headers(self):
        # 播放数据不做缓存、禁用内容嗅探（防同机恶意进程利用浏览器缓存/类型嗅探）
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")

    def _respond_forbidden(self):
        self.send_response(403, "Forbidden")
        self._security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _respond_503(self, why: str = "no-data"):
        # 数据尚未就绪（或并发超限）：返回 503 让客户端稍后重试，而不是吐零数据。
        # S8 诊断头（2026-09-27）：失败可自证——X-Why 取值集合：
        #   no-data（区间/头部数据未就绪）/ unavailable（可用性判不出来）/ overloaded（额度满）。
        # 自定义头播放器按 HTTP 规范忽略；X-Ready-Bytes 是就绪前缀字节数（未知为 0）。
        self._note(503, why=why)
        self.send_response(503, "Buffering")
        self._security_headers()
        self.send_header("Retry-After", "1")
        self.send_header("X-Why", why)
        self.send_header(
            "X-Ready-Bytes", str(int(getattr(self, "_last_ready", 0) or 0))
        )
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _respond_416(self, logical: int):
        # 请求区间数据未就绪/不可满足：告知逻辑总长，客户端可重试或等待。
        # Content-Length: 0 必须有 —— HTTP/1.1 keep-alive 下无长度语义的
        # 4xx 会让复用连接上的后续响应帧错位（A3）。
        self._note(416, why="range")
        self.send_response(416)
        self._security_headers()
        self.send_header("Content-Range", f"bytes */{logical}")
        self.send_header("X-Why", "range")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _respond_range(self, fp: str, start: int, end: int, logical: int,
                       send_body: bool):
        if not self._enter_serving():
            # 写 body 的额度已满：回 503 让客户端稍后重试（等待额度仍在手，
            # 由 _serve 的 finally 归还）。
            self._respond_503("overloaded")
            return
        self._note(206, start, end)
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        length = end - start + 1
        self.send_response(206)
        self._security_headers()
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        # Content-Range 中的总长用逻辑大小，播放器才能探测到尾部 moov 与正确时长
        self.send_header("Content-Range", f"bytes {start}-{end}/{logical}")
        self.end_headers()
        if send_body:
            self._send_file(fp, start, length)

    def _respond_full(self, fp: str, length: int, send_body: bool):
        if not self._enter_serving():
            self._respond_503("overloaded")
            return
        self._note(200)
        ctype = mimetypes.guess_type(fp)[0] or "application/octet-stream"
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.end_headers()
        if send_body:
            self._send_file(fp, 0, length)

    def _send_file(self, fp: str, start: int, length: int):
        # 写 body 期间**取消 socket 超时**（S5）：播放器暂停/缓冲时可能长时间
        # 不读，带超时的 send 到点会抛 socket.timeout 把传输打断；而请求头
        # 读取阶段的超时由类属性 `timeout` 提供（见其注释）。
        sock = self.connection
        try:
            prev_timeout = sock.gettimeout()
            sock.settimeout(None)
        except OSError:
            prev_timeout = None
        try:
            with open(fp, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except OSError as e:
            # 审计 2026-09-29（P2）：等待/传输期间文件被外部删除（remove_task
            # 带 delete_files、cache_quota LRU、cache_guard 清理）→ open/seek/read
            # 抛 FileNotFoundError 等 OSError。handle_error 只吞 4 类网络异常，
            # 不拦会打满屏 traceback 且客户端收到无响应断连；这里按「传输中断」
            # 收场（连接由调用方关闭），与对端断开的处理同级。
            log_warning("stream.send_file.gone",
                        f"传输中断（文件不可读，可能已被清理）：{fp}（{e}）")
        finally:
            # 还原超时，否则同一条 keep-alive 连接上的**下一个**请求会永久阻塞
            try:
                sock.settimeout(prev_timeout)
            except OSError:
                pass

    def _serve(self, send_body: bool):
        if not self._authorized():
            self._respond_forbidden()
            return
        # 并发上限分**两类额度**（S2，2026-09-18）：
        # - `wait_limiter`：整个请求周期都占（含最长 wait_timeout 的等待阶段）；
        # - `limiter`：只在**真正要写 body** 时换用（见 _enter_serving）。
        # 为什么必须分账：旧实现从入口就抢 `limiter`，而等待阶段最长 20s ——
        # 16 个「等数据」的请求就能把额度占满，之后**能立刻服务的请求也一律
        # 503**。播放器并发探测/seek 时很容易凑够十几个等待者。
        # 等待额度取 `max_concurrency × 4`（默认 64）：等待不写 body，
        # 成本只是「一个休眠线程」，额度可以宽松；**不改构造签名**（被
        # contract_check 冻结），由既有参数派生。
        cls = type(self)
        self._holding = "wait"
        if not cls.wait_limiter.acquire(blocking=False):
            self._holding = None
            self._respond_503("overloaded")
            return
        try:
            self._serve_inner(send_body)
        finally:
            # 归还**当前实际持有**的那一类额度（等待阶段可能已换成 serving）
            if self._holding == "serve":
                cls.limiter.release()
            elif self._holding == "wait":
                cls.wait_limiter.release()
            self._holding = None

    def _enter_serving(self) -> bool:
        """从「等待」阶段转入「写 body」阶段：换用 serving 额度。

        返回 False = serving 额度已满（调用方回 503，且继续持有等待额度，
        由 `_serve` 的 finally 归还）。已经在 serving 阶段时是幂等空操作。
        """
        cls = type(self)
        if self._holding == "serve":
            return True
        if not cls.limiter.acquire(blocking=False):
            return False
        if self._holding == "wait":
            cls.wait_limiter.release()
        self._holding = "serve"
        return True

    def _serve_inner(self, send_body: bool):
        rel = urllib.parse.unquote(urllib.parse.urlparse(self.path).path).lstrip("/")
        roots = self.base_dirs or (os.path.normpath(self.base_dir),)
        # 绝对路径（download_dir 在缓存目录外时，url_for 携带绝对落盘路径）
        # 直接采用；相对路径以第一根为基准拼接。
        fp = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(roots[0], rel))
        # 目录穿越防护：必须用 commonpath 而非 startswith —— 后者会把
        # 「root_evil」这类同前缀兄弟目录误判为合法（C:\cache 与 C:\cache_evil）。
        if not any(_is_within(b, fp) for b in roots) or not os.path.isfile(fp):
            self.send_error(404, "File not ready")
            return

        logical = os.path.getsize(fp)
        self._current_path = fp
        contig, pm, avail, ok = self._availability(fp, logical)
        if not ok:
            # 可用性回调异常：状态未知时不吐数据，让客户端稍后重试
            self._respond_503("unavailable")
            return

        # ---- Range 解析（后缀相对逻辑大小，等待尾部 moov 探测） ----
        start, end, partial = 0, logical - 1, False
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            first = rng[6:].split(",")[0]
            a, _, b = first.partition("-")
            try:
                if not a and b:          # bytes=-N（尾缀 N 字节）：相对逻辑大小
                    start, end = max(0, logical - int(b)), logical - 1
                    partial = True
                elif a:                   # bytes=N- / bytes=N-M
                    start = int(a)
                    end = int(b) if b else logical - 1
                    end = min(end, logical - 1)
                    partial = True
            except ValueError:
                start, end, partial = 0, logical - 1, False

        if not partial:
            # 无 Range：只发从头开始的连续可用前缀
            if contig <= 0:
                self._respond_503()
                return
            if contig < logical:
                # 审计 2026-09-29（P2）：文件未下完时回「200 + Content-Length=前缀」
                # 会让客户端把前缀长度当文件总大小（尾部 moov 的 mp4 探测不到
                # moov、时长错）。改回 206 + Content-Range：明示 total=logical，
                # 客户端按未完成资源处理、从就绪前缀继续发 Range 续拉。
                self._respond_range(fp, 0, contig - 1, logical, send_body)
                return
            self._respond_full(fp, contig, send_body)
            return

        if start > end or start >= logical:
            self._respond_416(logical)
            return

        # 请求区间整体已就绪 → 精确 206
        if self._range_available(pm, avail, start, end + 1):
            self._respond_range(fp, start, end, logical, send_body)
            return

        # 区间未就绪：先向调度器“点播”这段字节（播放器要什么就下载什么），
        # 再挂起等待数据到达。直接回 416/503 会被 FFmpeg 判为致命错误
        # （典型症状：moov atom not found）。
        self._demand(start, end)
        t_wait0 = time.time()
        ready = self._wait_ready(fp, start, end + 1)
        # 记录「等了多久 / 等到多少」——失败路径的可归因数据（C4）
        self._last_wait_ms = int((time.time() - t_wait0) * 1000)
        self._last_ready = ready
        if ready > start:
            # 渐进服务：就绪多少发多少。Content-Range 总长仍是逻辑大小，
            # FFmpeg 读尽 Content-Length 后会自动续发下一段 Range 请求
            # （与「无 Range 请求只发连续前缀」的顺序播放机制一致）。
            self._respond_range(fp, start, ready - 1, logical, send_body)
            return
        # 一个就绪字节都没有：连文件头都还没数据 → 503 让客户端稍后重试
        contig_now, _pm2, _avail2, _ = self._availability(fp, logical)
        if contig_now <= 0:
            self._respond_503()
        else:
            self._respond_416(logical)

    # ---------- 点播 / 等待 ----------

    def _demand(self, start: int, end: int):
        """通知调度器：这段字节现在就要（用于 moov 探测与任意位置拖动）。"""
        if self.demand_cb is None:
            return
        try:
            self.demand_cb(self._current_path, start, end + 1)
        except Exception as e:
            log_warning("stream.demand", f"{self._current_path} {start}-{end}: {e}")

    def _progressive_ready(self, pm, avail, start: int, end_excl: int) -> int:
        """从 start 起连续就绪的字节末端（开区间）。

        有分块信息时按 piece 精确延伸（``ready_until_of``：挂视图时增量推进，
        否则纯函数等价）；退化到旧接口（已下载前缀）时按前缀计；
        无可用性信息时视为整文件可读。
        """
        if pm is not None:
            return ready_until_of(pm, start, end_excl)
        if avail is not None:
            return max(start, min(end_excl, avail))
        return end_excl

    def _wait_ready(self, fp: str, start: int, end_excl: int) -> int:
        """轮询等待，返回从 start 起的**就绪前缀末端**（开区间）。

        与「整个区间就绪才返回」不同：只要 start 起就绪了 PROGRESSIVE_MIN
        字节就立即返回，让无上界请求（`bytes=X-`）也能渐进服务，避免
        seek 后必然超时 416、播放器进入 ErrorState。
        等待期间每 DEMAND_RENEW_SECS 重新向调度器点播一次（幂等续期），
        防止 ASAP 预约被中途清除。wait_timeout=0（测试用）时不阻塞，
        按当时的可用性立即返回 —— 保持「未就绪立即退化」的既有行为。
        """
        budget = max(0.0, float(self.wait_timeout))
        deadline = time.time() + budget
        next_demand = time.time() + DEMAND_RENEW_SECS
        last_ready, stall = start, 0
        while True:
            try:
                logical = os.path.getsize(fp)
            except OSError as e:
                # 审计 2026-09-29（P2）：等待期间文件被外部删除（remove_task
                # 带 delete_files、cache_quota LRU、cache_guard 清理）→
                # FileNotFoundError。不拦会冒泡进 handle_error（只吞 4 类
                # 网络异常）→ 满屏 traceback + 客户端无响应断连。按「不可
                # 判定」语义返回 start（上层照常应答 503/416，绝不降级整文件）。
                log_warning("stream.wait_ready.gone", f"{fp}: {e}")
                return start
            _, pm, avail, ok = self._availability(fp, logical)
            if not ok:
                return start
            if self._range_available(pm, avail, start, end_excl):
                return end_excl
            ready = self._progressive_ready(pm, avail, start, end_excl)
            if ready - start >= PROGRESSIVE_MIN:
                return ready
            if ready > start:
                # 停滞检测：就绪前缀已不再增长（点播区间补完或补不动了），
                # 提前把已就绪的部分交给播放器，避免空等整个超时周期。
                stall = stall + 1 if ready == last_ready else 0
                if stall >= STALL_POLLS:
                    return ready
            last_ready = ready
            if type(self).stop_evt.is_set():
                # plan/11 R-6B：服务正在关闭——立即交出已就绪前缀并返回。
                # 返回值交给上层按既有语义应答（什么都没有 → 503/416），
                # 不吞数据也不挂死。
                return ready
            if budget <= 0 or time.time() >= deadline:
                return ready
            if time.time() >= next_demand:
                self._demand(start, end_excl - 1)
                next_demand = time.time() + DEMAND_RENEW_SECS
            time.sleep(0.05)


class _QuietHTTPServer(ThreadingHTTPServer):
    """播放器（FFmpeg）中断请求属正常行为，静默连接类异常避免刷屏。"""

    def handle_error(self, request, client_address):
        exc = sys.exception()
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            return
        super().handle_error(request, client_address)


class StreamServer:
    """仅监听 127.0.0.1 的流媒体文件服务。

    avail_cb: 可选回调 avail_cb(disk_path) -> 已下载前缀字节数（旧接口，
    仅用于顺序下载场景；未提供时按静态文件的前缀全量可用处理）。
    pieces_cb: 可选回调 pieces_cb(disk_path) -> PieceMap | None；
    按分块精确判定任意区间可用性（moov 尾部优先需要）。
    """

    def __init__(self, base_dir: str, avail_cb=None, pieces_cb=None,
                 demand_cb=None, wait_timeout: float = 20.0,
                 bases: list | None = None, max_concurrency: int = 16):
        # 注意：直接把函数放进类字典会触发描述符协议（实例访问得到绑定方法，
        # 回调会被多传一个 self 参数）。staticmethod 的实例访问返回原函数，无此问题；
        # Python 3.13 起 functools.partial 放类字典也会有同样隐患。
        roots = [os.path.normpath(base_dir)]
        for b in (bases or []):
            nb = os.path.normpath(b)
            if nb not in roots:
                roots.append(nb)
        attrs = {"base_dir": base_dir, "base_dirs": tuple(roots),
                 "wait_timeout": wait_timeout,
                 "token": secrets.token_urlsafe(16),
                 "limiter": threading.BoundedSemaphore(max(1, max_concurrency)),
                 # S2：等待阶段单独一类额度（不写 body，成本只是一个休眠线程）。
                 # 由 max_concurrency 派生 → **构造签名不变**（被 contract 冻结）。
                 "wait_limiter": threading.BoundedSemaphore(
                     max(1, max_concurrency) * 4),
                 # 每个请求实例记录「当前持有哪一类额度」：BaseHTTPRequestHandler
                 # 每请求新建实例，故实例级状态是安全的。
                 "_holding": None,
                 # plan/11 R-6B：协作式停止事件。shutdown() 置位后，卡在
                 # `_wait_ready` 里的在途请求会立即交出已就绪前缀并返回，
                 # 而不是把 ``wait_timeout``（默认 20s）的上限耗满——
                 # 旧实现「先 session.shutdown() 后 server.shutdown()」时，
                 # 这些线程会白占 ``limiter`` 名额直到 20s 上限。
                 "stop_evt": threading.Event()}
        if avail_cb is not None:
            attrs["avail_cb"] = staticmethod(avail_cb)
        if pieces_cb is not None:
            attrs["pieces_cb"] = staticmethod(pieces_cb)
        if demand_cb is not None:
            attrs["demand_cb"] = staticmethod(demand_cb)
        handler = type("BoundHandler", (_StreamHandler,), attrs)
        self._httpd = _QuietHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def url_for(self, rel_path: str) -> str:
        return (f"http://127.0.0.1:{self.port}/{urllib.parse.quote(rel_path)}"
                f"?{TOKEN_PARAM}={self._httpd.RequestHandlerClass.token}")

    def last_response_note(self) -> str:
        """最近一次流应答的可读摘要（C4：把「播放失败」变成可归因）。

        返回空串 = 本次会话还没有任何应答记录。失败路径（503/416）同样会记录，
        否则最需要排查的时候恰好什么都看不到。

        「就绪 / 等待」只在**确有等待或属未就绪应答**时才打印：成功走快路径
        （区间本来就绪，没进过等待循环）时它们是默认值 0，打出来只会误导
        ——「206 且就绪 0 字节」是自相矛盾的读数。
        """
        note = getattr(self._httpd.RequestHandlerClass, "last_note", None)
        if not note:
            return ""
        code = int(note.get("code", 0) or 0)
        wait_ms = int(note.get("wait_ms", 0) or 0)
        parts = [str(code)]
        if note.get("range"):
            parts.append(f"bytes {note['range']}")
        if wait_ms > 0 or code >= 500 or code == 416:
            parts.append(f"就绪 {note.get('ready', 0)} 字节")
            parts.append(f"等待 {wait_ms}ms")
            if note.get("why"):     # S8：诊断原因（no-data/unavailable/overloaded/range）
                parts.append(f"原因 {note['why']}")
        return "流服务最近应答：" + "，".join(parts)

    def shutdown(self):
        # plan/11 R-6B：先置停止事件再关服务器——让卡在 `_wait_ready` 轮询里的
        # 在途 handler 立即返回，而不是等 `wait_timeout`（20s）耗满。
        # 注意顺序：`serve_forever` 之后的 `server_close` 不等待在途请求线程
        # （`daemon_threads=True`），所以这里的置位是它们能及时收尾的唯一信号。
        try:
            self._httpd.RequestHandlerClass.stop_evt.set()
        except Exception:
            pass
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass