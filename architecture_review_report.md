# magnet-viewer 综合架构评估报告

> 审查日期：2026-10-09  
> 审查范围：core/ (24 模块)、ui/ (11 模块)、android/ (30+ Kotlin 文件)、测试体系 (27 文件)、构建配置、文档

---

## 一、项目概况

| 维度 | 桌面端 | Android 端 |
|------|--------|------------|
| 语言 | Python 3.13 + PySide6 (Qt6) | Kotlin + Jetpack Compose |
| 核心库 | libtorrent (Python 绑定) | libtorrent4j |
| 代码量 | core/ 398KB + ui/ 199KB (~8,900 行) | 30+ Kotlin 文件 |
| 测试量 | 27 文件 / ~691KB / 25 套件 | 尚未接入 CI |
| 构建 | PyInstaller (onefile/onedir) | Gradle + R8 |
| CI | GitHub Actions (Windows) | GitHub Actions (Linux) |

**核心功能**：磁力链/种子实时解析 → 边下边播预览 → 完整下载管理，双平台实现。

---

## 二、模块依赖关系分析

### 2.1 依赖拓扑

通过对全部 import 语句的实证分析，core/ 内部形成以下依赖层次（无显式分层，实测推导）：

```
入口层    main.py → ui.main_window → core.fetcher (hub)
                                                      │
基础层    models.py ← parser.py ← states.py ← logutil.py
                                    │
数据层   registry.py ← metacache.py ← taskstore.py
              │           │
逻辑层   scheduler.py ← preview.py ← pieceview.py
              │           │
服务层   session.py ← resolver.py ← persist.py ← resume.py
              │           │
编排层   fetcher.py (hub) ← taskops.py ← stream_server.py
              │
设施层   config.py ← secretbox.py ← proxycheck.py
                          cache_guard.py ← cache_quota.py ← cache_mode.py
```

### 2.2 关键发现：fetcher.py 枢纽反模式

[fetcher.py](file:///c:/Users/Administrator/Desktop/magnet-viewer/core/fetcher.py) 以 635 行代码直接 import 了 **14 个 core 子模块**：

```python
from .registry import (...)      # TaskRegistry, hash_key, ih_from_params, DOWNLOADS_SUBDIR, METADATA_TIMEOUT...
from .session import (...)       # SessionCore, SessionDeps, DHT_STATE_FILE
from .scheduler import (...)     # PreviewScheduler
from .resolver import (...)      # ResolverCore, result_from_torrent_info
from .taskops import (...)       # TaskOps
from .persist import (...)       # TaskPersistence, PersistDeps, is_within, safe_task_save_path, save_subdir_of, task_dir
from .preview import (...)       # PreviewCore, STATE_NAMES
from .models import (...)        # ParseResult, TorrentFile, PieceMap, disk_root
from .states import (...)        # BOOTSTRAP_TRACKERS, DOWNLOAD_STATES, STATE_COMPLETED...
from .taskstore import (...)     # load_tasks, save_tasks, atomic_write_bytes, normalize_info_hash
from .cache_guard import (...)   # ensure_cache_dir, sweep_tmp_files
from .cache_mode import PREVIEW_CACHE_CONVERT
from .logutil import log_exception, log_warning
from . import proxycheck
```

**问题**：`SessionManager` 类充当事实上的「上帝对象」，组装并代理了 Registry、Scheduler、Persistence、TaskOps 等全部子系统的职责入口。任何新模块的加入都需要修改 fetcher.py 的 import 和初始化逻辑，违反开闭原则。

### 2.3 模块大小分布

| 行数区间 | 文件 | 评估 |
|----------|------|------|
| >600 行 | session.py (725)、scheduler.py (687)、fetcher.py (635) | **偏大**，需考虑拆分 |
| 400-600 | taskops.py (568)、stream_server.py (551)、ui/main_window.py (**1224**) | main_window 严重超标 |
| 300-400 | metacache.py (439)、resolver.py (422) | 可接受但接近上限 |
| <300 | 其余 19 个模块 | 大小合理 |

**重点关注**：
- [main_window.py](file:///c:/Users/Administrator/Desktop/magnet-viewer/ui/main_window.py) 1224 行，是全项目最大文件，承担了 UI、业务调度、线程管理、缓存清理等多重职责
- [session.py](file:///c:/Users/Administrator/Desktop/magnet-viewer/core/session.py) 725 行，混合了会话配置、alert 循环、超时看门狗、fastresume 写入

---

## 三、分层架构评估

### 3.1 当前状态：无显式分层

项目采用 **扁平包结构**（`core/` + `ui/`），内部无子包划分。通过代码实证推导出的隐式分层如上文拓扑图所示，但存在以下违反：

| 违反类型 | 位置 | 证据 |
|----------|------|------|
| UI 直接访问 Core 内部 | [main_window.py:L17-L28](file:///c:/Users/Administrator/Desktop/magnet-viewer/ui/main_window.py#L17-L28) | 直接 import `cache_guard`、`cache_quota`、`stream_server`、`proxycheck` 等 12 个 core 模块 |
| UI 承担业务逻辑 | main_window.py:L636-L644 | UI 层直接启动 `core.inbound` 线程、管理 stream_server 生命周期 |
| 无抽象层 | 全局 | 缺少 `services/` 或 `facade/` 层，UI 直接操作 core 内部实现 |

### 3.2 建议分层模型

```
ui/                     ← 纯视图层（Qt Widget / Composable）
  │ 仅依赖 application/
application/            ← 应用服务层（编排、DTO 转换）[新增]
  │ 依赖 domain/
domain/                 ← 领域模型 + 接口定义
  │ 依赖 infrastructure/
infrastructure/         ← libtorrent、文件系统、网络、持久化
```

---

## 四、并发与锁纪律

### 4.1 优势

1. **`*_locked` 命名约定**：[registry.py:L2-L21](file:///c:/Users/Administrator/Desktop/magnet-viewer/core/registry.py#L2-L21) 明确文档化了锁纪律——`with reg.lock` 内只调 `*_locked` 后缀方法，不持锁读写边界外操作
2. **单锁归属**：TaskRegistry 持有唯一 `threading.Lock()`，避免多锁死锁风险
3. **锁内快照拷贝**：`tasks()` 方法在锁内仅做浅拷贝，减少锁持有时间

### 4.2 不足

| 问题 | 位置 | 风险 |
|------|------|------|
| `_views` 字典无锁读写 | [preview.py:L60-L70](file:///c:/Users/Administrator/Desktop/magnet-viewer/core/preview.py#L60-L70) | 流服务探测线程与回收线程并发迭代 → 偶发 RuntimeError → 503 |
| UI 层直接操作线程 | [main_window.py:L636-L644](file:///c:/Users/Administrator/Desktop/magnet-viewer/ui/main_window.py#L636-L644) | UI 直接 `threading.Thread` 启动 inbound 服务，无生命周期管理抽象 |
| 锁序边冲突 | taskops.py:L438,530 | 持 `reg.lock` 调 `scheduler.stop`，存在锁序边（R-10 决策未完全闭环） |

---

## 五、错误处理机制

### 5.1 现状

全项目 core/ 目录有 **50+ 处 `except Exception`**（经 Grep 实证）：

| 模块 | except Exception 数量 | 评估 |
|------|----------------------|------|
| resolver.py | 12 | 最多，解析路径多分支容错 |
| fetcher.py | 7 | 核心编排层，需统一 |
| taskops.py | 7 | 任务操作层 |
| registry.py | 5 | 注册表层 |
| inbound.py | 5 | 入站服务 |
| cache_quota.py | 3 | 缓存配额 |
| logutil.py | 6 (自身容错) | 日志基础设施 |
| 其余 | 5+ | 分散 |

### 5.2 问题

1. **宽泛捕获**：绝大多数使用 `except Exception as e:` 而非具体异常类型，掩盖编程错误（如 TypeError、AttributeError）被当作业务异常处理
2. **无异常层级**：缺少自定义异常体系（如 `MagnetError → ParseError / SessionError / DownloadError`），所有异常均为原生或 libtorrent 抛出
3. **历史债务**：[logutil.py:L5](file:///c:/Users/Administrator/Desktop/magnet-viewer/core/logutil.py#L5) 明确记录"历史上多次因 `except Exception: pass` 掩盖真实缺陷"——虽已改为 log_warning，但结构未变

### 5.3 建议

```python
# 建立 exception hierarchy
class MagnetError(Exception): ...
class ParseError(MagnetError): ...
class SessionError(MagnetError): ...
class DownloadError(MagnetError): ...
class CacheError(MagnetError): ...

# 逐步将 except Exception 替换为具体类型
# except ParseError → 解析失败路径
# except SessionError → 会话异常路径
```

---

## 六、接口设计规范性

### 6.1 优势

1. **依赖注入模式**：`PersistDeps`、`SessionDeps` 等 dataclass 注入依赖，而非全局单例
2. **属性代理**：`SessionManager` 通过 `@property` 代理 registry 的 `lock`、`tasks()` 等，减少直接耦合
3. **文件头文档**：每个 core 模块顶部都有职责说明和锁纪律文档（registry.py、session.py、taskops.py 等）

### 6.2 不足

1. **无抽象基类/Protocol**：所有类都是具体实现，无 `abc.ABC` 或 `typing.Protocol` 定义接口，无法 mock 测试或替换实现
2. **`# noqa: F401` 再导出**：[fetcher.py:L33-L60](file:///c:/Users/Administrator/Desktop/magnet-viewer/core/fetcher.py#L33-L60) 有 6 处 `noqa: F401` 标记的再导出，说明 fetcher.py 充当非正式的 barrel export——但这是隐式的，无 `__all__` 定义
3. **无 `__all__` 声明**：除 cache_mode.py 外，所有模块均无 `__all__`，公共 API 边界不明确

---

## 七、扩展性设计

### 7.1 配置化

[config.py](file:///c:/Users/Administrator/Desktop/magnet-viewer/core/config.py) 以 `DEFAULTS` 字典 + QSettings 实现配置管理：

**优势**：
- 代理密码加密存储（secretbox.py）
- 热切换支持（ui_theme 运行时切换）
- 配置项集中定义

**不足**：
- 无插件注册机制，新增功能需修改 config.py 源码
- 无配置 schema 校验（类型仅靠运行时检查）
- QSettings 绑定 Qt，Android 端需另实现（SettingsStore.kt），配置层不跨平台

### 7.2 插件化

**当前状态：无插件架构**

- 流服务 (`stream_server.py`) 硬编码 HTTP 实现，无协议抽象
- 解析器 (`resolver.py`) 硬编码 libtorrent 路径，无解析策略接口
- 缓存策略 (`cache_mode.py` 仅 9 行枚举) 无策略模式抽象

**建议引入的扩展点**：

| 扩展点 | 当前实现 | 建议接口 |
|--------|---------|---------|
| 解析策略 | 硬编码 libtorrent | `Protocol Resolver: def resolve(uri) -> ParseResult` |
| 流媒体协议 | 硬编码 HTTP | `Protocol StreamServer: def serve(request) -> Response` |
| 缓存清理策略 | 硬编码 cache_guard | `Protocol CachePolicy: def evict(context) -> list[Path]` |
| 主题系统 | 固定 LIGHT/DARK | 注册式 `ThemeRegistry.register(name, palette)` |

---

## 八、跨平台架构

### 8.1 代码复用现状

| 功能域 | 桌面端 (Python) | Android 端 (Kotlin) | 复用方式 |
|--------|-----------------|---------------------|---------|
| 引擎管理 | session.py + fetcher.py | BtEngine.kt | **无复用**，完全重写 |
| 流媒体服务 | stream_server.py | StreamServer.kt | **无复用**，逻辑相同 |
| 任务操作 | taskops.py | TaskOps.kt | **无复用**，逻辑相同 |
| 任务存储 | taskstore.py | TaskStore.kt | **无复用**，逻辑相同 |
| 缓存配额 | cache_quota.py | CacheQuota.kt | **无复用**，逻辑相同 |
| 主题系统 | theme.py (QSS) | Theme.kt (Compose) | **语义对齐**（Theme.kt:L16 注明与桌面 theme.py 对应） |
| 数据模型 | models.py | model/ 子包 | **结构对齐**，无代码复用 |

### 8.2 问题

1. **双轨维护成本**：核心逻辑在 Python 和 Kotlin 中各实现一遍，bugfix 需双端口同步（如 audit_report 已记录的 P1 #4 cache_guard 与 BtEngine #5 注解错位是同类锁问题在两端分别出现）
2. **无共享协议定义**：缺少跨平台的接口契约（如 protobuf/JSON schema），两端行为一致性靠人工维护
3. **Android 端未接入 CI 测试**：`.github/workflows/android.yml` 存在但无单元测试

### 8.3 建议

- **短期**：建立「行为契约文档」——用平台无关的伪代码/流程图描述每个核心流程，两端实现后互验
- **中期**：提取共享数据模型为 JSON Schema 或 protobuf 定义，两端生成各自语言绑定
- **长期**：评估将核心引擎逻辑移入 Rust/Go 共享库，两端 FFI 调用

---

## 九、测试体系评估

### 9.1 优势

1. **25 套回归测试**，覆盖核心模块：[regression_run.py](file:///c:/Users/Administrator/Desktop/magnet-viewer/regression_run.py) 编排全部套件，含退出码语义（0=PASS / 1=FAIL / 2=SKIP）
2. **测试基础设施完善**：[test_support.py](file:///c:/Users/Administrator/Desktop/magnet-viewer/test_support.py) 提供本机做种端、种子构造、磁盘快照、轮询等待等共享工具
3. **多维测试**：冒烟测试 (smoke 1082 行)、并发测试 (concurrency 939 行)、性能测试 (perf_cache 496 行)、契约测试 (contract_check) 覆盖面广
4. **CI 集成**：GitHub Actions Windows runner + Python 3.13 + locked dependencies
5. **覆盖率配置**：.coveragerc 启用分支覆盖 + 并行模式

### 9.2 不足

| 问题 | 影响 |
|------|------|
| 测试文件散落在根目录 | 27 个 `*_test.py` 文件未组织到 `tests/` 目录，工作区混乱 |
| 无 mock/隔离 | 搜索未发现 `unittest.mock` 或 `patch` 使用，测试依赖真实 libtorrent + 网络 |
| Android 端无 CI 测试 | android.yml 工作流存在但仅构建，无单元测试执行 |
| 无覆盖率门槛 | .coveragerc 有配置但 CI 中未强制覆盖率阈值 |
| 性能测试环境依赖 | perf_cache_test.py 等依赖本地 IO 和网络，在 CI 中可能不稳定 |

---

## 十、文档完整性

### 10.1 现有文档

| 文档 | 大小 | 评估 |
|------|------|------|
| [README.md](file:///c:/Users/Administrator/Desktop/magnet-viewer/README.md) | 37KB | 详尽，含功能概述、运行/构建说明、打包流程 |
| [full_audit_report_2026-09-29.md](file:///c:/Users/Administrator/Desktop/magnet-viewer/full_audit_report_2026-09-29.md) | 12KB | 全项目审计报告，含 P0-P3 问题清单 |
| audit_report.md | 14KB | 较早的审计报告 |
| REVIEW.md / REVIEW-2026-09.md | 18KB + 9KB | 代码审查记录 |
| plan/ 目录 | 存在 | 设计文档（未深入审查） |

### 10.2 缺失

- **无架构文档**：缺少 `ARCHITECTURE.md` 或 `docs/architecture/` 描述系统设计决策、模块关系、数据流
- **无 API 文档**：缺少 `docs/api/` 或自动生成的 API 参考
- **无贡献指南**：缺少 `CONTRIBUTING.md`
- **无 CHANGELOG**：缺少变更日志
- **无 ADR**：缺少架构决策记录（Architecture Decision Records）

---

## 十一、性能优化潜力

### 11.1 已识别的性能热点

| 位置 | 问题 | 来源 |
|------|------|------|
| preview.py `_views` 无锁 | 并发字典迭代偶发 503 | audit_report P2 #8 |
| metacache.py:L428-441 | 锁内做 `scandir` I/O | audit_report P3 |
| persist.py:L288-292 | 锁外迭代活字典 | audit_report P3 |
| stream_server.py 无 Range 200 语义 | Content-Length 语义错误 | audit_report P2 #11 |

### 11.2 优化建议

1. **`_views` 改用 `concurrent.ThreadSafeDict` 或 `copy.copy` 快照迭代**：消除偶发 503
2. **锁内 I/O 外移**：metacache.py 的 `scandir` 改为锁内收集路径、锁外执行 I/O
3. **流服务 Range 支持**：补全 RFC 7233 语义，避免播放器误判截断
4. **懒加载/延迟初始化**：`SessionManager` 组装了全部子系统，但非所有场景都同时需要——可按需初始化减少内存占用

---

## 十二、综合评分

| 维度 | 评分 (1-5) | 关键依据 |
|------|-----------|---------|
| 模块划分合理性 | 3.0 | core/ 模块职责清晰但无子包分层，fetcher.py 枢纽反模式 |
| 代码组织结构 | 2.5 | 扁平结构，无 services/domain 层，测试文件散落根目录 |
| 依赖关系管理 | 2.5 | 无抽象层，UI 直接 import core 内部 12+ 模块 |
| 接口设计规范性 | 3.0 | 有 DI 和 property 代理，但无 Protocol/ABC/__all__ |
| 数据流程清晰度 | 3.5 | 文件头文档详尽，锁纪律文档化，但缺全局数据流图 |
| 错误处理机制 | 2.5 | 50+ 宽泛 except Exception，无自定义异常层级 |
| 扩展性设计 | 2.0 | 无插件架构，无策略接口，配置不可扩展 |
| 文档完整性 | 3.0 | README 详尽，但缺架构/API/ADR 文档 |
| 测试覆盖率 | 3.5 | 25 套件 + CI 集成，但无 mock 隔离、无覆盖率门槛 |
| 性能优化潜力 | 3.0 | 已识别热点，有优化空间但非瓶颈 |
| 跨平台复用 | 2.0 | 核心逻辑完全双写，仅主题语义对齐 |
| **加权综合** | **2.8** | 中等偏上，有清晰工程意识但缺少架构抽象 |

---

## 十三、改进建议（按优先级排序）

### P0 — 架构骨架重构（阻断性）

1. **引入 application/ 服务层**：在 ui/ 和 core/ 之间增加编排层，封装 `SessionManager` 的初始化逻辑和跨子系统操作，使 UI 仅依赖 application 层
2. **拆分 main_window.py**：将 1224 行的 MainWindow 拆为 MainWindow (纯视图) + DownloadController (下载编排) + StreamController (流媒体管理) + CacheController (缓存管理)
3. **建立异常层级**：创建 `core/exceptions.py`，定义 MagnetError 体系，逐步替换 `except Exception`

### P1 — 扩展性基础

4. **引入 Protocol 抽象**：为 Resolver、StreamServer、CachePolicy 定义 `typing.Protocol`，使实现可替换
5. **配置可扩展化**：config.py 增加 `register_config_key()` 接口，新功能可注册配置项而不改源码
6. **core/ 子包划分**：将 24 个扁平 .py 文件按职责分为 `core/engine/`、`core/storage/`、`core/streaming/`、`core/infra/`

### P2 — 工程规范

7. **测试目录化**：将 27 个 `*_test.py` 迁移到 `tests/` 目录，减少工作区混乱
8. **覆盖率门槛**：CI 中增加 `--fail-under=70` 阈值
9. **Android CI 测试**：接入 Kotlin 单元测试到 android.yml
10. **架构文档**：新增 `ARCHITECTURE.md` 描述分层模型、数据流、模块关系

### P3 — 跨平台优化

11. **行为契约文档**：用平台无关伪代码描述核心流程，两端互验
12. **共享数据模型**：提取 JSON Schema 定义，两端生成绑定
13. **评估 Rust 共享核心**：长期路线，将引擎逻辑移入 Rust FFI

### P4 — 性能微调

14. 修复 preview.py `_views` 并发安全
15. 外移 metacache.py 锁内 I/O
16. 补全 stream_server Range 语义

---

## 十四、优势总结

1. **锁纪律文档化**：`*_locked` 约定 + 文件头职责说明，并发设计意图清晰
2. **测试投入深**：25 套件 + 契约测试 + 性能基准 + 并发压测，远超同类项目
3. **构建链完整**：PyInstaller spec 含瘦身过滤 + pack_check 自检 + locked dependencies
4. **审计文化好**：多轮 audit report 留痕，P0-P3 分级清晰，有行动清单
5. **主题令牌体系**：LIGHT/DARK 语义色 + 间距 token (SP_XS/SM/MD/LG/XL) + 运行时热切换
6. **双平台语义对齐**：Android Theme.kt 明确标注与桌面 theme.py 的对应关系

---

## 十五、核心风险

| 风险 | 严重度 | 说明 |
|------|--------|------|
| main_window.py God Object | **高** | 1224 行、12+ core import，任何 UI 变更波及面大 |
| fetcher.py 枢纽 | **高** | 14 个 import，新模块必改此文件 |
| 双轨维护 | **高** | 核心逻辑 Python/Kotlin 双写，缺陷修两次 |
| 宽泛异常处理 | **中** | 50+ except Exception，掩盖编程错误 |
| 无扩展点 | **中** | 无 Protocol/ABC，替换实现需改源码 |
| 测试无隔离 | **中** | 无 mock，依赖真实 libtorrent + 网络 |

---

*本报告基于全项目源码静态审查 + 依赖分析 + 已有审计报告交叉验证生成。*
