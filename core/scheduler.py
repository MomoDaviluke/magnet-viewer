"""预览下载调度：单文件锁定 + 顺序分块 + 索引块（moov）优先。

MP4/MOV 家族的 `moov` 索引块通常位于文件**尾部**，而播放器（FFmpeg）
打开媒体前必须先读到它。因此对这类文件先预约一个“尾部窗口”，
使索引块尽早落盘、播放器探测即可成功；随后再按播放顺序向前滚动窗口。

其余格式（MKV/WebM/TS 等）不需要尾部即可开播，保持从头顺序下载。
"""
from __future__ import annotations

import math

import libtorrent as lt

from .logutil import log_warning
from .models import PieceMap, contiguous_bytes, have_from_bitmap

LOOKAHEAD_PIECES = 60        # 一次向前预约的分块数量
TAIL_BYTES_MIN = 4 * 1024 * 1024   # 尾部窗口下限（覆盖常见 moov 尺寸）
TAIL_BYTES_MAX = 64 * 1024 * 1024  # 尾部窗口上限（防超大文件过载预约）
TAIL_RATIO = 0.01            # 大文件按体积 1% 放大窗口
TAIL_MAX_PIECES = 128        # 一次预约的尾部块数上限

# moov 索引块在尾部的容器家族（FFmpeg mov 解封装器适用）
TAIL_FIRST_EXTS = {".mp4", ".mov", ".m4v", ".3gp", ".3g2", ".mj2"}


def tail_piece_window(file, piece_length: int) -> list[int]:
    """计算需要优先补拉的尾部窗口（闭区间 piece 列表，升序）。

    仅对 moov 在尾部的容器家族生效；返回空列表表示无需补拉。
    """
    if file.ext not in TAIL_FIRST_EXTS or file.size <= 0 or piece_length <= 0:
        return []
    tail_bytes = min(file.size, max(TAIL_BYTES_MIN, int(file.size * TAIL_RATIO)),
                     TAIL_BYTES_MAX)
    n = max(1, math.ceil(tail_bytes / piece_length))
    n = min(n, TAIL_MAX_PIECES, file.end_piece - file.start_piece + 1)
    return list(range(file.end_piece - n + 1, file.end_piece + 1))


class PreviewScheduler:
    """管理「预览某个文件」的下载策略。

    策略：
    1. prioritize_files 只保留目标文件优先级，其余置 0；
    2. MP4/MOV 家族先预约尾部索引窗口（moov），再按播放顺序预约分块；
    3. set_piece_deadline 让分块按播放顺序到达；
    4. tick() 周期性根据已完成字节向前滚动预约窗口。
    """

    def __init__(self):
        self.handle = None
        self.file = None
        self._scheduled_to = -1
        self._play_from = -1     # 播放起点块（begin=文件头 / seek=目标块）
        self._tail_pieces: list[int] = []
        self.on_file_completed = None  # callback(int file_index)，由会话层注入

    @property
    def active(self) -> bool:
        return self.handle is not None

    # ---------- 尾部索引窗口（moov） ----------

    def _request_tail(self) -> None:
        """把尾部窗口的每个分块置为 ASAP 截止时间（幂等）。"""
        if not self._tail_pieces or self.handle is None:
            return
        for p in self._tail_pieces:
            try:
                self.handle.set_piece_deadline(p, 0)
            except Exception as e:
                log_warning("scheduler.request_tail", f"piece={p}: {e}")

    def _have_snapshot(self):
        """一次性位图快照 have 回调（P2-1：N 次绑定调用 → 1 次 status()）。"""
        return have_from_bitmap(self.handle.status().pieces)

    def tail_ready(self) -> bool:
        """尾部索引窗口是否已全部落盘（无窗口时恒为 True）。"""
        if not self._tail_pieces or self.file is None or self.handle is None:
            return True
        try:
            have = self._have_snapshot()
            return all(have(p) for p in self._tail_pieces)
        except Exception as e:
            log_warning("scheduler.tail_ready", f"{e}")
            return False

    def contiguous_progress(self) -> int:
        """从文件头开始的连续可读字节数（播放器真正能消费的量）。"""
        if not self.active or self.file is None or self.handle is None:
            return 0
        try:
            ti = self.handle.torrent_file()
            if ti is None:
                return 0
            # P2-1：连续前缀扫描走一次性位图快照，不再逐块 have_piece
            pm = PieceMap(ti.piece_length(), self.file.offset,
                          self.file.start_piece, self.file.end_piece,
                          self.file.size, self._have_snapshot())
            return contiguous_bytes(pm)
        except Exception as e:
            log_warning("scheduler.contiguous_progress", f"{e}")
            return 0

    # ---------- 生命周期 ----------

    def begin(self, handle, file) -> None:
        """开始预览指定文件。"""
        ti = handle.torrent_file()
        if ti is None:
            raise RuntimeError("元数据尚未就绪")
        n = ti.num_files()
        prio = [0] * n
        prio[file.index] = 4  # top priority
        handle.unset_flags(lt.torrent_flags.upload_mode)
        handle.set_flags(lt.torrent_flags.auto_managed)
        handle.resume()
        handle.prioritize_files(prio)
        self.handle = handle
        self.file = file
        self._scheduled_to = file.start_piece - 1
        self._play_from = file.start_piece
        self._tail_pieces = tail_piece_window(file, ti.piece_length())
        # 先预约尾部索引块，再进入顺序窗口
        self._request_tail()
        self.tick()

    def request_range(self, start_byte: int, end_byte: int) -> None:
        """按字节区间即时点播（文件内相对偏移，end_byte 为开区间）。

        播放器（FFmpeg）要读哪段就立刻下载哪段——典型场景是 MP4 尾部 moov
        探测与任意位置拖动。重复调用是幂等的（deadline 会被覆盖为 ASAP）。

        单次点播的块数**必须有上限**：拖动进度条时 FFmpeg 发的是
        `bytes=X-`（X 到文件尾），不加限制会把剩余整个文件都置为 ASAP ——
        带宽被分散到几百 MB 上，反而拖慢 seek 目标本身、顺序性也随之丧失。
        点播是「临时插队」，不移动顺序窗口的锚点（否则尾部 moov 探测会把
        锚点推到文件尾，导致顺序窗口永久停摆）。
        """
        if not self.active or self.file is None or self.handle is None:
            return
        try:
            ti = self.handle.torrent_file()
            if ti is None:
                return
            pl = ti.piece_length()
            if pl <= 0:
                return
            first = max(self.file.start_piece,
                        self.file.start_piece + max(0, start_byte) // pl)
            last = min(self.file.end_piece,
                       self.file.start_piece + max(0, end_byte - 1) // pl)
            last = min(last, first + LOOKAHEAD_PIECES - 1)
            for p in range(first, last + 1):
                self.handle.set_piece_deadline(p, 0)
        except Exception as e:
            log_warning("scheduler.request_range",
                        f"{start_byte}-{end_byte}: {e}")

    def seek_to_byte(self, byte_offset: int) -> None:
        """播放位置跳转后，从对应 piece 重新开始预约。

        必须**立即**预约 seek 目标起的窗口：调度器只在 begin()/此处/周期性
        tick() 被调用，若只重置锚点而不预约，seek 点的数据就完全依赖流服务
        的点播回调（有上限、且只覆盖那一次 HTTP 请求的区间）。
        """
        if not self.active or self.file is None:
            return
        ti = self.handle.torrent_file()
        pl = ti.piece_length()
        piece = self.file.start_piece + byte_offset // pl
        piece = max(self.file.start_piece, min(piece, self.file.end_piece))
        # 跳转必须先清掉旧位置的 ASAP 预约：拖到未缓存区再拖回已缓存区时，
        # 残留的远端分块仍在以最高优先级并行下载，与新播放位置争带宽 →
        # 明明已缓存的区域反而卡顿。清完再重建「尾部窗口 + 新位置窗口」。
        self._clear_deadlines()
        self._play_from = piece
        self._scheduled_to = piece - 1
        target = min(self.file.end_piece, piece + LOOKAHEAD_PIECES)
        self._request_tail()      # 尾部 moov 窗口同样被清掉，必须重建
        for p in range(piece, target + 1):
            try:
                self.handle.set_piece_deadline(p, 0)
            except Exception as e:
                log_warning("scheduler.seek.deadline", f"piece={p}: {e}")
        self._scheduled_to = max(self._scheduled_to, target - 1)
        self.tick()

    def _clear_deadlines(self) -> None:
        """清空全部分块 deadline（跳转前调用，避免旧位置残留抢占带宽）。"""
        if self.handle is None:
            return
        try:
            self.handle.clear_piece_deadlines()
        except Exception as e:
            log_warning("scheduler.clear_deadlines", f"{e}")

    def _first_missing_from(self, from_piece: int) -> int:
        """从 from_piece 起向后第一个未落盘的块（全部就绪时返回 end_piece）。"""
        p = max(from_piece, self.file.start_piece)
        end = self.file.end_piece
        try:
            while p <= end and self.handle.have_piece(p):
                p += 1
        except Exception as e:
            log_warning("scheduler.first_missing", f"{e}")
        return min(p, end)

    def tick(self) -> None:
        """周期调用：从**播放位置**起按下载进度向前滚动预约窗口。

        滚动度量必须以播放位置（_play_from）为起点，而不是「从文件头的连续
        前缀」——seek 之后文件头前缀仍停留在旧位置，用它当判据会让窗口恒被
        判定为「充裕」而永不滚动（历史缺陷：seek 后 seek 点附近零预约）。
        """
        if not self.active or self.file is None:
            return
        ti = self.handle.torrent_file()
        if ti is None:
            return
        # 尾部索引窗口尚未就绪则持续补拉（探测 moov 是开播前提）
        if self._tail_pieces and not self.tail_ready():
            self._request_tail()
        first_missing = self._first_missing_from(self._play_from)
        if first_missing <= self._scheduled_to - 16:
            return  # 窗口仍然充裕，无需操作
        start = max(first_missing, self._scheduled_to + 1)
        target = min(self.file.end_piece, first_missing + LOOKAHEAD_PIECES)
        for p in range(start, target + 1):
            try:
                self.handle.set_piece_deadline(p, 0)
            except Exception as e:
                log_warning("scheduler.tick.deadline", f"piece={p}: {e}")
        self._scheduled_to = max(self._scheduled_to, target - 1)

    def buffer_progress(self) -> float:
        """当前预览文件的缓冲比例 0~1（按连续可读前缀计）。"""
        if not self.active or self.file is None or self.file.size == 0:
            return 0.0
        return min(1.0, self.contiguous_progress() / self.file.size)

    def stop(self) -> None:
        """取消预览：清空 deadline、全部文件优先级置 0 并暂停。

        同时撤掉 auto_managed：libtorrent 的队列管理（active_downloads）
        可能自动 resume 处于 paused 的种子，导致「停止预览」后仍在后台续传。
        """
        if self.handle is not None:
            try:
                self.handle.clear_piece_deadlines()
                ti = self.handle.torrent_file()
                if ti is not None:
                    self.handle.prioritize_files([0] * ti.num_files())
                    self.handle.unset_flags(lt.torrent_flags.auto_managed)
                self.handle.pause()
            except Exception as e:
                log_warning("scheduler.stop", f"{e}")
        self.handle = None
        self.file = None
        self._scheduled_to = -1
        self._tail_pieces = []