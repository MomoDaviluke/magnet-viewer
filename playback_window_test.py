"""plan/07 阶段 1：播放窗口按字节换算 + deadline 递增保序专项（假句柄，秒级）。

背景（真机实测 + 本机 A/B 实验，见 plan/07_large_piece_playback_plan.md）
--------------------------------------------------------------------
4.1GB / 4MB 块的 mp4：缓冲恒 0.0%、30s 不出画面。根因是**窗口按块数定、
与块尺寸脱钩**：`LOOKAHEAD_PIECES=60` 在 16KB 块下 = 960KB（合理），
在 4MB 块下 = **240MB 全 ASAP**（300MB 实验文件的 80%）。libtorrent 在
几十个同级「紧急」块里按可用性散抓（实测已下块 3/10/22/31/32/34/66 乱序、
头 3 块一个不下）→ 连续前缀凑不出 → 播放门控永不满足。

修复（阶段 1）：
1. 窗口块数 = ``window_pieces(piece_length)`` = 按 16MB 字节预算换算，
   再夹到 ``[4, LOOKAHEAD_PIECES]``；
2. 窗口内 deadline **递增**（第 i 块 = i * DEADLINE_STEP_MS），从播放位置
   起顺序取块——保证连续前缀先到齐，不再全 0 洪泛；
3. 尾部 moov 窗口的 deadline 排在头窗之后（``n_head*STEP + 1000 + j*STEP``）。

A/B 实验（300MB / 4MB 块 / 做种端限 1MB/s）：现状头 1 块就绪 14.0s，
本策略 9.0s（-36%），t=13s 已连续 8MB。

段：
- §A window_pieces：按字节换算 + 上下限夹取 + 非法块长兜底
- §B begin：4MB 块头窗恰 4 块、deadline 严格递增（不再全 0）
- §C 尾窗 deadline 严格大于头窗最大 deadline
- §D request_range：仍 ≤ LOOKAHEAD_PIECES 上限，但 deadline 递增
- §E seek_to_byte：从新位置重建窗口且 deadline 递增

退出码：0=通过，1=失败，2=SKIP（依赖缺失，绝不假装通过）。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import libtorrent as lt

    import test_support as ts
    from core import scheduler as sch
    from core.models import TorrentFile
except ImportError as e:    # 依赖缺失：显式 SKIP，绝不假装通过
    print(f"依赖缺失，无法执行播放窗口专项验收：{e}")
    sys.exit(2)

MB = 1024 * 1024


# --------------------------------------------------------------------------
# 假句柄：记录 deadline 序列（p, ms）
# --------------------------------------------------------------------------

class FakeTI:
    def __init__(self, pl: int, num_files: int = 1):
        self._pl = pl
        self._nf = num_files

    def piece_length(self) -> int:
        return self._pl

    def num_files(self) -> int:
        return self._nf


class FakeHandle:
    """PreviewScheduler 的最小句柄替身。

    - ``set_piece_deadline`` 追加到 ``deadlines``（可直接断言顺序）；
    - ``clear_piece_deadlines`` 清空记录（seek 前调用，只看重建后的序列）；
    - ``status().pieces`` 与 ``have_piece`` 全一致（P2-1 位图快照约定）。
    """

    def __init__(self, pl: int, num_pieces: int, have=()):
        self._pl = pl
        self._n = num_pieces
        self._have = set(have)
        self.deadlines: list[tuple[int, int]] = []
        self.cleared = 0
        self.resumed = 0
        self.paused = 0
        self.flags: list[tuple[str, object]] = []
        self.prio: list[int] | None = None

    # 生命周期
    def unset_flags(self, f):
        self.flags.append(("unset", f))

    def set_flags(self, f):
        self.flags.append(("set", f))

    def resume(self):
        self.resumed += 1

    def pause(self):
        self.paused += 1

    def prioritize_files(self, prio):
        self.prio = list(prio)

    # 元数据
    def torrent_file(self):
        return FakeTI(self._pl, 1)

    # 分块
    def set_piece_deadline(self, p, ms):
        self.deadlines.append((int(p), int(ms)))

    def clear_piece_deadlines(self):
        self.cleared += 1
        self.deadlines.clear()

    def have_piece(self, p):
        return p in self._have

    def status(self):
        return SimpleNamespace(pieces=[i in self._have for i in range(self._n)])


def mk(pl: int, size_mb: int, have=()) -> tuple:
    """构造 (scheduler, handle, file)：单文件 mp4，从头播放，块长 pl。"""
    n = size_mb * MB // pl
    f = TorrentFile(index=0, path="movie/big.mp4", size=size_mb * MB,
                    offset=0, start_piece=0, end_piece=n - 1)
    h = FakeHandle(pl, n, have=have)
    s = sch.PreviewScheduler()
    return s, h, f


def _head(dl: list[tuple[int, int]], n: int) -> list[tuple[int, int]]:
    """取 deadline 序列里最小的 n 个块号（= 头窗），保持原顺序。"""
    order = sorted({p for p, _ in dl})[:n]
    seen = set()
    out = []
    for p, ms in dl:
        if p in order and p not in seen:
            seen.add(p)
            out.append((p, ms))
    return out


# --------------------------------------------------------------------------

def section_window_pieces(ck):
    ck.section("§A window_pieces：按字节换算 + 上下限夹取")
    wp = sch.window_pieces
    ck.check(sch.LOOKAHEAD_BYTES == 16 * MB,
             f"LOOKAHEAD_BYTES == 16MB（实际 {getattr(sch, 'LOOKAHEAD_BYTES', None)}）")
    ck.check(sch.DEADLINE_STEP_MS == 400,
             f"DEADLINE_STEP_MS == 400（实际 {getattr(sch, 'DEADLINE_STEP_MS', None)}）")
    ck.check(sch.LOOKAHEAD_PIECES == 60,
             "LOOKAHEAD_PIECES 仍为 60（契约冻结项，仅降级为块数上限）")
    # 16KB 块：16MB 预算 = 1024 块 → 被 60 上限截断（等价旧行为 960KB）
    ck.check(wp(16 * 1024) == 60, f"16KB 块 → 60（上限绑定，实际 {wp(16 * 1024)}）")
    # 256KB 块：ceil(16MB/256KB) = 64，仍被 60 上限截断
    ck.check(wp(256 * 1024) == 60,
             f"256KB 块 → ceil=64 被 60 上限截断（实际 {wp(256 * 1024)}）")
    # 1MB 块：16MB 预算 = 恰 16 块
    ck.check(wp(MB) == 16, f"1MB 块 → 16（实际 {wp(MB)}）")
    # 4MB 块：16MB 预算 = 4 块（真机故障场景，240MB → 16MB）
    ck.check(wp(4 * MB) == 4, f"4MB 块 → 4（实际 {wp(4 * MB)}）")
    # 8MB 块：ceil(16MB/8MB) = 2 → 被 4 下限抬到 4
    ck.check(wp(8 * MB) == 4, f"8MB 块 → 4 下限（实际 {wp(8 * MB)}）")
    # 非法块长兜底：不得除零，回落上限常量（等价旧行为）
    ck.check(wp(0) == sch.LOOKAHEAD_PIECES and wp(-1) == sch.LOOKAHEAD_PIECES,
             "piece_length<=0 → 兜底 LOOKAHEAD_PIECES（不除零）")
    ck.check(wp(4 * MB) * 4 * MB == sch.LOOKAHEAD_BYTES,
             "4MB 块窗口字节量恰等于 LOOKAHEAD_BYTES（240MB → 16MB 的直接证据）")


def section_begin(ck):
    ck.section("§B begin：4MB 块头窗恰 4 块、deadline 严格递增")
    # 64MB / 4MB 块 = 16 块；尾窗仅 1 块（piece15），标记已就绪避免重复预约
    s, h, f = mk(4 * MB, 64, have={15})
    s.begin(h, f)
    head = _head(h.deadlines, 4)
    ck.check(len(head) == 4,
             f"头窗恰 4 块（实际 {len(head)}：{[p for p, _ in head]}）")
    ck.check([p for p, _ in head] == [0, 1, 2, 3],
             f"头窗为播放位置起连续 4 块（实际 {[p for p, _ in head]}）")
    ms = [m for _, m in head]
    ck.check(ms == sorted(ms) and len(set(ms)) == len(ms),
             f"头窗 deadline 严格递增（实际 {ms}）——旧实现全 0 洪泛")
    ck.check(ms[0] == 0 and ms == [0, 400, 800, 1200],
             f"第 i 块 deadline = i * DEADLINE_STEP_MS（实际 {ms}）")
    # 60 块洪泛的回归：任何时候都不得出现 >LOOKAHEAD_PIECES 的预约批次
    ck.check(len({p for p, _ in h.deadlines}) <= sch.LOOKAHEAD_PIECES,
             f"单批预约块数 ≤ 上限（实际 {len({p for p, _ in h.deadlines})}）")


def section_tail(ck):
    ck.section("§C 尾窗 deadline 严格大于头窗最大 deadline")
    s, h, f = mk(4 * MB, 64)          # 尾窗 [15] 未就绪 → 持续补拉
    s.begin(h, f)
    tail = [(p, m) for p, m in h.deadlines if p == f.end_piece]
    head = _head([(p, m) for p, m in h.deadlines if p != f.end_piece], 4)
    ck.check(bool(tail), "尾窗已预约（.mp4 家族先拉 moov）")
    ck.check(bool(head), "头窗已预约")
    ck.check(min(m for _, m in tail) > max(m for _, m in head),
             f"尾窗 deadline {[m for _, m in tail]} > 头窗最大 "
             f"{max(m for _, m in head)}（探测 moov 让位于顺序前缀）")
    ck.check(min(m for _, m in tail) >= 4 * sch.DEADLINE_STEP_MS + 1000,
             f"尾窗基值 ≥ n_head*STEP + 1000（实际 {min(m for _, m in tail)}）")


def section_range(ck):
    ck.section("§D request_range：≤ 上限且 deadline 递增（插队不再全 0）")
    # 400MB / 4MB 块 = 100 块 → 点播整文件必被 60 上限截断
    s, h, f = mk(4 * MB, 400, have={99})
    s.begin(h, f)
    h.deadlines.clear()
    s.request_range(0, f.size)
    n = len(h.deadlines)
    ck.check(n == sch.LOOKAHEAD_PIECES,
             f"单次点播恰 {sch.LOOKAHEAD_PIECES} 块（实际 {n}）")
    ck.check([p for p, _ in h.deadlines] == list(range(n)),
             "从区间头部连续预约（尾部不刷 ASAP）")
    ms = [m for _, m in h.deadlines]
    ck.check(ms == [i * sch.DEADLINE_STEP_MS for i in range(n)],
             f"deadline 递增 0/400/…/{(n - 1) * 400}（实际首尾 {ms[0]}/{ms[-1]}）")
    # 尾部小区间不受截断影响：390MB..尾 → piece 97/98/99
    h.deadlines.clear()
    s.request_range(390 * MB, f.size)
    ck.check([p for p, _ in h.deadlines] == [97, 98, 99],
             f"小区间（3 块）不被截断（实际 {[p for p, _ in h.deadlines]}）")


def section_seek(ck):
    ck.section("§E seek_to_byte：从新位置重建窗口且 deadline 递增")
    s, h, f = mk(4 * MB, 400, have={99})
    s.begin(h, f)
    h.deadlines.clear()
    s.seek_to_byte(200 * MB)          # 200MB / 4MB = piece 50
    got = h.deadlines
    ck.check(h.cleared >= 1, "seek 先清旧位置 deadline（防远端残留争带宽）")
    ck.check(bool(got) and min(p for p, _ in got) == 50,
             f"窗口从新位置 piece50 重建（实际最小块号 "
             f"{min((p for p, _ in got), default=None)}）")
    head = _head(got, 4)
    ck.check([p for p, _ in head] == [50, 51, 52, 53],
             f"新位置起连续 4 块（实际 {[p for p, _ in head]}）")
    ms = [m for _, m in head]
    ck.check(ms == [0, 400, 800, 1200],
             f"新窗口 deadline 从 0 递增（实际 {ms}）")
    ck.check(all(p >= 50 for p, _ in got),
             "旧位置（<50）分块不再被预约")


def main() -> int:
    ck = ts.Checker("playback_window_test（plan/07 阶段 1 播放窗口专项）")
    ck.section("按字节窗口 + deadline 递增保序（假句柄，不启会话）")
    section_window_pieces(ck)
    section_begin(ck)
    section_tail(ck)
    section_range(ck)
    section_seek(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
