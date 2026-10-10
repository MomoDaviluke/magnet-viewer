# magnet-viewer 架构文档

> 制定日期：2026-10-09（arch/roadmap-2026q4，Phase 3.4 / 方案 A 交付物）
> 定稿更新：2026-10-10（Wave 3 A6b——同步 Wave 2 facade 化/协作者拆分/tests 目录化
> 与 Wave 3 Protocol/配置注册/主题表/metacache 锁修；行数与计数均为当日实测）
> 依据：architecture_review_report.md（综合评分 2.8/5）+ technical_roadmap.md Phase 2.4 方案 A
> 维护规则：core/ 模块增删或依赖方向变化时，本文「分层地图」必须同步更新；
> 与源码冲突时以源码为准并回改本文。

---

## 一、系统概览

magnet-viewer 是磁力链实时解析 / 边下边播 / 下载管理工具，**双端独立实现**：

- **桌面端**：Python 3.13 + PySide6（Qt6）+ libtorrent（`core/` 26 模块 + `ui/` 15 模块，均不含 `__init__.py`，2026-10-10 实测）
- **安卓端**：Kotlin + Jetpack Compose + libtorrent4j（`android/`，main 源集 59 个 Kotlin 文件，2026-10-10 实测）

### 1.1 双端架构图

```
┌────────────────────── 桌面端 ──────────────────────┐
│ main.py                                            │
│   └─ ui/main_window.py (MainWindow, _Bridge)      │
│        │ ├─ ui/cache_host.py    (CacheHost)       │
│        │ ├─ ui/stream_host.py   (StreamHost)      │
│        │ ├─ ui/task_controller.py (TaskController)│
│        │ └─ ui/inbound_host.py  (InboundHost,     │
│        │      含 _InboundBridge)  ← Wave 2 协作者 │
│        │ 只依赖 facade/白名单 ▼（已收敛 0 越层）  │
│ core/fetcher.py (SessionManager — facade/hub)     │
│   ├─ session.py    (SessionCore: libtorrent 会话) │
│   ├─ registry.py   (TaskRegistry: 单锁+注册表)    │
│   ├─ scheduler.py  (PreviewScheduler: 分块调度)   │
│   ├─ resolver.py   (ResolverCore: 解析编排)       │
│   ├─ taskops.py    (TaskOps: 任务 CRUD)           │
│   ├─ persist.py    (TaskPersistence: 落盘/恢复)   │
│   ├─ preview.py    (PreviewCore: 预览桥/快照)     │
│   ├─ 数据/设施: taskstore / metacache / resume /  │
│   │  parser / models / states / logutil / config /│
│   │  cache_guard / cache_quota / cache_mode /     │
│   │  secretbox / proxycheck / inbound / exceptions│
│   └─ 契约: interfaces.py (4 Protocol, 纯声明)     │
│ core/stream_server.py (本地 HTTP Range 流服务)    │
└───────────────────────────────────────────────────┘

┌────────────────────── 安卓端 ──────────────────────┐
│ MainActivity / Navigation.kt                      │
│   └─ ui/magnet/MagnetViewModel.kt                 │
│        │ 依赖 ▼                                    │
│ engine/BtEngine.kt (会话+注册表+解析+任务操作)    │
│   ├─ engine/PreviewScheduler.kt  ↔ scheduler.py  │
│   ├─ engine/PieceView.kt         ↔ pieceview.py   │
│   ├─ data/TaskOps.kt             ↔ taskops.py     │
│   ├─ data/TaskStore.kt / MetaCache.kt /           │
│   │  ResumeStore.kt / CacheQuota.kt /             │
│   │  SettingsStore.kt / AppLog.kt                 │
│   └─ core/model/*, core/states/*, core/sched/*    │
│ stream/StreamServer.kt  ↔ stream_server.py        │
│ service/DownloadService.kt (dataSync 前台服务)    │
└───────────────────────────────────────────────────┘
```

### 1.2 数据流（磁力链输入 → 持久化）

```
用户输入磁力链/.torrent
  │
  ▼
[解析] ResolverCore.resolve (core/resolver.py)
  │   本地 .torrent → core/parser.py bdecode；磁力链 → add_torrent 等 DHT/ut_metadata
  │   先查元数据缓存 core/metacache.py (<cache>/.meta/<ih>.torrent)，命中即注入 ti，免网络往返
  ▼  metadata_received_alert (core/session.py 告警循环分发)
[注册] TaskRegistry.put_record_locked (core/registry.py, 持唯一任务锁)
  │   info_hash → TaskRecord；gen 换代防旧解析覆盖
  ▼
[调度] PreviewScheduler.begin (core/scheduler.py)
  │   单文件锁定 + 顺序分块 + MP4/MOV 尾部 moov 索引窗口优先
  ▼
[流媒体] StreamServer (core/stream_server.py, 仅监听 127.0.0.1)
  │   Range 请求 → PieceView/PreviewCore 判定分块可用性 → 只暴露已下载分块
  │   未就绪区间 → demand 点播回调 → scheduler.request_range 补拉 → 503+Retry-After 等待
  ▼
[播放/下载] ui/preview_player.py 内嵌 QMediaPlayer；完整下载走 TaskOps.add_task
  ▼
[持久化] TaskPersistence (core/persist.py)
      .tasks.json（清单，taskstore.py 原子写）+ .resume/<ih>.fastresume（续传）
      目录隔离：下载 <cache>/downloads/<ih>/，预览 <cache>/.preview/<ih>/
```

---

## 二、桌面端分层地图（方案 A 核心交付物）

`core/` 保持**扁平文件结构**（26 个 .py（不含 `__init__.py`），不做物理分包），
以下逻辑分层即本项目的分层契约。实测依赖依据各文件 import 语句与文件头注释
推导（详见 architecture_review_report.md §2.1）。

### 2.1 六层划分与模块职责

**层级顺序（上=更靠近 UI，下=更基础）**：编排层 → 服务层 → 逻辑层 → 数据层 → 设施层 → 基础层。

#### 基础层（被所有层引用，自身几乎无 core 内依赖）

| 模块 | 职责（一行） | 禁止事项（一行） |
|------|-------------|-----------------|
| `core/models.py` | 数据模型：ParseResult / TorrentFile / PieceMap 与连续字节换算纯函数 | PieceMap/contiguous_bytes/ready_until/range_available 语义被桌面↔安卓对拍夹具冻结（`android/tools/gen_parity_fixtures.py`），不得擅改 |
| `core/states.py` | 任务生命周期状态机常量 + bootstrap tracker 列表（叶子模块） | 不 import core 内任何东西（它的存在就是为了斩断 fetcher→persist→fetcher 环） |
| `core/logutil.py` | 统一日志入口：log_warning / log_exception，让静默失败留痕 | 绝不因日志本身抛异常、绝不向调用方传播；高危路径禁止 `except Exception: pass` 式吞错 |
| `core/parser.py` | bencode 编解码 + .torrent/magnet 解析（纯实现，零额外依赖） | 不得移除 DoS 防御上限（MAX_TORRENT_SIZE / MAX_BENCODE_DEPTH / MAX_INT_DIGITS） |
| `core/secretbox.py` | 凭据保护：Windows DPAPI 对称封装代理密码 | 注册表（QSettings）不得存明文密码；解密失败返回空串而非抛异常 |
| `core/interfaces.py` | 契约声明：Resolver / StreamTransport / CachePolicy / ThemeSource 四个 `runtime_checkable` Protocol（共 16 个方法签名，Wave 3 引入，见 ADR-0005） | 纯声明层、运行时零依赖（不碰 libtorrent/Qt，跨模块类型只在 TYPE_CHECKING 块引用）；不得加实现代码，也不得让实现显式继承 Protocol（结构化类型，满足性由 contract_check [5] 结构检查+签名指纹对拍守门） |
| `core/exceptions.py` | 业务异常层级：MagnetError → ParseError / SessionError / DownloadError / CacheError / StreamError / StorageError（7 类，Wave 1 引入） | 只定义层级不做转换——既有对外契约的 ValueError/RuntimeError（抛出侧）不在层级内，收窄的是捕获侧 |

#### 数据层（任务状态与磁盘表示）

| 模块 | 职责（一行） | 禁止事项（一行） |
|------|-------------|-----------------|
| `core/registry.py` | 任务注册表与**单锁归属**：TaskRecord / torrents / tasks / 当前别名组 / 唯一 `threading.Lock` | 公开方法与 `*_locked` 版本绝不互相嵌套（锁不可重入）；锁外不得裸写注册表字段（R-1 教训） |
| `core/taskstore.py` | `.tasks.json` 任务清单持久化：load/save/atomic_write_bytes/normalize_info_hash（纯函数） | 不得引入 Qt / libtorrent 会话依赖；不得用非原子写（临时文件+os.replace 是唯一写路径） |
| `core/metacache.py` | 磁力链元数据缓存：`<cache>/.meta/<ih>.torrent`，解析/恢复/添加三链路先查缓存 | 只缓存 info 段与 tracker 列表，不缓存用户数据；非法键按未命中处理，绝不拼路径（防穿越） |
| `core/resume.py` | fastresume 路径约定与编解码（纯函数，复用自研 bencode） | 不用 `lt.bencode`（2.1.x 绑定对 int 值/键不兼容）；损坏 fastresume 静默重建，绝不阻断启动 |
| `core/persist.py` | 任务持久化编排：清单原子写、fastresume 请求/落盘/清理、启动逐任务恢复 | 不反向依赖 fetcher（能力经 PersistDeps 回调注入）；一切异常就地 log，绝不向上抛 |

#### 逻辑层（下载策略与可用性判定）

| 模块 | 职责（一行） | 禁止事项（一行） |
|------|-------------|-----------------|
| `core/scheduler.py` | 预览下载调度：单文件锁定 + 顺序分块窗口 + moov 尾窗优先 + deadline 递增 | 本模块是**锁序末端**：`scheduler._lock` 段内绝不取 reg.lock / 落盘 / 任何 I/O；begin/seek_to_byte 调 tick 前必须已放锁 |
| `core/preview.py` | PreviewCore：预览桥（启停/路径反查/点播）与状态快照 | piece_map/demand 返回 None/False 表示「无法判定」，调用方绝不可降级为「整文件可用」（稀疏零数据喂播放器是历史 P0 根源）；句柄绑定调用一律出锁 |
| `core/pieceview.py` | 块可用性视图：一次性基线 + 增量连续前缀，判定成本规模无关 | 刻意不做 TTL 缓存、不订阅告警、不引入事件驱动状态机（不变量「块落盘即恒 have」是唯一前提） |

#### 服务层（会话与外部世界）

| 模块 | 职责（一行） | 禁止事项（一行） |
|------|-------------|-----------------|
| `core/session.py` | libtorrent 会话生命周期：配置构造/端口回退/代理限速热更新/alert 循环/per-task 超时看门狗/fastresume 周期脏写 | 不反向依赖 fetcher（SessionDeps 显式注入）；退出路径绝不能卡住进程或吞用户数据 |
| `core/resolver.py` | 解析编排：resolve 两入口 / begin_resolve 换代 / connect_peer / 元数据就绪与完成告警链 | 依赖方向定死（resolver → registry/persist/taskops），永不反向；换代后旧解析必须自弃（gen 语义） |
| `core/proxycheck.py` | 代理可用性自检：SOCKS5/HTTP 真握手探测 + 面向用户的中文诊断文案 | core 层不 import Qt；探测可超时、可失败，不向调用方抛异常 |
| `core/inbound.py` | 入站能力自检：内网 IP 枚举 / SSDP 探测 UPnP / 手动端口转发引导文案 | 只用标准库（不引入 psutil/netifaces）；所有网络探测可超时、绝不抛异常 |

#### 编排层（组装与对外入口）

| 模块 | 职责（一行） | 禁止事项（一行） |
|------|-------------|-----------------|
| `core/fetcher.py` | SessionManager：UI 唯一入口 facade——组装全部子系统 + 薄委托 + 兼容别名（历史再导出） | 不依赖 Qt（回调由后台线程触发，UI 经 Qt 信号转发）；新增子系统不得继续膨胀 hub（瘦身方向见 ADR-0002） |
| `core/taskops.py` | TaskOps：下载任务全生命周期 CRUD（添加/转正/激活/暂停恢复/优先级/焦点/移除/tasks() 快照） | 依赖方向定死 taskops → registry/persist/taskstore/parser/states，永不反向；`with reg.lock` 段内只调 `*_locked`，scheduler.stop 必须出锁再调（R-10） |
| `core/stream_server.py` | 本地 HTTP 流服务：按分块可用性支持 Range 供播放器拉流（仅 127.0.0.1） | 不得按逻辑大小响应未下载区间——只暴露已下载分块（历史 P0：稀疏零数据喂播放器） |

#### 设施层（配置与缓存治理）

| 模块 | 职责（一行） | 禁止事项（一行） |
|------|-------------|-----------------|
| `core/config.py` | AppConfig：QSettings 持久化 DEFAULTS 配置 + lt_proxy_settings 代理映射（唯一映射点） | 代理密码必须经 secretbox 加密存储；回灌旧 settings 不得盖掉本次限速/代理参数 |
| `core/cache_guard.py` | 缓存目录守卫：高风险目录拒作缓存目录 + CACHE_MARKER 标记 + 清理保留名单 | 盘符根/主目录/文档目录直接拒绝；清理必须走 clear_cache_contents 保留名单（downloads/.tasks.json/.resume/.meta 绝不参与） |
| `core/cache_mode.py` | convert/hold 两档预览缓存模式常量 | 值域即校验，不新增校验逻辑（9 行枚举，无策略抽象） |
| `core/cache_quota.py` | 预览缓存 LRU 配额：超限按最近活跃时间整目录清理 `.preview/` | 只动 `.preview/`，downloads/.tasks.json/.resume 归 cache_guard 管辖不得涉足；活跃任务保护名单删除前须重取；绝不抛异常 |

### 2.2 依赖规则（分层契约）

1. **只允许上层依赖下层**（编排→服务→逻辑→数据→设施/基础），禁止反向 import。
   各文件头注释已把「不反向依赖 fetcher」写为纪律（session.py / persist.py /
   taskops.py / resolver.py）。
2. **已知偏差（登记在案）**：
   - `resolver.py`（服务层）import `taskops.py`（编排层，resolver.py:39 实证）——
     resolver 头注释自认「persist/taskops 无环下游」，属登记在案的层间例外；
     Wave 3 Protocol 化（ADR-0005）只声明不重构，本例外**维持原状**、未消除；
   - `session.py`（服务层）import `config.py`（设施层，session.py:31
     `from .config import lt_proxy_settings`）——设施层是横切底座，
     视为合法的下行依赖。
3. **UI 层 import 白名单（已落地，2026-10-10 Wave 2 实测 0 越层）**：`ui/*.py` 只准
   import `core.models` / `core.config` / `core.fetcher`（facade）/ `core.logutil`。
   曾经的越层实证（main_window 直连 cache_guard/cache_mode/cache_quota/stream_server/
   proxycheck/inbound，settings_dialog 直连 cache_guard/cache_mode，共 9 处）已全部
   收敛：SessionManager 补 7 个薄委托方法（preview_root /
   enforce_preview_cache_limit / preview_downloaded_bytes / proxy_hint /
   inbound_selfcheck / inbound_hint_text / create_stream_server，fetcher.py
   L426-484 实证），cache_guard/cache_mode
   显式 path 参数符号经 facade 模块级再导出（`__all__` 7 符号）。
   验收 Grep（ui/ 内 `^(from|import) core\.` 去掉白名单四类）= 0 命中，2026-10-10 复核。
4. **契约层例外**：`core/interfaces.py` 是纯声明层（4 个 Protocol，运行时零依赖、
   不 import 任何实现模块），可被任何层安全 import 而不构成层间边——当前生产码
   无 import 点（满足性检查在 tests/contract_check.py），属「先立契约、后接消费」
   的预留接口面（ADR-0005）。

### 2.3 UI 层（ui/，15 模块，不含 `__init__.py`；Wave 2 拆分后）

| 模块 | 职责 |
|------|------|
| `ui/main_window.py` | MainWindow：core 装配 + 视图组装 + `_Bridge`（QObject Signal，跨线程回调转 UI 线程）信号槽接线 + 停机守卫入口槽 + 关闭路径；实测 838 行（Wave 2 拆分前 1365 行；未达 ≤400 目标——关闭路径为 Qt 事件覆写必须留子类、视图装配与 22 个守卫入口槽留在本类，继续硬拆违反行为零变化，路线图执行日志 2026-10-10 诚实登记） |
| `ui/cache_host.py` | CacheHost：缓存清理入口（手动/退出共用）、活任务保护名单、LRU 配额触发、状态栏「缓存占用」刷新；宿主成员晚绑定（不快照 cache_dir/cfg/session） |
| `ui/stream_host.py` | StreamHost：StreamServer 实例构造/启停（经 facade `create_stream_server` 工厂）、磁盘路径→文件映射回调、开播门控、播放失败指数退避重试、流侧会话状态（pending_video/stream_url 等由本类持有） |
| `ui/task_controller.py` | TaskController：添加下载流程 + 任务 CRUD 槽（暂停/恢复/删除/优先级/打开目录）；停机守卫仍在 MainWindow 入口槽首行，本类方法是守卫通过后的执行体 |
| `ui/inbound_host.py` | InboundHost：入站能力探测链（后台线程 SSDP 探测 → `_InboundBridge` 信号回主线程 → 弹窗 → 复位按钮）；探测调用经 facade `inbound_selfcheck`/`inbound_hint_text` |
| `ui/theme.py` | 全局设计系统：THEMES 注册表（LIGHT/DARK 经 `register_theme` 进表，Wave 3）+ SP_* 间距 + QSS（见 §6） |
| `ui/style.py` | ChevronStyle：下拉/微调箭头基样式（须在 apply_theme 之前安装） |
| `ui/preview_pane.py` / `preview_player.py` | 预览面板 / 内嵌 QMediaPlayer 播放 |
| `ui/downloads_pane.py` / `file_tree.py` / `status_panel.py` | 下载列表 / 文件树 / 状态栏 |
| `ui/add_download_dialog.py` / `settings_dialog.py` | 添加下载 / 设置面板 |
| `ui/gallery.py` | 图片画廊（按需下载 + 缩略图 + 大图缩放） |

### 2.4 测试布局（Wave 2 目录化，2026-10-10）

- **`tests/`**：29 个套件脚本全部迁入（`regression_run.py` SUITES 登记的
  27 套 + `live_test` + `pack_check`）。套件脚本自带双层 dirname 的
  sys.path 引导，从 tests/ 内直接运行与经运行器调度等价。
- **仓库根**：`regression_run.py`（全量回归运行器）、`coverage_run.py`、
  `test_support.py`（共享夹具）、`diag_*.py` / `probe_gate_redesign.py` /
  `ui_shot.py`（诊断与工具脚本）留根——它们不是 pytest 套件而是入口/工具。
- 门禁断言（contract_check / theme_check / concurrency_test AST 扫描等）
  锚定源码路径的写法随目录化同步修复（路线图执行日志 W2 A7）。

---

## 三、并发模型

一手来源：`core/registry.py` 头注释、`core/scheduler.py` 线程安全节、
`core/preview.py` 锁纪律节。

1. **单锁归属**：`TaskRegistry.lock`（`threading.Lock`，registry.py:120）是
   全项目**唯一任务锁**；SessionManager 的 `_lock` 只是它的 property 代理
   （fetcher.py:227-229）。taskops/resolver/preview 一律借用这一把锁。
   调度器另持**私有短锁** `scheduler._lock`，PieceView 持 `_lock`，
   PreviewCore 持 `_views_lock`（叶子锁）——均为对象私有，不参与任务锁序。
2. **`*_locked` 命名纪律**：「一次调用一个锁段」——公开方法自持锁；名字以
   `_locked` 结尾的版本要求调用方**已持锁**（复合临界区用）。两者绝不互相
   嵌套调用（`threading.Lock` 不可重入）。
3. **锁序（单向，必须遵守）**：`reg.lock` → `scheduler._lock` → `PieceView._lock`。
   scheduler 是锁序末端：其锁段内绝不取 reg.lock / 落盘。历史锁序边
   （持 reg.lock 调 scheduler.stop，审计 R-10）已在 taskops.py 出锁修复
   （remove_task：L434-444 锁内记 `stop_sched` 标志、L473-476 出锁再停；
   set_focus：L541-550 同款）。scheduler 侧不变式（锁段内不调 `_LOCK_TAKING`
   七入口、begin/seek_to_byte 调 tick 前必须已放锁）由 concurrency_test §F
   静态检查 + 确定性反例自检钉住。
4. **锁内禁 I/O**：任务锁段内禁止落盘 / fsync / `remove_torrent` 等阻塞调用；
   `tasks()` 快照锁内只做字典浅拷贝，`handle.status()` 等 libtorrent 绑定调用
   移出锁外派生（R-3）。scheduler 锁内的 libtorrent 调用是**登记在案的例外**
   （非阻塞状态变更、私有短锁、热路径非阻塞取锁跳过本拍），前提「锁内只做
   非阻塞状态变更」，若引入阻塞调用例外即失效。
   私有锁同样受此纪律约束：`core/metacache.py` 剪枝锁 `_PRUNE_LOCK` 的
   `_count_entries`（scandir I/O）两处路径——主 rescan 路径 2026-09-29 审计
   已外移；剪枝后重扫计数是**同型漏修点**，2026-10-10（Wave 3 A7，Phase 5.1）
   改为先锁外扫描、再回锁只写结果（metacache.py:456-466），由
   concurrency_test §E 的同型 AST 断言（敏感名单 `_count_entries` ×
   锁名 `_PRUNE_LOCK`）钉住防再犯。
5. **跨线程回调走 `_Bridge` Signal**：core 层不 import Qt；后台线程
   （alert 循环、流服务请求线程、inbound 探测线程）的回调经
   `ui/main_window.py` 的 `_Bridge`（main_window.py:60）与 `ui/inbound_host.py`
   的 `_InboundBridge`（inbound_host.py:21，Wave 2 拆分随入站探测链迁入协作者）
   （QObject + Signal）marshal 回 UI 线程。拆分 main_window 时 `_Bridge` 模式
   保留、不新增 Qt 线程模型（路线图 3.1）。

---

## 四、错误处理策略

现状（2026-10-10 实测）：core/ 宽泛捕获（`except Exception`/`except BaseException`
代码行）从 Wave 0 基线 132 处收窄至 **27 处**（Wave 1 A2 两批收窄，路线图
执行日志登记 26 处为当时代码口径，本次复核含 taskstore 原子写清理的
1 处 `except BaseException` 共 27）。两类场景区分对待（规则本体来自
路线图 3.4 节「异常层级替换的边界」）：

1. **业务分支捕获必须收窄**：解析、任务操作、持久化等可预期失败路径，
   应捕获具体异常类型，防止 TypeError/AttributeError 等编程错误被当作
   业务异常吞掉。
2. **基础设施兜底保留宽泛 + 必须 log_exception**：HTTP 服务 handler、
   alert 循环、清理路径等「进程不可因单请求/单任务崩溃」的边界，保留
   `except Exception` 但必须记录（`core/logutil.py` 头注释：历史上多次因
   `except Exception: pass` 掩盖真实缺陷）。宽泛兜底点须注释说明为何宽泛。
   保留的 27 处分布（按文件）：stream_server 6 / fetcher 5 / session 5 /
   cache_quota 2 / persist 2 / resolver 2 / config 1 / logutil 1 / preview 1 /
   taskops 1 / taskstore 1——全部有 log + 中文兜底理由注释。

**异常层级（已落地，Wave 1）**：`core/exceptions.py`（7 类：MagnetError →
ParseError / SessionError / DownloadError / CacheError / StreamError /
StorageError）已入库（554385b）。纪律：收窄的是**捕获侧**；既有对外契约
的 ValueError/RuntimeError（抛出侧，如 `TaskOps.add_task` 空源、会话未启动）
**不转换**，保持回归基线。层级类当前主要作领域语义预留，业务分支按
真实抛出面收窄为具体内置异常。

---

## 五、安卓端架构与双端对应表

安卓端为 Kotlin + Compose 原生重写（`android/app/src/main/java/com/bitseed/magnetviewer/`），
分层：`ui/`（Compose 屏幕 + ViewModel）→ `engine/`（BtEngine 会话/注册表/调度）→
`stream/`（StreamServer）→ `data/`（存储/配额/设置/日志）→ `core/`（模型/状态/HTTP/调度计划纯逻辑）。

| 安卓端 | 桌面端 | 对应关系（安卓文件头注释实证） |
|--------|--------|-------------------------------|
| `engine/BtEngine.kt` | `core/session.py` + `core/fetcher.py` + `core/registry.py` | 会话生命周期+注册表+解析编排合体；头注明确标注「任务注册表=桌面 core/registry.py 的最小对应物」「端口回退与桌面 session 同型」 |
| `engine/PreviewScheduler.kt` | `core/scheduler.py` | 「桌面 core/scheduler.py:PreviewScheduler 的 Kotlin 移植」 |
| `engine/PieceView.kt` | `core/pieceview.py` | 「plan/09 方案 D 的逐条移植」，`*_locked` 纪律与桌面同款 |
| `stream/StreamServer.kt` | `core/stream_server.py` | 「照搬桌面语义」：503+Retry-After、X-Why/X-Ready-Bytes 诊断头、15s handler timeout 均对齐 |
| `data/TaskOps.kt` | `core/taskops.py` | 「语义对齐」，头注释如实登记两处差异（优先级映射落点、转正目录搬迁） |
| `data/TaskStore.kt` / `ResumeStore.kt` / `MetaCache.kt` | `core/taskstore.py` / `resume.py` / `metacache.py` | `.tasks.json` 原子写 / `.resume/<ih>.fastresume` / `.meta/<ih>.torrent` 同布局 |
| `data/CacheQuota.kt` | `core/cache_quota.py` | 「逐条移植；纯逻辑 + 显式依赖，JVM 可测」 |
| `data/SettingsStore.kt` | `core/config.py` | 键名/值域与桌面 DEFAULTS 一致（DataStore 持久化替代 QSettings） |
| `data/AppLog.kt` | `core/logutil.py` | 运行日志总开关 `logging_enabled` 同款语义，绝不抛 |
| `core/states/TaskStates.kt` | `core/states.py` | 「逐字对齐」STATE_* 常量 |
| `core/CacheMode.kt` | `core/cache_mode.py` | 「逐字移植；值域即校验」 |
| `core/model/*`（ParseResult/TorrentFile/PieceMap） | `core/models.py` | 结构对齐；PieceMap 换算语义由对拍夹具冻结 |
| `theme/Theme.kt` + `Color.kt` + `Spacing.kt` + `Type.kt` | `ui/theme.py` | 语义对齐（见 §6） |
| `service/DownloadService.kt` | （无对应） | 安卓特有：dataSync 前台服务灭屏继续下载 |

**一致性保障**：跨语言对拍夹具（`android/tools/gen_parity_fixtures.py`，
当前夹具 `android/app/src/test/resources/parity/parity.json` 实测 14 组/156 条，
2026-10-10 复核；初版 523fd1c 为 13 组/155 条，S6/S8 审计修复后新增分组成员）
冻结 PieceMap/连续前缀语义；Phase 4 行为契约文档（`docs/contract_spec.md`，待产出）
覆盖 6 大核心流程。双轨策略的取舍见 ADR-0004。

---

## 六、主题令牌体系

一手来源：`ui/theme.py` 头注释、`android/.../theme/Theme.kt` 头注释。
机器门禁：`tests/theme_check.py`（回归套件之一）。

### 6.1 桌面端（ui/theme.py）

- **调色板注册表（Wave 3）**：`THEMES: dict`（主题名 → 调色板 dict），内置
  `LIGHT` / `DARK` 均经 `register_theme(name, palette)` 进表；未来自定义主题
  同走该入口，无需改 `apply_theme`。注册纪律：键集必须覆盖
  `_REQUIRED_PALETTE_KEYS`（缺键在注册点抛 ValueError，挡在热切换 KeyError
  之前）；重复名**拒绝、首注册者胜**（与 core.config.register_config_key 同一
  纪律，防生效主题取决于注册顺序）。`PALETTES` 为 THEMES 的同一对象别名
  （既有读法不受影响）。`THEMES` 满足 `core.interfaces.ThemeSource` 契约面
  （模块本体即实现，contract_check [5]/[7] 守门）。
- **双调色板**：`LIGHT`（默认）/ `DARK`，两套键集完全一致——固定 17 键
  （bg / bg_panel / bg_input / bg_hover / bg_selected / border / border_strong /
  text / text_muted / text_dim / accent / accent_hover / accent_pressed / ok /
  warn / danger / segment）+ 渲染扩展 4 键（alt_row / image_bg / bg_pressed /
  bg_compartment）。
- **运行时热切换**：`apply_theme(app, mode)`，mode ∈ light/dark/system；
  模块级常量（BG/BG_PANEL/…）始终等于当前激活色板取值。自绘控件必须在
  **绘制时**读模块属性（`import ui.theme as theme` 后 `theme.SLIDER_SEGMENT`），
  不得 `from ui.theme import X` 固化导入期快照。
- **间距令牌**：`SP_XS, SP_SM, SP_MD, SP_LG, SP_XL = 4, 8, 12, 16, 24`（8px 网格）；
  控件圆角 8、卡片/分组框/容器圆角 10；正文 14px、次级 13px、标题 16px。
- **硬规则（tests/theme_check.py 强制）**：
  1. `ui/theme.py` 之外任何 `ui/*.py` **不得出现 hex 色值**；
  2. 不得调 `setStyleSheet`——局部样式一律 `setObjectName` + theme.py 内
     QSS 规则（动态属性选择器，改属性后 `style().unpolish/polish`）；
  3. 下拉/微调右侧分区与箭头全部交 QSS 子控件（`::drop-down`/`::up-button`），
     SVG 资源在 `ui/assets/`，经 `asset_path` 解析（兼容 PyInstaller `_MEIPASS`）。

### 6.2 安卓端（theme/Theme.kt）

- `Palette.Light` 与桌面 LIGHT 字典**逐值对齐**；`Palette.Dark` 不新造色值，
  仅按深色语义重新分配（Phase 0 修复后 Dark 的 ok/danger/altRow 等容器色
  改用独立 DarkPal 值，解决「同色字底不可读」P0×3）。
- 间距 `theme/Spacing.kt`（xs=4/sm=8/md=12/lg=16/xl=24，4dp 网格）、
  字号 `theme/Type.kt`（ui/ 不得裸写 fontSize）、圆角 `LocalShapes` 三档
  （卡 16 / 按钮 12 / 芯片全圆）——与桌面同一套令牌语义，Compose 载体。
- 色板经 `LocalPal` CompositionLocal 下发，状态徽章底-字组合必须走
  LocalPal 语义对，杜绝裸 `(bg, fg)` 配色。
