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
        self._tail_pieces = tail_piece_window(file, ti.piece_length())
        # 先预约尾部索引块，再进入顺序窗口
        self._request_tail()
        self.tick()

    def request_range(self, start_byte: int, end_byte: int) -> None:
        """按字节区间即时点播（文件内相对偏移，end_byte 为开区间）。

        播放器（FFmpeg）要读哪段就立刻下载哪段——典型场景是 MP4 尾部 moov
        探测与任意位置拖动。重复调用是幂等的（deadline 会被覆盖为 ASAP）。
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
            for p in range(first, last + 1):
                self.handle.set_piece_deadline(p, 0)
        except Exception as e:
            log_warning("scheduler.request_range",
                        f"{start_byte}-{end_byte}: {e}")

    def seek_to_byte(self, byte_offset: int) -> None:
        """播放位置跳转后，从对应 piece 重新开始预约。"""
        if not self.active or self.file is None:
            return
        ti = self.handle.torrent_file()
        pl = ti.piece_length()
        piece = self.file.start_piece + byte_offset // pl
        self._scheduled_to = min(piece - 1, self.file.end_piece)
        self.tick()

    def tick(self) -> None:
        """周期调用：根据下载进度向前滚动预约窗口。"""
        if not self.active or self.file is None:
            return
        ti = self.handle.torrent_file()
        if ti is None:
            return
        pl = ti.piece_length()
        # 尾部索引窗口尚未就绪则持续补拉（探测 moov 是开播前提）
        if self._tail_pieces and not self.tail_ready():
            self._request_tail()
        done = self.contiguous_progress()
        first_missing = min(self.file.end_piece, self.file.start_piece + done // pl)
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