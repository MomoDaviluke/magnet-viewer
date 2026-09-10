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

LOOKAHEAD_PIECES = 60        # 一次向前预约的分块数量**上限**（块数口径，契约冻结）
LOOKAHEAD_BYTES = 16 * 1024 * 1024  # 播放窗口的**字节**预算（plan/07 阶段 1）
DEADLINE_STEP_MS = 400       # 窗口内相邻分块的 deadline 步长（递增保序）
# 尾部索引窗口按**字节**收敛（plan/07 阶段 2，2026-09-10 真机 4.1GB / 4MB 块 /
# 2.4MB/s）：旧式 `min(size, max(4MB, 1%·size), 64MB)` 在 4.1GB 上给出 **44MB**
# （11 块）整窗必须就绪才开门控——2.4MB/s 下光尾窗就 ≈18s，且与头部窗口抢带宽，
# 用户看到的就是「缓冲恒 0.0%」。新式 `max(2MB, 0.25%·size)` 收敛到 ≈10MB（3 块），
# 门控又只等「尾部入口」（最末 2MB = 1 块），尾窗规模不再是开播的瓶颈。
TAIL_BYTES_MIN = 2 * 1024 * 1024   # 尾部窗口下限（plan/07 阶段 2：4MB → 2MB）
TAIL_BYTES_MAX = 16 * 1024 * 1024  # 尾部窗口上限（plan/07 阶段 2：64MB → 16MB）
TAIL_RATIO = 0.0025          # 大文件按体积 0.25% 放大窗口（plan/07 阶段 2：1% → 0.25%）
TAIL_ENTRY_BYTES = 2 * 1024 * 1024  # 开播门控的「尾部入口」= 文件最后 2MB
TAIL_MAX_PIECES = 128        # 一次预约的尾部块数上限

# moov 索引块在尾部的容器家族（FFmpeg mov 解封装器适用）
TAIL_FIRST_EXTS = {".mp4", ".mov", ".m4v", ".3gp", ".3g2", ".mj2"}


def window_pieces(piece_length: int) -> int:
    """播放窗口块数：按**字节预算**换算，再夹到 ``[4, LOOKAHEAD_PIECES]``。

    为什么按字节而不按块数（plan/07 阶段 1，2026-09-10 真机 + 本机 A/B 实测）：
    ``LOOKAHEAD_PIECES=60`` 与块尺寸脱钩——16KB 块下窗口仅 960KB（合理），
    4MB 块下却是 **240MB 全 ASAP**（300MB 实验文件的 80%）。libtorrent 在
    几十个同级「紧急」块里按可用性散抓（实测已下块 3/10/22/31/32/34/66
    乱序、头 3 块一个不下）→ 连续前缀凑不出 → 播放门控永不满足（真机
    4.1GB / 4MB 块：缓冲恒 0.0%、30s 不见画面）。按字节换算后 4MB 块窗口
    收窄到 16MB（4 块），实测头 1 块就绪 14.0s → 9.0s（做种端限 1MB/s）。

    下限 4 块：8MB 及以上块长时 ``ceil(16MB/pl) <= 2``，窗口缩到 1~2 块会让
    「连续前缀」几乎没有余量，至少 4 块才能维持顺序流水。
    上限沿用 ``LOOKAHEAD_PIECES``（60，契约冻结项）：小块窗口尺寸与旧行为
    逐字一致（16KB → 60 块 = 960KB）。
    非法块长（<=0）兜底返回上限常量，不除零、等价旧行为。
    """
    if piece_length <= 0:
        return LOOKAHEAD_PIECES
    return max(4, min(LOOKAHEAD_PIECES,
                      math.ceil(LOOKAHEAD_BYTES / piece_length)))


def tail_window_bytes(size: int) -> int:
    """尾部索引窗口的**字节**预算：``min(size, max(2MB, 0.25%·size), 16MB)``。

    为什么按字节收敛（plan/07 阶段 2，2026-09-10 真机 + 本机实验）：
    旧式 ``min(size, max(4MB, 1%·size), 64MB)`` 在真机 4.1GB / 4MB 块的
    mp4 上给出 **44MB**（11 块）——2.4MB/s 慢链路下光尾窗就 ≈18s，且旧门控
    要求「整尾窗就绪」才开播，尾窗与头部顺序前缀抢带宽 → 用户实测
    「缓冲恒 0.0%、30s 不出画面」。收敛后 4.1GB → ≈10MB（3 块）、1GB →
    ≈2.5MB、500MB 及以下 → 2MB 下限；小文件不超自身大小（100KB → 100KB）。
    纯函数（不碰句柄/文件对象），便于单测与后续调参（contract_check 冻结）。

    ``size <= 0`` 兜底返回 0（调用方 ``tail_piece_window`` 已先行拒绝非法
    文件；此处防空文件算出负数窗口）。
    """
    if size <= 0:
        return 0
    return min(size, max(TAIL_BYTES_MIN, int(size * TAIL_RATIO)), TAIL_BYTES_MAX)


def tail_entry_pieces(file, piece_length: int) -> list[int]:
    """「尾部入口」= 文件最后 ``min(2MB, size)`` 字节覆盖的块（闭区间，升序）。

    这是**开播门控**的判据（plan/07 阶段 2）：moov 就在普通 mp4 的最末尾，
    4MB 块下只需最末 1 块就位即可让 FFmpeg 探测成功，**不必**等整个尾窗
    （4.1GB 的 10MB 尾窗 = 3 块）。整尾窗仍由 tick() 在后台继续补拉，
    真失败走既有的退避重试——门控只负责「可以一试了」。

    非法输入（size<=0 / piece_length<=0）返回空列表，调用方据此视作
    「无尾部入口」恒就绪（非 moov 尾部家族同此）。
    """
    if file is None or file.size <= 0 or piece_length <= 0:
        return []
    span = min(TAIL_ENTRY_BYTES, file.size)
    n = max(1, math.ceil(span / piece_length))
    n = min(n, file.end_piece - file.start_piece + 1)
    return list(range(file.end_piece - n + 1, file.end_piece + 1))


def tail_piece_window(file, piece_length: int) -> list[int]:
    """计算需要优先补拉的尾部窗口（闭区间 piece 列表，升序）。

    仅对 moov 在尾部的容器家族生效；返回空列表表示无需补拉。
    窗口字节量走纯函数 ``tail_window_bytes``（plan/07 阶段 2 收敛，
    4.1GB 44MB → ≈10MB），再受 ``TAIL_MAX_PIECES`` 与文件实际块数夹取。
    """
    if file.ext not in TAIL_FIRST_EXTS or file.size <= 0 or piece_length <= 0:
        return []
    tail_bytes = tail_window_bytes(file.size)
    n = max(1, math.ceil(tail_bytes / piece_length))
    n = min(n, TAIL_MAX_PIECES, file.end_piece - file.start_piece + 1)
    return list(range(file.end_piece - n + 1, file.end_piece + 1))


class PreviewScheduler:
    """管理「预览某个文件」的下载策略。

    策略：
    1. prioritize_files 只保留目标文件优先级，其余置 0；
    2. MP4/MOV 家族先预约尾部索引窗口（moov，按字节收敛，plan/07 阶段 2），
       再按播放顺序预约分块；
    3. 窗口块数按**字节预算**换算（``window_pieces``），窗口内
       ``set_piece_deadline`` 按序号**递增**（``DEADLINE_STEP_MS``）——
       连续前缀先到齐；**尾部入口块提前**到头窗头几块并行（阶段 2.5），
       其余尾块仍排在头窗之后；
    4. tick() 周期性根据已完成字节向前滚动预约窗口，并在整尾窗未齐时持续
       补拉（入口就绪不取消整尾窗）；
    5. 开播门控看 ``tail_entry_ready``（最末 2MB），不看 ``tail_ready``（整尾窗）。
    """

    def __init__(self):
        self.handle = None
        self.file = None
        self._scheduled_to = -1
        self._play_from = -1     # 播放起点块（begin=文件头 / seek=目标块）
        self._head_n = 0         # 当前播放窗口块数（window_pieces 换算结果）
        self._tail_pieces: list[int] = []
        self._entry_pieces: list[int] = []   # 尾部入口块（开播门控判据）
        self.on_file_completed = None  # callback(int file_index)，由会话层注入

    @property
    def active(self) -> bool:
        return self.handle is not None

    # ---------- 尾部索引窗口（moov） ----------

    def _request_tail(self) -> None:
        """预约尾部索引窗口（幂等）：**入口块提前**，其余尾块排在头窗之后。

        阶段 2.5 起「入口块」（``_entry_pieces`` = 文件最后 ``min(2MB, size)``
        覆盖的块，moov 所在的最末块）的 deadline 提到 ``DEADLINE_STEP_MS``
        ——与头窗第 2 块同级：**开播门控必需的两块是「头第 1 块 + 尾入口」**，
        它们应当并行尽早到齐。阶段 2 实测 4.1GB / 4MB 块 / 2MB/s 下门控
        19.0s = 「头 8.0s + 入口 11.0s」——入口被排在**整个头窗**（4 块 =
        16MB ≈ 8s）之后白白多等一个头窗；提前后门控 ≈「头块就绪 + 3s 内」。

        权衡：**只提前入口、不提前整尾窗**。入口只需 ``min(2MB, size)``
        （4MB 块下 1 块），抢占代价可忽略；**整尾窗**（4.1GB ≈10MB = 3 块）
        仍排在头窗之后（``n_head*STEP + 1000 + j*STEP``），由 ``tick()``
        持续补拉——它是 moov 大于入口时的兜底，若一并提前会与顺序前缀抢
        带宽、破坏「头窗保序」（``playback_window_test §B``）。

        注：``_entry_pieces`` 是 ``_tail_pieces`` 的后缀（正常几何），循环仍只
        遍历 ``_tail_pieces``，故单批预约块数不受入口数量影响。
        """
        if not self._tail_pieces or self.handle is None:
            return
        base = self._head_n * DEADLINE_STEP_MS + 1000
        entry = set(self._entry_pieces)
        for j, p in enumerate(self._tail_pieces):
            ms = (DEADLINE_STEP_MS if p in entry
                  else base + j * DEADLINE_STEP_MS)
            try:
                self.handle.set_piece_deadline(p, ms)
            except Exception as e:
                log_warning("scheduler.request_tail", f"piece={p}: {e}")

    def _have_snapshot(self):
        """一次性位图快照 have 回调（P2-1：N 次绑定调用 → 1 次 status()）。"""
        return have_from_bitmap(self.handle.status().pieces)

    def tail_ready(self) -> bool:
        """尾部索引窗口是否已全部落盘（无窗口时恒为 True）。

        plan/07 阶段 2 起**不再**是开播门控判据（尾窗收敛后仍有 10MB ≈ 3 块，
        等整窗对 2.4MB/s 慢链路仍是秒级开销），但保留：契约与方法面冻结、
        ``tick()`` 靠它决定是否继续补拉尾窗。
        """
        if not self._tail_pieces or self.file is None or self.handle is None:
            return True
        try:
            have = self._have_snapshot()
            return all(have(p) for p in self._tail_pieces)
        except Exception as e:
            log_warning("scheduler.tail_ready", f"{e}")
            return False

    def tail_entry_ready(self) -> bool:
        """尾部**入口**是否就绪（开播门控判据，plan/07 阶段 2）。

        只判定文件最后 ``min(2MB, size)`` 字节覆盖的块（``_entry_pieces``，
        moov 所在处），而不是整个尾窗：4.1GB / 4MB 块真机上旧门控要等
        「整尾窗 44MB 齐」（2.4MB/s ≈18s，实测缓冲恒 0.0%），新门控只需
        最末 1 块 —— 头窗之后排到的第 1 个尾部块，秒级即可开播。
        整尾窗（≈10MB）仍由 ``tick()`` 在后台继续补拉，**不取消**；若播放器
        真因 mdat/moov 越界打开失败，走既有的退避重试（`_on_stream_failed`）。

        无尾部入口（非 moov 尾部家族 / 非法文件 / 未 begin）恒为 True，
        与 ``tail_ready`` 的空窗口语义一致（MKV 等不需要尾部即可开播）。
        """
        if not self._entry_pieces or self.file is None or self.handle is None:
            return True
        try:
            have = self._have_snapshot()
            return all(have(p) for p in self._entry_pieces)
        except Exception as e:
            log_warning("scheduler.tail_entry_ready", f"{e}")
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
        """开始预览指定文件。

        窗口块数按**字节预算**换算（``window_pieces``，plan/07 阶段 1）：
        旧实现固定 60 块，4MB 块下 = 240MB 全 ASAP → libtorrent 在几十个
        同级「紧急」块里散抓、连续前缀凑不出（真机 4.1GB/4MB 块：缓冲恒
        0.0%、30s 不见画面）。窗口内 deadline 按序号**递增**
        （第 i 块 = ``i * DEADLINE_STEP_MS``）：从播放位置起顺序取块，保证
        连续前缀先到齐；旧实现全 0 等于不携带任何排序信息。

        注（A0 真链路确证，2026-09-09 本机做种实证）：ASAP 窗口只管**顺序**，
        全文件由 file-priority 4 负责持续落盘——窗口外块照常下载，只是不插队。
        """
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
        self._head_n = window_pieces(ti.piece_length())
        self._tail_pieces = tail_piece_window(file, ti.piece_length())
        # 尾部入口 = 开播门控判据（plan/07 阶段 2）；无尾窗（非 moov 家族）时为空
        self._entry_pieces = (tail_entry_pieces(file, ti.piece_length())
                              if self._tail_pieces else [])
        # 先预约尾部索引块，再进入顺序窗口
        self._request_tail()
        self.tick()

    def request_range(self, start_byte: int, end_byte: int) -> None:
        """按字节区间即时点播（文件内相对偏移，end_byte 为开区间）。

        播放器（FFmpeg）要读哪段就立刻下载哪段——典型场景是 MP4 尾部 moov
        探测与任意位置拖动。重复调用是幂等的（deadline 会被覆盖）。

        单次点播的块数**必须有上限**：拖动进度条时 FFmpeg 发的是
        `bytes=X-`（X 到文件尾），不加限制会把剩余整个文件都置为 ASAP ——
        带宽被分散到几百 MB 上，反而拖慢 seek 目标本身、顺序性也随之丧失。
        点播是「临时插队」，不移动顺序窗口的锚点（否则尾部 moov 探测会把
        锚点推到文件尾，导致顺序窗口永久停摆）。

        deadline 改**递增**（第 i 块 = ``i * DEADLINE_STEP_MS``，plan/07
        阶段 1）：插队语义不变（首块仍立即），但把区间内顺序也表达出来，
        不再全 0 洪泛（与窗口策略同源）。
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
                self.handle.set_piece_deadline(p,
                                              (p - first) * DEADLINE_STEP_MS)
        except Exception as e:
            log_warning("scheduler.request_range",
                        f"{start_byte}-{end_byte}: {e}")

    def seek_to_byte(self, byte_offset: int) -> None:
        """播放位置跳转后，从对应 piece 重新开始预约。

        必须**立即**预约 seek 目标起的窗口：调度器只在 begin()/此处/周期性
        tick() 被调用，若只重置锚点而不预约，seek 点的数据就完全依赖流服务
        的点播回调（有上限、且只覆盖那一次 HTTP 请求的区间）。

        窗口块数同样走 ``window_pieces``（字节预算，plan/07 阶段 1），窗口内
        deadline 从新位置起递增。
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
        self._head_n = window_pieces(pl)
        target = min(self.file.end_piece, piece + self._head_n - 1)
        self._request_tail()      # 尾部 moov 窗口同样被清掉，必须重建
        for p in range(piece, target + 1):
            try:
                self.handle.set_piece_deadline(
                    p, (p - piece) * DEADLINE_STEP_MS)
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

        窗口块数走 ``window_pieces``（字节预算），防抖仍用既有的
        first_missing / _scheduled_to 语义；窗口内 deadline 按
        **播放位置起递增**（``(p - _play_from) * DEADLINE_STEP_MS``）——
        与头窗同源，保证滚动新增的远端块不会插到近端块前面
        （plan/07 阶段 1：全 0 洪泛 = 无序）。
        """
        if not self.active or self.file is None:
            return
        ti = self.handle.torrent_file()
        if ti is None:
            return
        # 尾部索引窗口尚未就绪则持续补拉（探测 moov 是开播前提）
        if self._tail_pieces and not self.tail_ready():
            self._request_tail()
        n = self._head_n or window_pieces(ti.piece_length())
        first_missing = self._first_missing_from(self._play_from)
        if first_missing <= self._scheduled_to - 16:
            return  # 窗口仍然充裕，无需操作
        start = max(first_missing, self._scheduled_to + 1)
        target = min(self.file.end_piece, first_missing + n - 1)
        for p in range(start, target + 1):
            try:
                self.handle.set_piece_deadline(
                    p, (p - self._play_from) * DEADLINE_STEP_MS)
            except Exception as e:
                log_warning("scheduler.tick.deadline", f"piece={p}: {e}")
        self._scheduled_to = max(self._scheduled_to, target - 1)

    def buffer_progress(self) -> float:
        """当前预览文件的缓冲比例 0~1（按连续可读前缀计）。"""
        if not self.active or self.file is None or self.file.size == 0:
            return 0.0
        return min(1.0, self.contiguous_progress() / self.file.size)

    def stop(self, release_only: bool = False) -> None:
        """取消预览：清空 deadline 并还原调度锚点。

        release_only=False（hold 档／基线行为）：全部文件优先级置 0 并暂停。
        同时撤掉 auto_managed：libtorrent 的队列管理（active_downloads）
        可能自动 resume 处于 paused 的种子，导致「停止预览」后仍在后台续传。

        release_only=True（convert 档）：**只**清 deadline + 还原锚点——
        不清文件优先级、不 pause、不撤 auto_managed。begin() 已把目标文件
        file-priority 置 4（引擎本就在全文件落盘，A0 实证），是否转正由
        宿主（SessionManager）在锁外决策；转正后的句柄继续按优先级缓存。
        """
        if self.handle is not None:
            try:
                self.handle.clear_piece_deadlines()
                if not release_only:
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
        self._head_n = 0
        self._tail_pieces = []
        self._entry_pieces = []