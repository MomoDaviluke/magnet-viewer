"""块可用性视图（plan/09 方案 D）：按需 have + 增量连续前缀＋一次性基线。

解决的根因
----------
旧实现每次判定「哪一段可读」都要 ``list(handle.status().pieces)`` 物化**全量**
位图。实测（真句柄，本机）：位图物化 **18.75 ns/块**、bitfield 逐项索引
**21 ns/块**，而 ``have_piece()`` 单次 **5.5 µs 但**与块数无关**。
于是「判定成本随种子规模线性增长」，且这类判定在**一轮** UI 轮询里要发生 3 次
（状态快照 / tick / 进度条分段）+ 流服务每 50ms 一次 —— 40 万块时单轮
27ms、650 万块（100GB @16KB）外推 447ms，全部落在 GUI 线程。

本模块把判定成本改成**规模无关**：

    一次性基线（物化一次位图，只在 begin / seek / 失效后）
      + 之后只查「本轮新增的连续块」→ 稳态摊销 O(1)，零位图物化

依赖的唯一不变量
----------------
**块一旦落盘就恒为 have**（块不会「变回未下载」），因此「从 start 起的连续
前缀」单调不减 —— 这是增量成立的全部前提。破坏它的场景只有三种：删除文件、
重新校验、句柄被移除重建。三者都有便宜的 O(1) 观测信号（见 :meth:`_check_valid`）。

零新失效面
----------
刻意**不做** TTL 缓存、不订阅告警、不引入事件驱动状态机：
- TTL 缓存仍是 O(N) 只是降频，且与「同路径连续查询必须反映实时状态」冲突；
- ``piece_finished_alert`` 的告警量 = 块数（650 万块 = 650 万条），
  ``alert_queue_size=5000`` 一旦溢出即状态漂移，反而要周期性全量对账。

与纯函数层的关系
----------------
``models.PieceMap`` / ``contiguous_bytes`` / ``ready_until`` / ``range_available``
的语义被**桌面↔安卓对账夹具**冻结（``android/tools/gen_parity_fixtures.py``），
本模块一行都不改它们：只是换掉 ``PieceMap.have`` 的**提供者**，并复用
``models.bytes_up_to_piece`` 这一共用换算公式，使「逐值一致」由构造保证。
"""
from __future__ import annotations

import threading
from typing import Any, Callable

from .logutil import log_warning

# 单次「用 have_piece 推进连续前缀」的块数上限。
# 稳态下每轮只推进 0~3 块；触顶说明基线异常（例如句柄被换过），
# 此时**作废锚点并要求调用方重建基线**——绝不让一次上限截断被当成
# 「连续前缀到此为止」（那会误判「不可读」，比多花一次基线更糟）。
MAX_ADVANCE = 4096

# 进度条分段的探测预算（次绑定调用）。超过此预算的块范围走降采样。
# 取 256：进度条约千级像素，256 个探测点足以把每个连续段定位到 ≤4px；
# 而 256 × 5.6µs ≈ 1.4ms，是「限频显示用途」可以接受的上界。
# 区间块数 ≤ 该预算时分段**逐块精确**（k=1，与原实现同结果）。
SEG_PROBE_BUDGET = 256


def _checking_states() -> frozenset:
    """需要作废基线的 torrent 状态集合（导入失败时返回空集，不阻断）。"""
    try:
        import libtorrent as lt
    except Exception:
        return frozenset()
    ts = lt.torrent_status
    out = set()
    for name in ("checking_files", "checking_resume_data", "allocating"):
        v = getattr(ts, name, None)
        if v is not None:
            out.add(v)
    return frozenset(out)


class PieceView:
    """单个种子句柄的块可用性视图（线程安全）。

    Args:
        handle: libtorrent ``torrent_handle``（或其测试替身——替身只需
            实现 ``have_piece`` / ``status`` 即可，``status`` 不支持 flags
            时自动退回无参调用）。
        piece_length: 分块字节长度（<=0 时视为不可判定，所有查询保守返回）。
        npieces: 总块数（用于越界判定与分段预算）。

    失败策略：任何异常都**不向上抛**，就地 ``log_warning`` 并返回
    「不可判定」结果（``have`` → False、``contiguous_from`` → None）。
    调用方据此保守处理 —— 绝不允许因为一次查询失败就放行开播或喂零数据。
    """

    def __init__(self, handle, piece_length: int, npieces: int):
        self._handle = handle
        self._pl = int(piece_length)
        self._n = int(npieces)
        self._lock = threading.Lock()
        # 一次性基线位图（list[bool]）；None = 尚未建立
        self._baseline: list | None = None
        self._baseline_count = 0          # 诊断用：基线重建次数
        # 各锚点的连续前缀末端（块号，开区间）；键 = 起点块号
        self._anchors: dict[int, int] = {}
        self._total_done = -1
        self._state = None                # 上次观测到的 status.state（边沿判据用）
        self._checking = _checking_states()

    # ---------- 身份与诊断 ----------

    def matches(self, handle) -> bool:
        """是否就是该句柄的视图（句柄被替换后必须重建，防串台）。"""
        return self._handle is handle

    @property
    def npieces(self) -> int:
        return self._n

    @property
    def piece_length(self) -> int:
        return self._pl

    def stats(self) -> dict:
        """诊断计数（基线重建次数 / 锚点数）——供探针与测试断言。"""
        with self._lock:
            return {"baseline_count": self._baseline_count,
                    "anchors": len(self._anchors)}

    # ---------- 基线与失效 ----------

    def invalidate(self) -> None:
        """作废基线与本轮所有锚点（begin / seek / 删除 / 重新校验后调用）。"""
        with self._lock:
            self._baseline = None
            self._anchors.clear()
            self._total_done = -1

    def _check_valid(self) -> None:
        """O(1) 失效自检（须持锁）。

        两个信号，都来自 ``status(flags)``（已关位图，恒定 ~6µs）：

        - ``total_done`` 比上次记录值**下降** → 有块从 have 退回 not-have
          （删除文件 / 校验发现坏块），连续前缀会回退 → 必须作废基线。
          这是**电平**判据（每次都查）。
        - ``state`` **进入** ``checking_*`` / ``allocating`` → 即将发生大规模
          have 变动 → 作废基线重算。这是**边沿**判据（只在状态发生变化且新
          状态是校验态时触发）。

        为什么状态必须是边沿触发（实测踩到）：电平触发下，「长期处于
        ``checking_resume_data``」的种子会在**每次查询**都作废基线，于是每次
        都重新物化一次位图 —— 比优化前更糟（探针 D5 首次跑生产接线即暴露：
        单轮 7 次物化、8197 次绑定调用）。边沿触发只在「刚进入校验」那一次
        重建，之后在校验态内沿用基线；而校验期间真正把块丢掉的情况由上面的
        ``total_done`` 电平判据兜住。

        ``status`` 不接受 flags（测试替身）时退回无参调用；再失败则跳过自检
        （不做判定，宁可依赖显式 ``invalidate()``，也不误判为失效）。
        """
        try:
            st = self._handle.status(0)
        except TypeError:
            try:
                st = self._handle.status()
            except Exception:
                return
        except Exception:
            return
        try:
            td = int(getattr(st, "total_done", 0) or 0)
        except (TypeError, ValueError):
            td = self._total_done
        bad = False
        if self._total_done >= 0 and td < self._total_done:
            bad = True
            log_warning("pieceview.total_done_shrank",
                        f"total_done {self._total_done} → {td}，作废基线")
        self._total_done = td
        cur = getattr(st, "state", None)
        prev, self._state = self._state, cur
        if self._checking and prev is not None and cur != prev \
                and cur in self._checking:
            bad = True
        if bad:
            self._baseline = None
            self._anchors.clear()

    def rebuild_baseline(self, force: bool = False) -> bool:
        """建立一次性基线（物化一次位图）。返回是否真的重建了。

        只有 begin / seek / 失效后 / 首次查询需要走这里；之后**再也不物化**。
        已有有效基线且 ``force=False`` 时为幂等空操作。
        """
        with self._lock:
            return self._rebuild_baseline_locked(force)

    def _rebuild_baseline_locked(self, force: bool = False) -> bool:
        """基线的锁内实现（**须持 ``self._lock``**）。

        锁纪律（与 `core/registry.py` 同款，因为 `threading.Lock` **不可重入**）：
        ``*_locked`` 方法绝不调用自持锁的公开方法，公开方法也绝不调用另一个
        公开方法。历史上这里踩过：``contiguous_from`` 持锁后调
        ``rebuild_baseline()`` → 自锁死锁（本机制首次跑对拍即暴露）。
        """
        if self._baseline is not None and not force:
            return False
        try:
            bits = list(getattr(self._handle.status(), "pieces", []) or [])
        except Exception as e:
            log_warning("pieceview.baseline", f"{e}")
            bits = []
        # 与 self._n 对齐（句柄未就绪时 pieces 可能为空 → 全 False 保守）
        if len(bits) < self._n:
            bits = list(bits) + [False] * (self._n - len(bits))
        self._baseline = bits
        self._baseline_count += 1
        self._anchors.clear()
        self._check_valid()
        return True

    # ---------- 查询 ----------

    def have(self, p) -> bool:
        """单块是否已完整落盘。

        不可判定一律 False —— 与 ``models.have_from_bitmap`` 的语义完全一致
        （越界 / 非整数 / 绑定异常都是 False，绝不误判可读）。
        """
        try:
            p = int(p)
        except (TypeError, ValueError):
            return False
        if p < 0 or (self._n > 0 and p >= self._n):
            return False
        try:
            return bool(self._handle.have_piece(p))
        except Exception as e:
            log_warning("pieceview.have", f"piece={p}: {e}")
            return False

    def _baseline_run(self, start: int, limit: int) -> int:
        """用基线位图求 [start, limit) 内的连续前缀末端（O(连续长度)，查表 20ns/次）。

        为什么要基线位图参与：**起点变化时不能靠 have_piece 重扫**——
        seek 到「已下 90% 的文件」中部时，从该点起的连续前缀可能长达数十万块，
        逐块绑定调用要秒级。基线位图是**一次物化、长期复用**的免费下限，
        扫它是 20ns/块（比绑定调用便宜 275 倍）。
        """
        bits = self._baseline
        p = start
        if bits is None:
            return start
        while p < limit and p < len(bits) and bits[p]:
            p += 1
        return p

    def contiguous_from(self, start: int, limit: int | None = None):
        """从 ``start`` 起**连续**已落盘的块末端（第一个未 have 的块号，开区间）。

        与 ``models.contiguous_bytes`` 内部的块扫描口径一致
        （``p = start; while have(p): p += 1``），但成本是**摊销 O(1)**：
        缓存每个起点的已知末端，下一个调用只需从那里继续（正常播放每轮新增
        0~3 块 → 每轮 0~3 次绑定调用，与种子规模无关）。

        返回 ``None`` = **不可判定**（基线失效 / 推进触顶 / 分块长度非法）。
        调用方必须按不可判定保守处理，不得当成「已就绪」。
        """
        if self._pl <= 0 or self._n <= 0:
            return None
        start = max(0, int(start))
        end = self._n if limit is None else min(self._n, int(limit))
        with self._lock:
            self._check_valid()
            if self._baseline is None:
                self._rebuild_baseline_locked()
            anchor = self._anchors.get(start)
            if anchor is None:
                # 新起点：先用基线位图拿到「已知下限」，再增量推进
                anchor = self._baseline_run(start, end)
                self._anchors[start] = anchor
            p = max(start, min(anchor, end))
            steps = 0
            while p < end and self.have(p):
                p += 1
                steps += 1
                if steps > MAX_ADVANCE:
                    # 触顶 = 基线已陈旧（句柄被换过 / 未显式失效）：重建基线
                    # 并用位图直接给出结论，绝不把截断当成「就绪到此为止」
                    log_warning("pieceview.advance_limited",
                                f"连续前缀推进超过 {MAX_ADVANCE} 块，重建基线")
                    self._rebuild_baseline_locked(force=True)
                    anchor = self._baseline_run(start, end)
                    self._anchors[start] = anchor
                    return anchor
            self._anchors[start] = p
            return p

    def range_ready_pieces(self, p0: int, p1: int, _lock: bool = True):
        """块号闭区间 ``[p0, p1]`` 是否全部 have。

        返回 ``True`` / ``False`` / ``None``（``None`` = 不可判定，调用方必须
        保守处理，不得当成就绪）。

        成本：``contiguous_from(p0, p1+1)`` 只在区间内推进且**首缺即停**，
        并把结果存入锚点缓存 → 跨调用摊销 O(1)。区间形状在本项目的两种实际
        用法（FFmpeg 的小段请求、`bytes=X-` 到文件尾）下都只需很少次绑定调用。
        """
        if p1 < p0:
            return True
        contig = self.contiguous_from(p0, p1 + 1)
        if contig is None:
            return None
        return contig > p1

    # ---------- 显示用途：进度条分段 ----------

    def buffered_runs(self, sa: int, sb: int) -> list[tuple[int, int]] | None:
        """``[sa, sb]``（块号闭区间）内已落盘的**连续段**列表（块号闭区间）。

        **唯一**仍需要「整片块状态」的用途（进度条着色），因此单独设计：
        全部走**实时** ``have_piece`` 探测，探测点数封顶在 ``SEG_PROBE_BUDGET``：

        - 区间块数 ≤ 预算（默认 256）→ ``k = 1``，**逐块精确**，与原实现同结果；
        - 超过 → 每 ``k = ceil(n / 预算)`` 块探一次，分段边界精确到 ±k 块
          （每个探测点都是实时值，只牺牲边界精度）。

        为什么**不用**缓存的基线位图（实测踩到）：基线只在 begin/seek/失效时
        重建，下载过程中它必然陈旧 —— 用它算分段会让进度条**永远停在开播那一刻**
        （`local_torrent_test` 实测「分段覆盖 0%/0 段」）。而基线在本模块里只作
        ``contiguous_from`` 的**下限**使用，陈旧只会多走几次实时探测、不影响正确性；
        显示用途没有这个性质，必须实时。

        代价：≤ ``SEG_PROBE_BUDGET`` 次绑定调用（256 × 5.6 µs ≈ 1.4 ms），
        与种子规模**无关**，且零位图物化。调用方（进度条）应限频调用。

        返回 None = 不可用（分块长度非法）——调用方退化为不着色。
        """
        if self._pl <= 0 or sa > sb:
            return None
        n = sb - sa + 1
        k = max(1, -(-n // SEG_PROBE_BUDGET))
        runs: list[tuple[int, int]] = []
        run = None
        for p in range(sa, sb + 1, k):
            if self.have(p):
                if run is None:
                    run = p
            elif run is not None:
                # 保守：段尾取「最后一个已确认落盘的探测点」，不把未确认的
                # 中间块算进已缓存（宁可少报，不可多报）
                runs.append((run, max(run, p - k)))
                run = None
        if run is not None:
            runs.append((run, sb))
        return runs
