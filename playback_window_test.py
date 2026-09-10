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

修复（阶段 2）：
4. 尾窗按**字节**收敛：``tail_window_bytes(size)`` =
   ``min(size, max(2MB, 0.25%·size), 16MB)``——4.1GB 从 44MB（11 块）→
   ≈10MB（3 块）、1GB → ≈2.5MB、500MB 及以下 → 2MB 下限、小文件不超自身；
5. 开播门控改判「尾部**入口**就绪」= ``tail_entry_ready()``（文件最后
   ``min(2MB, size)`` 覆盖块，4MB 块下仅最末 1 块），不再等整尾窗；
   ``tail_ready()``（整尾窗）保留但只用于 tick() 继续补拉的判据。

修复（阶段 2.5，本次）：**入口块的 deadline 提前**到头窗头几块并行的位置。
阶段 2 实测 4.1GB / 4MB 块 / 2MB/s 下门控 36.0s→19.0s，但 19.0s = 「头 8.0s
+ 入口 11.0s」——入口块被排在**整个头窗（4 块 = 16MB ≈8s）之后**，而门控
其实只需要「头第 1 块 + 尾入口」两块。把入口块（``_entry_pieces``）的
deadline 提到 ``DEADLINE_STEP_MS``（与头窗第 2 块同级），其余尾块仍排在头窗
之后——只提前入口、不提前整尾窗（入口仅 min(2MB,size)，抢占代价可忽略；
整尾窗提前会与顺序前缀抢带宽、破坏 §B 的头窗保序）。目标：门控 ≈「头块
就绪 + 3s 内」。

A/B 实验（300MB / 4MB 块 / 做种端限 1MB/s）：现状头 1 块就绪 14.0s，
本策略 9.0s（-36%），t=13s 已连续 8MB。

段：
- §A window_pieces：按字节换算 + 上下限夹取 + 非法块长兜底
- §B begin：4MB 块头窗恰 4 块、deadline 严格递增（不再全 0）
- §C 入口块提前到头窗并行、其余尾块仍严格大于头窗最大 deadline（阶段 2.5）
- §D request_range：仍 ≤ LOOKAHEAD_PIECES 上限，但 deadline 递增
- §E seek_to_byte：从新位置重建窗口且 deadline 递增
- §F tail_window_bytes：按比例收敛 + 上下限夹取（4.1GB ≈10MB）
- §G tail_piece_window：4.1GB / 4MB 块尾窗 11 块 → 3 块
- §H tail_entry_ready：只下最末 min(2MB,size) 覆盖块即就绪，且不取消整尾窗，
     入口 deadline 提前到头窗并行（阶段 2.5）

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


def _eff(dl: list[tuple[int, int]]) -> dict[int, int]:
    """同一块可能被 ``begin``/``tick`` 重复预约：取**最后一次**（生效值）。"""
    out: dict[int, int] = {}
    for p, m in dl:
        out[p] = m
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
    ck.section("§C 入口块提前到头窗并行；其余尾块仍排在头窗之后（阶段 2.5）")
    # 8GB / 4MB：尾窗 4 块 [2044..2047]，入口 = 仅末块 2047——同时存在
    # 「入口」与「其余尾块」，才能既验证提前、又验证整尾窗不提前。
    s, h, f = mkf(4 * MB, 8 * 1024 * MB)
    s.begin(h, f)
    eff = _eff(h.deadlines)
    head = _head(h.deadlines, 4)
    head_max = max(m for _, m in head)
    entry_m = eff.get(f.end_piece)
    rest = [(p, m) for p, m in eff.items()
            if p in s._tail_pieces and p != f.end_piece]
    ck.check(bool(head), "头窗已预约")
    ck.check(entry_m is not None, "尾部入口已预约（.mp4 家族先拉 moov）")
    ck.check(entry_m == sch.DEADLINE_STEP_MS,
             f"入口块 deadline == DEADLINE_STEP_MS（{sch.DEADLINE_STEP_MS}，"
             f"与头窗第 2 块并行；实际 {entry_m}）")
    ck.check(bool(rest) and min(m for _, m in rest) > head_max,
             f"其余尾块 deadline {sorted(m for _, m in rest)} 仍 > 头窗最大 "
             f"{head_max}（整尾窗不提前，不与顺序前缀抢带宽）")
    ck.check(bool(rest) and min(m for _, m in rest)
             >= s._head_n * sch.DEADLINE_STEP_MS + 1000,
             f"其余尾块基值 ≥ n_head*STEP + 1000（实际 "
             f"{min((m for _, m in rest), default=None)}）")
    ck.check(entry_m is not None and bool(rest)
             and entry_m < min(m for _, m in rest),
             "入口块早于其余尾块（入口=门控必需，整尾窗只是兜底）")


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


def section_tail_bytes(ck):
    ck.section("§F tail_window_bytes：按比例收敛 + 上下限夹取")
    tw = sch.tail_window_bytes
    GiB = 1024 ** 3
    ck.check(sch.TAIL_BYTES_MIN == 2 * MB and sch.TAIL_BYTES_MAX == 16 * MB
             and sch.TAIL_RATIO == 0.0025 and sch.TAIL_ENTRY_BYTES == 2 * MB,
             "阶段 2 常量：下限 2MB / 上限 16MB / 比例 0.25% / 入口 2MB")
    ck.check(sch.TAIL_MAX_PIECES == 128, "TAIL_MAX_PIECES 上限保留（128）")
    s41 = int(4.1 * GiB)
    b41 = tw(s41)
    ck.check(b41 == int(s41 * sch.TAIL_RATIO),
             f"4.1GB → 恰为 0.25% 比例项（实际 {b41}）")
    ck.check(10 * MB < b41 <= 11 * MB,
             f"4.1GB → ≈10MB（实际 {b41 / MB:.2f}MB，旧式 44MB）")
    b1 = tw(GiB)
    ck.check(b1 == int(GiB * sch.TAIL_RATIO) and 2 * MB < b1 < 3 * MB,
             f"1GB → ≈2.5MB（实际 {b1 / MB:.2f}MB）")
    ck.check(tw(500 * MB) == 2 * MB,
             f"500MB → 2MB 下限（实际 {tw(500 * MB) / MB:.2f}MB）")
    ck.check(tw(100 * 1024) == 100 * 1024,
             "100KB → 不超文件本身（实际 %d）" % tw(100 * 1024))
    ck.check(tw(100 * GiB) == 16 * MB,
             f"超大文件 → 16MB 上限（实际 {tw(100 * GiB) / MB:.0f}MB）")
    ck.check(tw(0) == 0 and tw(-1) == 0, "size<=0 → 0（不产生负窗口）")


def section_tail_conv(ck):
    ck.section("§G 4.1GB / 4MB 块：尾窗 11 块（44MB）→ 3 块（≈10MB）")
    GiB = 1024 ** 3
    size = int(4.1 * GiB)
    s, h, f = mkf(4 * MB, size)
    win = sch.tail_piece_window(f, 4 * MB)
    ck.check(bool(win) and win[-1] == f.end_piece, "尾窗必含末块")
    ck.check(win == list(range(f.end_piece - 2, f.end_piece + 1)),
             f"尾窗恰末 3 块（实际 {len(win)} 块：{win}）")
    # 旧式公式对照：min(size, max(4MB, 1%·size), 64MB) = 44MB = 11 块
    old_bytes = min(size, max(4 * MB, int(size * 0.01)), 64 * MB)
    old_n = max(1, -(-old_bytes // (4 * MB)))
    ck.check(old_n == 11 and len(win) < old_n,
             f"新尾窗 {len(win)} 块 << 旧尾窗 {old_n} 块（{old_bytes / MB:.0f}MB）")
    ck.check(len(win) * 4 * MB <= 16 * MB, "尾窗字节 ≤ 16MB 上限")
    # 小文件：尾窗不超文件本身（300KB / 16KB 块 = 19 块整文件）——与旧行为一致
    tiny = TorrentFile(2, "root/a.mp4", 300 * 1024, 0, 0, 18)
    ck.check(sch.tail_piece_window(tiny, 16 * 1024) == list(range(19)),
             "小文件尾窗 = 整文件（行为与旧式逐字一致）")


def mkf(pl: int, size_bytes: int, path: str = "movie/big.mp4", have=()) -> tuple:
    """按**精确字节**构造 (scheduler, handle, file)（mk 只收整数 MB）。"""
    n = max(1, -(-size_bytes // pl))
    f = TorrentFile(0, path, size_bytes, 0, 0, n - 1)
    h = FakeHandle(pl, n, have=have)
    s = sch.PreviewScheduler()
    return s, h, f


def section_entry(ck):
    ck.section("§H tail_entry_ready：只下最末 min(2MB,size) 覆盖块即就绪")
    # 8GB / 4MB 块：尾窗 16MB（上限）= 4 块，入口 = 2MB = 仅最末 1 块
    s, h, f = mkf(4 * MB, 8 * 1024 * MB, have={2047})
    s.begin(h, f)
    ck.check(s._entry_pieces == [f.end_piece],
             f"8GB/4MB 入口 = 仅末块（实际 {s._entry_pieces}）")
    ck.check(len(s._tail_pieces) == 4,
             f"同文件整尾窗 4 块（实际 {s._tail_pieces}）")
    ck.check(s.tail_entry_ready() is True,
             "只末块就绪 → 入口就绪（新门控放行）")
    ck.check(s.tail_ready() is False,
             "整尾窗未齐 → 旧门控仍关（证明门控确实提前，非恒真）")
    # 入口就绪**不取消**整尾窗：tick() 继续预约未就绪的尾部块
    h.deadlines.clear()
    s.tick()
    dl = {p for p, _ in h.deadlines}
    ck.check({2044, 2045, 2046} <= dl,
             f"入口就绪后整尾窗仍在补拉（实际 {sorted(dl)}）")
    # 缺末块 → 入口未就绪
    h._have = set()
    ck.check(s.tail_entry_ready() is False, "末块缺失 → 入口未就绪")
    h._have = {2046}
    ck.check(s.tail_entry_ready() is False,
             "非末块（尾窗内其它块）就绪不算入口就绪")
    h._have = {2047}

    # 4.1GB / 4MB：入口 1 块 vs 整尾窗 3 块——门控只需 1/3
    s2, h2, f2 = mkf(4 * MB, int(4.1 * 1024 ** 3))
    h2._have = {f2.end_piece}
    s2.begin(h2, f2)
    ck.check(s2._entry_pieces == [f2.end_piece]
             and len(s2._tail_pieces) == 3,
             f"4.1GB：入口 {len(s2._entry_pieces)} 块 / 整尾窗 "
             f"{len(s2._tail_pieces)} 块（旧式 11 块需整窗齐）")

    # 入口 deadline 提前到头窗头几块并行（阶段 2.5）：门控必需的「头第 1 块 +
    # 尾入口」两块应当尽早到齐；入口只占 min(2MB, size)，抢占代价可忽略。
    eff2 = _eff(h2.deadlines)
    head2 = _head([(p, m) for p, m in h2.deadlines if p != f2.end_piece], 4)
    ck.check(eff2.get(f2.end_piece) == sch.DEADLINE_STEP_MS,
             f"尾部入口 deadline == DEADLINE_STEP_MS 与头窗并行（实际 "
             f"{eff2.get(f2.end_piece)}）")
    ck.check(bool(head2)
             and eff2.get(f2.end_piece) <= max(m for _, m in head2),
             "入口不再排在头窗之后（阶段 2.5 提前，旧「尾窗排后」契约更新）")

    # 非 moov 尾部家族（.mkv）无入口 → 恒就绪（不需要尾部即可开播）
    s3, h3, f3 = mkf(4 * MB, 8 * 1024 * MB, path="movie/big.mkv")
    s3.begin(h3, f3)
    ck.check(s3._entry_pieces == [] and s3._tail_pieces == [],
             ".mkv 无尾窗/无入口（tail_piece_window 家族门禁不变）")
    ck.check(s3.tail_entry_ready() is True, ".mkv 入口恒就绪（不阻塞开播）")

    # 小块场景：16KB 块入口 = 最后 2MB = 128 块（与尾窗判定口径一致）
    s4, h4, f4 = mkf(16 * 1024, 100 * MB)
    s4.begin(h4, f4)
    ck.check(len(s4._entry_pieces) == 128
             and s4._entry_pieces[-1] == f4.end_piece
             and s4._entry_pieces[0] == f4.end_piece - 127,
             f"16KB 块入口 = 最末 128 块（实际 {len(s4._entry_pieces)}）")


def main() -> int:
    ck = ts.Checker("playback_window_test（plan/07 阶段 1 播放窗口专项）")
    ck.section("按字节窗口 + deadline 递增保序（假句柄，不启会话）")
    section_window_pieces(ck)
    section_begin(ck)
    section_tail(ck)
    section_range(ck)
    section_seek(ck)
    section_tail_bytes(ck)
    section_tail_conv(ck)
    section_entry(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
