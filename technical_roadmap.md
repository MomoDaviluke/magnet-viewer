# magnet-viewer 架构演进技术路线图

> 制定日期：2026-10-09  
> 依据：architecture_review_report.md（综合评分 2.8/5）+ full_audit_report_2026-09-29.md（P0×3/P1×3/P2×11/P3×16）  
> 状态：待用户批准后启动执行

---

## 一、目标体系

### 1.1 北极星目标

将项目从「扁平实现、双轨维护、无抽象层」演进为「分层清晰、接口可替换、单源契约」的可长期维护架构，综合评分 2.8 → **3.8+/5**。

### 1.2 量化目标（验收基线）

| 指标 | 基线（2026-10-09 实测） | 目标 | 终测（2026-10-11 Wave 5） | 度量方法 |
|------|------|------|------|------|
| main_window.py 行数 | 1224 | ≤ 400 | **838**（未达标，已登记合理保留：关闭路径/视图组装/22 守卫槽，见 ADR-0003） | `Measure-Object -Line` |
| fetcher.py 相对导入行数 | 22 | ≤ 10 | **10 ✅** | Grep `^from \.` |
| core/ 宽泛 `except Exception` | 132 | ≤ 40（保留边界兜底） | **28 ✅**（26 基础设施兜底 + A2b config 容错 1 + A3 facade 1，均有 log+理由注释） | Grep `except\s+(Exception|BaseException)` |
| UI 层越层 import core 内部 | 6 处（实测 9） | 0（全部经 facade） | **0 ✅** | Grep `^from core\.(?!models|config|fetcher|logutil)` in ui/ |
| Protocol/ABC 抽象接口 | 0 | ≥ 4 | **4 ✅**（Resolver/StreamTransport/CachePolicy/ThemeSource） | 代码检索 |
| 测试文件组织 | 27 个散落根目录 | 全部入 `tests/` | **29/29 入 tests/ ✅** | 目录检查 |
| CI 覆盖率门槛 | 无 | ≥ 70%（core/，分支覆盖） | **81% 基线，fail-under=70 硬门禁 ✅** | coverage --fail-under |
| Android CI 单元测试 | 未接入 | 已接入 android.yml | **已接入，364/364 绿 ✅** | workflow 检查 |
| 跨平台行为契约 | 无 | contract_spec.md 覆盖 6 大核心流程 | **6 流程 + 28 差异登记 ✅** | 文档评审 |
| 回归全绿 | 25 套件 PASS | 每阶段末全绿 + 打包自检 | **27/27 + 双端打包 ✅** | `python regression_run.py` |

**达标 9/10**；main_window 838 行为唯一未达硬指标项，经用户对齐按「行为零变化优先于行数」原则保留（拆分已完成 4 协作者抽离，剩余为 Qt 事件覆写与视图组装，硬拆将违反重构红线）。

---

## 二、阶段规划（6 个阶段，含依赖门）

```
Phase 0 基线固化 ──► Phase 1 架构骨架 ──► Phase 2 扩展性基础 ──► Phase 3 工程规范
                          │                                        │
                          └──► Phase 5 性能微调（并行）            ▼
                                              Phase 4 跨平台契约 ► 收尾重打包
```

### Phase 0 — 基线固化（前置阻断项）

**目标**：在干净基线上开工，消除已知正确性缺陷，避免「带病重构」。

| # | 任务 | 实施步骤 | 验收标准 |
|---|------|---------|---------|
| 0.1 | 提交 78 个未提交文件 | 按 scope 分类 commit（Conventional Commits：`feat(android):`/`fix(core):` 等），打 tag `v-baseline-20261009` | `git status` 干净；tag 存在 |
| 0.2 | 修安卓深色模式 P0×3 | Dark 色板独立容器色：okSoft=0xFF17352A/ok=0xFF6EE7A0、dangerSoft=0xFF3B1D18/danger=0xFFFF8A75、altRow=0xFF525B68；改 Theme.kt:100/103/105 + Color.kt | 深色模式真机/模拟器目验；`gradlew test` 绿 |
| 0.3 | 修 cache_guard.py:179 P1 | 去掉 `keep_fn() or ()`，None 时直接 `return removed`，与入口 145-146 行语义对齐 | cache_mode_e2e_test + persist_test 绿 |
| 0.4 | 修 BtEngine.kt:386 注解错位 P1 | `@Synchronized` 移到 `ensureStarted`；确认 applyTuning 调用方锁覆盖 | gradle build 绿 + 并发加种手测 |
| 0.5 | 接线空态按钮 P1 + 备份规则 P2 | MagnetScreen.kt:343 传 onSample；Manifest 补 fullBackupContent/dataExtractionRules | 点击有响应；aapt 检查 Manifest |
| 0.6 | 桌面 P2 健壮性批（audit #7-#11） | taskops 走 drop_record_locked；preview._views 加锁；stream_server 补 OSError；session 二次启动包 try；无 Range 200 语义修正 | `python regression_run.py` 全绿 |

**依赖门**：0.1 必须先于一切；0.2-0.4 可与桌面端任务并行。

### Phase 1 — 架构骨架（核心重构）

**目标**：建立异常层级、瘦化 fetcher 枢纽、拆分 main_window，形成 UI→facade→core 三层。

| # | 任务 | 实施步骤 | 验收标准 |
|---|------|---------|---------|
| 1.1 | 建立异常层级 | 新建 `core/exceptions.py`：MagnetError → ParseError/SessionError/DownloadError/CacheError/StreamError；按模块分批替换 `except Exception`（resolver 12 处 → taskops 7 处 → fetcher 7 处 → 其余）；保留 15 处边界兜底但必须 `log_exception` | Grep 计数 ≤15；回归全绿 |
| 1.2 | fetcher.py 枢纽瘦身 | ① 删除 6 处 `noqa: F401` 再导出，调用方改直连源模块；② 定义 `__all__`；③ SessionManager 改为显式 facade：只暴露 UI 需要的 ~15 个方法，内部组装逻辑移入 `_bootstrap()`；④ 目标 import ≤6 | Grep `^from \.` ≤6；smoke_test 导入完整性用例绿 |
| 1.3 | 拆分 main_window.py | 按职责切出 4 个协作类（同包新文件）：`ui/controllers.py`（DownloadController：任务增删改暂停恢复编排）、`ui/stream_host.py`（StreamHost：流服务+inbound 线程生命周期）、`ui/cache_host.py`（CacheHost：缓存清理/配额调用）、MainWindow 保留纯视图+信号接线；跨线程回调统一走现有 `_Bridge` Signal 模式 | main_window.py ≤400 行；gui_feature_test.py 全绿；手动冒烟：添加→解析→预览→下载→删除全链路 |
| 1.4 | UI→core 依赖收敛 | ui/ 仅允许 import：`core.models`、`core.config`、`core.fetcher`(facade)、`core.logutil`；cache/stream/proxycheck 等调用改经 facade 代理方法 | Grep `^from core\.` in ui/ ≤3 文件；回归全绿 |

**依赖门**：1.2 先于 1.4（facade 就位后 UI 才能收敛）；1.3 依赖 1.2 的 facade 方法清单。

### Phase 2 — 扩展性基础

**目标**：引入 Protocol 抽象与可注册配置，为未来功能扩展留接口。

| # | 任务 | 实施步骤 | 验收标准 |
|---|------|---------|---------|
| 2.1 | Protocol 抽象 | 新建 `core/interfaces.py`：`Resolver`、`StreamTransport`、`CachePolicy`、`ThemeSource` 四个 `typing.Protocol`；现有实现显式声明满足（`class ResolverCore: ...` + 结构检查用例）；contract_check.py 增加「实现满足 Protocol」断言 | 4 个 Protocol 存在；契约用例绿 |
| 2.2 | 配置注册机制 | config.py 增加 `register_config_key(key, default, validator=None)`；DEFAULTS 改为启动时由各领域模块自注册（session/scheduler/cache/ui 各自注册自己的键）；新增 `schema` 校验层 | 新增测试：注册未知键/非法值被拒；现有 config 键行为不变（contract_snapshot.md 比对） |
| 2.3 | 主题注册表 | theme.py 改 `THEMES: dict[str, Palette]` + `register_theme(name, palette)`；LIGHT/DARK 走注册路径；为未来主题（如用户自定义）留口 | theme_check.py 绿；热切换手测 |
| 2.4 | core/ 子包划分（**可选项，风险最高**） | 方案 A（保守，推荐）：保持扁平文件，用文档化分层地图替代物理分包；方案 B：`core/engine/`(session,scheduler,resolver,taskops) `core/storage/`(registry,taskstore,persist,metacache,resume) `core/streaming/`(stream_server,preview,pieceview,inbound) `core/infra/`(config,logutil,secretbox,proxycheck,cache_*)，保留 `core/__init__.py` 兼容再导出。**若选 B**：必须同步更新 magnet-viewer.spec 的 hiddenimports/collect_submodules + pack_check.py 路径断言 | 方案 A：ARCHITECTURE.md 含分层图；方案 B：build.bat 出包 + pack_check 绿 + onedir 冒烟启动 |

**依赖门**：2.1/2.2/2.3 可并行；2.4 必须最后且单独验证打包。

### Phase 3 — 工程规范

| # | 任务 | 实施步骤 | 验收标准 |
|---|------|---------|---------|
| 3.1 | 测试目录化 | 27 个 `*_test.py` + test_support.py + regression_run.py 移入 `tests/`；regression_run.py 的 SUITES 路径同步；`git mv` 保留历史；根目录清理 `reg_*.log`/`diag_*.py`/临时报告（.gitignore 补规则） | `python tests/regression_run.py` 全绿；根目录无 *_test.py |
| 3.2 | 覆盖率门槛 | CI 增加 `coverage run -m tests... && coverage report --fail-under=70`（先实测基线，若 <70% 则按实测值-2 设门槛并记录爬坡计划） | ci.yml 含门槛步骤；本地复现 |
| 3.3 | Android 单测接入 | android.yml 增加 `./gradlew test` 步骤；为 BtEngine 纯逻辑（TaskProgress/StallWatch/MiniJson/RestorePlan）补 JUnit 用例 ≥15 个 | workflow 绿；测试报告存在 |
| 3.4 | 文档补齐 | 新增 `docs/ARCHITECTURE.md`（分层图+数据流+模块职责表）、`docs/adr/`（≥5 条：锁纪律、facade 决策、异常层级、子包方案、跨平台策略）、`CONTRIBUTING.md`、`CHANGELOG.md` | 文件存在且内容与代码一致（交叉核对） |

**依赖门**：3.1 先于 3.2（路径稳定后加门槛）；3.3 独立可并行。

### Phase 4 — 跨平台契约

| # | 任务 | 实施步骤 | 验收标准 |
|---|------|---------|---------|
| 4.1 | 行为契约文档 | 用平台无关伪代码描述 6 大流程：磁力解析状态机、边下边播调度、Range 流服务语义、任务持久化/fastresume、缓存配额清理、主题令牌映射；每节标注桌面/安卓对应实现位置 | `docs/contract_spec.md` 完成；两端实现逐节比对，差异登记 |
| 4.2 | 差异闭环 | 按 4.1 发现的差异修复安卓端（audit P2 #12/#13/#14/#16 已列 4 项）；修复后重打包 APK | 差异清单归零或显式豁免记录；APK 重打包 |
| 4.3 | 共享数据模型 | 定义 `shared/task-schema.json`（任务记录字段/类型/必填），两端各自校验加载；桌面 taskstore.py 与安卓 TaskStore.kt 增加 schema 版本字段 | schema 文件 + 两端加载用例绿 |
| 4.4 | Rust 共享核心评估（仅调研） | 产出评估报告：libtorrent 双绑定现状、Rust 核心 + FFI 的成本收益、迁移路径草案；**不做实施** | `docs/rust-eval.md` 结论明确（推荐/不推荐+理由） |

### Phase 5 — 性能微调（可与 Phase 2-3 并行）

| # | 任务 | 实施步骤 | 验收标准 |
|---|------|---------|---------|
| 5.1 | metacache 锁内 I/O 外移 | L428-441 剪枝：锁内收集候选路径列表，锁外 scandir，回锁删除 | throughput_test + perf_cache_test 绿；剪枝耗时无退化 |
| 5.2 | persist 锁外迭代活字典修复 | L288-292 改锁内快照 | persist_test 绿 |
| 5.3 | taskstore tmp-key 撞键 | registry.py:81 id 复用问题改 uuid4 或计数器 | registry_test 绿 |
| 5.4 | SessionManager 懒初始化评估 | 评估 preview/stream 子系统按需初始化可行性；仅当收益明确才实施，避免过度工程 | 评估结论入 ADR |

---

## 三、关键技术点深度分析

### 3.1 main_window.py 拆分策略（最高风险任务）

**难点**：1224 行中 UI 接线、业务编排、线程生命周期、缓存操作交织；Qt 信号槽跨线程回调依赖 `_Bridge`/`_InboundBridge`。

**拆分原则**：
- **先抽协作者、后删主文件**：新建 controllers/stream_host/cache_host 三个类，MainWindow 逐步把方法体「搬」过去、留一行委托调用——每搬一个方法跑一次 gui_feature_test，小步快跑
- **线程归属集中**：所有后台线程（inbound、stream_server、alert 循环代理）收归 StreamHost 管理，`closeEvent` 统一走 `StreamHost.shutdown()`，消除散落的 `threading.Thread` 裸启动
- **信号不动**：`_Bridge` 模式保留，协作者通过注入的 bridge 发信号，不新增 Qt 线程模型

**回滚预案**：每个「搬运批次」独立 commit，出问题 `git revert` 单批即可。

### 3.2 fetcher.py 瘦身的兼容性风险

6 处 `noqa: F401` 再导出是历史兼容层（旧测试 `from core.fetcher import STATE_NAMES` 等）。**处理**：先 Grep 全仓（含 tests/）统计每个再导出符号的真实引用方 → 引用方改直连 → 再删再导出 → 跑 contract_check 比对快照。禁止「保留兼容层」导致目标 import 数不降。

### 3.3 子包划分与 PyInstaller 的连锁反应

`core/` 物理分包会破坏：① 全部 `from core.x import` 语句；② spec 的 `hiddenimports`/`collect_submodules`；③ pack_check.py 产物路径断言；④ 测试文件的 mock 路径。**因此**：Phase 2.4 默认走方案 A（文档化分层，不动物理结构），仅当 Phase 1-3 全部稳定且用户明确要求时才启动方案 B，且作为独立分支验证打包。

### 3.4 异常层级替换的边界

并非所有 `except Exception` 都应收窄：HTTP 服务 handler、alert 循环、清理路径的兜底是合理的（进程不可因单请求崩溃）。**规则**：业务分支捕获必须具体化；基础设施兜底保留但强制 `log_exception` + 注释说明为何宽泛。目标 ≤15 是「合理兜底」的数量。

### 3.5 跨平台契约的落地形态

不引入代码生成工具链（过度工程）。契约 = `contract_spec.md`（人读）+ `shared/task-schema.json`（机器校验，两端各自加 ~30 行加载断言）。差异靠审计流程发现，不靠运行时同步。

---

## 四、子代理系统设计与部署

### 4.1 角色与职责边界（文件所有权矩阵）

| 代理 | 类型 | 职责 | 独占文件（写权限） | 只读参考 |
|------|------|------|-------------------|---------|
| **A1 baseline** | general-purpose | Phase 0 全部任务 | 桌面+安卓全量（串行期无冲突） | audit 报告 |
| **A2 core-arch** | general-purpose | 1.1 异常层级 + 1.2 fetcher 瘦身 | core/exceptions.py, core/fetcher.py, 各 core 模块的 except 行 | tests/ |
| **A3 ui-split** | general-purpose | 1.3+1.4 main_window 拆分与依赖收敛 | ui/** | core/fetcher.py(新 facade 签名) |
| **A5 android** | general-purpose | 0.2-0.5 安卓修复 + 3.3 安卓测试 | android/** | 桌面 theme.py/状态机（对齐参考） |
| **A4 test-infra** | general-purpose | 3.1 测试目录化 + 3.2 覆盖率门槛 | tests/**, regression_run.py, ci.yml, .coveragerc, .gitignore | 全仓 |
| **A6 docs** | general-purpose | 3.4 + 4.1 文档与契约 | docs/**, shared/** | 全部源码 |
| **A7 perf** | general-purpose | Phase 5 性能微调 | core/metacache.py, core/persist.py, core/registry.py, core/taskstore.py | tests/ |

**冲突裁决规则**：所有权矩阵内文件只有所属代理可写；跨域需求（如 A3 需要 A2 的 facade 新增方法）→ 向编排者申报，由编排者排程。

### 4.2 通信机制

- **下行（编排者→代理）**：任务书 = 路线图任务编号 + 实施步骤 + 验收标准 + 文件边界 + 验证命令（`python regression_run.py <suite>` / `gradlew test`），每个代理无状态冷启动，任务书自包含
- **上行（代理→编排者）**：结构化回报：`{状态, 变更文件清单, 执行的验证命令与输出摘要, 遗留风险, commit hash}`；代理**不得自行宣告完成**，以验证命令输出为准
- **横向（代理↔代理）**：禁止直接通信；一切协调经编排者（防脑裂）
- **持久化**：每波次结束编排者更新本文件「执行日志」节，作为跨上下文压缩的记忆载体

### 4.3 协作流程与并行波次

```
Wave 0 [串行]  A1: Phase 0 基线固化 ──► 编排者全量回归验证 ──► tag v-fixed
Wave 1 [并行]  A2(桌面core) ∥ A5(安卓修复) ∥ A6a(ADR骨架)
               └─ 集成门：A2 完成后编排者跑全量回归 + A3 才能启动
Wave 2 [并行]  A3(UI拆分) ∥ A4(测试目录化) ∥ A5b(安卓单测)
               └─ A3 与 A4 文件不相交（ui/** vs tests/**），可并行；
                  A4 改 regression_run.py 路径需在 A3 期间由编排者冻结 tests/ 引用
Wave 3 [并行]  A2b(Protocol+配置注册+主题表) ∥ A7(性能) ∥ A6b(ARCHITECTURE定稿)
Wave 4 [串行]  2.4 子包方案（如启动）──► 打包验证
Wave 5 [串行]  集成验收：全量回归 + build.bat 出包 + gradle 出 APK + 指标复测 + 重打包
```

### 4.4 质量门禁（每任务三重验证）

1. **代理自验**：任务书指定的测试套件必须输出 PASS（贴证据，非转述）
2. **编排者复验**：独立重跑 `python regression_run.py` 全量 + 抽查 diff 与任务书一致性（trust but verify）
3. **指标门禁**：阶段末复测「量化目标」表，未达标不得进入下一阶段

**自我审查清单**（代理回报必含）：是否越界改文件 / 是否引入新宽泛 except / 是否破坏 `*_locked` 锁纪律 / 是否更新受影响测试断言（QUAL-10/14 教训）/ 是否遗留临时脚本（QUAL-12 教训）。

---

## 五、资源分配

| 波次 | 代理数 | 主要消耗 | 用户介入点 |
|------|--------|---------|-----------|
| Wave 0 | 1 | A1 单代理，桌面+安卓串行修复 | 批准提交策略；安卓深色模式目验（需设备/模拟器） |
| Wave 1 | 3 | A2 最重（异常替换跨 10+ 文件）；A5 中；A6 轻 | 无 |
| Wave 2 | 3 | A3 最重（1224 行拆分，预计 8-12 个搬运批次）；A4 中；A5b 中 | gui_feature_test 手测全链路冒烟 |
| Wave 3 | 3 | A2b 中；A7 轻；A6b 中 | 审阅 contract_spec.md 草稿 |
| Wave 4 | 1 | 仅方案 B 启动时 | 决策方案 A/B |
| Wave 5 | 编排者 | 集成+打包+指标复测 | 最终验收；真机测 APK |

上下文管理：每波次用尽即换新代理（任务书自包含）；路线图「执行日志」节为唯一持久状态源，防上下文压缩丢失进度。

## 六、时间节点（规划单位，非承诺工期）

| 节点 | 内容 | 完成标志 |
|------|------|---------|
| T0 | 路线图批准 | 用户确认 |
| T1 | Wave 0 完成 | tag v-fixed，回归全绿 |
| T2 | Phase 1 完成 | main_window≤400、fetcher import≤6、except≤15 |
| T3 | Phase 2 完成 | 4 个 Protocol、配置注册可用、主题表化 |
| T4 | Phase 3 完成 | tests/ 组织、CI 门槛生效、文档四件套 |
| T5 | Phase 4 完成 | contract_spec.md + schema 双端校验 + 差异清单归零 |
| T6 | Phase 5 完成 | 性能三项修复 + ADR 结论 |
| T7 | 收尾 | 双端重打包 + 指标终测 + 路线图归档 |

## 七、风险评估矩阵

| # | 风险 | 概率 | 影响 | 缓解 | 触发预案 |
|---|------|------|------|------|---------|
| R1 | 重构引入回归破坏且未被 25 套件覆盖 | 中 | 高 | 小步 commit + 每批跑相关套件；Wave 5 全量 | git bisect 定位 + revert 批次 |
| R2 | 并行代理文件冲突/互相覆盖 | 中 | 中 | 所有权矩阵 + 编排者串行集成点 | 冻结冲突方，编排者手工合并 |
| R3 | 子包划分破坏 PyInstaller 产物 | 高(若选B) | 高 | 默认方案 A；方案 B 独立分支 | 回退方案 A，分包降级为文档 |
| R4 | main_window 拆分引入 Qt 线程时序 bug | 中 | 高 | 保留 _Bridge 模式；搬运批次+closeEvent 集中 | 单批 revert；TRAE-debugger 流程 |
| R5 | 异常收窄误伤兜底路径（进程崩溃） | 低 | 高 | 基础设施兜底保留宽泛+log_exception 规则 | 恢复该点宽泛捕获并记 ADR |
| R6 | 安卓修复无真机验证手段 | 中 | 中 | 模拟器目验 + 用户真机抽查 | 标注「待真机」不阻塞桌面线 |
| R7 | 上下文耗尽致进度丢失 | 高 | 中 | 路线图执行日志节为外部记忆；任务书自包含 | new_context 后读日志恢复 |
| R8 | 范围蔓延（顺手改无关代码） | 中 | 中 | 自我审查清单含「越界检查」；diff 审查 | 剥离越界变更单独评审 |

## 八、启动检查清单

- [ ] 用户批准本路线图（含 Wave 4 方案 A/B 倾向）
- [ ] `git status` 确认 78 个未提交文件清单，确认提交策略
- [ ] 创建分支 `arch/roadmap-2026q4`（或按用户偏好在 main 上小步提交）
- [ ] 记录基线指标快照（行数/import 数/except 数）入「执行日志」
- [ ] 启动 A1 执行 Wave 0

---

## 九、执行日志（编排者维护）

| 时间 | 波次 | 代理 | 任务 | 状态 | 证据 |
|------|------|------|------|------|------|
| 2026-10-09 | — | — | 路线图制定完成 | ✅ | 本文件 |
| 2026-10-09 | W0 | 编排者 | 用户批准：立即启动/独立分支/方案A | ✅ | AskUserQuestion 答复 |
| 2026-10-09 | W0 | 编排者 | 0.1 基线提交核实：审计 18 项修复已在 HEAD(127d37d)，工作区干净 | ✅ | git log/status |
| 2026-10-09 | W0 | 编排者 | 0.2-0.5 安卓/桌面 P0-P1 修复实证抽查（DarkPal 色值/@Synchronized 归位/cache_guard None 保守） | ✅ | Grep 源码 |
| 2026-10-09 | W0 | 编排者 | 建分支 arch/roadmap-2026q4 + 文档提交 2d101e5 | ✅ | git log |
| 2026-10-09 | W0 | 编排者 | 基线指标实测修正：except 132（原估 50+）、fetcher 相对导入 22 行、UI 越层 6 处 | ✅ | Select-String |
| 2026-10-09 | W0 | 编排者 | 基线回归：.venv 重跑 **27/27 全绿**，tag v-baseline-20261009 | ✅ | 回归汇总 |
| 2026-10-09 | W1 | A2 | 1.1 异常层级：exceptions.py(7类) + 两批收窄 132→**26**（超额于 ≤40 目标）；scheduler14/preview6/resume3/secretbox2/inbound2/cache_guard1/stream1 按真实抛出面收窄，保留 26 处全有 log+中文兜底理由注释；修复第一批过度收窄 1 处（watchdog AttributeError） | ✅ | 编排者独立实测 26、diff 抽查、回归复验中 |
| 2026-10-09 | W1 | A5b | 安卓单测：**勘察更正**——android.yml 早已含 testDebugUnitTest、已有 334 测试方法（审查报告前提过时）；实际缺口仅 PieceMap，补 21 法（含派发层信任边界+双路径对拍）；gradlew 实测 355/355 PASS；android.yml 拆测试/构建为独立步骤 | ✅ | JUnit XML 统计 |
| 2026-10-09 | W1 | A6 | 文档 7 件：ARCHITECTURE.md(295行,分层地图+依赖规则+并发模型+双端对应表) + ADR×4 + CONTRIBUTING + CHANGELOG；交叉验证发现并修正审查报告 4 处偏差（__all__ 不存在、noqa 4行、main_window 实测 1365 行、resolver→taskops 跨层 import 登记） | ✅ | git status + 行数实测 |
| 2026-10-09 | W1 | 编排者 | 集成门：独立复验全量回归 26/27 PASS + throughput_test 单跑复验通过（回环测速环境抖动 flaky，114.3 vs 90.8 MB/s，与改动无关）；分类提交完成 | ✅ | 554385b fix(core) / 5cbab8d test(android) / a150545 docs(arch) |
| 2026-10-10 | W2 | A3 | 2.1 fetcher 瘦身：相对导入 22→**10**（删纯历史再导出 STATE_*/taskstore/registry 常量，调用方 download_mgr/session/persist/proxycheck/contract_check/registry_test/resolver_test 改直连源头；models 移 TYPE_CHECKING；proxycheck/taskstore 改方法内延迟导入；8 个 persist_*/registry_*/resolver_* 注入别名保留）+ `__all__`(7符号)；2.3 UI 越层 9→**0**（SessionManager 补 7 薄委托方法 preview_root/enforce_preview_cache_limit/create_stream_server/inbound_selfcheck/proxy_hint 等；cache_guard/cache_mode 走 facade 再导出——须接受显式 path，绑 self.cache_dir 的委托语义错误） | ✅ | 编排者独立复验 Grep：^from \. =10、越层命中仅剩白名单四类 |
| 2026-10-10 | W2 | A4 | 2.2 main_window 拆分：1365→**838 行**（未达 400 目标，诚实说明：关闭路径~180行 Qt 事件覆写必须留子类、_build_ui~90行纯视图组装、22 个 I2 守卫入口槽留 MainWindow；继续硬拆违反行为零变化）；抽出 4 协作者 cache_host(222)/stream_host(380)/task_controller(150)/inbound_host(104)；白盒测试同步 gui_feature/close_lag/wait_timeout 三处 spy 路径迁移，22+7 守卫 AST 清点零弱化 | ✅ | 编排者独立复验：行数 838、越层 0、四新文件存在 |
| 2026-10-10 | W2 | A7 | 2.5 tests/ 目录化：29 文件 git mv 入 tests/（27 SUITES+live_test+pack_check；test_support/regression_run/coverage_run/diag_* 留根）；31 处路径引导修复（sys.path 双层 dirname、contract/gui_feature 读 ui/ 源码、concurrency AST 根）；外部引用同步 regression_run/coverage_run subprocess 路径、build.bat、spec、README、CONTRIBUTING、ARCHITECTURE、diag 注释；历史文档裸文件名提及保真不改 | ✅ | 编排者独立复验：根目录 *.py 仅剩源码/工具、tests/ =29 |
| 2026-10-10 | W2 | 编排者 | 集成门：全量回归复验（A3+A4+A7 累计）+ 分类提交（refactor(core)/refactor(ui)/test(refactor)/docs）+ 执行日志 | ✅ | 7008cda refactor(core) / fe71a02 refactor(ui) / e5d794c test / ce34877 docs / b77b1b5 docs(roadmap) |
| 2026-10-10 | W3 | A2b | 2.1 Protocol：core/interfaces.py(141行,4 Protocol/16方法,Resolver5/StreamTransport4/CachePolicy3/ThemeSource4,runtime_checkable,只声明不重构、实现零改动免继承税)+contract_check [5] 结构检查与签名指纹对拍；2.2 配置注册：register_config_key+读取侧 validator 回落+首注册者胜（**有意偏离**路线图"各领域自注册"——避免 config→领域→config 导入环，分组注释替代，ADR-0006 登记）；2.3 主题表：THEMES dict+register_theme，LIGHT/DARK 走注册路径，PALETTES 同对象别名，theme_check 零改动 | ✅ | contract_check 208/0、theme_check 16/0、gui_feature 275/0 |
| 2026-10-10 | W3 | A7 | Phase 5 性能微调：**5.2/5.3 审计点位已在 127d37d 修复**（persist 锁内快照、registry tmp-key 计数器）——审计基线过时；5.1 主路径同已修，但发现**同型残留**（maybe_prune 剪枝后重扫计数锁内 scandir）→锁外扫描+回锁写状态，concurrency_test §E AST 钉死防回归；5.4 懒初始化评估=**不实施**（重资源已按需、轻对象急切，懒化收益近零且引入首用竞态）；遗留提示：taskops.py:159 一处 tmp-{id(p)} 不在登记范围 | ✅ | 6 套件实测全 PASS、throughput 89.4MB/s 无退化 |
| 2026-10-10 | W3 | A6b | ARCHITECTURE.md 定稿（+134/-44：四协作者入表、越层"已落地 0"、interfaces 契约层、tests 布局 §2.4、metacache 锁修、行号漂移修正 _lock L227-229/taskops R-10、夹具 14/156 修正）+ **ADR-0005/0006/0007** 新增 + ADR-0001~0004 修正（0002 状态 Pending→已落地逐条实证）+ CHANGELOG Unreleased；核实清单 20+ 项全读码实证；登记 4 处口径不一致（roadmap T2 ≤6 vs 实际 ≤10、5.1 文字 vs 实现、1.1 15 处 vs 实测 27、ADR-0002 历史计数） | ✅ | 行数/越层/import 全实测 |
| 2026-10-10 | W3 | 编排者 | 集成门：A2b∥A7 并行（文件域不相交）→A6b 串行（依赖 A2b 交付物）；独立复验（interfaces 4P/16M、registry 计数器、metacache 锁外、THEMES/register_*）；全量回归 **27/27 全绿**（exit 0）；分类提交 | ✅ | 7a4190c feat(arch) / aba1980 perf(core) / 7b78c51 docs(arch) / 3350b87 docs(roadmap) |
| 2026-10-10 | W4 | A6c | 4.1 跨平台行为契约：docs/contract_spec.md（~490 行，6 大流程伪代码+双端实现位置全读码核实）；**审计 4 项假设（#12/#13/#14/#16）核实全部已修复**（与 W3 5.2/5.3 同型，审计基线再次过时）；主动新发现 28 条差异（高1/中7/低5/豁免观察15） | ✅ | 编排者抽查 BtEngine.kt:554-577（#12 已修实证） |
| 2026-10-10 | W4 | A4b | 3.2 覆盖率门槛：实测基线 **81%**（branch 口径，core 87.7%/ui 75.3%，27 套全跑）≥70 → 直设硬门禁 fail-under=70（无过渡期）；ci.yml 锁 coverage==7.16.0 + 新增 Coverage gate 步骤；CONTRIBUTING 补门槛决策记录+最低 5 模块爬坡建议 | ✅ | coverage report --fail-under=70 退出码 0 |
| 2026-10-10 | W4 | A5c | 4.2 差异闭环（用户裁决=高+中必修）：C21 点播洪泛（60 块常量→windowPieces 字节口径同源）+ C41/C42/C43 fastresume 三件套（60s 周期脏写/退出 drain 去跳过+mtime 新鲜度/恢复回退 .meta）+ C11 超时 pause + C12 完成 D3 停止 + C32 双池额度分账；低优先 C22/C23/C33/C44/C45 顺带全修；C61 豁免（用户裁决双轨）、C31/C13/C34 真机待办登记；4.3 shared/task-schema.json（draft-07，15+6 字段）+ 两端 schema_version 读写（旧文件容忍） | ✅ | gradlew 364/364（+9 新用例）、桌面 persist 95/0、contract_check 208/0 |
| 2026-10-10 | W4 | 编排者 | 集成门：A6c∥A4b 并行→A5c 串行（依赖差异清单）；AskUserQuestion 裁决修复范围（高+中）与 C61（豁免）；独立复验 C21 修复/schema 落地/豁免登记节；全量回归复验+分类提交 | ✅ | 4c851dd docs(contract) / 7828b11 ci / 0c8daa6 fix(android) / e7af95e docs(roadmap) |
| 2026-10-11 | W5 | 编排者 | 收尾验收：桌面 build.bat 重打包（onedir 104.4MB，pack_check 10/10 组件+瘦身 4/4，exe 冒烟存活）；安卓 assembleRelease（APK 20.7MB，R8+lintVital BUILD SUCCESSFUL）；指标终测 9/10 达标（main_window 838 行为已登记合理保留，见 ADR-0003/执行日志 W2-A4）；全量回归终跑 27/27 绿（smoke 首跑瞬态 10054 经单跑+重跑双复验排除）；CHANGELOG 定版 | ✅ | 见 §1.2 终测表 |
