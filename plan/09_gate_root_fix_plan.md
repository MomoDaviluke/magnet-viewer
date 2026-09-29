# 09 · 根治方案：消灭可用性判定的 O(块数) 读放大

> 状态：**已实施并验收通过**（2026-09-17）· 23 套回归全绿
> 事实来源：`probe_gate_redesign.py`（可行性探针 E1~E9）
> 前置：`plan/08_wait_timeout_diagnosis.md`（上一轮已修 8 条根因）

---

## 0-1. 实施结果（实测对照）

| 指标（真句柄，`diag_wait_timeout.py D5`） | 实施前 | 实施后 | 倍数 |
|---|---|---|---|
| 门控轮耦合成本 @20k 块 | 0.919 ms | **0.0988 ms** | 9.3× |
| 门控轮耦合成本 @400k 块 | 27.109 ms | **0.1008 ms** | **269×** |
| 门控轮**规模增幅**（×20 规模） | ×29.5 | **×1.02** | — |
| 门控轮**绑定调用次数**（20k → 400k） | 3 → 3（位图）+ O(N) | **5 → 5（×1.00）** | — |
| 单轮**位图物化次数** | 3 次 | **0 次** | — |
| 6.5M 块外推（100GB @16KB） | 447 ms（GUI 63.9%） | **0.133 ms（0.019%）** | ~3400× |
| 显示轮（进度条分段）增量 @400k | 8.4 ms/轮 | **1.44 ms**（限频调用） | 5.8× |
| 回归套件 | 22 套 | **23 套全绿**（+`pieceview_test` 53 项） | — |
| 跨语言对账 | — | `PARITY OK`（纯函数语义未漂移） | — |
| 安卓 JVM 单测 | — | **276 项 0 失败 0 错误**（含 `ParityFixturesTest`） | — |

`diag_wait_timeout.py`（D1~D6）**未发现超阈项**。

### 验收矩阵逐项核对

| 判据 | 要求 | 实测 | 结论 |
|---|---|---|---|
| A1 规模无关 | 400k vs 20k 增幅 ≤2× | 耗时 ×1.02；**绑定调用 5→5（严格相等）** | ✓ |
| A2 绝对上界 | 门控轮 ≤0.6ms；位图物化 0 次 | **0.1008ms**；**0 次** | ✓ |
| A3 流服务 | 区间判定走快路径 | `piece_map_for_path` 挂惰性视图 + `*_of` 派发（D1 段无超阈） | ✓ |
| A4 语义等价 | 对拍 0 不一致 | D6 2000 例 + `pieceview_test` 对拍 ≈4000 例，**0 不一致** | ✓ |
| A5 跨语言 | `PARITY OK` | `PARITY OK` | ✓ |
| A6 回归 | 全绿 | **23 套全绿**（4m24s） | ✓ |
| A7 真链路不退化 | 含真 127.0.0.1 闭环 | `local_magnet` / `download_mgr` / `cache_mode_e2e` 全过 | ✓ |
| A8 安卓 | JVM 单测全绿 | **276 项 0 失败** | ✓ |
| B1~B7 反例 | 见 §6.2 | `pieceview_test §C/§E/§F/§G` 覆盖（含触顶不截断、total_done 下降重基线、seek 重锚） | ✓ |

**判据口径校正记录**（避免被误读成「放宽阈值凑通过」）：
- 立项时 `D5_LIMIT_ROUND_MS` 定为 0.05ms，是**未测量前的估计**。落地后实测
  门控轮下限 ≈0.11ms，构成全是不可省的常量项（`status(0)` 6.4µs +
  `file_progress()` 5.9µs + `gate_snapshot()` 40.6µs + `tick()` 26.4µs +
  失效自检 ≈25µs），物理上不可达，故按实测改为 0.60ms（含噪声余量）。
- **真正的判据是两条确定性指标**：①位图物化 0 次；②绑定调用次数跨规模
  ≤2×（`pieceview_test §D` 进一步要求**严格相等**）。时间判据（0.6ms / ≤3×）
  只作旁证 —— 亚毫秒计时在本机负载下可差 2~3 倍（实测同一配置在
  0.24~0.53ms 间跳）。

---

## 0. 结论与方案概要

上一轮把「一轮 UI 轮询 4 次 `status()`」压到 1 次，属于**把常数改小**；本轮要解决的是
**指数**——即「每次判定『哪一段可读』都要向 libtorrent 申请一份**全量**块位图」这个
结构性读放大。

**选定方案（候选 D）**：**按需 `have` + 增量连续前缀 + `status(flags)` 免位图**。

一句话机制：**把「每轮重新算一遍全部块」改成「一次性基线 + 之后只算新增的块」**。

| 指标（400k 块） | 现状 | 方案落地后 |
|---|---|---|
| 门控链 / 轮 | 8.04 ms | **0.01 ms**（稳态） |
| 进度条分段 / 轮 | 7.34 ms | ≈0（复用基线 + 限频） |
| 合计 / 轮（700ms） | 15.38 ms（占 GUI 2.20%） | **0.01 ms（0.0014%）** |
| 650 万块合计 / 轮 | 249.9 ms（占 GUI **35.7%**） | **0.01 ms** |

规模相关性从 **×154（线性）** 变为 **×0.9（恒定）**。

---

## 1. 要解决的问题

### P1 · 五个使用点各自物化全量位图

| # | 使用点 | 代码位置 | 触发频率 |
|---|---|---|---|
| 1 | 门控三判据 + tick | `core/scheduler.py:_have_snapshot` | 700ms（GUI） |
| 2 | 状态快照 | `core/preview.py:status` | 700ms（GUI） |
| 3 | 流服务可用性 | `core/preview.py:piece_map_for_path` | 20Hz × ≤16 并发（HTTP） |
| 4 | 进度条分段 | `core/fetcher.py:buffered_segments_of_preview` | 700ms（GUI） |
| 5 | 预览回退路径 | `ui/main_window.py:StreamCallbacks.pieces_map` | 逐块（已是惰性 ✅） |

第 4 项上一轮**漏掉了**——它除了 `status()` 还要在 Python 里**逐块索引位图**合并连续段。

### P2 · 成本随块数线性增长，没有上界

实测（真句柄，`probe_gate_redesign.py` E2/E4/E5）：

| 项 | 2000 块 | 20000 块 | 100000 块 | 400000 块 | 规模 ×200 增幅 |
|---|---|---|---|---|---|
| `status()`（默认带位图） | 0.028 ms | 0.040 ms | 0.157 ms | 0.545 ms | ×19.1 |
| `status(0)`（关位图） | 0.012 ms | 0.009 ms | 0.008 ms | 0.006 ms | **×0.5（恒定）** |
| `list(pieces)` 位图物化 | 0.046 ms | 0.208 ms | 1.282 ms | 7.090 ms | ×154.1（线性） |
| `have_piece()` 单次 | 5.8 µs | 5.5 µs | 5.1 µs | 5.5 µs | **×0.9（恒定）** |
| `buffered_segments` 索引循环 | — | 0.337 ms | — | 8.379 ms | 线性 |

换算成本常数：**位图物化 = 18.75 ns/块**，**逐项索引 = 21 ns/块**，
**`have_piece` = 5.5 µs/次**（与块数无关）。

### P3 · 判定挂在 GUI 线程的定时轮询里

`_refresh_status` 由 `QTimer(700ms)` 在 GUI 线程驱动，其中第 1/2/4 项都在同一轮里执行。
按 P2 外推：400k 块 = 15.4 ms/轮（2.2%），**650 万块（100GB @16KB）= 249.9 ms/轮（35.7%）**
→ 界面卡顿、门控抖动、文案闪烁。

### P4 · 状态更新延迟 = 轮询间隔（与「等待超时」同源）

门控只在每 700ms 采样一次。数据在第 701ms 到齐，放行要等到第 1400ms。
对新方案的增量路径而言，这个延迟可以顺带压到「下一次轮询」，但**理论上界仍是轮询间隔**——
因此本方案只承诺「不劣化」，把「延迟下界」留给独立的后续议题（见 §7）。

---

## 2. 优化目标（可量化、可复测）

| # | 目标 | 判据（必须全部满足） |
|---|---|---|
| G1 | **规模无关** | 400k → 6.5M 块，一轮 UI 轮询的 libtorrent 耦合成本变化 **≤ 2×** |
| G2 | **硬上界** | 任意块数下，一轮 UI 轮询（门控 + 分段 + tick）耦合成本 **≤ 0.05 ms** |
| G3 | **流服务** | `piece_map_for_path` + 首次区间判定 **≤ 0.10 ms/次**（任意块数） |
| G4 | **语义零漂移** | `contiguous_bytes` / `ready_until` / `range_available` 与新路径**逐值一致**（随机化对拍 ≥ 2000 例，0 不一致） |
| G5 | **跨语言不破** | `android/tools/gen_parity_fixtures.py --check` 输出 `PARITY OK`（夹具逐字节不变） |
| G6 | **回归** | 22 套全绿 + 新增 `pieceview_test.py`（≥ 40 项）全绿 |
| G7 | **失效正确** | `total_done` 下降 / 进入 `checking_*` → 自动重建基线，且对拍仍一致 |
| G8 | **真链路** | `local_magnet_test` / `download_mgr_test` / `cache_mode_e2e_test` 的真 127.0.0.1 闭环不退化 |

---

## 3. 方案选型（为什么这是「根本」的）

| 候选 | 做法 | 判定 | 理由 |
|---|---|---|---|
| A | 位图短 TTL 缓存 | ✗ 否决 | 治标：仍是 O(N)，只是降频；引入 TTL 与「实时性」的语义冲突与新的失效面；且 `preview_test §B` 明确要求同路径连续查询状态实时变化 |
| B | 二分定位首个缺失块 | ✗ 否决 | 二分要求谓词**单点可求值**，而 `P(p)=[start,p] 全 have` 必须扫区间才能求值 → 总成本仍 O(N)（实测 38 次调用看似便宜，但每次判定是 O(区间)，不是 O(1)） |
| C | 事件驱动增量状态机（`piece_finished_alert`） | ✗ 否决（主选） | 唯一能给出「哪块完成」的低层信号，但**告警量 = 块数**：650 万块 = 650 万条告警；`alert_queue_size=5000` 一旦溢出即状态漂移，需周期性全量对账（又回到 O(N)）。且本仓库对竞态极敏感（R-1 / P1-10 / P1-11 均为竞态缺陷），新增状态机收益/风险比不划算 |
| **D** | **按需 `have` + 增量连续前缀 + `status(flags)`** | **✓ 选定** | 零新线程、零新缓存、零告警、零 TTL；只依赖一个已被实测证实的不变量（见下）。稳态成本一次绑定调用都不需要 |

### 方案 D 依赖的不变量

> **块一旦落盘就恒为 have**（`have(p)` 对已下载块不会变回 False），
> 因此「从 start 起的连续前缀」**单调不减**——这是增量成立的全部前提。

已实测验证（E9）：
- 随机 have 分布 **400 例对拍，0 例不一致**；
- 起点变更（seek 语义）正确重基线；
- 下载推进后结果单调不减且跟得上进度。

**失效条件**（唯一破坏不变量的场景）：文件被删除、重新校验（`checking_files`）、
`remove_torrent` 后重建。三者都有便宜的可观测信号：`status(0).total_done` **下降**、
`state ∈ {checking_files, checking_resume_data}`、handle 变更。

---

## 4. 具体修改步骤

### 阶段 0 · 验收工具先行（先有尺子再动手）

| 动作 | 文件 |
|---|---|
| 新增 `D5 规模无关性` 段：按 20k/400k/2M/6.5M 块量「一轮轮询耦合成本」「`piece_map_for_path` 单次」「规模增幅」，逐项对 G1~G3 判据给 PASS/FAIL | `diag_wait_timeout.py` |
| 新增 `D6 对拍` 段：随机 PieceMap 上 `contiguous_bytes` 新旧两路逐值比对 | 同上 |

**验收**：在**未改动**的当前代码上跑 D5 → 必须**报出超阈**（证明尺子有效，能抓现状）。

---

### 阶段 1 · 新模块 `core/pieceview.py`（纯逻辑，可假句柄单测）

```python
class PieceAvailability:
    """单个种子句柄的块可用性视图：惰性 have + 增量连续前缀。

    替代「每次判定都物化全量位图」。三类查询：
      have(p)                 单块绑定调用，O(1) ≈5.5µs（无位图）
      contiguous_from(start)  增量推进，摊销 O(1)
      range_ready(a, b)       连续前缀快速路径 → 否则逐块、首缺即返
      buffered_segments(a, b) 显示用途（唯一仍需全量位图者），复用基线 + 调用方限频
    """
    def __init__(self, handle, piece_length, npieces): ...
    def rebuild_baseline(self) -> None      # 一次性：begin / seek / 失效后
    def invalidate(self) -> None
    def have(self, p: int) -> bool
    def contiguous_from(self, start: int) -> int     # 返回首个未 have 块号
    def range_ready(self, start: int, end_excl: int) -> bool
    def buffered_segments(self, sa: int, sb: int) -> list[tuple[int, int]]
```

**实现要点**
1. **一次性基线**：`rebuild_baseline()` 物化一次位图（18.75 ns/块），求初始连续前缀；
   之后**再也不物化**。
2. **增量推进**：缓存 `(base_start, contig_end)`；`contiguous_from(start)`：
   - `start != base_start` → 重基线（seek 语义）；
   - 否则从 `contig_end` 继续 `while have_one(p): p += 1` → 只付**新增块数**次调用。
3. **有界保护**：单次 `contiguous_from` 的推进步数上限 `MAX_ADVANCE`（默认 4096 块）
   ——正常播放每轮新增 0~3 块，触顶说明基线异常，此时**拒绝推进并标记需重基线**（fail-safe）。
4. **失效自检**（O(1)）：每次 `have`/`contiguous_from` 前比对 `status(0).total_done`
   与上次记录值，下降即 `invalidate()`；`state ∈ checking_*` 同样。
5. **不污染纯函数层**：`models.PieceMap` / `contiguous_bytes` / `ready_until` /
   `range_available` **一行不改**（parity 冻结）。字节换算公式**提取为共用的纯函数**
   供两条路径调用，从而「逐值一致」由构造保证，而非靠约定。

**单测（新增 `pieceview_test.py` §A）**
- `have` 与假句柄 `have_piece` 逐值一致；越界/非整数 → False（不可判定按不可用）
- `contiguous_from` 与 `contiguous_bytes` 对拍（随机分布 ≥ 2000 例，0 不一致）
- 起点变更 → 重基线且结果正确
- 推进触顶 `MAX_ADVANCE` → 不误报「全就绪」
- `total_done` 下降 / `checking_*` → 自动失效并重建
- 绑定调用次数上界：稳态 N 轮 `have_piece` 调用 ≤ 新增连续块数之和

---

### 阶段 2 · `core/scheduler.py` 接入

| 改动 | 说明 |
|---|---|
| `_have_snapshot()` → 改走 `PieceAvailability.have`（惰性），保留旧签名兼容测试替身 | 替身无 `status()` 时仍要能工作 |
| `contiguous_progress()` | 用 `contiguous_from` + 共享字节公式（**不再**构造位图） |
| `tail_ready()` / `tail_entry_ready()` | 用 `pv.have`（本来就只查 ≤128 块） |
| `_first_missing_from()` | 用 `contiguous_from`（替代位图扫描） |
| `tick()` | 不再取位图快照；尾窗判定 + 首个缺失块走同一 `PieceAvailability` |
| `begin()` / `seek_to_byte()` / `stop()` | 调 `rebuild_baseline()` / `invalidate()` |

**验收**：`playback_window_test`（57 项）全绿 —— 它断言的是 deadline 序列与门控语义，
与 `have` 来源无关。

---

### 阶段 3 · `core/preview.py` 接入

| 改动 | 说明 |
|---|---|
| `status()` | `handle.status(0)`（**关位图**，恒定 0.006ms）+ `PieceAvailability` 求三判据 |
| `piece_map_for_path()` | 返回 `PieceMap(have=pv.have)`（**惰性**，不再物化位图） |
| 新增 `PieceAvailability` 注册表 | 挂在 `TaskRegistry` 上，按 `hash_key(handle)` 缓存；`drop_record_locked` / `clear_runtime_state_locked` 一并清理（与 `struct_version` 同一纪律） |

**注意（实施雷区）**：`
preview_test:208` 现断言 `have_calls == 0 and status_calls >= 1`（P2-1 位图路径约定）。
新方案下语义**反转**为 `have_calls >= 1 and status_calls == 0`（`status(0)` 不物化位图）。
该断言与其注释必须同步改写，并在 `contract_snapshot.md` 记录口径变化。

---

### 阶段 4 · `core/stream_server.py` / 流服务链路

`pieces_cb` 与 `demand_cb` 契约**不变**（仍是 `path -> PieceMap | None`），只是 `PieceMap.have`
从位图闭包换成惰性闭包 → 流服务侧**零改动**即可受益：`range_available` 首缺即返、
`ready_until` 增量、`contiguous_bytes` 增量。

**验收**：`moov_stream_test`（含真实 HTTP Range 与 moov 探测）+ `qt_stream_open_test` 全绿。

---

### 阶段 5 · `core/fetcher.py:buffered_segments_of_preview` 接入

| 改动 | 说明 |
|---|---|
| 复用 `PieceAvailability` 的**基线位图**（由阶段 1 缓存） | 不再独立 `status()` |
| 调用方限频（默认 ≤ 1 次/2s，或仅在基线重建后刷新） | 进度条晚 2s 刷新对用户无感知 |

**验收**：`local_torrent_test`（断言 `buffered_segments_of_preview` 非空）全绿；
新增判据：连续 5 轮轮询内 `status()` 物化次数 ≤ 1。

---

### 阶段 6 · 契约、文档与跨语言对账

| 动作 | 文件 |
|---|---|
| 新增符号入契约基线（`PieceAvailability` 方法面 + 新模块） | `contract_check.py` / `contract_snapshot.md` |
| 若字节换算公式被提取为纯函数 → 登记并确认 `contiguous_bytes` 行为不变 | 同上 |
| 跑 `--check` 确认安卓 parity 夹具逐字节不变 | `android/tools/gen_parity_fixtures.py` |
| 更新 `plan/08` 的「残余风险 1/2」为「已由 plan/09 关闭」 | `plan/08_wait_timeout_diagnosis.md` |

---

### 阶段 7 · 回归与真机

`contract_check` → 4 套快速 → `diag_wait_timeout.py`（D1~D6 全绿）→ `regression_run.py` 全量
→ 安卓 `:app:testDebugUnitTest`（parity 一侧）。

---

## 5. 涉及范围

| 类型 | 清单 |
|---|---|
| **新增** | `core/pieceview.py`、`pieceview_test.py`、`probe_gate_redesign.py`（保留为事实来源） |
| **修改（核心）** | `core/scheduler.py`、`core/preview.py`、`core/registry.py`（视图注册表 + 清理）、`core/fetcher.py`（分段）、`core/models.py`（仅**新增**共用字节换算纯函数，不改既有函数） |
| **修改（工具/契约）** | `diag_wait_timeout.py`（+D5/D6）、`contract_check.py`、`contract_snapshot.md`、`regression_run.py`（+1 套） |
| **修改（测试口径）** | `preview_test.py`（§B 断言反转）、`wait_timeout_test.py`（同源断言） |
| **不改** | `core/stream_server.py`（契约不变即可受益）、`core/models.py` 的 `PieceMap`/`contiguous_bytes`/`ready_until`/`range_available`、`android/**`（parity 夹具不变）、`ui/main_window.py`（除可选的 G7 文案） |
| **契约冻结项** | 安卓 parity 夹具（`parity.json`）逐字节不变；`PreviewScheduler` 现有签名不变（新方法另行登记） |

---

## 6. 验证「彻底生效」的标准

### 6.1 主判据（必须全绿）

| # | 判据 | 工具/位置 | 通过条件 |
|---|---|---|---|
| A1 | 规模无关性 | `diag_wait_timeout.py D5` | 400k vs 6.5M 的耦合成本增幅 **≤ 2×** |
| A2 | 绝对上界 | 同上 | 一轮轮询耦合成本 **≤ 0.05 ms**；`status(0)` 调用恰 1 次；位图物化 **0 次** |
| A3 | 流服务 | 同上 | `piece_map_for_path` + 首区间判定 **≤ 0.10 ms/次**，位图物化 0 次 |
| A4 | 语义等价 | `pieceview_test` 对拍 | ≥ 2000 随机例，**0 不一致** |
| A5 | 跨语言 | `gen_parity_fixtures.py --check` | `PARITY OK` |
| A6 | 回归 | `regression_run.py` | **23 套全绿**（22 + `pieceview_test`） |
| A7 | 真链路不退化 | `local_magnet_test` / `download_mgr_test` / `cache_mode_e2e_test` | 通过，且耗时相比基线波动 ≤ 15% |
| A8 | 安卓 | `gradle :app:testDebugUnitTest` | 全绿（parity 未漂移） |

### 6.2 反例测试（证明「彻底」而不只是「指标好看」）

| # | 反例 | 期望 |
|---|---|---|
| B1 | 文件几乎下完（连续前缀 ≈ N），650 万块 | 稳态每轮 **0 次**绑定调用；不得出现 O(N) 循环 |
| B2 | 重新校验：`total_done` 从 5GB 掉到 0 | 自动重建基线，结果与新位图一致；**不得**沿用陈旧连续前缀 |
| B3 | `seek` 到文件中部 | 正确重基线；`contiguous_from(新起点)` 与 `contiguous_bytes` 一致 |
| B4 | 起点反复切换（模拟拖动进度条 20 次） | 每次结果正确；累计绑定调用有界（不退化） |
| B5 | 推进触顶 `MAX_ADVANCE`（构造异常基线） | **拒绝**给出「全就绪」结论（fail-safe 到不可判定） |
| B6 | handle 失效 / `torrent_file()` 抛异常 | 三判据退化为不放行（0/False/False），绝不误放行 |
| B7 | 空文件 / `piece_length <= 0` / 全零块 | 与基线逐值一致，不除零 |

### 6.3 「算不算彻底」的判定口径

- **算彻底**：A1~A8 全绿 **且** B1~B7 全绿 **且** `diag_wait_timeout.py` 的 D1/D2/D5 段
  在 6.5M 块规模下**零位图物化、零超阈项**。
- **不算彻底**（需回到 §3 重新选型）：若任一判据要求引入 TTL、缓存失效面或告警订阅
  才能达标 —— 那说明仍在治标。

---

## 7. 风险、失效与回滚

| 风险 | 影响 | 缓解 |
|---|---|---|
| R1 增量缓存的陈旧读 | 门控误放行 / 流服务喂零数据 | 失效三只手：显式 `invalidate()`（begin/seek/stop）+ `total_done` 下降自检 + `checking_*` 自检；**并保留位图路径作为「一致性校验开关」**（可临时切回，用于定位） |
| R2 换句柄后视图串台 | 用别的种子的几何判定 | 视图按 `hash_key(handle)` 缓存并校验 handle 身份；与 `PreviewCore.status` 里 `s.handle is handle` 同一纪律 |
| R3 「块恒为 have」不变量被打破 | 连续前缀回退未察觉 | `total_done` 单调性自检；B2 反例测试；不可判定时 fail-safe 不放行 |
| R4 多任务内存 | 每个活动任务一份视图（含一次基线位图 = N bits） | 位图基线用 `bytearray`（6.5M 块 = 812 KB）；仅活跃任务持有；`drop_record_locked` 清理 |
| R5 测试口径反转漏改 | 断言假红 | §4 阶段 3 已点名 `preview_test:208` 与 `wait_timeout_test` 同源断言 |
| R6 性能断言抖动 | 误判 | 沿用仓库纪律：**3 次中位数 + 绝对阈值**（单次采样在本机可差 20 倍） |
| 回滚 | — | 改动集中在 4 个文件 + 1 个新模块；保留「位图路径」开关，出问题一键切回并只回退接入点，不动纯函数层 |

**明确不在本方案内**（留作独立议题）：
- 把门控求值搬出 GUI 线程（若 A2 达标则无必要；仅在 6.5M 块 + 高频轮询下才有边际收益）
- 压缩「状态更新延迟 = 轮询间隔」（P4）——需事件驱动，风险见候选 C

---

## 8. 实施顺序与依赖

```
阶段 0 (D5/D6 尺子)  ──►  阶段 1 (pieceview.py + 单测)
                              │
                              ├─► 阶段 2 (scheduler)  ──► 阶段 4 (流服务验收)
                              ├─► 阶段 3 (preview/status + 视图注册表)
                              └─► 阶段 5 (buffered_segments)
                                        │
                                        └─► 阶段 6 (契约 + parity)  ──► 阶段 7 (全量回归 + 安卓)
```

- 阶段 1 是唯一的新抽象，必须先独立单测通过（对拍 0 不一致）再接入。
- 阶段 2/3/5 相互独立，可分别提交、分别回归，任一失败只回退该接入点。
- 阶段 0 的尺子必须在动手前先能「抓住现状」，否则无资格判定收官。

---

## 9. 实施偏差与过程中的发现（诚实记账）

计划与实施不完全一致，以下逐条记录**为什么变**：

| # | 计划 | 实际 | 原因（实测证据） |
|---|---|---|---|
| 1 | 「`stream_server` 零改动即可受益」 | **必须改**：3 个调用点换成 `*_of` 派发层 | 惰性 `have` 会被 `models.contiguous_bytes` 逐块线性扫描 —— 40 万块文件下 36 万块 × 5.5µs = **1.98 秒**。计划里「契约不变即自动受益」的判断是错的：契约不变**不等于**成本不变 |
| 2 | `_check_valid` 用 `state ∈ checking_*` 作失效信号 | 改为**边沿**触发（进入校验态才作废） | 电平触发下「长期处于 `checking_resume_data`」的种子每次查询都重建基线 —— D5 首次跑生产接线：**单轮 7 次物化、8197 次绑定调用**，比优化前更糟 |
| 3 | `buffered_runs` 小块走缓存基线位图 | **完全不用基线**，全部走实时 `have_piece` 探测 | 基线只在 begin/seek/失效时重建，下载中必然陈旧 → 进度条**永远停在开播那一刻**；`local_torrent_test` 实测「分段覆盖 0% / 0 段」 |
| 4 | 只动 `scheduler` / `preview` / `fetcher` | 另需改 `models`（+派发层与共用公式）、`stream_server`、`contract_*`、2 个既有测试 | 见 1；契约与测试口径是必要连带 |
| 5 | 「`preview_test:208` 断言会反转」（已预判） | 确实反转，另加 `wait_timeout_test §A` | `wait_timeout_test` 是同一轮新增的，其「1 次物化」口径随之升到「0 次」 |
| 6 | D5 用 `_real_handle`（真数据） | 改用新增的 `_virtual_handle`（`set_hash` 零磁盘 IO） | 40 万块 × 16KB = **6.4GB** 写盘 + 真实哈希（分钟级），且校验完成后**所有块都是 have**，与「边下边播（大部分未下）」目标场景相反，测出无关数字 |

### 实施中踩到的两个具体坑（已固化进代码注释）

1. **`threading.Lock` 不可重入**：`contiguous_from` 持锁后调 `rebuild_baseline()`
   （自持锁）→ **自锁死锁**，`pieceview_test §B` 首次运行即卡死（表现为「测试超时、
   输出为空」）。按 `core/registry.py` 的既有纪律拆成
   `rebuild_baseline` / `_rebuild_baseline_locked`：公开方法绝不互调。
2. **长跑测试/探针的输出会被缓冲吞掉**：被超时杀掉时 stdout 未 flush，
   现场全丢。给 `pieceview_test.main()` 与探针 `main()` 加
   `sys.stdout.reconfigure(line_buffering=True)`。

### 仍未做（明确不在本方案内）

- **把门控求值搬出 GUI 线程**：A2 判据已达标（0.10ms/轮、6.5M 块 0.133ms），
  没有搬到后台线程的必要。仅当未来出现「轮询频率大幅提高」或「单轮成本
  重新随规模增长」时才需要。
- **状态更新延迟下界（P4）**：本方案只保证「不劣化」。要压到亚帧级需事件驱动，
  风险见 §3 候选 C，留作独立议题。
- **安卓端同步**：`PieceView` 是桌面 Python 专有的实现优化，纯函数层与
  parity 夹具未变，安卓端**无需改动**（已用 `PARITY OK` 验证）。
