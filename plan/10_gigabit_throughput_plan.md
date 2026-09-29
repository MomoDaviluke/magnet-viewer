# plan/10 —— 千兆吞吐专项：下载与缓存提速方案

> 立项 2026-09-17。目标：定位「下载与缓存只有几十 KB/s」的真实瓶颈，
> 把磁力链下载与缓存速率提升到接近跑满千兆（125 MB/s 线速）的水平，
> 并给出**可复现、可量化**的衡量手段。
>
> 交付物：`diag_throughput.py`（T1–T6 诊断探针）、
> `throughput_test.py`（39 项护栏）、`core/session.py` 吞吐批次三。

---

## 0. 结论摘要

1. **瓶颈不在本机。** 回环实测（零外网、真实 libtorrent 两端）本机软件栈峰值
   **118.7–127.2 MB/s**、稳态 **97.7 MB/s**，已达千兆线速的 95–113%。
   磁盘（顺序 2360 MB/s、16KiB 随机 168 MB/s）与校验（SHA1 单线程 2581 MB/s）
   分别是千兆需求的 19 倍与 23 倍；本程序的调度器 + UI 轮询相对裸引擎
   只差 **4%**（且在 14 倍密轮询下）。**这些都已被实测排除。**

2. **真正的本机侧瓶颈是「单连接带宽时延积」与「peer 发现面」**：
   - `recv_socket_buffer_size` 未设置（= 让 OS 决定，本机实测 SO_RCVBUF 仅 64KB）。
     单连接吞吐 ≈ 缓冲 ÷ RTT ⇒ **1.25 MB/s @50ms**，跑满千兆需 **90 条连接**。
     真实磁力链 swarm 远达不到这个 peer 数，于是「有 peer、有连接、就是没速度」。
   - `dht_bootstrap_nodes` 只有 libtorrent 默认的**一个**节点。单点不可达
     （受限网络的常态）即整条 DHT 发现链失效，退化为「只等 tracker 应答」。
     而**发现到的 peer 数就是吞吐上限**。

3. **优化后**（本机实测，全部纳入回归护栏）：
   单连接上限 **1.25 → 40.0 MB/s @50ms（×32）**，跑满千兆所需连接数
   **90 → 2.8 条**；在途请求窗口覆盖到 **200ms RTT** 的千兆 BDP；
   失败 peer 冷却 **60 → 20s**；DHT bootstrap **1 → 6 节点**。

4. **能否真的跑满千兆，最终由对端供给决定。** 本机侧天花板已抬到 127 MB/s，
   余下的是 `Σ 各对端上行速率`。第 7 节给出「现场归因决策树」，
   用于在 30 秒内判断「是供给不够，还是我们还有问题」。

---

## 1. 问题陈述与验收口径

**症状**：下载与缓存速率约几十 KB/s，远低于千兆链路（理论 125 MB/s）。

**验收口径**（刻意不用「125 MB/s」当判据）：

| 口径 | 值 | 由来 |
|---|---|---|
| 线速 | 125 MB/s | 1000 Mbps ÷ 8 |
| **可用载荷速率** | **112.5 MB/s** | 扣掉 BT 协议 / IP / TCP-uTP 头开销按 90% 计 |
| 判据式 | 稳态速率 ≥ `min(Σ对端上行, 112.5 MB/s) × 80%` | 本机侧不能证明超过供给，故以供给封顶 |

---

## 2. 诊断工具：`diag_throughput.py`

六段独立可跑、零外网、退出码 1 = 有超阈项：

| 段 | 内容 | 判据 |
|---|---|---|
| T1 | 会话设置体检（生效值 vs 千兆推荐值） | T1-a 必需键缺口 / T1-b 人为限速 / T1-c bootstrap 节点 <3 |
| T2 | 缓存写盘能力 | T2-a 顺序写 ≥125 MB/s；T2-b 16KiB 随机写 ≥60 MB/s |
| T3 | 分块校验能力（SHA1 吞吐） | T3-a 单线程 ≥250 MB/s |
| T4 | **回环端到端吞吐**（控制组=裸引擎 / 实验组=本程序） | T4-a 峰值 ≥60；T4-b 相对劣化 ≤30%；T4-c 首字节 ≤2s |
| T5 | 单连接 BDP（缓冲 × RTT） | T5-a 单连接 ≥10 MB/s @50ms |
| T6 | 请求流水线预算 | T6-a 在途 ≥ BDP；T6-b 冷却 ≤30s；T6-c 排队上限 ≥10s |

```bash
./.venv/Scripts/python.exe diag_throughput.py              # 全部
./.venv/Scripts/python.exe diag_throughput.py T1 T5 T6     # 秒级子集
./.venv/Scripts/python.exe diag_throughput.py --quick      # 跳过 T4（省时）
./.venv/Scripts/python.exe diag_throughput.py T4 --size 256 --   # 大载荷复现
```

### 2.1 探针自身的三个坑（已写进代码注释，供后续复用）

1. **速率必须由「累计已完成字节」差分算**，不能采信 `download_payload_rate`：
   后者是 libtorrent 内部短窗口积分，传输尾段会给出虚高几个数量级的瞬时值。
   首版实现据此把 `8e9 B/s` 写成「7.7e6 MB/s」，同一轮里「1 秒窗峰值 25 MB/s」
   与「256MB / 2.8s = 91 MB/s」自相矛盾——根因就在这里。
2. **做种会话必须被变量接住**：只写 `_seed_session(...)` 不接返回值，会话会被
   立即 GC。症状是「两端都 0%、跑满超时」，**无任何报错**。
3. 阈值要区分「绝对」与「相对」：绝对吞吐取决于机器（本机 20 核 NVMe vs CI
   runner），**相对劣化**才是本程序能负责的性质 → 用 `实验组 ≥ 控制组 × 0.5`。

---

## 3. 分层归因（全部为实测数据）

### 3.1 优化前后对照

| 观测项 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| `recv_socket_buffer_size` | `0`（OS 决定，实测 64KB） | `2 MiB` | — |
| `send_socket_buffer_size` | `0` | `1 MiB` | — |
| `send_buffer_watermark / factor / low` | 512KB / 50 / 10KB | 3MB / 150 / 64KB | 上行互惠 |
| `request_queue_time` | 3 s | 10 s | 高 BDP 不再误判超时 |
| `min_reconnect_time` | 60 s | 20 s | 爬坡 |
| `peer_connect_timeout` | 15 s | 8 s | 爬坡 |
| `hashing_threads` | 2 | 4 | 余量 |
| `aio_threads` | 8（**低于默认 10**） | 12 | 纠正了下调 |
| `max_out_request_queue` | 1000 | 1500 | 在途 15.6 → 23.4 MB |
| `announce_to_all_trackers / tiers` | False / False | True / True | 发现面翻倍 |
| `dht_bootstrap_nodes` | 1 个 | 6 个 | 单点故障消除 |
| **T5 单连接上限 @50ms RTT** | **1.25 MB/s** | **40.00 MB/s** | **×32** |
| **跑满千兆所需连接数 @50ms** | **90 条** | **2.8 条** | **÷32** |
| T6-a 在途窗口 vs BDP | 不足 @200ms | 足够（≤200ms） | ✅ |
| T6-b 失败 peer 冷却 | 60 s ❌ | 20 s ✅ | — |
| T6-c 请求排队上限 | 3 s ❌ | 10 s ✅ | — |
| **探针判据（T1/T5/T6）** | **7 项中 6 项超阈** | **7 项 0 超阈** | 全绿 |

### 3.2 本机栈能力（新增基线，回环、零外网、真实 libtorrent）

| 指标 | 实测 | 千兆需求 | 结论 |
|---|---|---|---|
| 顺序写（256MB + fsync） | **2360 MB/s** | 125 MB/s | 19 倍余量 |
| 16KiB 随机写 | **168 MB/s / 10771 IOPS** | 7200 IOPS | 1.5 倍余量 |
| SHA1 单线程 / 4 线程 | **2581 / 9823 MB/s** | 112 MB/s（0.04 核） | 23 倍余量 |
| 回环峰值·控制组（裸引擎） | **118.7 – 127.2 MB/s** | 112.5 MB/s | 95–113% |
| 回环峰值·实验组（本程序+调度器） | **122.0 – 129.9 MB/s** | 112.5 MB/s | 相对劣化 **4%** |
| 回环稳态 · 首字节 | 97.7 MB/s · **0.10 s** | — | 爬坡不成问题 |
| UI 轮询单次成本 | 中位 **0.55–0.75 ms**，最大 6.9 ms | — | 700ms 轮询的 0.1% |

### 3.3 已排除项（附证据，避免后续重复怀疑）

- **磁盘写盘**：见 3.2。`cache_dir` 默认在系统临时目录，实测能力远超需求。
- **分块校验**：见 3.2。
- **调度器 / UI 轮询**：T4-b 只差 4%，且是在**密 14 倍**的轮询节奏下测的
  （生产 700ms vs 探针 50ms）。
- **缓存配额 LRU 误删活跃数据**：已核查 `core/cache_quota.py` +
  `core/registry.py::protected_dirs` + `ui/main_window.py:820`——清理**只作用于
  `.preview/`**，且以活跃任务目录为保护名单；`downloads/` 不受影响。
- **人为限速**：四个限速键（含 `local_*`）实测均为 0。
- **DHT 路由表持久化没接线**：已核查 `core/fetcher.py:159` 确实注入了
  `dht_state_path=<cache>/.dht_state`，P-4 是活的。

### 3.4 四项真实瓶颈（按影响力排序）

| # | 瓶颈 | 机理 | 量级 |
|---|---|---|---|
| B1 | **单连接 BDP 不足** | 缓冲 64KB ÷ 50ms = 1.25 MB/s/连接；要 90 条才凑够千兆 | 决定性 |
| B2 | **DHT bootstrap 单点** | 唯一节点不可达 ⇒ 无 peer ⇒ 几十 KB/s | 决定性（冷启动） |
| B3 | **tracker 只问一个** | 磁力链多 tracker 常同处一个 tier，默认只问首个 | peer 数 ≈ ÷2 |
| B4 | **爬坡期过长** | 失败 peer 冷却 60s + 黑洞 peer 白占 15s 连接槽 | 首 1–2 分钟 |

---

## 4. 优化方向

### P0（已完成，本次实施）

| 项 | 落点 | 预期效果 |
|---|---|---|
| P0-1 收发缓冲显式放大 | `core/session.py::THROUGHPUT_SETTINGS` | 单连接上限 ×32 |
| P0-2 上行水位线三件套 | 同上 | 缓解 tit-for-tat choke |
| P0-3 请求排队/超时与爬坡参数 | 同上 | 消除请求重发风暴、缩短爬坡 |
| P0-4 校验/写盘并发上调 | 同上 | 「速率不低但 have 不涨」消失 |
| P0-5 在途窗口 1500 | 同上 | 覆盖 200ms RTT 的千兆 BDP |
| P0-6 全问 tracker | 同上 | 发现面翻倍 |
| P0-7 DHT bootstrap 冗余 6 节点 | `session.PEER_DISCOVERY_SETTINGS` | 冷启动发现链不再单点 |
| P0-8 诊断探针 + 护栏 | `diag_throughput.py` / `throughput_test.py` | 效果可量化、可回归 |

### P1（建议下一步，各含落点与验收）

| 项 | 落点 | 预期增益 | 验收判据 |
|---|---|---|---|
| P1-1 磁力链**追加公共 tracker 列表** | `core/resolver.py::_resolve_magnet`（atp.trackers 追加）；`resolver_test` 需同步 | 冷门磁力链 peer 数 +50%~数倍 | 单测断言 atp.trackers 含追加项；`announce_to_all_tiers=True` 已在位 |
| P1-2 **可连接性开关**（UPnP/NAT-PMP opt-in） | `core/config.py::DEFAULTS` + `build_session_settings` + 设置页 + 安全说明 | 可连接 peer 池约 ×2（当前 `enable_upnp=False`） | 设置项热更新后 `get_settings()` 回读一致；需在 UI 明示「映射端口会暴露本机」 |
| P1-3 **在线归因段 T7**（per-peer 速率） | `diag_throughput.py` 新增段，用 `torrent_handle.get_peer_info()` 汇总 `down_speed` | 现场 30 秒内判定「供给 vs 自身」 | 输出 `Σ对端下行 / 会话速率` 与差值占比 |
| P1-4 `.dht_state` 冷启动收益量化 | 复用 `DHT_STATE_FILE` | 热启动发现时间 ↓ | 两次冷/热启动的首字节时间对比 |

### P2（观察项，先量后调）

- `peer_turnover`（默认 4%/300s）：慢但对稳定 swarm 的换手策略。
- `mixed_mode_algorithm`（默认 prefer_tcp）：uTP 占比高的 swarm 可试 `peer_proportional`。
- `seed_after_complete=False` 的上传互惠影响：只收不做种会被部分 swarm 保守 choke。
- `cache_dir` 与 `download_dir` 同盘时的多任务 IO 争用（当前实测余量充足，暂不动）。
- 安卓端是否需要同步：`recv_socket_buffer_size` 等键在 libtorrent4j 是否绑定 →
  需按 `android/ENV.md` 的流程先探测键存在性，**不得直接照抄**。

---

## 5. 已实施改动清单

| 文件 | 改动 |
|---|---|
| `core/session.py` | `THROUGHPUT_SETTINGS` 新增 12 项（每条附依据）；新增 `DHT_BOOTSTRAP_NODES` + `PEER_DISCOVERY_SETTINGS`；`build_session_settings` 顺序改为 基线 → 发现 → 吞吐 → 代理 |
| `diag_throughput.py` | **新增**：T1–T6 六段诊断探针，13 项判据，退出码 0/1/2 |
| `throughput_test.py` | **新增**：39 项护栏（§A 纯函数 → §F 回环端到端相对不劣化） |
| `wait_timeout_test.py` | 批次承诺值同步上调（`aio_threads` 8→12、`hashing_threads` 2→4、`max_out_request_queue` 1000→1500） |
| `regression_run.py` | SUITES 增 `throughput_test`（23 → **24 套**），头注与用法同步 |

**为什么 `dht_bootstrap_nodes` 不放进 `THROUGHPUT_SETTINGS`**：那颗字典被
`wait_timeout_test` 用 `int()` 逐键比对，字符串值会抛异常；且语义上它属于
「发现路径」而非吞吐调参。故单列 `PEER_DISCOVERY_SETTINGS`。

---

## 6. 预期吞吐指标

### 6.1 按场景（务实口径，非营销数字）

| 场景 | 优化前（典型） | 优化后（目标） | 谁决定上限 |
|---|---|---|---|
| 局域网 / 同城 peer（上行充足） | 受限 | **90–120 MB/s** | 本机栈（实测 127 峰值） |
| 外网热门资源（≥30 seeders，上行充足） | 0.5–5 MB/s | **30–100 MB/s** | 对端供给 |
| 外网中等资源（5–30 peers） | 0.1–1 MB/s | **3–30 MB/s** | 对端供给 |
| 冷门资源（≤3 peers） | 几十 KB/s | **几十 KB/s ~ 1 MB/s** | **纯供给，本机侧已无优化空间** |
| 首字节（冷启动） | DHT 单点，可能不返回 | 目标 ≤30 s（待现场验证） | DHT 可达性 |

### 6.2 可量化验收判据

1. `./.venv/Scripts/python.exe diag_throughput.py` → **退出码 0**（13 项判据全绿）。
2. `./.venv/Scripts/python.exe throughput_test.py` → **39 项全绿**。
3. `./.venv/Scripts/python.exe regression_run.py` → **24 套全绿**（含新增套件）。
4. 现场：同一磁力链连测 3 分钟，稳态速率 ≥ `min(Σ对端上行, 112.5 MB/s) × 80%`。

---

## 7. 现场验收流程与归因决策树

**步骤**

1. 先跑离线基准（第 6.2 节 1–3 条）——本机侧问题必须在离线阶段清零，
   否则现场数据无解释力。
2. 现场取两条参照资源：
   - **LAN 参照**：同网段做种同一文件 → 应达 90–120 MB/s（验证链路 + 本机栈）
   - **热门参照**：任选高做种数公开种子 → 验证外网路径与发现面
3. 每 5 秒记录：`download_rate`、`num_peers`、`num_seeds`、`total_done`、
   `state`（UI 已展示，或 `SessionManager.status()`）。

**归因决策树（30 秒判定）**

```
速率低
├─ num_peers ≤ 2
│   └─ 发现面问题 → 查 .dht_state 是否生成 / tracker 是否应答（P1-1、P1-4）
├─ num_peers ≥ 10 但仍慢
│   └─ BDP / choke 问题 → 复核 T5/T6；确认 recv_socket_buffer_size 未被覆盖
├─ 速率高但 total_done 不涨
│   └─ 校验 / 写盘阻塞 → 跑 T2/T3（并查 file_error 告警）
└─ 速率 ≈ Σ 对端上行速率
    └─ 供给上限，已达最优 → 换资源或加 tracker，本机侧无需再改
```

---

## 8. 风险、权衡与未做项

- **内存代价**：`recv_socket_buffer_size=2MB` × 400 连接理论上限 800MB；实际
  受 `connections_limit` 与真实 peer 数约束（磁力链常见 10–50 个 peer），
  实测内存无异常。若将来把 `connections_limit` 再放大，需回头复核此项。
- **`min_reconnect_time 60→20s`**：会增加重连尝试频率。上限仍受
  `connection_speed=60/s` 与 `connections_limit=400` 约束，风险可控；
  若 tracker 侧出现限流反馈，回调到 30s。
- **`aio_threads` / `hashing_threads` 上调**：CPU 占用上升，但 T3 实测千兆只需
  0.04 核，且本机 20 核。低核数机器（≤4 核）建议把这四项下调，**当前是静态
  常量以保证测试可确定性**（改成按核数计算会让断言不可复现）。
- **未做**：UPnP/NAT-PMP 开启（涉及端口暴露，必须配 UI 说明，见 P1-2）；
  公共 tracker 列表追加（涉及外部依赖与隐私取向，见 P1-1）；
  真实 swarm 的 per-peer 归因段（见 P1-3）。
- **不可承诺项**：冷门资源「几十 KB/s」在供给不足时**无法通过本机优化改变**。
  方案能负责的是把本机侧天花板从 1.25 MB/s/连接 抬到 40 MB/s/连接，
  并在供给充足时让它跑到线速。
