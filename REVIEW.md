# 项目全面审查报告

> 审查日期：2026-09-03
> 审查对象：`magnet-viewer`（磁力链实时解析查看器）
> 代码规模：业务代码 2295 行（8 个模块），测试脚本 1502 行（8 个）
> 环境：Python 3.13.12 + libtorrent 2.1.1 + PySide6 6.11.2（Windows）

---

## 一、审查方法

采用「静态扫描 → 定点审读 → 实证复现」三段式，而非通读式走查：

1. **静态扫描**：AST 解析全部模块的导入与引用关系，定位未使用导入、死代码、宽泛异常捕获（31 处）。
2. **定点审读**：沿数据流（种子/磁力链 → 解析 → 调度 → 流服务 → 播放器）核对每一处路径与哈希构造。
3. **实证复现**：对每个疑似缺陷构造最小可运行用例，用真实 libtorrent 会话验证「修复前 / 修复后」行为差异——**未经实证的推断一律标注为推测**。

本方法在本轮审查中查出 5 个此前未被测试覆盖的真实缺陷（含 2 个导致功能完全失效）。

---

## 二、总体评估

| 维度 | 评价 | 说明 |
|------|------|------|
| 架构分层 | 良好 | `core/`（解析·调度·流服务）与 `ui/` 边界清晰，跨线程通过 Signal 桥接，无 UI 层直接操作 libtorrent |
| 核心机制 | 扎实 | 分块级可用性判定、「点播+等待」流媒体调度、moov 尾部优先，是本项目的价值所在且设计正确 |
| 关键路径正确性 | **存在严重缺口** | 单文件种子预览完全不可用；BEP-52 v2 种子 info_hash 错误 |
| 异常可观测性 | **薄弱** | 31 处 `except Exception` 静默吞异常，是 5 个缺陷长期隐藏的直接原因 |
| 测试体系 | 中等，但**覆盖维度有偏** | 8 个测试脚本、闭环验证思路出色；但按「功能模块」而非「数据入口路径」铺排，导致单文件/本地种子两条路径失守 |
| 性能可扩展性 | **未验证大文件** | 缓冲区计算为 O(分片数) 且调用频次失控，大文件下会拖垮主线程 |
| 资源管理 | 有隐患 | 预览缓存无淘汰策略，默认落在系统临时目录 |

**结论**：核心技术创新点（边下边播的流媒体调度）实现质量高，真正的问题集中在**边界覆盖**与**可观测性**上——不是设计错了，是某些入口没走到、出错时没留下痕迹。

---

## 三、缺陷清单

### P0 — 功能失效（已实证，✅ 已修复）

> 2026-09-03 后续：P0-1、P0-2、P1-1 三项已修复并通过回归（6 套测试全绿）。

#### P0-1 单文件种子预览完全不可用（✅ 已修复）

**现象**：单文件种子（单个 `.mkv`/`.mp4`，BT 场景极常见）解析与下载均正常，但预览打不开，流服务返回 404。

**根因**：单文件与多文件种子的落盘结构不同。

| | libtorrent 实际落盘 | 本项目构造的 `path` |
|---|---|---|
| 多文件 | `save_path/root/inner` | `root/inner` ✅ |
| **单文件** | `save_path/name` | `name/name` ❌ 多套一层 |

涉及两处同构代码：`core/parser.py:93` 与 `core/fetcher.py:327`。

**实证**（`single_file_test.py`，本地种子与磁力链两条入口各跑一遍）：

```
入口 A：本地 .torrent 文件
  [FAIL] 单文件 path 不含多余目录层：'BigMovie.mkv/BigMovie.mkv'
  [FAIL] file_disk_path 指向真实文件
        缓存目录实际内容：['BigMovie.mkv']
  [FAIL] 流服务请求失败：HTTP 404（url=http://127.0.0.1:61469/BigMovie.mkv/BigMovie.mkv）
入口 B：磁力链 —— 同样 3 项失败
```

**影响链**：`file_disk_path` 错位 → 主窗口「磁盘路径→文件」映射键错位 → `_pieces_map`/`_demand_range` 全部落空 → `url_for(f.path)` 请求 404 → **预览彻底不可用**。文件本身能正常下载（600 KB 实测下载成功），但播放器拿不到数据。

**方案**：`path` 构造改为单文件时不加前缀（仅 `name`），多文件保持 `root/inner`。同步修正 `ui/file_tree.py:51` 的单文件判定（现为 `path.split("/")[0] == name`，需改为 `path == name`）。

**成本**：低。约 4 处改动 + 回归。

**修复结果**（2026-09-03）：`parser.py` / `fetcher.py` 单文件不再套前缀；`file_tree.py` 移除已冗余的 `single` 判定（单文件 `path` 无分隔符，`split("/")` 自然平铺）。`single_file_test.py` 由 5 通过 / 7 失败转为 **12/12 全通过**。

---

#### P0-2 BEP-52 v2 / 混合种子 info_hash 计算错误（✅ 已修复）

**现象**：libtorrent 2.x 的 `create_torrent()` **默认产出 v1+v2 混合种子**（`meta version=2`）。本项目对这类种子算出的 info_hash 既不等于 v1 也不等于 v2。

**根因**：`core/parser.py:106` 固定用 `sha1(bencode(info))`。而 BEP-52 规定：

- v1 种子：info_hash = SHA-1（bencoded info）
- **v2 / 混合种子：info_hash = SHA-256**（bencoded info，含 v2 键）

**实证**：

```
libtorrent v1 (sha1)   = df28a0a65d16298bf6c450cc0dec8662fc6795f0
libtorrent v2 (sha256) = be6e569594449b4151b1829c08725cf1fd80bfa162a7d33ba33f7a5fa365fda7
本项目 sha1(全量 info)  = 38e1af8193b7a9643f30b47083d447e3c55d93e6   ← 两者都不是
本项目 sha1(剥离 v2 键) = c8ee3b55d669771569f681bd5ef4ad133f7c002f   ← 两者都不是
验证 sha256(bencode(info)) 与 libtorrent v2 一致：True
```

**影响**：info_hash 是种子的唯一标识，用于状态展示、去重、以及后续任何按 hash 关联的逻辑。错误的 hash 会导致同一资源被识别成不同实体，且无法与外部工具（如 qBittorrent 导出的种子）对齐。

**方案**：按 `meta version` 分支——`>= 2` 用 SHA-256，否则用 SHA-1。为与 libtorrent `info_hash()` 的返回保持一致（v2 截断为前 20 字节），可取 `sha256(...)[:40]`。建议同时把 v1/v2 双哈希都保留在 `ParseResult` 中。

**成本**：低。约 10 行 + 测试。

**修复结果**（2026-09-03）：新增 `parser.torrent_info_hash()`，按 `meta version` 分支——v2/混合用 SHA-256（截取前 20 字节以与 libtorrent `info_hash()` 一致，保证两条入口对同一资源得到相同标识），v1 仍用 SHA-1。`smoke_test.py` 新增 [2c] 覆盖三种版本（混合 / 纯 v1 / 纯 v2），全部通过。

---

### P1 — 正确性与健壮性

#### P1-1 纯 v2 种子解析直接抛异常（已实证，✅ 已修复）

**实证**：构造仅含 BEP-52 键（`file tree` / `meta version`）的 info 字典：

```
解析抛异常: KeyError: b'length'
```

**现状**：异常被 `_resolve_torrent_file` 捕获并弹「种子文件解析失败」提示，**不会崩溃**，但这类种子完全不支持。

**方案**：二选一——(a) 识别纯 v2 并给出明确提示「暂不支持纯 v2 种子」；(b) 实现 `file tree` 递归解析以完整支持 BEP-52。推荐先做 (a)（成本低、体验明确），(b) 视需求排期。

**修复结果**（2026-09-03）：已采用方案 (a)。新增 `parser.is_pure_v2()`，在解析前拦截并抛出含原因的 `ValueError`（「纯 BitTorrent v2 种子（BEP-52，仅含 file tree），暂不支持」），替代此前的 `KeyError: b'length'`。方案 (b) 仍作为可选增强保留。

#### P1-2 31 处宽泛异常捕获，静默吞掉错误

**分布**：`core/fetcher.py` 18 处、`core/scheduler.py` 6 处、`core/stream_server.py` 4 处、`ui/main_window.py` 3 处。

**这是本轮 5 个缺陷长期隐藏的直接原因**——历史上已两次因静默吞异常导致真实故障：告警批次被整体丢弃（`metadata_received_alert` 丢失）、回调签名错误被吞后静默降级到错误逻辑。

**方案**：引入统一日志模块，按风险分级改造：

| 级别 | 场景 | 处理方式 |
|------|------|----------|
| 高危 | 告警循环、回调链、路径构造 | 记录 `logger.exception()` 并保留降级行为 |
| 中危 | libtorrent 状态读取 | 记录 `logger.warning()` |
| 低危 | 播放器断开连接等预期异常 | 保持静默（现状合理） |

日志写入缓存目录下的 `magnet-viewer.log`（滚动，单文件 ≤1 MB），默认开启，设置面板可关。

**成本**：中。约 31 处改动，但可分批进行，优先改高危 8 处。

#### P1-3 `SessionManager.shutdown()` 未加锁且不等待线程退出

`core/fetcher.py:112-115`：`self._ses = None` 在 `self._lock` 之外执行，而后台告警线程正在读 `self._ses`；同时未 `join()` 线程。

**现状影响**：低（daemon 线程，进程退出即结束）。但存在竞态窗口，且 `shutdown()` 后立刻 `start()` 会有状态残留。

**方案**：加锁清理 + `thread.join(timeout=2)`。

---

### P2 — 性能与资源

#### P2-1 缓冲区计算为 O(分片数)，且调用频次失控（已实测量化）

`contiguous_bytes()` 每次都从 `start_piece` 起逐块调用 `handle.have_piece()` 线性扫描。

**实测单次调用成本**：`have_piece()` = **3.76 µs**（20000 次采样）。

**放大效应**（以 20 GB / 1 MB 分片 = 20000 块为例）：

| 调用点 | 频次 | 单次耗时 |
|--------|------|----------|
| `status()` 中 `buffer_progress()` | 每 700 ms（UI 定时器） | 75 ms |
| `status()` 中 `contiguous_progress()` | 每 700 ms（**重复计算**） | 75 ms |
| 流服务 `_availability()` | **每个 HTTP 请求** | 75 ms |

- UI 轮询：每 700 ms 消耗 150 ms CPU（约 21% 单核）
- 播放期间：FFmpeg 每秒发起数十个 Range 请求，仅此项 ≈ **1.5 秒 CPU / 每秒**（即单核饱和）

**方案**（按性价比排序）：

1. **消除重复计算**：`status()` 中 `buffer` 与 `contiguous` 同源，算一次复用。立即省一半。
2. **增量扫描**：连续前缀在下载过程中单调递增，缓存上次位置并从该处继续（哈希校验失败时回退全量扫描）。
3. **批量取位图**：用 `handle.status().pieces` 一次性取回位图（单次调用），在 Python 侧做连续前缀判定，把 N 次绑定调用降为 1 次。

建议先做 1+2（改动小、风险低），3 作为大文件场景的彻底方案。

#### P2-2 预览缓存无淘汰策略，磁盘无界增长

**现状**：`clear_cache_on_exit` 默认 `False`；切换预览文件时**不释放**上一个文件已下载的数据；仅提供「退出时清理」与「立即清理」两个手动入口。

**风险**：预览几部 4 GB 影片即在系统临时目录（默认 `%TEMP%\magnet_viewer_cache`，通常在 C 盘）累积数十 GB，且用户无感知。

**方案**：设置面板增加「缓存上限」（默认 2 GB）；预览切换与退出时按 LRU 清理超限部分；状态栏显示当前缓存占用。

---

### P3 — 安全（本轮已修复，留档）

#### P3-1 目录穿越 ✅ 已修复

两处缺陷，均已实测证实并修复：

1. **恶意种子路径穿越**：种子可声明 `path:["..","..","Windows","win.ini"]`，旧代码在 `file_disk_path()` 原样拼接 → 逃出缓存目录。已新增 `core/models.py: safe_rel_path()` 净化，parser 与 fetcher 两条链路均已接入。
2. **流服务越界校验可被同前缀兄弟目录绕过**：`fp.startswith(root)` 对 `C:\...\cacheT` 与 `C:\...\cacheT_evil\secret.mp4` 返回 **True 并放行**。已改为 `_is_within()`（基于 `os.path.commonpath` + `normcase`）。

#### P3-2 本地 .torrent 预览静默降级 ✅ 已修复

`parse_torrent_file()` 从不设置 `ParseResult.cache_dir`（磁力链路径会注入，本地种子路径漏了）→ 主窗口映射键退化为相对路径 → `_pieces_map`/`_demand_range` 全部落空 → 预览退化为「按完整静态文件服务」，把未下载的稀疏零数据喂给播放器。**5 套测试原本都覆盖不到这条路径**。

---

## 四、测试体系改进

### 根本问题：覆盖维度有偏

现有 8 个测试脚本按「功能模块」组织，**未按「数据入口路径」铺排**。本轮 3 个缺陷（本地种子 cache_dir、单文件种子路径、v2 info_hash）全部源于此——同一功能的不同入口没有各自覆盖。

### 改进方案

**1. 建立入口路径矩阵，每条路径独立验证**

| 入口 | 单文件种子 | 多文件种子 | 混合 v2 种子 |
|------|-----------|-----------|-------------|
| 本地 `.torrent` | ❌ 缺失（P0-1） | ✅ `local_torrent_test.py` | ❌ 缺失（P0-2） |
| 磁力链 | ❌ 缺失（P0-1） | ✅ `local_magnet_test.py` | ❌ 缺失（P0-2） |

本轮已新增 `single_file_test.py`（覆盖单文件 × 两条入口）。建议补齐混合 v2 一列。

**2. 补充确定性单元测试**

当前重度依赖集成测试（需启 libtorrent 会话、耗时长）。建议为纯函数补快速单元测试：`safe_rel_path`（边界组合）、`tail_piece_window`、`contiguous_bytes`、`range_available`、`lt_proxy_settings`。这类测试毫秒级，可高频运行。

**3. 引入覆盖率度量**

接入 `coverage.py`，明确 `core/` 的行覆盖与分支覆盖，把「未被任何测试触达的代码」显性化——P3-2 的 `file_disk_path` 正是因为零覆盖才在审读中被发现。

**4. 统一测试入口**

新增 `run_tests.py` 串行跑全部 8 个脚本并汇总结果，避免逐个人工执行（当前回归一轮需手动跑 6 次）。

---

## 五、工程化与分发

| 项 | 现状 | 建议 | 优先级 |
|----|------|------|--------|
| 版本管理 | **无 `.git` 仓库** | 立即初始化，当前全部工作成果无版本保护 | **高** |
| 打包分发 | 仅 `start.bat`（需用户装 Python） | PyInstaller 打包为独立 exe | 中 |
| 依赖锁定 | `requirements.txt` 仅 2 行宽松约束 | 生成 `requirements.lock` 锁定版本（libtorrent 2.0/2.1 API 不兼容，风险实际存在） | 中 |
| 持续集成 | 无 | GitHub Actions 跑冒烟测试（沙箱可跑的 6 个脚本） | 中 |
| 日志 | 无 | 见 P1-2 | 高 |
| 类型标注 | 部分 | 关键模块补 `mypy` 检查 | 低 |

---

## 六、建议执行顺序

### 第一阶段：止血（P0 + 工程基础）— ✅ 已完成

1. ~~**P0-1 单文件种子路径**~~ ✅ 已修复（12/12）
2. ~~**P0-2 v2 info_hash**~~ ✅ 已修复（三版本分支全覆盖）
3. ~~**初始化 Git 仓库**~~ ✅ 已完成（含 `.gitignore` / `.gitattributes`，初始提交 `d438e92`）
4. ~~**P1-1 纯 v2 明确提示**~~ ✅ 已修复（改为含原因的 ValueError）

**第一阶段回归结果**：6 套测试全绿 —— smoke_test、local_magnet_test、local_torrent_test（9/9）、single_file_test（12/12）、gui_feature_test（24/24）、moov_stream_test、qt_stream_open_test。

### 第二阶段：可观测性（P1）— 🟡 大部分已完成

5. **P1-2 日志系统 + 高危异常改造**
   - ✅ 日志设施 `core/logutil.py`（滚动 1 MB×3，线程安全，可关闭，绝不因日志自身抛异常）
   - ✅ 63 处 `except` 已接入日志；原报告点名的 8 处高危点覆盖 7 处
   - ✅ 重构收口后复测（AST 逐块，2026-09-07）：核心链路 fetcher/registry/resolver/preview/scheduler **零静默**；全项目有日志/转抛 72 处，仍静默 44 处——集中在外围与合理场景（logutil 自身的「绝不因日志抛异常」设计、stream_server 播放器断连等预期异常、taskstore/cache_quota 纯函数防御、UI 渲染吞异常），高危路径已无死角
   - ✅ `main_window._pieces_map` 未命中告警已补（含按路径节流，避免每请求刷屏）
6. **P1-3 shutdown 加锁与 join** — ✅ 已完成（加锁 + 落盘 tasks/fastresume + join）

### 第三阶段：性能与资源（P2）— 🟡 部分完成

7. **P2-1 缓冲区计算优化**
   - ✅ 已消除 `status()` 内 `contiguous` 与 `buffer` 的重复扫描（`fetcher.py:1042-1049`）
   - ✅ 批量取位图（2026-09-07，重构收口后）：三条热路径（流服务 piece_map、
     scheduler contiguous/tail_ready）改 `handle.status().pieces` 一次快照 +
     `models.have_from_bitmap` O(1) 索引，N 次绑定调用 → 1 次；preview_test
     计数断言钉死（have_piece 恰 0 次）
8. **P2-2 缓存上限与 LRU** — ❌ 未实现
   - 注意：`core/cache_guard.py` 是**防误删守卫**（高风险目录拒绝 + CACHE_MARKER + 保留名单），
     不是配额管理，两者勿混淆。缓存无界增长问题仍然存在。

### 第四阶段：质量基建（长期）— 🟡 部分完成

9. 测试矩阵补齐（混合 v2 × 两入口）+ 纯函数单元测试 — ❌ 未做
10. 覆盖率接入 + 统一测试入口
    - ✅ `regression_run.py` 统一入口（现已纳入 `download_mgr_test`，共 8 套）
    - ❌ 覆盖率（coverage.py）未接入
11. 打包分发与依赖锁定 — ❌ 未做

---

## 七、遗留事项

1. ~~`.venv_old_broken`（约 830 MB）需手动删除~~ ✅ 已处理（2026-09-03 复查已不存在）
2. `live_test.py`（公网 DHT 验证）在当前沙箱不可用——环境屏蔽 BT/UDP 出站（`dht_nodes` 恒为 0）。需在正常 BT 网络下执行复核。
3. Windows 文件名长度限制（260 字符）未做处理，深层目录种子可能落盘失败——属低概率场景，建议在处理 P0-1 时顺带考虑。

---

*本报告中所有标注「已实证」的结论均附最小可复现用例，可随时重新验证；标注「推测」的部分未经实证。*
