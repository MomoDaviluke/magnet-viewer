# plan/11 —— 并发与多线程风险分析（现有风险清单的补充）

> 立项 2026-09-17。`plan/10` 只覆盖了吞吐调参引入的风险（内存/线程数）。
> 本文补齐**并发场景**的风险：竞态条件、死锁、数据不一致、资源争用，
> 外加本项目特有的**句柄生命周期**与**关闭顺序**两类。
>
> 所有条目均给出 `文件:行号` 证据；证据不足的明确标注「未发现」或「待验证」。
> 每条给**方案 + 适用条件 + 权衡 + 验收方式**，不写无法落地的原则性建议。

---

## 1. 线程模型与共享状态

### 1.1 线程清单（Python 侧）

| # | 线程 | 创建点 | 职责 | 触碰的核心状态 |
|---|---|---|---|---|
| T1 | Qt 主线程（GUI） | Qt 事件循环 | 700ms `_refresh_status`、`scheduler.tick()`、任务 CRUD、UI 更新 | 注册表、调度器、任务清单、`path_to_file` |
| T2 | libtorrent 告警线程 | `core/fetcher.py:147` | 消化告警、元数据看门狗、周期 fastresume 脏写 | 注册表、任务清单、`SessionCore` 成员 |
| T3 | 元数据解析线程 | `core/resolver.py:122` | 本地 .torrent / 磁力链解析 | 注册表、`.meta` 落盘 |
| T4 | HTTP 服务线程 | `core/stream_server.py:418` | `serve_forever` + 每请求一线程（`ThreadingHTTPServer`） | **经回调进入**注册表/调度器/`PieceView` |
| T5 | HTTP 请求线程（≤16） | 同上（`limiter` 限流） | Range 响应、`_wait_ready` 轮询、点播回调 | `pieces_cb`/`demand_cb` → 注册表索引 + 调度器 |
| T6 | 关闭收尾线程 | `ui/main_window.py:1082` | `session.shutdown()` → 清理 → `server.shutdown()` | 全会话 |
| C++ | libtorrent 网络/磁盘/校验线程 | `lt.session(...)` | — | 会话内部；`aio_threads=12`、`hashing_threads=4` |

### 1.2 锁清单（全部）

| 锁 | 位置 | 保护对象 | 可重入 | 备注 |
|---|---|---|---|---|
| `TaskRegistry.lock` | `core/registry.py:113` | `torrents` / `tasks` / `current_ih` / `struct_version` | **否** | 唯一的全局锁，T1–T6 全都持有过 |
| `PieceView._lock` | `core/pieceview.py:91` | 视图基线位图与连续前缀 | 否 | 已有 `*_locked` 拆分纪律 |
| `metacache._PRUNE_LOCK` | `core/metacache.py:55` | 剪枝节流状态 | 否 | **不保护实际删除**（见 R-1 相邻项） |
| `metacache._STATS_LOCK` | `core/metacache.py:60` | 命中统计 | 否 | — |
| `StreamServer.limiter` | `core/stream_server.py:404` | HTTP 并发度（16） | — | `BoundedSemaphore`，释放已保证 |

### 1.3 项目既有的两条并发纪律（本文以它们为准绳）

1. **「锁内零 libtorrent」**：`with reg.lock` 段内只做纯字典操作与快照，句柄/session
   调用一律出锁（`core/fetcher.py:462` 注释、`core/preview.py:176-178` 实践）。
2. **「一次调用一个锁段 + TOCTOU 复核」**：`core/fetcher.py:462-489` 是正确范式——
   锁内取快照 → 出锁做重活 → 重新入锁复核（`481-486` 的行为正是为此加的）。

---

## 2. 风险登记表

等级口径：**高** = 会造成用户可见的数据丢失或崩溃；**中** = 功能降级/静默失败/可复现的
非崩溃缺陷；**低** = 影响可忽略或已知且可接受；**未发现** = 查过且无问题（留档防重复排查）。

### R-1【数据不一致·高】原子写并非并发安全：固定临时文件名 + 无锁

**证据**：`core/taskstore.py:163-178`（修复前）

```
tmp = f"{path}.tmp"          # 临时文件名固定，无唯一后缀
with open(tmp, "wb") as f:   # 无锁、无 per-target 互斥
    ...
os.replace(tmp, path)
```

**触发**：两个线程在同一 `open→fsync→replace` 窗口内写**同一路径**。同一路径的
现实场景：`.tasks.json`（T1 任务 CRUD / T2 元数据到达 / T6 关闭，均有调用点，
见 `core/taskops.py:116,140,200,237,366,386,411,462`、`core/session.py:574,642`、
`core/resolver.py:336,414`、`core/session.py:401`）。`fsync` 让窗口达毫秒级。

**失败形态（2026-09-17 实测修正，与最初的推断不同）**：

原分析写的是「临时文件内容交错 → `os.replace` 搬走半截文件（静默损坏）」。
**实测（负向对照，见下）表明 Windows 上的主导形态是另一种、且更严重**：
两个写者同时持有同一临时文件时，先完成者调用 `os.replace` 会撞
**`PermissionError [WinError 32] 另一个程序正在使用此文件`**——因为**源文件
仍被另一个线程打开**，`MoveFileEx` 无法移动它。

实测数据（5 写者 × 30 轮，64KiB 载荷，5 次重复，`.workbuddy/tmp/negative_control_r1.py`）：

| 实现 | 每轮写者异常数 | 判据命中 |
|---|---|---|
| 旧实现（固定临时名 + 无锁） | **147–149 次 / 150 次写入** | **5/5 轮不合格** |
| 新实现（唯一名 + 分片锁） | **0** | 0/5 |

即：**并发写同一路径时，旧实现几乎每一次写入都以异常收场**，而不是「偶尔
产出坏文件」。内容交错是 POSIX 语义下的形态（两个 fd 各自从偏移 0 覆写），
在 Windows 上先被共享冲突挡住。

**后果因此被上调**：异常从 `atomic_write_bytes` 抛出后，`persist_tasks` 的
`except Exception`（`core/persist.py:166-169`）把它吞成一行 `log_warning`
⇒ **几乎所有并发落盘都静默失败**，而不是「偶发」。对 `.tasks.json` 而言，
读端 `load_tasks` 对损坏/缺失的策略是「返回空表」（`core/taskstore.py:206-218`）
⇒ 用户下次启动**整个下载任务列表为空**。
`.meta` / `.fastresume` / `.dht_state` 同受影响（`.meta` 有内容校验自愈，
表现为缓存未命中 + 重取元数据）。

**方案 A（推荐，已实施）——移植安卓侧已落地的实现**：
`android/.../data/TaskStore.kt:192-218` 已经把三件事做全：按目标路径分锁、
临时名带唯一后缀、失败删 tmp。桌面侧照搬（互斥实现改为分片数组，理由见下）。

- 适用条件：所有 `atomic_write_bytes` 调用方；单进程单实例运行。
- 权衡：`writeLocks` 在安卓是「按路径建字典」，桌面侧改用 **64 片固定分片数组**
  ——桌面还有 `.meta/<ih>.torrent` 这种随「看过多少种子」单调增长的路径，
  字典会无界增长，分片换取内存有界；代价是不同路径偶发共用一把锁（轻微串行化），
  对「写盘频率极低」的场景无影响。分片仍是进程内互斥，防不住跨进程多开。
- 验收：`concurrency_test.py §A`（写者无异常 + 产物是某一次完整写入 +
  无临时残留 + 失败路径清理 + 同路径恒得同一把锁），已随第 1 批交付。

**方案 B——调用侧串行化**：`persist_tasks` 走单一写线程/队列。
适用：写频率高、希望顺带做写合并时。权衡：引入队列与生命周期管理，
`fsync` 阻塞只是从调用者搬到了写线程；对本项目过重。**不推荐**。

**方案 C——把写盘收进 `reg.lock`**：权衡最差：`fsync` 在锁内会反卡 HTTP 的
`_dir_index` 与告警线程，把「数据不一致」换成「锁争用 + GUI 卡顿」。
`core/session.py` 的旧 `shutdown` 已是这个形状（见 R-6b，第 1 批一并修掉），
不应扩散。**明确不采用**。

---

### R-2【数据不一致·中】落盘读的是「活字典」引用，不是快照

**证据**：`core/persist.py:163-169` → `save_tasks(d.cache_dir, d.tasks_get())`，
而 `tasks_get` 是 `lambda: self._tasks`（`core/fetcher.py:111`），返回**活引用**。
对照正确写法：`core/persist.py:239-240` 的 `drain_resume_alerts` 是**锁内 `dict()` 快照**。

**后果**：`json.dumps` 迭代期间另一线程插入键 → `RuntimeError: dictionary changed
size during iteration` → 被 `core/persist.py:166-169` 的 `except Exception` 吞成
一行告警 ⇒ **该次落盘静默丢失**；即便不抛，取到的也可能是撕裂组合
（`state` 与 `error` 不是同一次写入的状态）。

**方案 A（推荐）**：`persist_tasks` 内先在 `with d.lock` 下做
「顶层 `dict()` + 逐任务浅拷贝」快照，出锁再序列化。适用：所有落盘路径。
权衡：每任务一次浅拷贝，任务数 10² 级、开销微秒级，可忽略。

**方案 B——不可变快照模型**：每次任务变更生成新 dict（copy-on-write）。
权衡：彻底消灭该类竞态，但要重构 `upsert_task` 等既有 API，改动面大，
`registry`/`taskops` 的既有测试需同步。**列为长期项**。

**验收**：并发 `upsert`（T1）+ `persist_tasks`（T2）压力用例，
断言落盘文件可解析且任务数 ≥ 某一时刻的最小期望值。

---

### R-3【竞态·中高】PreviewScheduler 完全无锁，却有两个线程在写

**证据**：`core/scheduler.py` 全文无 `Lock`/`threading` 导入，`handle` / `file` /
`_play_from` / `_scheduled_to` / `_view` 都是裸字段。

写者（含线程标注）：

| 方法 | 调用点 | 线程 |
|---|---|---|
| `tick()` | `ui/main_window.py:868`（QTimer） | T1 |
| `request_range()` | `ui/main_window.py:261 demand_cb` → `ui/main_window.py:168-178` | **T5（HTTP）** |
| `seek_to_byte()` | `ui/main_window.py:752` | T1 |
| `rearm()` | `ui/main_window.py:961` | T1 |
| `begin()` | `core/preview.py:90` ← `ui/main_window.py:715/776` | T1 |
| `stop()` | `core/resolver.py:140`（持锁）、`core/taskops.py:438`、`core/taskops.py:530`、`core/fetcher.py:474` | T1 |
| `stop()` | **`core/session.py:396`** | **T6（shutdown）** |

**具体竞态**（两类）：

1. **检查-使用窗口**：`tick()` 在 `core/scheduler.py:499` 判 `self.active`
   （= `handle is not None`），随后 `core/scheduler.py:501` 直接
   `self.handle.torrent_file()`——**这一行没有 try**。若在两次读之间
   `stop()`（`core/scheduler.py:591-599`）把 `handle` 置 `None`，即
   `AttributeError: 'NoneType' object has no attribute 'torrent_file'`。
   目前仅靠调用方 `ui/main_window.py:866-870` 的外层 try 兜住 ⇒ 表现为
   「偶发一条异常日志」，而不是崩——但这是**运气，不是设计**。
   同类：`core/scheduler.py:344`（`begin` 的裸调，T1 内同步，风险低）。
2. **读-改-写非原子**：`_scheduled_to` 的「读 → 比较 → 写」
   （`core/scheduler.py:517-527`）与 `_play_from` 的读写（`431-443`、`553-554`）
   没有原子性；T5 的 `request_range` 与 T1 的 `tick` 交错时，可能丢一次预约
   （表现为「点播后 50ms 才生效」或退避重试），不会错算数据。

**方案 A（推荐，目标态）——给调度器一把自有锁**：把状态字段的读改写收进
`*_locked` 私有方法，公开方法「锁内取快照 → 锁外出锁调用 libtorrent」
（沿用 §1.3 纪律 1）。适用：现有全部调用点，**不改公开签名**
⇒ `contract_check` 的基线表无需改（改动落在私有层）。

- 权衡：
  - 需要在 `begin/tick/stop/seek_to_byte/rearm/request_range` 六处拆锁段，
    改动面中等，但都是机械改写；
  - **必须定义锁序**：`scheduler._lock → PieceView._lock → reg.lock`（单向）。
    `PieceView` 内部不会回头调 scheduler，故该顺序可保持无环；
    反之（先 PieceView 后 scheduler）会形成环 ⇒ 死锁风险；
  - `_view` 的注入/解除（`attach_view`）要与 PieceView 的锁保持上述单向顺序。

**方案 B——把调度器变成单线程亲和（GUI 线程独占）**：让 T5 的 `request_range`
经 Qt 队列连接投递到 T1。适用：想彻底消除该类竞态、且能接受调度延迟
（≤1 个事件循环周期）。
- 权衡：代价是 **core 层引入 Qt 依赖**，破坏当前「core 无 Qt」的分层
  （现有 `ui/main_window.py:52-55` 的 `_Bridge` 信号是这条路的正确落点，
  但要把调度器调用搬到信号槽里，需要把 `session` 的调度接口改成异步语义）；
  且 HTTP 点播从「立即」变「下一拍」。**分层代价高于收益，当前不推荐。**

**方案 C（推荐作为 A 之前的快速修复，成本极低）——防御式局部引用 + try**：
`h = self.handle; if h is None: return` 后再用 `h`；给 `tick()` 整体包 try
（与 `request_range` 一致）。适用：只想先消除「偶发异常日志」。
- 权衡：竞态仍在（可能丢一次预约），但把「靠调用方兜底」变成「自身不崩」，
  是 A 的安全前置。**建议与 A 同批交付，C 先落地。**

**验收**：三线程并发用例——T5 高频 `request_range` + T1 高频 `tick` +
随机 `stop`/`begin`，断言（a）无异常逃逸，（b）`_scheduled_to` 单调不减，
（c）`stop` 之后 `active is False`。

---

### R-4【资源争用·中】全局单锁 + 锁内句柄调用 ⇒ 锁车队

**证据**：唯一全局锁 `core/registry.py:113`，被 T1–T6 全部持有。以下位置
**在锁内调用 libtorrent**（违反 §1.3 纪律 1）：

- `core/registry.py:273`：锁内 `ses.remove_torrent(...)`
- `core/resolver.py:139-169`：锁内 `scheduler.stop()` / `remove_torrent`
- `core/taskops.py:432-461`：锁内句柄操作

**机理**：`remove_torrent` / `clear_piece_deadlines` 是毫秒级 C 调用；
16 个 HTTP 线程（T5）的 `_dir_index` 重建（`core/preview.py:120-122`）与
T2 的看门狗都需要同一把锁。持锁者在 C 调用中会释放 GIL，但等待者被唤醒后
还要重新抢 GIL ⇒ **锁 + GIL 叠加放大**，表现为 GUI 轮询抖动、预览点播延迟。

**方案 A（推荐，目标态）**：把句柄调用移出锁，采用既有 TOCTOU 范式
（快照 → 出锁 → 调用 → 必要时入锁复核），参照 `core/fetcher.py:462-489`。
适用：所有「锁内出现 `handle.*` / `ses.*`」的位置。
- 权衡：每处都要论证 TOCTOU 复核条件（工作量集中在这里，但都是机械改写）；
  复核引入的二次入锁有 ABA 风险，需按 `481-486` 的写法带上身份/状态断言。

**方案 B——改用 `RLock`**：权衡：只是让自锁不爆，反而掩盖设计问题，
**不推荐**（仅在其他方案无法落地时作为兜底）。

**方案 C——按任务分锁（细粒度锁）**：权衡：能消除全局争用，但 `_dir_index`
依赖全局 `struct_version` 的排序语义、`clear_runtime_state_locked` 是全局操作，
重构量大且回归面广。**当前不做**。

**方案 D（推荐先做，零改动）——先量化再决定**：项目已有探针判据
`diag_wait_timeout.py` 的 **D4-a：注册表锁持有 P99 > 5ms 即超阈**。
先跑它拿数字；不超阈则只做 A 的低风险子集（`registry.remove_torrent` 一处）。

**验收**：D4-a 复测；并发用例下 `_dir_index` 重建平均耗时不劣化。

---

### R-5【生命周期·中】句柄被移除后仍可能被使用

**证据**：`ses.remove_torrent` 调用点：`core/registry.py:273`、
`core/resolver.py:149/160/222`、`core/taskops.py:443`、`core/session.py:435`。

移除后仍有使用方（逐条核对兜底情况）：

| 使用方 | 位置 | 兜底 |
|---|---|---|
| `PieceView.have`（T5） | `core/pieceview.py:227-231` | ✅ try |
| `PreviewCore.status`（T1/T5） | `core/preview.py:296-302` | ✅ try |
| `scheduler.request_range` | `core/scheduler.py:437-441, 522-526` | ✅ try |
| `scheduler.tick` 的 `torrent_file()` | `core/scheduler.py:501` | ❌ **无 try**（同 R-3-1） |
| `persist.resume_sweep` → `request_resume` | `core/persist.py:192-197` | ✅ try |
| `taskops` 列表里的 `handle.status()` | `core/taskops.py:578-585` | ✅ try |

**结论**：兜底覆盖已相当完整，**唯一裸露点是 `scheduler.py:501`**。

**方案（推荐，低风险）**：在 scheduler 内引入一个私有辅助
`_torrent_file_or_none()`（局部取引用 + 判空 + try），替换全部句柄访问点。
适用：scheduler 的所有句柄调用。权衡：几行代码、零语义变更；
**不解决** R-3 的语义竞态（那是另一条，需锁）。

---

### R-6【关闭顺序·中】先摘句柄、后关流服务；流服务不等在途请求

**证据**：
- `ui/main_window.py:1026` `session.shutdown()` → **之后** `1028 server.shutdown()`
- `core/session.py:396` `scheduler.stop()` → `429-439` `remove_torrent`
  → `clear_runtime_state()` → `ses_set(None)`
- `core/stream_server.py:425-430` 的 `shutdown()` 只做 `httpd.shutdown()+server_close()`，
  且 `daemon_threads=True`（`413`）⇒ **不 join 在途 handler 线程**
- 在途线程可能在 `_wait_ready` 内循环，上限 `wait_timeout=20s`（`core/stream_server.py:341`）

**实际后果**（已核实为降级而非崩溃）：会话清空后，`_availability` 返回 not ok
⇒ `core/stream_server.py:348-349` 提前 return ⇒ 退化为 503；进程退出时
daemon 线程被强杀，可能留下半截 socket 写。**不产生数据不一致。**

**方案 A（目标态）**：**反转顺序**——先 `server.shutdown()`（并等待在途请求），
再 `session.shutdown()`。
- 适用条件：所有退出路径。
- 权衡：退出总耗时增加，最坏要等在途请求走完 `_wait_ready`；**必须同时**
  给流服务加协作式停止标志（否则会被 20s 上限等满，用户感知为「关不掉」）。

**方案 B（推荐先做，低风险）**：保留现顺序，给 `_wait_ready` 加
`threading.Event`（取消事件），`shutdown()` 时置位使其**立即返回**。
- 适用条件：不想动关闭编排。
- 权衡：改动小、立竿见影；「HTTP 用已清空注册表」的窗口仍在，但窗口内行为是
  安全的 503（已验证），故可接受。

**方案 C**：把流服务与引擎纳入同一生命周期对象统一编排停止顺序。
- 权衡：架构最清晰，改动大，属长期项。

**建议**：B 先落地（低风险），A 作为目标态，二者不冲突。

---

### R-6b【资源争用·低中】shutdown 在锁内做 `fsync` 级写

**证据**：`core/session.py:399-402`——`with d.lock:` 内调 `d.persist_tasks()`，
而 `persist_tasks` → `atomic_write_bytes` 含 `flush + fsync + replace`
（`core/taskstore.py:173-177`）。同文件 `core/fetcher.py:490` 的同类调用
就在锁**外**（注释明写「P1-4：同步 I/O 不在锁内」）。

**后果**：关闭时告警线程（T2）与 HTTP 线程（T5，经 `_dir_index`）可能被这把锁
挡在 `fsync` 期间（毫秒到数十毫秒）。影响面窄（仅退出路径），故等级不高，
但它是**纪律不一致**的明确证据，容易在下一次改动里被复制。

**方案（推荐）**：把 `persist_tasks()` 与 `save_resume_data` 的调用拆成
「锁内快照 → 出锁调用」。适用：`shutdown` 一处即可。权衡：几乎零成本；
`save_resume_data` 是异步入队，出锁后调用同样安全。

---

### R-7【数据不一致·中】缓存清理的 TOCTOU：保护名单只在入口取一次

**证据**：`core/cache_guard.py:119-168`
- `keep_paths` 在**函数入口**由 `keep_dirs` 转换成快照（`120-121`）
- 删除循环（`144-166`）只用这个快照比较，**无逐项复核**
- 对照：`core/cache_quota.py:169-177` 有 C2 逐项复核 —— 同一项目两套做法不一致
- 函数自带文档承认「删除活任务目录 ⇒ 引擎假完成」（`114-117`），
  并称「当前所有 UI 清理入口均以快照规避，故不可达」

**该「不可达」论证的缺口**：它只覆盖「名单里有没有活任务」，
**不覆盖「取名单 → 执行删除」之间新起的任务**。触发链：
清理进行中，T2 完成元数据 ⇒ 新任务（或 preview→convert 转正）落到
`.preview/<ih>` ⇒ 不在快照里 ⇒ 被 `shutil.rmtree(..., ignore_errors=True)`（`159`）
整棵删除。后果正是文档自述的「继续报进度直至 finished、磁盘为空、
零错误日志、不重下」——**用户看到「下载完成但文件没了」**。

**方案 A（推荐）**：把 `keep_dirs` 从**快照集合**改为**回调**
（`keep_fn() -> set`），在删除循环内按**顶层条目**粒度复核（对齐
`core/cache_quota.py` 的做法，不动嵌套语义）。

> **该范式在本仓库内已被验证，不是新设计**：`core/cache_quota.py:164-177`
> 已经实现了 `callable(keep_dirs)` 分支——每次 `rmtree` 前重取名单，
> 且「取名单失败则**中止本轮剩余删除**（保守）」。也就是说 `cache_quota` 与
> `cache_guard` 对同一份保护名单采了两种签名：前者支持回调、后者只吃快照。
> 改造因此从「设计新机制」降级为**统一两处签名**，风险与工作量都低一个档。
- 适用条件：`ui/main_window.py:446`、`ui/main_window.py:1038`、
  `ui/settings_dialog.py:453` 三个入口。
- 权衡：每次清理多抓一次 `reg.lock`（O(任务数)，毫秒级）——可接受；
  `keep_fn` 必须复用 `protected_dirs()` 的「锁内纯计算」实现，
  **不得**在回调里调 libtorrent，否则违反纪律 1。

**方案 B**：清理前暂停全部任务并等落盘静止。
- 适用条件：退出路径（本就在 `session.shutdown()` 之后，风险最低，可不改）。
- 权衡：手动清理时暂停/恢复会打断用户下载，需要状态恢复逻辑；成本高于 A。

**方案 C**：把活任务落盘目录移出可清理范围（例如 `.active/<ih>` 与
`.preview/<ih>` 分离）。
- 权衡：最干净，但改落盘布局会连带影响 `cache_mode_e2e_test`、
  `local_torrent_test` 等夹具与 `plan/06` 的语义。**列为长期项。**

**验收**：用例模拟「清理过程中新注册任务目录」，断言新目录未被删除。

---

### R-8【竞态·低】`release_only` 停止不是原子的（登记，建议不改）

**证据**：`core/fetcher.py:474` 先 `stop_preview(release_only=True)`（内部
`core/scheduler.py:568-599` 清 `handle`/`file`/`_view`），`482-486` 才入锁复核
`rec.download`。两者之间 T5 的 `request_range` 可能命中已停的调度器
⇒ `core/scheduler.py:390` 的 `if not self.active: return` 静默无操作。

**后果**：丢失一次点播 ⇒ 播放器多等一个 50ms 轮询周期或退避重试。**不产生
数据不一致**；转正语义的主要竞态已被 `482-486` 的复核覆盖。

**方案**：若要消除，需引入「正在转正」标志让 `request_range` 早退并在转正后
重新点播。权衡：增加状态机复杂度，收益是「省一次 50ms 重试」。
**建议只登记、不实现**，避免过度设计。**等级：低。**

---

### R-9【资源争用·中】`plan/10` 新引入的资源叠加

**证据**：`core/session.py` 的 `aio_threads=12`、`hashing_threads=4` 是
**每会话**线程数；`recv_socket_buffer_size=2MiB` × `connections_limit=400`
的理论内存上限 ≈ 800MB/会话。测试与探针会并发开 3 个会话
（`diag_throughput.py` 的 T4 段同时开「做种端 + 裸引擎对照组 + `SessionManager`」）
⇒ 瞬时约 48 个 libtorrent 侧线程（3 × (12+4) + 网络线程），再叠加
Qt、告警、HTTP、解析、收尾线程。

**方案 A**：参数改为按核数/会话数计算。权衡：**破坏
`throughput_test.py` 与 `wait_timeout_test.py` 的确定性断言**——本项目明确规定
性能断言必须可复现（见 `plan/10` §8），故除非同时引入「快照基线」机制，否则不采用。

**方案 B（推荐）**：保持静态常量，但**提高可见性**——`diag_throughput.py T1`
已打印核数与单项线程数，建议再补一行「N 个会话 ⇒ N×16 线程」的换算提示，
并在 `plan/10` 的风险节登记低核数机器的下调建议（已登记）。权衡：不改行为，
零风险。

**方案 C**：进程内单例会话（多个 `SessionManager` 共享一个 `lt.session`）。
权衡：彻底消除叠加，但改架构（会话生命周期与 `SessionDeps` 全部要重排）。长期项。

---

### R-10【死锁·低（当前）/ 中（引入 R-3 后）】锁序尚未显式定义

**现状**：今天只有一把全局锁 + 一把叶子锁（`PieceView._lock`），
两者的调用方向是单向的（scheduler → PieceView），**无环**，故当前无死锁。

**唯一已发生过的死锁**：`PieceView.contiguous_from` 持 `_lock` 后调用自持锁的
`rebuild_baseline`（`threading.Lock` 不可重入）——**症状是「测试超时且输出为空」**，
不是报错。修复方式是沿用 registry 的 `*_locked` 拆分纪律。

**引入 R-3 的 `scheduler._lock` 之后的环风险**：
`scheduler._lock → PieceView._lock → reg.lock` 必须**单向**。
若某处写成「先 `reg.lock` 后 `scheduler._lock`」（例如 `core/resolver.py:139-169`
在 `with reg.lock` 内调 `scheduler.stop()`），就会与「先 scheduler 后 reg」的路径
构成环路 ⇒ 死锁。

**方案 A（推荐）**：在 `core/scheduler.py` 顶部用注释**显式写明锁序**，
并在 `core/registry.py` 的锁纪律注释里登记「禁止在 `reg.lock` 内调用
`scheduler` 公开方法」；`core/resolver.py:139-169` 的 `scheduler.stop()`
改为「锁内取快照 → 出锁 stop」。
- 适用条件：R-3 落地的同一批改动。
- 权衡：需要小幅重排 `resolver.begin_resolve` 的锁段，属机械改写。

**方案 B（诊断增强，建议一并做）**：给长跑用例/探针加
`faulthandler.dump_traceback_later(30, exit=True)`。
- 适用条件：所有长跑测试与探针（本项目已有「被超时杀掉会丢缓冲」的教训，
  `sys.stdout.reconfigure(line_buffering=True)` 只解决了输出，不解决栈现场）。
- 权衡：零成本（标准库），但会让进程在 30s 后强退——需按用例设定合适秒数。

---

## 3. 两类「额外风险」（`plan/10` 未覆盖，与本主题强相关）

### R-11【潜在雷·未触发】`on_file_completed` 一旦接线，告警线程会加入调度器写者集合

**证据**：`core/scheduler.py:137` 与 `core/fetcher.py:87` 都把
`on_file_completed` 初始化为 `None`，**全项目没有任何赋值点**
（`grep -rn on_file_completed core/ ui/` 仅命中三处：两个初始化 + 一处读取
`core/session.py:497`）。因此 `core/session.py:497-499` 的分支恒不成立，
**T2 目前完全不触碰 scheduler** —— 这是当前 R-3 只算「中高」而不是「高」的原因。

**风险**：该回调的设计意图是「文件下载完成时让调度器推进窗口」。一旦接线
（例如为 `plan/07` 的连续播放做优化），T2 会立刻成为 scheduler 的第三个写者，
与 T1（UI）、T5（HTTP）并发 ⇒ R-3 从「可能丢一次预约」升级为
「告警驱动的状态机与 UI 驱动的状态机互相覆盖」。

**方案**：把「接线 `on_file_completed`」与「R-3 的调度器加锁」**绑成同一批改动**，
在契约基线（`contract_check.py` / `contract_snapshot.md`）里同时登记。
- 适用条件：任何时候有人要接这条回调。
- 权衡：无额外成本（本来就要改 scheduler）；不这样做则是在引入一个新竞态源。
- **建议**：在 `core/scheduler.py:137` 的注释里补一句「接入前必须先落 R-3 的锁」，
  把口头约定变成代码里的约束。

### R-12【资源争用·低】HTTP 线程读 `path_to_file` 裸字典

**证据**：`ui/main_window.py:175-176`（T5 回调路径）读
`self.path_to_file`；该字典由 T1 在解析/预览切换时重建。

**后果**：CPython 下单个 `dict.get` 是原子的，最坏情况是取到「上一代」映射
⇒ `f is not self._preview_file_getter()` 判空后 return（`176-177`），
**表现为一次点播丢失**（同 R-8 的影响等级）。

**方案**：把 `path_to_file` 与 `_preview_file` 收进一个不可变快照对象，
T1 重建时整体替换引用（copy-on-write）。适用：`ui/main_window.py` 的映射维护点。
权衡：改动面小，但要保证所有读取方都通过同一个 getter（`_preview_file_getter`
已是这个形状，可扩展）。**等级：低，可延后。**

### R-13【跨线程协调·中】`delete_files=1` 的**异步删文件**与流服务读取无任何协调

**证据**：`delete_files=1` 共 4 处调用点——`core/registry.py:273`（同 ih 换句柄）、
`core/resolver.py:149`、`core/resolver.py:160`（`begin_resolve` 换代清旧）、
`core/resolver.py:222`、`core/taskops.py:443`（用户删任务）。
反证：`shutdown` 刻意用 `0` 并在代码里写明理由——
`core/session.py:428`「`remove_torrent` 的 `delete_files=1` 会异步删文件，
shutdown 绝不可用」。**说明项目已经认识到这条性质，但只在退出路径上规避了。**

**机理**：libtorrent 的 delete_files 由**它自己的磁盘线程异步执行**——不返回到
我们的调用栈，也不受 `reg.lock` 约束。删除发起的那一刻，可能同时存在：

- T5（HTTP）正在 `core/stream_server.py:206-210` 用 Python 文件对象逐块读同一路径；
- T1 的播放器正在消费那段字节。

**后果**（两种，第二种需现场确认）：

1. Windows 下「被打开的文件无法删除」⇒ 删除**静默失败**，留下残留目录。
   项目已在 `core/taskops.py:509` 附近处理了「空目录残留」的另一半情形。
2. 若能删成功（无打开句柄），流服务下一次 `open` 得到 ENOENT ⇒ 播放器
   ErrorState ⇒ 走既有退避重试（`ui/main_window._on_stream_failed`）。

**不是数据损坏，是可用性抖动**；但触发点是「解析新磁力链」与「删除任务」这两个
最高频的用户动作。最容易命中的一条：`core/resolver.py:149/160` 的 `begin_resolve`
会删掉**上一代的落盘目录**，而该目录可能正是用户当前预览的 `_current_path`。

**方案 A（推荐）**：删除前先把该路径从流服务的可服务集合里**摘除**，让在途请求
快速失败（返回 416/503，交由既有退避重试），即「先取消订阅，再删」。
落点：`StreamServer` 增加 `retire_path(fp)`（清 `_current_path` 并标记退役），
与删除调用点配对。
- 适用条件：`core/resolver.py:149/160`（换代）与 `core/taskops.py:443`（删任务）。
- 权衡：需要在 UI 层把「删目录」与「通知流服务」串起来；`remove_task` 本就
  是用户主动删除，抖动可接受，可只对 `begin_resolve` 做。

**方案 B**：把删除**延迟到当前预览停止之后**（`stop_preview` 完成再发起
`remove_torrent(..., 1)`）。
- 适用条件：`begin_resolve` 换代路径。
- 权衡：需要「待删队列」，多一处状态；好处是完全不动流服务接口。

**方案 C**：删除前 `pause()` 并等待在途读结束——libtorrent 无此 API，
**仅登记不采用**。

**方案 D（最低成本）**：把 `delete_files=1` 改为 `remove_torrent(..., 0)`
（只摘句柄），目录删除交给既有的 `cache_guard` / `cache_quota` 设施在清理时机处理。
- 权衡：语义变化——删除不再即时。但换来的是「删文件」这件事被收敛进
  **已经有保护名单与逐项复核**的清理设施（见 R-7），一致性更好。
  需先确认 `.preview/<ih>` 会被既有清理覆盖（`cache_guard.CLEANUP_KEEP` 不含
  `.preview`，可覆盖到）。
- 代价：`remove_task` 的用户预期是「删了就没了」，需在 UI 上给出「已停止，
  缓存稍后清理」的措辞。

**验收**：用例——在预览播放中触发 `begin_resolve` 换代 / `remove_task`，
断言流服务要么继续服务旧路径（延迟删），要么返回可重试状态码且播放器走既有
退避重试，**不出现**未捕获异常、半截文件或「磁盘为空但报完成」。

### R-14【新引入·低中】唯一临时名带出的孤儿文件（2026-09-17 第 1 批复审发现）

**现象**：R-1 把临时名从固定 `<目标>.tmp` 改成 `<目标>.<pid>.<tid>.<序号>.tmp` 之后，
**进程崩溃/被强杀**留下的残留不再会被「下次写同一目标」覆盖（旧固定名每目标最多
留一个），而是**每次崩溃新增一个孤儿**。

**影响面**：
- `.resume/<ih>.<...>.tmp` —— `core/taskops.py:479-490` 只删 `<ih>.fastresume`，
  `core/cache_guard.py:27-29` 整目录保留 `.resume` ⇒ **无人清理**；
- `.meta/` —— 会被按 mtime 的 LRU 当最旧条目回收（自愈）；
- `.tasks.json.<...>.tmp` —— 与目标同目录，重启后无清理路径。

**严重度**：低（只影响磁盘垃圾，不影响正确性），但**无界累积**且是本批**新引入**的
行为差异 ⇒ 必须登记。

**方案 A（推荐）**：在**启动时**（`SessionManager.start()`）或 `cache_guard` 的清理
路径上，对 `.meta/`、`.resume/`、`cache_dir` 各做一次 `*.tmp` 清扫。
- 适用条件：启动时无并发写者，是最安全的清扫点。
- 权衡：启动多一次目录扫描（`.meta` 可能数千条目，毫秒级）。
- **明确不做**「写前扫目录清同目标旧临时文件」：那会把目录扫描挪回写路径，
  与 `plan/08` D3-b 已修的缺陷（「30 次写入触发 30 次全目录扫描」）直接冲突。

**归属**：第 3 批（与 R-7 / R-13 的清理路径同批，共用 `cache_guard` 入口）。

### R-15【数据不一致·低中】读写同路径：`os.replace` 撞 WinError 32（第 1 批复审发现）

**现象**：Windows 上 `os.replace`（`core/taskstore.py:225`）的**目标**文件若正被读
句柄打开（`open(path, "rb")` 不共享删除），`MoveFileEx` 抛 `WinError 32`。
R-1 的负向对照只测了**写-写**冲突，未覆盖写-读。

**现实窗口**：`load_tasks`（启动时读 `.tasks.json`）与 T2 的写入；`metacache.read_meta`
与写入；`read_resume` 与写入。读窗口很短，故概率远低于 R-1。

**后果**：该次写入失败 → `persist_tasks` 的 `except` 吞成一行告警 → **丢失一次落盘**
（与 R-1 后果同级，概率低得多）。

**方案 A（推荐）**：给 `os.replace` 包**有界重试**（3 次，退避 2/10/30ms），只重试
`PermissionError`；仍失败则照原样上抛。
- 适用条件：`atomic_write_bytes` 单点改动。
- 权衡：只动失败路径，成功路径零成本；最坏把一次落盘延迟 ~40ms（远小于「直接失败」
  的代价）。注意**不得在持 registry 锁时重试**——`atomic_write_bytes` 不持该锁（✓），
  但 `persist_tasks` 的调用方必须仍遵守「锁外落盘」（§E 已机械守住）。
- 待办：**本机未复现该窗口**，实施前先写一个「读句柄持有 + 并发 replace」的
  最小复现用例确认；复现不出来就只登记不实现。

**归属**：第 1 批收尾补丁或第 2 批（改动小，但需先复现）。

### R-16【残余·低】第 2 批复审枚举的未加锁外部访问（登记不修）

调度器加锁后，仍有一批**外部直接访问** `scheduler` 字段的代码（复审全仓枚举）：

| 位置 | 访问 | 后果 |
|---|---|---|
| `core/fetcher.py:463,471` | 持 `reg.lock`（**不是** scheduler 锁）读 `.handle/.file` | 快照可能错配；`473` 有 None 守卫 |
| `core/fetcher.py:532-534` | 读 `.active/.file/.handle`，出锁后才 `torrent_file()` | 不抛 |
| `core/fetcher.py:549`、`core/preview.py:328-330,339` | 裸读 `.piece_view/.handle/.file` | 不抛（339 已局部快照） |
| `ui/main_window.py:867` | 读 `.active`（bool） | 无 |

**判定**：全部是**只读**且低风险（最坏退一拍，被 700ms 轮询吸收），不值得为此给
每个读点加锁。**残余条件**：若将来新增「外部**写**调度器字段」的路径，必须改走
公开方法（自带互斥）——`on_file_completed` 的接入约束注释已写明同类要求。

**另**：诊断/测试基础设施会**绕锁直写**调度器字段（`diag_wait_timeout.py:340-342,
358-359,418-424,462-464,788-795`、`concurrency_test.py:388-393,449,803-804`）——
它们**非线程安全**，**不得作为并发基线**（复审要求登记）。

### R-17【护栏·已修】AST 扫描器必须同时认「装饰器锁」与「显式 with 锁」

第 2 批用 `@_exclusive` 装饰器加锁后，`concurrency_test.py §F` 的静态红线扫描器
（只认 `with self._lock`）**对 4 个装饰器入口完全失效**——护栏恰好在它要守的
新机制处是空的。已修：扫描器把 `@_exclusive(...)` 装饰的方法体**整个**视作锁内
（现覆盖 7 个入口的全部 6 处 + 装饰器 4 处），并把灵敏度自检升级为
「装饰器锁 1 处 + `with` 锁 1 处 + 自由 1 处不算 = 命中 2」。
**教训：给加锁机制换形态时，必须同步升级守它的静态检查，否则红线自动失效。**

---

## 4. 已核查、未发现问题（留档，避免重复排查）

| 项 | 结论 | 证据 |
|---|---|---|
| HTTP 并发额度泄漏 | **未发现**：非阻塞 `acquire` + `try/finally: release` | `core/stream_server.py:225-232` |
| HTTP 等待循环持锁睡眠 | **未发现**：`time.sleep(0.05)` 不在任何 registry 锁内；`_dir_index` 只短暂持锁 | `core/stream_server.py:367`、`core/preview.py:120-122` |
| 日志并发写 | **未发现**：`logging` + `RotatingFileHandler` 自带锁，轮转也在锁内 | `core/logutil.py:77-81` |
| Qt 跨线程访问控件 | **未发现**：core 侧零 Qt 引用，后台回调统一经 `_Bridge` 信号投递 | `ui/main_window.py:52-55, 245-246, 274-275` |
| 元数据剪枝与写入互斥 | **部分**：`_PRUNE_LOCK` 只护节流状态，`prune_meta` 删除无锁；但因按 mtime 淘汰「最旧」，刚写入的文件不会被删，且 `.meta` 有内容校验自愈 ⇒ 后果仅为缓存未命中 | `core/metacache.py:428-479`、`344,356` |
| `_path_index` 锁外赋值 | **已知且可接受**：幂等重建，同 `struct_version` 下内容等价，CPython 元组赋值原子 | `core/preview.py:133` |
| `requests`/句柄身份校验 | **未发现**：`PreviewCore._view_for` 按 `hash_key` 缓存并校验句柄身份 | `core/preview.py:116-134` |

---

## 5. 建议的实施顺序（按「风险 × 成本」排序）

| 批次 | 内容 | 理由 |
|---|---|---|
| **第 1 批**（低成本、高收益、互相独立） | R-1 方案 A（唯一 tmp 名 + per-target 锁，照搬安卓实现）；R-2 方案 A（锁内快照）；R-5（`_torrent_file_or_none`）；R-6b（shutdown 出锁落盘） | 四处都是局部机械改写，不动公开签名，`contract_check` 不破；直接消灭「任务列表整体丢失」这条高等级风险 |
| **第 2 批（✅ 已落地，2026-09-17）** | R-3 方案 C（防御式局部引用 + try，随第 1 批先落）→ R-3 方案 A（**调度器加锁**）+ 显式锁序；R-10 方案 A（`resolver` 锁段重排 + 锁序文档）；R-11（`on_file_completed` 接入约束注释）。**R-10 方案 B（faulthandler）未做**——低价值，随第 3 批再议。<br>**实现方式与原计划不同（复审后确定）**：给 `core/scheduler.py` 加了**方法级装饰器** `_exclusive(blocking=)` + 自有锁 `_lock`，**方法体几乎不动**（小 diff = 易复审）；`functools.wraps` 保住原签名，`contract_check` 184 项不破。热路径 `tick`/`request_range` 用**非阻塞**取锁（拿不到就跳过本拍，永不排队）；`rearm`/`stop` 阻塞；`begin`/`seek_to_byte` 只把「状态赋值区间」包进 `with self._lock`（尾部 `self.tick()` 必须在锁外，否则非阻塞取锁必然失败 ⇒ 首窗静默丢失）。锁内会执行 libtorrent 绑定调用（非阻塞状态变更），已在 docstring **例外登记**（实测最坏 1ms / 退化路径 10ms）。 | 唯一有设计含量的一批；新增 §F 56 项（静态锁序红线 + `begin` 首窗预约护栏 + 确定性反例自检 + 三线程压力 1.2s/13.97 万次换代） |
| **第 3 批（✅ 已落地，2026-09-17）** | R-6 方案 B（`StreamServer` 停止事件：`shutdown()` 置位，在途 `_wait_ready` 立即交出就绪前缀，不再耗满 20s 上限）；R-7 方案 A（`cache_guard.clear_cache_contents` 的 `keep_dirs` 支持**回调**，顶层逐条复核，名单源失败抛 `RuntimeError` 保守中止；**手动清理传回调、退出清理保持快照**——两路径的时机语义不同，勿混）；R-14 方案 A（`cache_guard.sweep_tmp_files`：启动时清扫 `.meta/.resume/根` 三处过期 `*.tmp`，带 60s 新鲜度护栏，**不在写路径上扫**）；R-15 方案 A（`taskstore._replace_with_retry`：`os.replace` 对 `PermissionError` 有界重试 2/10/30ms，先做最小复现确认 Windows 行为）；R-13 **范围收窄**（分支分析证实 `begin_resolve` 的 `delete_files=1` 只作用于**无 result** 的任务 ⇒ 不可能是正在流服务的那个；唯一用户可见的窗口是 `remove_task` 边播边删，且它已先 `sched.stop()` ⇒ 降级为「在途响应读到半删文件 → 播放器重试」，**登记不修**）。**R-10 方案 B（faulthandler）未做**——低价值，随第 3 批再议。<br>新增护栏 `concurrency_test.py §G`（68 项全套）：G1 清扫（过期删/新鲜留/非 tmp 不动/守卫拒非受管目录）、G2 名单回调（**中途新增的活任务被逐条复核保住** + 名单失败保守中止）、G3 重试（PermissionError 被 3 次 `os.replace` 吸收 + 耗尽后上抛且旧文件完好）、G4 停止事件（shutdown 后在途请求 ≤6s 返回而非 20s） | 关闭/清理/删除路径；`gui_feature_test` 273 项同步过 |

### 第 3 批过程中的环境修复（顺带，与本专项无关但必须记录）

`regression_run.py` 的子进程**未设 UTF-8** ⇒ 任何套件里含非 GBK 字符（如
`gui_feature_test.py:1741` 的 `'−'`）的 `print` 都会以 `UnicodeEncodeError`
当场杀掉整个套件，且症状是「某套件莫名提前退出」，与被测代码无关、极难排查
（本项目 CI 已在 a22d264 修过同一类问题，本地 runner 漏了）。已按 CI 同款给
`run_one` 的子进程显式设 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`。
修复后 `gui_feature_test` 273 项全过（此前 7.1s 即崩）。
| **第 4 批（先量后改）** | 跑 `diag_wait_timeout.py D4-a` 拿锁持有 P99；据此决定 R-4 方案 A 的覆盖范围 | 没有数字就不动全局锁的调用形态 |
| **长期/不推荐** | R-2 方案 B（不可变快照）、R-3 方案 B（Qt 亲和）、R-4 方案 C（细粒度锁）、R-7 方案 C（落盘布局）、R-9 方案 C（单例会话）、R-8（明确不改） | 收益明确但重构面大，需在架构层面统一决策 |

---

## 6. Android 端对照审计（2026-09-17 追加，独立审计代理完成）

**回答用户的问题「安卓端有没有一样的问题」：有 —— 部分缺陷与桌面同源，
部分安卓反而先修好了，另有一些是 JVM 特有的新形态。**

### 6.1 线程模型（与桌面 6 线程结构不同）

| 线程 | 创建点 | 触碰的共享状态 |
|---|---|---|
| libtorrent4j 告警线程 | `BtEngine.kt:453,473`（addListener）→ `onAlert:849`、`onMetadataReceived:922`(@Synchronized)、`writeResumeFromAlert:1136`（**落盘**） | taskMap/taskLock、cacheRoot、resume/.meta 落盘 |
| UI 主线程 | `MagnetViewModel.kt:1814-1879`（`previewTick:1823`、`syncFromEngine:1825` 直接在主调度器跑）；`DownloadService.kt:49-87` → `EngineHub.tick:90` → `engine.tick:2534` | **scheduler 写者**、StateFlow、tasks |
| IO 协程 | `withContext(Dispatchers.IO)`（`MagnetViewModel:1333,1380,599`；shutdown `:1892`） | scheduler begin/stop/pause/resume/seek、taskMap、tasks |
| HTTP 池线程(8) | `StreamServer.kt:69` + accept `:78-93` → `waitForRange:231` → `demand:2625` → `scheduler.requestRange:303` | **scheduler 写者** |

⇒ **安卓的 PreviewScheduler 同样是三个写者**（主线程 tick / IO 协程 begin·stop·seek /
HTTP 池 requestRange），与桌面 R-3 的结构**完全同源**。

### 6.2 逐条对照（桌面 17 条 → 安卓）

| R | 安卓状态 | 证据与要点 |
|---|---|---|
| R-1 | **已修**（且是**实测发现后修的**：`TaskStore.kt:188-189` 自记「rename 失败」现象） | 全部写盘点走 `atomicWriteBytes`（TaskStore:237 / MetaCache:107 / ResumeStore:45），grep 无裸写；**遗留**：`*.tmp` 崩溃孤儿**无清扫**（MetaCache.prune:126 只滤 `*.torrent`）⇒ R-14 在安卓**同样存在** |
| R-2 | **部分存在** | 落盘侧安全（`upsertTask:303` copy-on-write + CONFLATED 通道单协程串行写 `:390-409`）；**内存 `tasks:329` 是普通 var**，告警线程（`onTaskStateChanged:284,296`）与主/IO（`syncFromEngine:828`、`markStateFor:1084`）无同步读改写 ⇒ 丢更新 + 可见性，下轮轮询自愈 |
| R-3 | **存在（中）** | `PreviewScheduler.kt:38-56` 全 plain 字段、全文无锁；写者三方（见 7.1）；`scheduledTo/playFrom` 读改写非原子（`:332-340,357-363`）；缓解：句柄调用全过 `usable():62`+`safe():411` 全量 try ⇒ 桌面 R-3-1 的「裸调用抛异常」形态**不存在**，余下是竞态语义 + JVM 可见性 |
| R-4 | **存在（低中）** | `@Synchronized onMetadataReceived:922` 在引擎监视器内跑 `saveMetadataCache:981`（fsync 级）+ 句柄调用；`@Synchronized restoreTasks:1264` 监视器内整段 native addTorrent；fsync 期间告警堆积（queue 5000:449 兜着）；taskLock 是叶子锁、锁内零外调（`applyQueueOrder:1885-1897`），**无环无死锁** |
| R-5 | 窄窗口，登记不修 | `isValid` 三道门（`isHandleUsable:2642`、`TorrentHandleOps.guarded:191`、`usable:62`）；残余 isValid→调用间 TOCTOU（`taskSnapshots:1703-1718`）⇒ 理论 SIGSEGV，窗口极窄 |
| R-6 | **不存在** | `onCleared:1888` 先 `server.close()`（`shutdownNow:99` 中断 `waitForRange:249` 的 sleep → 立即 504，**等价于桌面方案 B 的协作停止**），后引擎停机；顺序正确 |
| R-7 | **不存在（先于桌面已修）** | `CacheQuota.kt:264-276,361-372` keepProvider 回调 + 删除前逐条复核 + 失败保守中止 —— 正是桌面第 3 批照抄的 C2 模式 |
| R-10 | 不存在 | 锁少且单向（engine 监视器 → taskLock，`:1031`），无环 |
| R-11 | 未接线 | 告警线程**不触碰** scheduler（`onAlert:849-914` 无引用）——与桌面同款潜在雷，接入前必须先加锁 |
| R-13 | 部分，登记不修 | `removeTask:2041` 边播边删，但已先 `:2030 sched.stop()`；残余=在途响应读到半删文件 → 播放器重试 |
| R-15 | 不适用 | Android/Linux 无 WinError 32 形态；`Files.move` 回退链 `:223-229` 无重试也无此需要 |
| R-16/R-17 | 不适用 | 无桌面式锁绕过问题、无扫描器 |

### 6.3 JVM 特有坑（桌面没有的新形态）

1. `tasks:329` / `currentFiles:316` 跨线程 plain var 无 `@Volatile`（同 R-2，中低）；
2. scheduler 全字段无 volatile（同 R-3）；
3. `watchdog Job:317` 在告警线程 cancel（`:229,265`）与主线程 launch（`:1341`）并发 —— Job 非线程安全（低）；
4. `taskSeq` 非原子：`tempTaskKey:1495` 未同步自增 vs `registerTask:1518` 锁内自增，并发解析可能撞号（低）；
5. 良好面：EngineHub 全 `@Volatile`(72-78)、AppLog `@Volatile`(27,39)、continuationHashes 并发集合、**无 SimpleDateFormat**。

### 6.4 结论与建议的「Android 修复批」（待用户确认后开批）

**必修三条（一个小批）**：
1. `PreviewScheduler.kt` 加锁 —— **照搬桌面第 2 批方案**（自有锁 + 方法级互斥 +
   热路径非阻塞 + begin/seek 锁段不含尾部 tick + JVM 侧补 `@Volatile`）；
2. `MagnetViewModel` 的 `tasks`/`currentFiles` 写线程收敛到单协程 + `@Volatile`；
3. `BtEngine` 监视器内的 `saveMetadataCache`（fsync）与 native 句柄调用**出锁**
   （锁内取快照，对齐桌面 R-4 的判断）。

**随批顺带**：启动时 `*.tmp` 清扫（照搬桌面 `sweep_tmp_files`，清 TaskStore/MetaCache
两处）；`taskSeq` 改 `AtomicInteger`。

**明确不修（登记即可）**：R-5 TOCTOU、R-6 关闭顺序、R-13 边播边删、R-15。

**验证方式与限制**：JVM 单测（`:app:testDebugUnitTest`，现 276 项）+ 新增调度器
互斥护栏；`assembleRelease` 重建 APK。**限制**：引擎 native 行为本就未经真机/
模拟器验证（`android/ENV.md`），调度器加锁的**真机行为**需一次真机冒烟
（本项目未装模拟器），须在交付说明中明示。

---

## 7. 与既有文档的关系

- `plan/08` / `plan/09`：等待超时与门控成本——**已完成的**并发优化
  （`PieceView` 把 O(块数) 绑定调用降为摊销 O(1)，本质是减少持锁/绑定调用的频次）。
- `plan/10`：千兆吞吐专项——本文 §R-9 补充它引入的资源叠加风险，
  §R-4 补充「锁内句柄调用」对吞吐的间接影响（锁车队同样是吞吐杀手）。
- 本文：**并发正确性**视角的补充，不改 `plan/10` 的任何结论。
