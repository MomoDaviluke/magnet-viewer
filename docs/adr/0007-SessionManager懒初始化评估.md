# ADR-0007：SessionManager 懒初始化评估——结论：不实施

- **状态**：Accepted（评估结论，2026-10-10，Wave 3 A7 / 路线图 Phase 5.4 交付物）
- **日期**：2026-10-10
- **来源**：technical_roadmap Phase 5.4（「评估 preview/stream 子系统按需初始化
  可行性；仅当收益明确才实施，避免过度工程；评估结论入 ADR」）、
  `core/fetcher.py` `SessionManager.__init__`（L80-221）、`core/session.py`
  `SessionCore.start`、`tests/contract_check.py` [3] 节

## 背景

路线图 5.4 提出：SessionManager 是 UI 启动即构造的 facade，其 `__init__`
组装全部子系统（registry / scheduler / persist / session core / taskops /
resolver / preview）。若某些子系统实际很少用到，按需（懒）初始化可以缩短
启动路径、降低空转成本。本 ADR 登记评估过程与结论。

## 评估

**重资源已经天然按需，无需再懒**：

1. libtorrent 会话（真正的重资源：端口绑定、DHT、alert 线程）在
   `SessionCore.start()` 里才创建（session.py:345 `lt.session(settings)`，
   含端口回退重试）——构造 SessionManager **不碰** libtorrent；
2. 流服务实例（HTTP 服务器 + 线程）经 facade `create_stream_server` 工厂
   在开播时才构造（fetcher.py:474-484，延迟 import stream_server），
   生命周期由 UI 编排；
3. fastresume 落盘、任务恢复扫描等在 start 路径按需执行。

**`__init__` 急切构造的剩余成员全是轻对象**（fetcher.py:130-221 实证）：

- `TaskRegistry`：一把 `threading.Lock` + 若干空 dict/None 字段
  （registry.py:118-125）；
- `PreviewScheduler`：一把私有短锁 + None 字段（scheduler.py `__init__`）；
- `TaskPersistence` / `SessionCore` / `TaskOps` / `ResolverCore` /
  `PreviewCore`：依赖 dataclass/回调闭包装配，无 I/O、无网络、无线程。

即急切构造的成本 = 建几把锁 + 空容器 + 闭包，**微秒级**；懒化可省的是
微秒，收益近零。

**而懒化的代价是实打实的**：

1. **多线程首用竞态**：子系统被 GUI 线程、alert 后台线程、流服务请求线程
   并发触达（ARCHITECTURE.md §三）。懒初始化要么引入新的初始化锁（违背
   ADR-0001「不再各自新增任务级锁」的收敛方向），要么写双检锁样板
   （易错、且「首用恰好并发」路径测试覆盖困难）；
2. **破坏 contract_check [3] 成员恒存在断言**：UI 与白盒测试直访
   `mgr.scheduler`、`mgr._ses` 等实例成员（contract_check.py:131-141
   冻结「构造后成员恒存在」，构造**不**启会话）；懒化后这些成员在首用前
   为 None/缺失，直访方（含 close_lag / gui_feature 白盒断言）全部要加
   判空——行为变化面扩大，与重构「行为零变化」红线冲突；
3. **状态机复杂化**：每个懒成员引入「未初始化/初始化中/已初始化」三态，
   shutdown 路径（close_lag 守卫的 22 入口）要逐成员处理「未初始化即
   跳过」，回归成本远超收益。

## 决策

**不实施懒初始化**。SessionManager 维持「轻对象急切构造、重资源天然按需」
的现状——按需化的两个真实目标（会话、流服务）已经分别落在
`SessionCore.start` 与 `create_stream_server` 工厂上，无需重复建设。

## 后果

- 路线图 5.4 验收口径 = 本 ADR（「评估结论入 ADR」），不产生代码改动；
- 若未来出现真正昂贵的急切构造成员（如启动即建线程/读大文件的子系统），
  本 ADR 的评估框架可复用：先量化构造成本，再对照「重资源按需」既有
  模式（start/工厂），最后权衡竞态与契约破坏成本；
- 登记一个反向约束：contract_check [3] 的「成员恒存在」断言在，任何
  后续懒化尝试都会先被该门禁拦住——这是有意保留的护栏。