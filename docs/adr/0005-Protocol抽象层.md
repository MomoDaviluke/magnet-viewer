# ADR-0005：core/interfaces.py Protocol 抽象层——只声明、不重构

- **状态**：Accepted（2026-10-10，Wave 3 任务 2.1 落地）
- **日期**：2026-10-10
- **来源**：technical_roadmap Phase 2.1、`core/interfaces.py` 文件头注释、
  `tests/contract_check.py` [5] 节

## 背景

架构审查（architecture_review_report）把「扩展性」列为短板之一：核心子系统
（解析、流服务、缓存策略、主题）没有显式接口面，替换实现（如换一种缓存
淘汰策略、接一种新的流传输）只能改实现本体。方案 A（ADR-0003）决定
core/ 不做物理分包后，「契约」需要一个文档之外的机器可检查载体。

同时项目处于重构窗口期，红线是**行为零变化**：任何「为抽象而抽象」的
重构（改类结构、加继承、动调用链）都违背这条红线。

## 决策

1. **新建 `core/interfaces.py`，声明四个 `@runtime_checkable` Protocol**
   （共 16 个方法签名）：
   - `Resolver`（5 方法）——磁力链/.torrent 解析与元数据编排，当前唯一实现
     `core.resolver.ResolverCore`；
   - `StreamTransport`（4 方法）——本地流媒体文件服务生命周期 + 取 URL +
     应答诊断，当前唯一实现 `core.stream_server.StreamServer`；
   - `CachePolicy`（3 方法）——预览缓存配额/清理策略供给面，当前唯一实现
     `core.fetcher.SessionManager` 的 facade 委托段（preview_root /
     enforce_preview_cache_limit / protected_dirs）；
   - `ThemeSource`（4 方法）——主题供给，当前唯一实现 `ui.theme` 模块本体。
2. **只声明、不重构**：现有实现零改动；**不写** `class ResolverCore(Resolver)`
   这类显式继承——Protocol 是结构化类型（鸭子类型），实现满足方法面即天然
   是「实现」；显式继承反而把接口变成基类依赖，违反行为零变化红线。
3. **方法签名与现有实现逐字一致**（参数名/默认值/返回类型），提取自
   contract_check 已冻结的签名表，避免契约与实现「两张皮」。
4. **纯声明层、运行时零依赖**：模块不 import libtorrent/Qt；注解涉及的其它
   模块类型（如 `TaskRecord`）放 `TYPE_CHECKING` 块，配合
   `from __future__ import annotations` 使注解全部惰性化——interfaces.py
   可被任何层安全 import 而不构成层间依赖边（ARCHITECTURE.md §2.2 第 4 条）。

## 理由

- 为「替换实现 / 插件化」留口（roadmap Phase 2 扩展性基础），同时**零行为
  变化**：不动任何调用链，审查成本 = 读一个 141 行纯声明文件；
- 结构化类型免继承税：实现改名/移动时协议不受牵连，反之由门禁报警；
- 相比 ABC：不强迫继承、不引入 MRO 复杂度，与本项目「文档化分层 +
  机器门禁」的方案 A 风格一致。

## 后果

- **正面**：能力面第一次成为可执行契约；新实现（如自定义 CachePolicy）
  只需满足方法面即可接入，无需触碰现有代码。
- **代价 / 风险**：Protocol 不强制任何东西——漂移（实现改名/丢方法/改签名）
  不会让生产码报错，只会让契约失真。**缓解**：contract_check [5] 把漂移
  变成红灯——`issubclass(实现, Protocol)` 结构检查 + 逐方法**签名指纹对拍**
  （`fp(proto.m) == fp(impl.m)`，四组协议×实现全表比对），实现侧任何
  方法面变化必须同步协议声明，反之亦然。
- **已知边界**：`fp` 指纹不含类型注解（与 contract_check 全文件口径统一），
  注解漂移不在对拍覆盖面内；`ThemeSource.apply_theme` 默认值写字面
  `"light"`（实现侧是 `DEFAULT_MODE` 常量，theme_check R4 已冻结其取值
  == "light"，二者等价）。
- 当前生产码无 interfaces.py 的 import 点（消费方是测试门禁与未来实现），
  属「先立契约、后接消费」的预留——这是有意为之，不是遗漏。