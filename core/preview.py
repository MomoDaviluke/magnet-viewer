"""预览桥与状态快照（fetcher 重构阶段 5 抽出）。

职责边界：
    ``PreviewCore`` = 播放器/画廊/状态栏背后的这条链——预览启停
    （scheduler.begin/stop）、「磁盘路径 → (记录, 文件)」反查、分块可用性
    映射（PieceMap）、按字节区间的任务级点播补拉（demand）、当前任务的
    分块查询与状态快照（status）。

核心安全语义（历史上多个 P0/P1 缺陷的沉淀，一条都不许改）：
    - ``piece_map_for_path`` / ``demand_for_path`` 返回 **None/False 表示
      「无法判定」**——流服务调用方绝不可降级为「整文件可用」，那会把未
      下载的稀疏零数据喂给播放器（partial file / Invalid data 缺陷根源）；
    - 路径反查用 normpath 归一 + 前缀匹配，Windows 反斜杠与正斜杠混用
      均须命中（目录隔离改造后键为绝对路径）；
    - ``status()`` 里 contiguous 与 buffer 同源一次扫描复用；resolving/
      elapsed 走 registry 一致快照（R-1 家族）。

锁纪律：with reg.lock 段内只调 *_locked / 裸字典读；句柄绑定调用
（status()/have_piece/torrent_file）一律出锁——沿用阶段 4 R-3 的原则，
UI 轮询与流服务回调不按住任务锁。
"""
from __future__ import annotations

import os
import time

import libtorrent as lt

from .logutil import log_warning
from .models import ParseResult, PieceMap, TorrentFile, have_from_bitmap
from .registry import TaskRecord, TaskRegistry
from .scheduler import LOOKAHEAD_PIECES

STATE_NAMES = {
    getattr(lt.torrent_status, k, None): k.replace("_", " ")
    for k in ("checking_files", "downloading_metadata", "downloading",
              "finished", "seeding", "allocating", "checking_resume_data")
    if getattr(lt.torrent_status, k, None) is not None
}


class PreviewCore:
    """预览与状态（挂在 SessionManager 上，经其薄委托对外）。"""

    def __init__(self, reg: TaskRegistry,
                 scheduler_get, ses_get=None):
        self.reg = reg
        self._scheduler_get = scheduler_get

    # ---------- 预览 ----------

    def start_preview(self, f: TorrentFile):
        """开始预览某个文件（边下边播 / 图片下载）。"""
        with self.reg.lock:
            handle, result = self.reg.handle, self.reg.result
        if handle is None or result is None:
            raise RuntimeError("请先解析种子")
        self._scheduler_get().begin(handle, f)

    def stop_preview(self):
        self._scheduler_get().stop()

    # ---------- 磁盘路径反查（流服务回调入口） ----------

    def find_record_for_path(self, disk_path: str):
        """磁盘路径 -> (TaskRecord, TorrentFile) 反查：匹配任务落盘目录前缀。

        供流服务回调按实际磁盘路径定位任务句柄（下载中任务文件的分块
        可用性判定），返回 None 表示未知/无句柄（调用方不得降级为全量）。
        """
        np_ = os.path.normpath(disk_path)
        with self.reg.lock:
            recs = list(self.reg.torrents.values())
        for rec in recs:
            if not rec.save_path or rec.handle is None or rec.result is None:
                continue
            base = os.path.normpath(rec.save_path)
            if np_ == base or np_.startswith(base + os.sep):
                rel = os.path.relpath(np_, base).replace("\\", "/")
                for f in rec.result.files:
                    if f.path == rel:
                        return rec, f
        return None

    def piece_map_for_path(self, disk_path: str) -> PieceMap | None:
        """按磁盘路径提供分块可用性映射（下载任务文件；未知返回 None）。

        None 表示「无法判定可用性」——调用方（流服务 pieces_cb）绝不
        能把 None 当作整文件可用：那会把未下载的稀疏零数据喂给播放器
        （历史「partial file / Invalid data」缺陷的直接根源）。
        """
        hit = self.find_record_for_path(disk_path)
        if hit is None:
            return None
        rec, f = hit
        handle = rec.handle
        try:
            # 单次锁外句柄调用（P1-8：异常/缺失 → None，绝不整文件可用）
            st = handle.status()          # P2-1：位图快照，替代逐块 have_piece
            pl = handle.torrent_file().piece_length()
        except Exception as e:
            log_warning("fetcher.piece_map.piece_length", f"{e}")
            return None
        return PieceMap(piece_length=pl, offset=f.offset,
                        start_piece=f.start_piece, end_piece=f.end_piece,
                        size=f.size, have=have_from_bitmap(st.pieces))

    def demand_for_path(self, disk_path: str, start_byte: int,
                        end_excl: int) -> bool:
        """按磁盘路径触发任务级按需补拉（播放器要哪段就先下哪段）。

        与 scheduler.request_range 语义一致，但作用于任意下载任务句柄
        （流服务 demand_cb 对非预览文件的请求也生效）。

        单次点播同样**必须有 LOOKAHEAD_PIECES 上限**（对齐
        scheduler.py:156 及其注释的论证）：FFmpeg 拖动发的是 `bytes=X-`
        （X 到文件尾），不截断会把剩余整文件全刷成 ASAP——带宽摊到几百
        MB 上反而拖慢点播目标本身。全量落盘由 file-priority 负责，
        点播只是临时插队，不需要也不应该包揽整文件。
        """
        hit = self.find_record_for_path(disk_path)
        if hit is None:
            return False
        rec, f = hit
        try:
            pl = rec.handle.torrent_file().piece_length()
            if pl <= 0:
                return False
            first = max(f.start_piece,
                        f.start_piece + max(0, start_byte) // pl)
            last = min(f.end_piece,
                       f.start_piece + max(0, end_excl - 1) // pl)
            last = min(last, first + LOOKAHEAD_PIECES - 1)
            for p in range(first, last + 1):
                rec.handle.set_piece_deadline(p, 0)
            return True
        except Exception as e:
            log_warning("fetcher.demand_for_path", f"{disk_path}: {e}")
            return False

    # ---------- 当前任务查询 ----------

    def task_result(self, task_id: str) -> ParseResult | None:
        """下载任务的解析结果（供 UI 载入文件树/预览）。"""
        key = str(task_id or "")
        with self.reg.lock:
            rec = self.reg.torrents.get(key)
            return rec.result if rec is not None else None

    def have_piece(self, piece: int) -> bool:
        """指定种子分块是否已完整落盘（供流服务/调度器判定可读区间）。"""
        with self.reg.lock:
            handle = self.reg.handle
        if handle is None:
            return False
        try:
            return bool(handle.have_piece(int(piece)))
        except Exception as e:
            log_warning("fetcher.have_piece", f"分块查询失败 piece={piece}: {e}")
            return False

    def piece_length(self) -> int | None:
        """当前种子分块大小；元数据未就绪时返回 None。"""
        with self.reg.lock:
            handle = self.reg.handle
        if handle is None:
            return None
        try:
            if not handle.has_metadata():
                return None
            return handle.torrent_file().piece_length()
        except Exception as e:
            log_warning("fetcher.piece_length", f"{e}")
            return None

    def current_result(self) -> ParseResult | None:
        """当前任务解析结果（锁读）。"""
        with self.reg.lock:
            return self.reg.result

    def status(self) -> dict | None:
        """线程安全的状态快照，供 UI 定时轮询。"""
        reg = self.reg
        with reg.lock:
            handle = reg.handle
        if handle is None:
            return None
        try:
            s = handle.status()
        except Exception as e:
            log_warning("fetcher.status.handle", f"{e}")
            return None
        st = {
            "state": STATE_NAMES.get(s.state, str(s.state)),
            "num_peers": s.num_peers,
            "num_seeds": s.num_seeds,
            "download_rate": s.download_payload_rate,
            "total_done": s.total_done,
            "metadata_ready": handle.has_metadata(),
        }
        sched = self._scheduler_get()
        # contiguous 与 buffer 同源（连续可读前缀），一次扫描复用——
        # 避免每 700ms 重复两次 O(分片数) 的 have_piece 线性扫描
        contig = sched.contiguous_progress() if sched.active else 0
        pf = sched.file
        st["buffer"] = (min(1.0, contig / pf.size)
                        if sched.active and pf is not None and pf.size > 0
                        else 0.0)
        st["contiguous"] = contig
        st["tail_ready"] = sched.tail_ready() if sched.active else True
        st["preview_file"] = pf
        # R-1 家族（D4）：resolving/elapsed 成对读走 registry 一致快照，
        # 不再锁外分两次读别名（撕裂窗口）。
        st["resolving"], st["elapsed"] = reg.resolving_snapshot()
        try:
            st["file_progress"] = list(handle.file_progress())
        except Exception as e:
            log_warning("fetcher.status.file_progress", f"{e}")
            st["file_progress"] = []
        return st
