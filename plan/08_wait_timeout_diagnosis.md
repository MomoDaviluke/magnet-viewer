# 等待超时专项：根因定位、排查手册与优化落地

> 2026-09-17 · 对象：`magnet-viewer`（Python + libtorrent 2.1.1 + PySide6）
> 症状关键词：**索引块 / 头部数据等待超时**
> 伴生交付：`diag_wait_timeout.py`（常驻诊断探针）、`wait_timeout_test.py`（回归护栏，58 项）

---

## 0. 结论摘要

「等待超时」不是网络下行慢造成的，而是**自身可用性探测链的开销与判据缺口**造成的。
真正的等待上限（流服务 `_wait_ready` 的 20s）之所以被打满，是因为**门控判据长时间
算不出来或算得很慢**，而门控与探测恰好都跑在 GUI 线程与 HTTP 线程这两个最敏感的
位置上。

| # | 根因 | 量化证据 | 影响 |
|---|---|---|---|
| RC-1 | 一轮 UI 轮询取 **4 次** `handle.status()` + 4 次位图物化（O(块数)） | 单次物化 ≈20ns/块：2万块 0.20ms、10万块 1.38ms、**40万块 7.89ms**；4 次 = 31.5ms | 全部落在 **GUI 线程**；40万块占 700ms 轮询 4.5%，百万块量级直接卡界面 |
| RC-2 | 流服务可用性探测 50ms 一次，旧实现 O(任务数 × 文件数) + 每次抓任务锁 | 实测 253µs/次；16 并发 × 20Hz = **8.1% 单核**，并与 GUI/告警线程争锁 | 16 并发长时间等待时成为拥塞源 |
| RC-3 | `tick()` 里 `_first_missing_from` 逐块 `have_piece()` 绑定调用（P2-1 漏网） | 实测 501 次绑定调用（3.76µs/次 ≈ 1.9ms）；块数越多越糟 | GUI 线程每 700ms 一次，且随下载进度恶化 |
| RC-4 | `.meta` 缓存**不校验内容 info_hash 与文件名一致** | 探针 D3-a：错配条目被当**命中**返回 | 正确性缺陷 + 缓存投毒面：按错误元数据建文件树、算分块几何 |
| RC-5 | 混合 v1+v2 种子的 `info_hash()` **≠ v1**，缓存只落一个键 | 本环境实测 `info_hash()=5a678b08…`，而 `info_hashes().v1=1ffc12d5…` | **命中率缺口**：v1 btih 的磁力链必然 miss，白跑一次 DHT + ut_metadata |
| RC-6 | 每次元数据写入都全目录 `scandir + stat` 剪枝 | 探针 D3-b：30 次写入 → **30 次**全目录扫描 | 开销挂在「元数据到达 → 出文件树」这条最敏感链路上 |
| RC-7 | 播放失败重试是线性退避且**第 4 次后放弃**，失败时不催数据 | `_on_stream_failed` 旧逻辑 `attempts >= 4: return` | 慢链路下数据只差几秒就位，用户已看到终态错误 |
| RC-8 | `STALL_POLLS` 注释与实际名义时长不符（把轮询自身开销算进了注释） | 注释「≈1.5s」，实际 `10 × 0.05s = 0.50s` | 文档失真，掩盖了 RC-2 的存在（这条恰好是 RC-2 的旁证） |

**修复后对照**（同一探针、同一量）：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| 单轮 UI 轮询 `handle.status()` 次数（mp4 预览路径） | **4** | **1** |
| `tick()` 逐块 `have_piece()` 绑定调用 | 501 次（随块数线性增长） | **0** |
| 路径反查（2 任务 × 2000 文件） | 41.5µs + 每次抓任务锁 | 36.5µs + **零锁**（索引未变时） |
| `.meta` 内容错配 | 被当命中返回 | 识别 → 删除 → 计 `mismatch` |
| 混合种子 v1 btih 查缓存 | 未命中 | **命中**（3 个别名键） |
| 30 次元数据写入的目录扫描 | 30 次 | **0~1 次** |
| 播放失败退避 | 1.5s / 3.0s / 4.5s / 6.0s → **放弃** | 0.8 / 1.6 / 3.2 / 6.4 / 12s → **不放弃**，且每次失败主动催数据 |
| 回归 | 21 套 | **22 套**（新增 `wait_timeout_test` 58 项） |

---

## 1. 术语对齐（先消除歧义）

| 说法 | 在本代码库里的确切对象 |
|---|---|
| **索引块** | 容器格式的索引 atom：MP4/MOV 家族的 **`moov`**（旧注释与 UI 文案通称「索引块」） |
| **头部数据** | 文件**头部连续可读前缀**（`contiguous`，播放器能顺序消费的量） |
| **等待超时** | 两处上限：① 开播门控（无限等，只显示「缓冲中，等待数据与索引块就绪」）；② 流服务 `_wait_ready`（`wait_timeout=20s`，到点回 416/503 → **FFmpeg 判致命错误**） |

**症状到链路的映射**（`ui/main_window.py::_refresh_status`，700ms 轮询）：

```
          ┌─ data_ok  = contiguous >= min(1MB, size)      ← 头部数据
门控 ─────┤
          └─ index_ok = tail_entry_ready()                ← 尾部 moov 入口（最末 min(2MB,size) 覆盖块）
                 │
        两者都真 ─┴─→ preview.video.set_stream(url)  否则 → set_waiting_stage(WAIT_DATA|WAIT_INDEX)
```

`moov` 在普通 mp4 的**最末尾**，所以「等索引块」实质是等**最后一个 piece**。
`Edit`→`moov` 就绪后 FFmpeg 才会发尾缀 Range 探测，此时流服务必须已在等，否则
416/503 直接把它打成 `ErrorState`。

---

## 2. 系统化排查步骤与诊断方法

一把常驻探针：**`diag_wait_timeout.py`**，四段独立可跑，零外网依赖，逐项给判据与阈值。

```bash
.\.venv\Scripts\python.exe diag_wait_timeout.py          # 全部（约 3s）
.\.venv\Scripts\python.exe diag_wait_timeout.py D1 D2    # 只查探测链
```

### D1 可用性探测成本（网络延迟之外的第一嫌疑人）

```bash
.\.venv\Scripts\python.exe diag_wait_timeout.py D1 D1S
```

| 判据 | 阈值 | 命中含义 |
|---|---|---|
| D1-a 单轮轮询 `status()` 次数 | > 1 即异常 | 快照链冗余，位图物化被重复付 |
| D1-b `find_record_for_path` 单次 | > 200µs | 路径反查成了热路径瓶颈（O(任务×文件)） |
| D1-c `status()+位图物化` 单次 | > 2ms | 位图规模过大，O(块数) 成本显形 |
| D1-d 流服务探测 16 并发占核 | > 10% | HTTP 侧拥塞源 |
| D1-e 单轮 4 次物化占 GUI 线程 | > 10% | GUI 卡顿与门控抖动 |

**方法论要点（踩过的坑，务必照做）**
1. **必须用真句柄量 `status()`**。`torrent_status.pieces` 是 libtorrent 的 **bitfield**，
   其 Python 物化成本只有真绑定才体现。本探针第一版用假句柄（Python list）把这项
   测成 0.009ms，得出「快照很便宜」的**错误**结论，改用真句柄后是 0.20ms（差 20 倍）。
2. **必须做规模外推**。20ns/块在 2 万块时只有 0.2ms，看不出问题；`D1S` 用
   `create_torrent.set_hash()` 造虚拟种子（零磁盘 IO）扫 2万/10万/40万块，才看出
   线性增长到 7.9ms/次。

### D2 门控判据拆解（头部连续前缀 vs 尾部 moov 入口）

```bash
.\.venv\Scripts\python.exe diag_wait_timeout.py D2
```

| 判据 | 阈值 | 命中含义 |
|---|---|---|
| D2-b 热路径 `gate_snapshot()` | `status()` 必须恰 1 次 | 未折叠 → RC-1 |
| D2-b' 传入外部 `have` 后 | 必须 0 次 | 未复用调用方快照 |
| D2-c 共享快照路径的 `have_piece` | 必须 0 次 | P2-1 漏网（RC-3） |
| D2-d `STALL_POLLS` 注释与名义时长 | 必须一致 | 文档失真往往正是漏掉的缺陷 |

**判据拆解手法**：把「等索引块」拆成 `tail_entry_ready()`（只判最末 `min(2MB,size)`）
与 `tail_ready()`（判整尾窗）两个量分别计时——历史缺陷正是**门控用错了那个**
（等整尾窗 = 4.1GB 上 44MB，2.4MB/s 下 ≈18s，永远等不到）。

### D3 元数据缓存命中链

```bash
.\.venv\Scripts\python.exe diag_wait_timeout.py D3
```

| 判据 | 阈值 | 命中含义 |
|---|---|---|
| D3-a 内容 info_hash 与文件名一致性 | 必须识别 | 缓存错配/投毒（RC-4） |
| D3-c 混合种子用**真 v1 btih** 查缓存 | 必须命中 | 命中率缺口（RC-5） |
| D3-b 30 次写入触发的目录扫描 | ≤ 2 次 | 剪枝挂在写入热路径上（RC-6） |

**方法论要点**：探测 `info_hash() ≠ v1` 这条事实时，不要假设「40 位就是 v1」——
本环境 libtorrent 2.1.1 对混合种子返回的是 **v2 哈希截断到 20 字节**。
必须逐字段打印 `info_hashes().v1 / .v2` 与 `info_hash()` 三者比对。

### D4 线程阻塞与吞吐参数

```bash
.\.venv\Scripts\python.exe diag_wait_timeout.py D4
```

| 判据 | 阈值 | 命中含义 |
|---|---|---|
| D4-a 注册表锁持有时间 | > 5ms | 告警线程/HTTP 线程被阻塞 |
| D4-b 吞吐参数「存在但未生效」 | 必须为空 | 拼错的键会被 libtorrent **静默丢弃** |

**方法论要点（同一类坑）**：D4-b 第一版用**裸会话** `lt.session({...})` 读回设置，
于是所有键都显示「未生效」——那只是默认值。必须用应用自己的
`build_session_settings()` 建会话才能验真实生效性。

### 常规排查清单（探针之外，按顺序）

1. **分清「谁在等」**：门控（UI，无限等）还是流服务（HTTP，20s 硬上限）。
   看日志 tag：`fetcher.timeout.pause`（元数据超时）/ `stream.availability.*` / `scheduler.*`。
2. **网络延迟**：`status().num_peers / num_seeds / download_payload_rate`。
   peer 数为 0 → 是发现层问题（DHT/tracker/端口），与本次缺陷无关。
3. **线程阻塞**：给 `registry.lock` 打点（`threading.Lock` 包装计时），或直接看
   D4-a。锁持有超过毫秒级即会与 700ms UI 轮询、150ms 告警循环互相拖累。
4. **数据读取与解析**：`piece_map_for_path` 必须**可判定**为「不可用」，绝不
   降级为整文件可用（否则喂稀疏零数据）。任何「反查未命中」都要留痕
   （`main_window.pieces_map.miss`）。
5. **元数据获取**：区分「网络慢」与「缓存未命中」。`metacache.stats()` 的
   `hits/misses/mismatch/corrupt` 一眼可辨。

---

## 3. 优化落地（代码位置与机制）

### 3.1 并发效率

| 文件 | 改动 | 机制 |
|---|---|---|
| `core/scheduler.py` | 新增 `gate_snapshot(have=None)` | 把 `contiguous / tail_ready / tail_entry_ready` 折叠为**一次**位图快照；可选 `have` 让调用方把已握有的快照传进来，做到**零额外绑定调用** |
| `core/preview.py` | `PreviewCore.status()` 改用 `gate_snapshot`，并把 `s.handle is handle` 时的 `s.pieces` 直接作为共享快照传入 | 一轮 UI 轮询 4 次 `status()` → **1 次**；句柄不一致时（后台任务与预览目标不同）退回自取快照，宁可多一次拷贝也绝不错用别的种子位图 |
| `core/scheduler.py` | `contiguous_progress / tail_ready / tail_entry_ready` 新增**可选** `have` 参数 | 单独调用时行为与基线逐字一致（契约自检已同步基线） |
| `core/scheduler.py` | `tick()` 内取一次快照供尾窗判定与首个缺失块定位复用 | 少一次绑定调用 |
| `core/scheduler.py` | `_first_missing_from(from_piece, have=None)` 改走位图 | P2-1 的**对称**修复；无 `status()` 的替身（`smoke_test._FakeHandle`）自动退回逐块，行为不变 |
| `core/registry.py` | 新增 `struct_version` / `_bump_struct_locked` / `drop_record_locked` | 结构代次：记录集合变化即失效 |
| `core/preview.py` | `_dir_index()` + `find_record_for_path` 沿路径**逐级向上**查索引 | O(任务数 × 文件数) → O(路径深度)；索引未变时**不抓任务锁** |
| `core/resolver.py` | 换代丢弃 review 任务改走 `reg.drop_record_locked` | 避免绕过结构代次 |

### 3.2 重试容错

| 文件 | 改动 | 机制 |
|---|---|---|
| `ui/main_window.py` | 播放失败退避改**指数**：`0.8/1.6/3.2/6.4/12s`，封顶 12s | 常量 `STREAM_RETRY_BASE_MS / MAX_MS / SOFT_LIMIT` |
| `ui/main_window.py` | **不再硬性放弃**：超过软上限只提示一次，重试链继续跑 | 旧逻辑 `attempts >= 4: return` 会让「差几秒就绪」变成终态错误 |
| `ui/main_window.py` | 新增 `_rearm_download()`，每次失败先**催数据** | 把「等数据」变「催数据」 |
| `core/scheduler.py` | 新增 `rearm()` | 与 `tick()` 的区别：**没有**「窗口充裕即返回」的防抖——无条件重刷尾部入口与播放位置窗口，并保持有界（不刷全文件） |

### 3.3 缓存命中率

| 文件 | 改动 | 机制 |
|---|---|---|
| `core/metacache.py` | 新增 `info_hash_of()` / `alias_keys()` | 同时给出 `info_hash()` / `v1` / `v2` 三个可查键 |
| `core/metacache.py` | `save_torrent_info` 同时落盘**别名键** | 混合种子的 v1 btih 与 v2 截断值都能命中（元数据仅 KB 级，多写 1~2 个文件） |
| `core/session.py` | `THROUGHPUT_SETTINGS` 追加 5 项（16 项，全部经真会话回读验证） | `aio_threads=8`（稀疏随机写并发）、`hashing_threads=2`（**校验堆积会直接压住「连续前缀」增长**）、`piece_extent_affinity=True`（同 extent 亲和 → 顺序性）、`max_out_request_queue=1000`（填满高延迟链路的 BDP）、`max_failcount=5`（纯容错增益） |

### 3.4 失效策略

| 文件 | 改动 | 机制 |
|---|---|---|
| `core/metacache.py` | `load_torrent_info` 命中即**校验**内容 info_hash，不符则删除 + 计 `mismatch` | 自愈 + 防投毒；最坏情况只是回退一次网络获取 |
| `core/metacache.py` | 新增 `invalidate_meta(cache_dir, info_hash)` | 显式失效入口（清缓存 / 重建任务 / 改 tracker 后强取），幂等且便宜 |
| `core/metacache.py` | 新增 `maybe_prune()`：写入路径**节流**剪枝 | 首次/每 60s 重扫一次计数，其余按写入量递增；只有可能超限才真扫。`PRUNE_RESYNC` 兜住外部直接写 `.meta` 的计数漂移 |
| `core/stream_server.py` | 订正 `STALL_POLLS` 注释 | 名义 0.50s（旧注释「≈1.5s」把缺陷本身当成了正常开销） |

---

## 4. 已验证结果

```
$ .\.venv\Scripts\python.exe diag_wait_timeout.py
  D1  含尾部 moov 窗口时单轮 status()      : 1 次        （基线 4 次）
  D2  [热路径] gate_snapshot()            : status() 1 次
  D2  [热路径] gate_snapshot(外部 have)   : status() 0 次
  D2  _first_missing_from（共享快照）      : have_piece 0 次（基线 501 次）
  D3  .meta 错配识别                       : 是
  D3  真 v1 btih 查缓存                    : 命中（3 个别名键落盘）
  D3  30 次写入触发目录扫描                : 0 次        （基线 30 次）
  D4  16 个吞吐键存在且生效                : 是
  === 未发现超阈项。===
```

回归：`regression_run.py` **22 套**（新增 `wait_timeout_test`，58 项），详见运行日志。

---

## 5. 残余风险与后续建议

1. **流服务探测仍每轮物化一次位图**（实测 253µs @2万块，16 并发 ≈8% 单核）。
   当前判定为可接受。若真机在「多任务 + 长时间等待」下出现 CPU 竞争，再引入
   **有界 TTL 的位图快照缓存**——注意该缓存会被 `preview_test §B`（同一路径连续
   查询且期望状态实时变化）视为行为变更，需要同步设计失效信号。
2. **超大种子（≥百万块）的 GUI 线程仍有压力**：即便降到 1 次物化，100GB/16KB 块
   仍是 131ms/轮询。进一步的解法是把门控求值移出 GUI 线程（后台线程算、信号回传），
   或只物化「头部窗口 + 尾部入口」覆盖的位图区间而非全量。本轮未做（改动面大、
   需重新论证并发与失效）。
3. `max_failcount=5`、`max_out_request_queue=1000` 的收益属**参数级**判断，已由
   真会话回读确认「生效」，但**未做 A/B 吞吐实测**（需要外网或更长本机闭环）。
   后续可在真机上做「同种子、同 peer、改前/改后」的三次中位数对比。
4. `alias_keys` 会让同一份元数据占用 1~3 个文件条目，`META_MAX_FILES=1000` 的实际
   覆盖资源数相应下降约 1/3；如需要可把上限提到 1500。
