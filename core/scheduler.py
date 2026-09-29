"""预览下载调度：单文件锁定 + 顺序分块 + 索引块（moov）优先。

MP4/MOV 家族的 `moov` 索引块通常位于文件**尾部**，而播放器（FFmpeg）
打开媒体前必须先读到它。因此对这类文件先预约一个“尾部窗口”，
使索引块尽早落盘、播放器探测即可成功；随后再按播放顺序向前滚动窗口。

其余格式（MKV/WebM/TS 等）不需要尾部即可开播，保持从头顺序下载。
"""
from __future__ import annotations

import functools
import math
import threading

import libtorrent as lt

from .logutil import log_warning
from .models import (PieceMap, contiguous_bytes, contiguous_bytes_of,
                     have_from_bitmap)

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


# --------------------------------------------------------------- 互斥装饰器
# plan/11 R-3：PreviewScheduler 的**状态读改写**必须互斥（GUI / HTTP / shutdown
# 三方并发写），但**不能**用「把每个方法体都改成 try/finally 包锁」的做法——
# 那是 6 处 × 十几行的改动面，每一处都是一个新引入 bug 的机会。
# 用一个方法级装饰器把「取锁/放锁」收在一处，方法体一行不动：
#   - functools.wraps 保证 __name__/__doc__/__wrapped__ 不变，故
#     inspect.signature 仍返回**原签名**，contract_check 的基线表不受影响；
#   - blocking=False 用于热路径（tick / request_range），拿不到锁就**跳过本拍**
#     ——周期任务跳一拍无害（700ms 后还会来），点播是「hint」，流服务
#     `_wait_ready` 每 DEMAND_RENEW_SECS 会重新点播。**这样永不排队、
#     永不形成锁车队**；
#   - blocking=True 用于生命周期与用户动作（begin / seek_to_byte / stop / rearm），
#     它们必须真正生效，不能静默跳过。
#
# **锁内不得调用会取锁的方法**（threading.Lock 不可重入）。当前取锁的是
# begin / tick / seek_to_byte / rearm / request_range / stop / attach_view
# **七个**入口，即下面的 `_LOCK_TAKING`，供静态检查（concurrency_test §F）用。
# 特别注意 `begin()` 与 `seek_to_byte()` **在调用 `self.tick()` 之前必须已放锁**
# （它们只包住状态赋值区间）——否则 tick 的非阻塞取锁必然失败，首次窗口预约会
# 静默丢失（播放永远不开，且**不报错**；§F 有确定性反例自检钉住这一点）。
_LOCK_TAKING = frozenset({
    "begin", "tick", "seek_to_byte", "rearm", "request_range", "stop",
    "attach_view",
})


def _exclusive(blocking: bool = True):
    """方法级互斥：进入方法前取 ``self._lock``，退出时释放。

    ``blocking=False`` 时取不到锁就**直接返回 None**（跳过本拍），
    调用方必须能接受「这一拍什么都没做」——tick/request_range 满足该前提。
    """
    def _deco(fn):
        @functools.wraps(fn)
        def _wrapper(self, *args, **kwargs):
            if blocking:
                self._lock.acquire()
            elif not self._lock.acquire(blocking=False):
                return None
            try:
                return fn(self, *args, **kwargs)
            finally:
                self._lock.release()
        return _wrapper
    return _deco


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

    线程安全（plan/11 R-3 / R-10）
    -----------------------------
    **写者三个线程**：T1 GUI（``tick``/``seek_to_byte``/``rearm``/``begin``）、
    **T5 HTTP 请求线程**（``ui/main_window.py:261`` 的 demand_cb →
    ``demand_range`` → ``request_range``）、T6 shutdown 线程（``stop``）。
    读探针（``tail_ready``/``tail_entry_ready``/``contiguous_progress``/
    ``gate_snapshot``/``buffer_progress``/``active``/``piece_view``）由 T1 与 T5 调用。

    **只给写者加锁，读探针刻意不加锁**：读探针是幂等的展示/门控判据，最坏返回
    一次陈旧值，下一拍（700ms）即修正；给它们加锁会把「HTTP 每 50ms 一次、
    16 并发」的轮询卷进这把锁，收益不抵风险。读探针取用状态时已用局部引用
    （``_torrent_file_or_none`` 等，plan/11 R-5），故并发置 None 不会抛。

    **锁序（单向，必须遵守）**：``scheduler._lock`` → ``PieceView._lock``
    →（本模块**不取** registry 的全局锁）。**本模块是锁序的末端**：
    任何在 ``with self._lock`` 段内去读注册表 / 落盘 / 取 reg.lock 的改动都会
    立刻制造环路（第 2 批已把原先唯一的反向边
    ``core/resolver.py`` 的「持 reg.lock 调 stop()」**去掉**，故当前**无环**）。

    **与「锁内零 libtorrent」纪律的关系（例外登记，第 2 批复审要求写明）**
    ---------------------------------------------------------------------
    本类的锁内**确实**会执行 libtorrent 绑定调用（``set_piece_deadline`` /
    ``clear_piece_deadlines`` / ``prioritize_files`` / ``pause`` /
    ``torrent_file()``）。这是**有意为之的例外**，依据三条：

    1. 它们是**非阻塞的状态变更**，不是 I/O。那条纪律（`core/fetcher.py:462`
       的「锁内零 libtorrent」）针对的是 ``remove_torrent`` / 落盘 / fsync 这类
       会真的阻塞的调用 —— 本类锁内没有任何 I/O；
    2. 锁是**本对象私有**的短锁。实测最坏持锁 ≈ **1ms**（生产路径注入了
       `PieceView`，`_have_snapshot` 走 ``view.have`` 不物化位图）；
       上限 ≈ **10ms** 只出现在**未注入视图**的退化路径（测试 / 直接 begin 的
       用法），此时 ``handle.status().pieces`` 是 O(块数) 物化（40 万块 ≈ 7.9ms）；
    3. 两个热路径（``tick`` / ``request_range``）用**非阻塞**取锁，拿不到就跳过
       本拍 ⇒ **永不排队、不成锁车队**；只有 ``begin``/``seek_to_byte``/
       ``rearm``/``stop`` 会阻塞等待，最坏等一个 tick（≈1ms）。

    ⇒ **本条例外的前提是「锁内只做非阻塞状态变更」**。若将来有人在 ``_lock``
    段内落盘、读文件或调用 ``ses.remove_torrent`` 之类的阻塞绑定，例外立即失效，
    必须改成「锁内取快照 → 锁外调用」。

    **不变式**：锁内不得调用 ``_LOCK_TAKING`` 里的任何方法（Lock 不可重入）；
    ``begin``/``seek_to_byte`` 必须在调 ``self.tick()`` 之前放锁。
    两条都由 ``concurrency_test.py §F`` 静态检查（扫描器同时认
    ``@_exclusive`` 装饰的方法体与显式 ``with self._lock`` 两种形态）。
    """

    def __init__(self):
        # plan/11 R-3：调度器**自有**的互斥锁（与 registry 的全局锁是两把）。
        # 见类文档「线程安全」一节：它只护本对象的字段读改写，且**绝不**在
        # 持锁状态下调用另一个自持锁的公开方法。
        self._lock = threading.Lock()
        self.handle = None
        self.file = None
        self._scheduled_to = -1
        self._play_from = -1     # 播放起点块（begin=文件头 / seek=目标块）
        self._head_n = 0         # 当前播放窗口块数（window_pieces 换算结果）
        self._tail_pieces: list[int] = []
        self._entry_pieces: list[int] = []   # 尾部入口块（开播门控判据）
        # callback(int file_index)，由会话层注入（当前**全项目无赋值点**，恒 None）。
        # plan/11 R-11：接入后告警线程（T2）会成为调度器的**第四个写者**。
        # 第 2 批已给状态读改写加锁，故接入本身安全——但接入方**必须**只通过
        # 本类公开方法操作（它们自带互斥），**不得**直接读写 handle/file/
        # _play_from/_scheduled_to 等字段（绕过锁 = 把竞态重新引回来）。
        self.on_file_completed = None
        # plan/09：可选注入的增量可用性视图（core.pieceview.PieceView）。
        # 有它时门控/tick 的判定成本与种子规模**无关**；没有它（测试替身、
        # 直接调 begin 的用例）退回 P2-1 的位图路径，行为与基线逐字一致。
        # 刻意**不自动创建**：替身句柄大多没有可用的 status()/have_piece，
        # 自动创建会把「不可判定」误当成「全部未落盘」而破坏门控语义。
        self._view = None

    def attach_view(self, view) -> None:
        """注入/解除增量可用性视图（``None`` = 解除，退回位图路径）。

        由 ``PreviewCore`` 在 ``start_preview`` 时注入；句柄被替换时必须重新
        注入（视图内部按句柄身份校验，串台时自动重建基线）。
        """
        with self._lock:
            self._view = view

    @property
    def piece_view(self):
        """当前注入的视图（无则 None）——UI 状态快照复用它以求门控三判据。"""
        return self._view

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

    def _have_snapshot(self, handle=None):
        """可用性判定用的 have 回调（P2-1：N 次绑定调用 → 1 次 status()）。

        plan/09 起优先返回**增量视图**的 ``have``：它按需单块查询、并把连续
        前缀摊销到 O(1)，因此不再物化位图。没有视图时（测试替身 / 未注入）
        退回一次性位图快照，行为与基线逐字一致。
        """
        if handle is None and self._view is not None:
            return self._view.have
        h = self.handle if handle is None else handle
        return have_from_bitmap(h.status().pieces)

    @staticmethod
    def _is_contiguous(pieces) -> bool:
        """块列表是否为连续闭区间（升序去重时 O(1) 判定）。

        ``_tail_pieces`` / ``_entry_pieces`` 都由 ``list(range(a, b+1))`` 构造，
        恒为 True；仍做一次校验，是为了让「用区间查询替代逐块 all()」这条
        优化**只在该前提成立时生效**（否则退回逐块，绝不改变语义）。
        """
        return bool(pieces) and pieces[-1] - pieces[0] + 1 == len(pieces)

    def _tail_ready(self, pieces, have, tag: str = "scheduler.tail_ready") -> bool:
        """给定 have 回调判定 pieces 是否全部落盘（无窗口恒 True）。"""
        if not pieces or self.file is None or self.handle is None:
            return True
        # 快路径：pieces 是连续区间且视图在场 → 用区间判定（摊销 O(1)），
        # 避免 128 块 × 5.5µs ≈ 0.7ms 的逐块绑定调用（每轮两处 = 1.4ms）。
        # 传入的 have 若正是本视图的 have，同样可以走这条路径。
        if self._view is not None and self._is_contiguous(pieces) \
                and (have is None or have == self._view.have):
            ok = self._view.range_ready_pieces(pieces[0], pieces[-1])
            if ok is not None:
                return bool(ok)
        try:
            if have is None:
                have = self._have_snapshot()
            return all(have(p) for p in pieces)
        except Exception as e:
            log_warning(tag, f"{e}")
            return False

    def tail_ready(self, have=None) -> bool:
        """尾部索引窗口是否已全部落盘（无窗口时恒为 True）。

        plan/07 阶段 2 起**不再**是开播门控判据（尾窗收敛后仍有 10MB ≈ 3 块，
        等整窗对 2.4MB/s 慢链路仍是秒级开销），但保留：契约与方法面冻结、
        ``tick()`` 靠它决定是否继续补拉尾窗。

        ``have``：可选的一次性位图快照回调——UI 轮询经 ``gate_snapshot()``
        传入共享快照，把本轮的门控判据折叠为一次 ``status()``（详见
        ``gate_snapshot`` 的论证）。None = 自行取快照（行为与基线一致）。
        """
        return self._tail_ready(self._tail_pieces, have)

    def tail_entry_ready(self, have=None) -> bool:
        """尾部**入口**是否就绪（开播门控判据，plan/07 阶段 2）。

        只判定文件最后 ``min(2MB, size)`` 字节覆盖的块（``_entry_pieces``，
        moov 所在处），而不是整个尾窗：4.1GB / 4MB 块真机上旧门控要等
        「整尾窗 44MB 齐」（2.4MB/s ≈18s，实测缓冲恒 0.0%），新门控只需
        最末 1 块 —— 头窗之后排到的第 1 个尾部块，秒级即可开播。
        整尾窗（≈10MB）仍由 ``tick()`` 在后台继续补拉，**不取消**；若播放器
        真因 mdat/moov 越界打开失败，走既有的退避重试（`_on_stream_failed`）。

        无尾部入口（非 moov 尾部家族 / 非法文件 / 未 begin）恒为 True，
        与 ``tail_ready`` 的空窗口语义一致（MKV 等不需要尾部即可开播）。
        ``have`` 语义同 ``tail_ready``。
        """
        return self._tail_ready(self._entry_pieces, have,
                                "scheduler.tail_entry_ready")

    def contiguous_progress(self, have=None) -> int:
        """从文件头开始的连续可读字节数（播放器真正能消费的量）。

        ``have``：可选的一次性位图快照回调（见 ``gate_snapshot``）。

        注入视图时（plan/09）走 ``contiguous_bytes_of`` 的增量路径，成本与
        种子规模无关；否则退回 ``contiguous_bytes`` 的位图逐块路径。
        """
        if not self.active or self.file is None or self.handle is None:
            return 0
        try:
            ti = self._torrent_file_or_none()
            if ti is None:
                return 0
            view = None
            if have is None:
                # P2-1：连续前缀扫描走一次性位图快照，不再逐块 have_piece
                have = self._have_snapshot()
                view = self._view
            elif self._view is not None and have == self._view.have:
                view = self._view      # 调用方传的就是本视图的 have（生产路径）
            pm = PieceMap(ti.piece_length(), self.file.offset,
                          self.file.start_piece, self.file.end_piece,
                          self.file.size, have, view=view)
            return contiguous_bytes_of(pm)
        except Exception as e:
            log_warning("scheduler.contiguous_progress", f"{e}")
            return 0

    def gate_snapshot(self, have=None) -> dict:
        """开播门控三判据的**单快照**求值（UI 轮询热路径，2026-09-17 优化）。

        问题：``contiguous_progress`` / ``tail_ready`` / ``tail_entry_ready``
        各自调一次 ``handle.status()``，而一次 UI 轮询（``_refresh_status``，
        700ms）在 mp4 预览路径上要取这三项 + ``PreviewCore.status`` 里的一次，
        **合计 4 次** ``torrent_status`` 拷贝与 4 次位图物化（本机探针 D1-a
        实测 4 次）。位图物化是 O(块数)：实测 ≈20ns/块，4 万块 0.2ms、
        10 万块 1.4ms、40 万块 7.9ms；按 4 次计，40 万块（≈6.4GB / 16KB 块）
        单轮就是 31.5ms，占 700ms 轮询的 4.5%，且**全部落在 GUI 线程**上
        （``_refresh_status`` 由 QTimer 在 GUI 线程驱动）。十万块以上的大种子
        即表现为缓冲百分比/门控文案的可见抖动。

        本方法把三处折叠为一次快照求值。未激活时返回与旧实现**逐值一致**的
        缺省（0 / True / True），空窗口语义不变。

        返回 ``{"contiguous": int, "tail_ready": bool, "tail_entry_ready": bool}``。
        快照取用失败（句柄失效等）时三项退化为基线兜底值（0 / False / False
        或空窗口 True），绝不因为求值失败而放行开播。
        """
        if not self.active or self.file is None or self.handle is None:
            return {"contiguous": 0, "tail_ready": True, "tail_entry_ready": True}
        if have is None:
            try:
                have = self._have_snapshot()
            except Exception as e:
                log_warning("scheduler.gate_snapshot", f"{e}")
        return {
            "contiguous": self.contiguous_progress(have),
            "tail_ready": self.tail_ready(have),
            "tail_entry_ready": self.tail_entry_ready(have),
        }

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
        # plan/11 R-3：**只把状态赋值区间纳入锁内**。libtorrent 调用已在上方完成；
        # 下方的 `_request_tail()` / `tick()` 必须在**放锁后**调用——tick 是非阻塞
        # 取锁，若在这里持锁调它会必然取锁失败并静默跳过，导致「首次窗口预约丢失、
        # 播放永远不开」。
        with self._lock:
            self.handle = handle
            self.file = file
            self._scheduled_to = file.start_piece - 1
            self._play_from = file.start_piece
            self._head_n = window_pieces(ti.piece_length())
            self._tail_pieces = tail_piece_window(file, ti.piece_length())
            # 尾部入口 = 开播门控判据（plan/07 阶段 2）；无尾窗（非 moov 家族）时为空
            self._entry_pieces = (tail_entry_pieces(file, ti.piece_length())
                                  if self._tail_pieces else [])
        # 视图在场：确保基线新鲜（非强制 → 已有有效基线时是幂等空操作，
        # 不会因为「换文件预览」白付一次 O(块数) 物化）
        if self._view is not None:
            try:
                self._view.rebuild_baseline()
            except Exception as e:
                log_warning("scheduler.begin.view", f"{e}")
        # 先预约尾部索引块，再进入顺序窗口
        self._request_tail()
        self.tick()

    @_exclusive(blocking=False)      # 热路径：拿不到锁就跳过本拍（700ms 后还会来）
    def request_range(self, start_byte: int, end_byte: int) -> None:
        """按字节区间即时点播（文件内相对偏移，end_byte 为开区间）。

        播放器（FFmpeg）要读哪段就立刻下载哪段——典型场景是 MP4 尾部 moov
        探测与任意位置拖动。重复调用是幂等的（deadline 会被覆盖）。

        单次点播的块数**必须有上限**：拖动进度条时 FFmpeg 发的是
        `bytes=X-`（X 到文件尾），不加限制会把剩余整个文件都置为 ASAP ——
        带宽被分散到几百 MB 上，反而拖慢 seek 目标本身、顺序性也随之丧失。

        **上限与顺序窗口同口径（字节预算）**：旧实现用 ``LOOKAHEAD_PIECES``
        (60 块) 封顶，在 16KB 块下是 960KB（合理），4MB 块下却是 **240MB
        全 ASAP** —— 这正是 :func:`window_pieces` 的 docstring 里论证过的
        「连续前缀凑不出、门控永不满足」的成因。两处口径必须同源，否则
        等于把已经修好的问题从窗口侧搬到了点播侧。
        注：``LOOKAHEAD_PIECES`` 的**常量值**被 contract_check 冻结（=60），
        这里只改用法，常量原样保留。

        点播是「临时插队」，不移动顺序窗口的锚点（否则尾部 moov 探测会把
        锚点推到文件尾，导致顺序窗口永久停摆）。

        deadline 改**递增**（第 i 块 = ``i * DEADLINE_STEP_MS``，plan/07
        阶段 1）：插队语义不变（首块仍立即），但把区间内顺序也表达出来，
        不再全 0 洪泛（与窗口策略同源）。
        """
        if not self.active or self.file is None or self.handle is None:
            return
        try:
            ti = self._torrent_file_or_none()
            if ti is None:
                return
            pl = ti.piece_length()
            if pl <= 0:
                return
            first = max(self.file.start_piece,
                        self.file.start_piece + max(0, start_byte) // pl)
            last = min(self.file.end_piece,
                       self.file.start_piece + max(0, end_byte - 1) // pl)
            # 与顺序窗口同源：16MB 字节预算换算的块数（夹 [4,60]）。
            # 4MB 块 → 4 块（16MB）；16KB 块 → 60 块（仍是旧行为的 960KB）。
            n = max(1, window_pieces(pl))
            last = min(last, first + n - 1)
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
        # plan/11 R-3：本方法取锁（用户拖动必须真正生效，不能静默跳过），但
        # **必须在调用 self.tick() 之前放锁**——tick 是非阻塞取锁，持锁调它
        # 只会被跳过（与 begin 同款结构：锁段覆盖「读状态 → 清预约 → 重建预约」，
        # 尾部 tick 留在锁外）。
        with self._lock:
            if not self.active or self.file is None:
                return
            ti = self._torrent_file_or_none()
            if ti is None:
                # 句柄被并发 stop()/remove_torrent 摘除：跳转无从预约，安静返回
                # （plan/11 R-5；此处是 replace_all 替换后**唯一**缺 None 守卫的
                # 调用点——`pl = ti.piece_length()` 会在 None 上抛 AttributeError）
                return
            pl = ti.piece_length()
            piece = self.file.start_piece + byte_offset // pl
            piece = max(self.file.start_piece,
                        min(piece, self.file.end_piece))
            # 跳转必须先清掉旧位置的 ASAP 预约：拖到未缓存区再拖回已缓存区时，
            # 残留的远端分块仍在以最高优先级并行下载，与新播放位置争带宽 →
            # 明明已缓存的区域反而卡顿。清完再重建「尾部窗口 + 新位置窗口」。
            self._clear_deadlines()
            self._play_from = piece
            self._scheduled_to = piece - 1
            self._head_n = window_pieces(pl)
            target = min(self.file.end_piece, piece + self._head_n - 1)
            self._request_tail()  # 尾部 moov 窗口同样被清掉，必须重建
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

    def _first_missing_from(self, from_piece: int, have=None) -> int:
        """从 from_piece 起向后第一个未落盘的块（全部就绪时返回 end_piece）。

        P2-1 的**对称修复**（2026-09-17）：旧实现逐块 ``have_piece(p)``——
        每块一次 libtorrent 绑定调用（实测 ≈3.76 µs，见 models.have_from_bitmap
        的论证）。文件已下到接近末尾时该循环要走完剩余全部块，几十万块量级
        即数十毫秒；而 ``tick()`` 由 GUI 线程每 700ms 驱动一次，等于把这段
        开销挂进 UI 轮询。改走一次性位图快照（与 ``contiguous_progress`` /
        流服务同源）；``have`` 由 ``tick()`` 传入复用，None 表示「快照不可用」。

        快照不可用时退回逐块绑定，行为与基线逐值一致——
        ``smoke_test._FakeHandle`` 没有 ``status()``，走的正是这条回退路。
        """
        p = max(from_piece, self.file.start_piece)
        end = self.file.end_piece
        # 视图在场：直接问「从 p 起第一个缺失块」（内部摊销 O(1)），
        # 既不做逐块绑定调用，也不物化位图。返回 None = 不可判定 → 走回退。
        if self._view is not None:
            got = self._view.contiguous_from(p, end + 1)
            if got is not None:
                return min(got, end)
        if have is not None:
            while p <= end and have(p):
                p += 1
            return min(p, end)
        try:
            while p <= end and self.handle.have_piece(p):
                p += 1
        except Exception as e:
            log_warning("scheduler.first_missing", f"{e}")
        return min(p, end)

    def _torrent_file_or_none(self):
        """取 ``self.handle`` 的 torrent_info；句柄缺失/已失效时返回 None。

        plan/11 R-5（句柄生命周期）：``self.handle`` 可能被另一线程的
        ``stop()`` 置 None，或句柄已被 ``ses.remove_torrent`` 摘除——两者都会
        让裸调 ``torrent_file()`` 抛 ``AttributeError`` / ``RuntimeError``。
        此前 ``seek_to_byte`` / ``tick`` / ``rearm`` 三处是裸调，仅靠调用方
        外层 try 兜住（靠运气，不是设计）。

        本方法把「局部取引用 → 判空 → try」收敛到一处：**先取局部变量**是关键，
        它同时消除了「检查 ``self.handle`` 非空」与「使用 ``self.handle``」之间
        的窗口（即 plan/11 R-3 方案 C 的防御式局部引用）。

        ``handle is None`` 时**静默**返回 None 而不告警：停止预览后句柄归 None
        是合法状态，报错只会污染日志（真正的异常仍由 log_warning 记录）。

        注（2026-09-17 复审修正，原注释与此处实际改动矛盾）：本方法落地时用
        ``replace_all`` 替换 ``self.handle.torrent_file()``，实际命中 **6 处**
        而非计划里的 3 处裸调——8 空格缩进的搜索串是 12/20 空格缩进的**子串**，
        故 ``contiguous_progress`` / ``request_range`` / ``stop`` 三处 try 段内的
        调用也一并被替换。逐处复核结论：**语义无变化**
        （那三处失败路径原本就是 ``return 0`` / ``return`` / 跳过后继续），
        仅日志 tag 统一为 ``scheduler.torrent_file``；``stop`` 处由
        「``torrent_file()`` 抛异常 ⇒ 外层 ``except`` 连带跳过 ``pause()``」变为
        「返回 None ⇒ 跳过 ``prioritize_files`` 但仍 ``pause()``」，更贴合意图。
        另：``seek_to_byte`` 是唯一**缺** None 守卫的替换点，已补
        ``if ti is None: return``（否则 ``ti.piece_length()`` 必抛 AttributeError）。
        """
        h = self.handle
        if h is None:
            return None
        try:
            return h.torrent_file()
        except Exception as e:
            log_warning("scheduler.torrent_file", f"{e}")
            return None

    @_exclusive(blocking=False)      # 热路径：HTTP 点播是 hint，`_wait_ready` 会重发
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
        ti = self._torrent_file_or_none()
        if ti is None:
            return
        # 一次位图快照供本轮复用（尾窗判定 + 首个缺失块定位）：tick() 由 GUI
        # 线程每 700ms 驱动，两个判据各自 status() 会把位图物化（O(块数)，
        # 实测 ≈20ns/块）付两遍——论证见 gate_snapshot。
        have = None
        try:
            have = self._have_snapshot()
        except Exception as e:
            log_warning("scheduler.tick.snapshot", f"{e}")
        # 尾部索引窗口尚未就绪则持续补拉（探测 moov 是开播前提）
        if self._tail_pieces and not self.tail_ready(have):
            self._request_tail()
        n = self._head_n or window_pieces(ti.piece_length())
        first_missing = self._first_missing_from(self._play_from, have)
        # 防抖裕量必须与**窗口规模**成比例（C2，2026-09-18）。
        # 旧实现写死 16 块：窗口按字节预算换算后 4MB 块下只有 4 块，
        # `first_missing <= _scheduled_to - 16` 于是**恒为假** —— 每拍（700ms）
        # 都重写一次 deadline，窗口锚点被反复推前、近端优先级被稀释。
        # 改为 min(16, n//2)：小块（16KB 块 n=60）仍得 16，**行为与旧实现逐字一致**；
        # 大块（4MB 块 n=4）得 2，防抖真正生效。
        slack = max(1, min(16, n // 2))
        if first_missing <= self._scheduled_to - slack:
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

    @_exclusive()                    # 失败重试的催数据路径：必须真正生效
    def rearm(self) -> None:
        """**无条件**重刷「尾部索引窗 + 播放位置窗口」的预约（失败重试催数据用）。

        与 ``tick()`` 的区别（2026-09-17 等待超时专项）：``tick()`` 有
        「``first_missing <= _scheduled_to - 16`` 即返回」的防抖，正常情况下
        这是对的（避免重复预约）；但**播放器打开失败**这一路径要的恰恰是
        「不管窗口看起来多充裕，现在就把尾部入口与播放位置窗口重刷一遍」。

        为什么有用：门控放行（尾部入口就绪 + 连续前缀 ≥1MB）与播放器真正能解出
        画面之间还差 mdat/moov 的更多数据；失败时若只等 ``tick()`` 按 700ms
        一拍滚动，慢链路上要白等若干拍。这里把两块立刻置回最高优先级
        （deadline 覆盖式写入，幂等），把「等数据」变成「催数据」。

        有界性：只覆盖 ``[播放位置, 播放位置 + 窗口块数)`` 与尾部窗口，
        不刷全文件——避免把 `bytes=X-` 那种「整段到文件尾」变成全量 ASAP
        （论证见 ``request_range``）。
        """
        if not self.active or self.file is None or self.handle is None:
            return
        self._request_tail()
        ti = self._torrent_file_or_none()
        if ti is None:
            return
        n = self._head_n or window_pieces(ti.piece_length())
        first = max(self.file.start_piece, self._play_from)
        target = min(self.file.end_piece, first + n - 1)
        for p in range(first, target + 1):
            try:
                self.handle.set_piece_deadline(
                    p, (p - first) * DEADLINE_STEP_MS)
            except Exception as e:
                log_warning("scheduler.rearm.deadline", f"piece={p}: {e}")

    def buffer_progress(self) -> float:
        """当前预览文件的缓冲比例 0~1（按连续可读前缀计）。

        plan/11 第 2 批复审：``self.file`` 被二次解引用（判 None 一次、取
        ``.size`` 一次），并发 ``stop()`` 在两次之间置 None 会抛
        ``AttributeError``。当前**无生产调用点**（仅探针/测试用），未构成
        实际缺陷，但按 R-5 的纪律改为局部快照（与 ``seek_to_byte`` 同款）。
        """
        f = self.file
        if not self.active or f is None or f.size == 0:
            return 0.0
        return min(1.0, self.contiguous_progress() / f.size)

    @_exclusive()                    # 生命周期：停止必须真正生效，不得跳过
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
                    ti = self._torrent_file_or_none()
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
        # 解除视图（视图实例仍由 PreviewCore 缓存，下次开始预览时重新注入；
        # 视图内的锚点单调有效，不需要因为停止预览而作废）
        self._view = None
