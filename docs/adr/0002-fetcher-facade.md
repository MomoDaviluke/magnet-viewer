# ADR-0002：fetcher.py 作为 SessionManager facade 的枢纽决策与瘦身方向

- **状态**：Accepted（现状确认）+ 瘦身计划 Pending（路线图 Phase 1.2，Wave 1 A2 实施中）
- **日期**：2026-10-09
- **来源**：architecture_review_report §2.2「fetcher.py 枢纽反模式」、
  technical_roadmap Phase 1.2 / 3.2、`core/fetcher.py` 文件头注释

## 背景

`core/fetcher.py`（实测 766 行；审查报告登记的 635 行为审计修复前快照）是 UI 唯一直接触达的 core 入口：`SessionManager`
组装 libtorrent 会话、注册表、调度器、解析器、任务 CRUD、持久化、预览桥
等全部子系统，并以薄委托 + property 代理（`_lock`/`_torrents`/`_tasks` 等
转发到 registry）对外暴露能力。

实测（Grep `^from \.`，2026-10-09 基线）：fetcher.py 有 **22 行相对 import、
覆盖 14 个 core 子模块**。其中 4 行带 `# noqa: F401` 的历史兼容再导出
（STATE_NAMES / DOWNLOADS_SUBDIR / METADATA_TIMEOUT / TaskRecord / STATE_* /
load_tasks 等符号组；审查报告按符号组计为「6 处」），供旧测试
`from core.fetcher import ...` 的写法使用——fetcher 事实上兼任
barrel export，但无 `__all__` 声明，公共 API 边界不明确。

审查报告将其定性为「枢纽反模式」：SessionManager 充当事实上的上帝对象，
任何新模块加入都要改 fetcher 的 import 与初始化逻辑，违反开闭原则；
它是项目两大高风险项之一。

## 决策

1. **保留 facade 模式**：UI 继续只认 `SessionManager` 一个入口（这是
   「UI→facade→core」三层收敛的前提，路线图 1.4 的 UI import 白名单
   `core.fetcher` 即指它）。不把 24 个 core 模块直接摊给 UI——那会让
   main_window 的依赖面更糟。
2. **但 facade 必须瘦身**（Phase 1.2，按序）：
   - ① 删除 6 处 `noqa: F401` 再导出：先 Grep 全仓统计每个再导出符号的
     真实引用方 → 引用方改直连源模块 → 再删再导出 → contract_check 比对
     快照（路线图 3.2 明令禁止「保留兼容层」导致 import 数不降）；
   - ② 定义 `__all__`，把「facade 暴露什么」从隐式变显式；
   - ③ SessionManager 改为显式 facade：只暴露 UI 需要的 ~15 个方法，
     内部组装逻辑移入 `_bootstrap()`；
   - ④ 量化目标：相对 import 22 行 → **≤10**（路线图验收基线）。
3. **组装期依赖注入不变**：子系统间仍用 getter/回调注入会被重绑的宿主状态
   （ses_get / scheduler_get / emit），引用稳定的一次性注入直接传服务对象
   （taskops 头注释定死的规则）——瘦的是 fetcher 的 import 面，不是 DI 结构。

## 后果

**正面**：
- fetcher 回归「组装 + 委托」单一职责，新模块接入不再必然修改 hub；
- `__all__` + 再导出清除让公共 API 边界可检查（contract_check 守 23 个
  公开接口签名）；
- UI 收敛到 facade 后，Phase 1.3 拆分 main_window 才有稳定的依赖面。

**负面 / 风险**：
- 再导出删除是破坏性变更：旧测试/旧脚本 `from core.fetcher import STATE_*`
  写法会断，必须「先改引用方、再删导出」严格排序，并以 smoke_test
  导入完整性用例 + contract_check 快照比对兜底；
- 「~15 个方法」的 facade 清单需要 UI 拆分（A3）反向确认，两任务存在
  排期耦合（路线图依赖门：1.2 先于 1.4）。
