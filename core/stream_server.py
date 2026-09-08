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
from .models import contiguous_bytes, range_available

CHUNK = 256 * 1024

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
    base_dir = ""       # 由 StreamServer 注入：第一根目录（相对路径落点）
    base_dirs = ()      # 由 StreamServer 注入：全部根目录（规范化的元组）
    avail_cb = None     # 由 StreamServer 注入：path -> 已下载前缀字节数（可选）
    pieces_cb = None    # 由 StreamServer 注入：path -> PieceMap | None（可选）
    demand_cb = None    # 由 StreamServer 注入：(path, start, end_excl) -> None（可选）
    wait_timeout = 20.0  # 未就绪区间的等待上限（秒）；0 = 不等待（测试用）
    token = ""          # 由 StreamServer 注入：每会话随机鉴权 token

    protocol_version = "HTTP/1.1"

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
        """
        if self.pieces_cb is not None:
            try:
                pm = self.pieces_cb(fp)
            except Exception as e:
                log_warning("stream.availability.pieces_cb", f"{fp}: {e}")
                return 0, None, None, False
            if pm is not None:
                return contiguous_bytes(pm), pm, None, True
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
        """[start, end_excl) 是否全部可读。"""
        if pm is not None:
            return range_available(pm, start, end_excl)
        if avail is not None:
            return end_excl <= avail
        return True

    # ---------- 响应 ----------

    def _security_headers(self):
        # 播放数据不做缓存、禁用内容嗅探（防同机恶意进程利用浏览器缓存/类型嗅探）
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")

    def _respond_forbidden(self):
        self.send_response(403, "Forbidden")
        self._security_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _respond_503(self):
        # 数据尚未就绪：返回 503 让客户端稍后重试，而不是吐零数据
        self.send_response(503, "Buffering")
        self._security_headers()
        self.send_header("Retry-After", "1")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _respond_416(self, logical: int):
        # 请求区间数据未就绪/不可满足：告知逻辑总长，客户端可重试或等待
        self.send_response(416)
        self._security_headers()
        self.send_header("Content-Range", f"bytes */{logical}")
        self.end_headers()

    def _respond_range(self, fp: str, start: int, end: int, logical: int,
                       send_body: bool):
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

    def _serve(self, send_body: bool):
        if not self._authorized():
            self._respond_forbidden()
            return
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
            self._respond_503()
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
        if self._wait_ready(fp, start, end + 1):
            self._respond_range(fp, start, end, logical, send_body)
            return

        # 等待超时：能给出前缀就给前缀，否则告知客户端稍后重试
        contig_now, pm2, avail2, _ = self._availability(fp, logical)
        if start < contig_now:
            self._respond_range(fp, start, min(end, contig_now - 1), logical, send_body)
        elif contig_now <= 0:
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

    def _wait_ready(self, fp: str, start: int, end_excl: int) -> bool:
        """轮询等待区间就绪，最长 wait_timeout 秒（0 表示不等待，便于测试）。"""
        budget = max(0.0, float(self.wait_timeout))
        if budget <= 0:
            return False
        deadline = time.time() + budget
        while True:
            logical = os.path.getsize(fp)
            _, pm, avail, ok = self._availability(fp, logical)
            # ok 必须参与判定：等待期间回调可能开始抛异常/反查失效，
            # 此时 (pm, avail) 双 None 若被 _range_available 当「全量可用」，
            # 等待结束反而吐出稀疏零数据（2026-09-08 审计 P0-4 第二触发点）
            if ok and self._range_available(pm, avail, start, end_excl):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.15)


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
                 bases: list | None = None):
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
                 "token": secrets.token_urlsafe(16)}
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

    def shutdown(self):
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass