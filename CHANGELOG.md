# 更新日志（Changelog）

本项目的所有重要变更都记录在此文件中。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
按日期分组（尚未启用语义化版本 tag；`v-baseline-20261009` 基线 tag 计划见
technical_roadmap Phase 0.1）。变更记录自 `git log` 提炼。

---

## [2026-10-09] — 架构路线图启动（Wave 0）

### 新增
- 架构：综合架构评估报告（12 维度评分 2.8/5、依赖拓扑、fetcher 枢纽反模式
  实证）与架构演进技术路线图（6 阶段 + 子代理波次计划）入库
  （`architecture_review_report.md` / `technical_roadmap.md`，2d101e5）。
- 分支 `arch/roadmap-2026q4` 建立；用户批准「立即启动 / 独立分支 / 方案 A
  （文档化分层，不做物理分包）」。
- 基线指标实测修正：core/ 宽泛 except 132 处、fetcher 相对导入 22 行、
  UI 越层 import 6 处（原估算偏乐观，以 Select-String 实测为准）。

## [2026-09-29] — S6/S8 两端可用性 + 全项目审计修复 18 项

### 修复
- 全项目审计（4 路并行子代理 + 主代理逐条实证）登记的 18 项问题批量修复
  （127d37d），覆盖：
  - 安卓深色模式 P0×3：Dark 色板独立容器色（okSoft/ok、dangerSoft/danger、
    altRow），修复「已完成」徽章绿字绿底、失败徽章红字红底、斑马纹失去区分；
  - 桌面 cache_guard `keep_fn() or ()` 吞 None 导致活任务目录被误删（P1）；
  - 安卓 BtEngine `@Synchronized` 注解错位、ensureStarted 无同步（P1）；
  - 安卓空态「示例磁力链」按钮未接线（P1）；
  - 桌面 taskops remove_task 裸 del 绕过 `drop_record_locked`、
    preview `_views` 无锁并发迭代、stream_server 缺 OSError 捕获、
    session 端口回退二次启动无兜底、无 Range 200 的 Content-Length 语义、
    安卓 fastresume 焦点任务回退污染 / drainResume 漏非焦点任务 /
    复用分支误改下载型 / EngineTask 字段可见性 / StreamServer 写阶段无超时 /
    硬编码色对比度 / Manifest 备份规则孤儿文件等 P2×11。
- S6（PieceView 增量可用性视图双端移植）与 S8（503 诊断头 X-Why /
  X-Ready-Bytes 双端对齐）两端可用性与诊断收口。

## [2026-09-15] — 安卓端实测缺陷集中修复 + 发布门禁

### 新增
- 安卓发布门禁 `release_check.py`（签名 / 16KB页对齐 / ABI / 产物对账，eb8341c）。

### 修复
- 设备实测问题：播放控件被挤出首屏（D-02）、停滞计时跨任务不复位、
  预览失败面板残留空态、开播门控超时后状态复位与重试（D-01）；
- 「假完成」：完成判定补「选中集合已落字节」分子（RB-1）；
- 仅解析任务点「继续」被误判 COMPLETED、百分比与字节数不同口径（J-01/C-02）
  及其补漏（J-01 状态判据取引擎真值）；
- 下载停滞零反馈：新增 StallWatch 看门狗 + UI stallNote 字段（C-01）；
- 下载限速补逐任务 setDownloadLimit（会话级对 local peer 不生效，I-01）；
  坏代理探活 + 回退直连（I-03）；「运行日志」开关接到全部日志出口（I-02）；
- 启动期配额误删活跃任务数据：顺序守卫/配额兜底/摘要口径三层修复（B-01）；
- 转正后清理空 `.preview/<ih>` 残留目录（H-02 及补漏）；
- 播放中按返回先停预览（A-01）、启动类信息提示只在空态展示（A-02）、
  画廊说明重复（E-01）；
- gradlew 补可执行位（CI Permission denied）。

### 变更
- 方案 B 卡片重构落地（e1ae2bc）。

## [2026-09-14] — 安卓端能力主线成型

### 新增
- 安卓工程骨架（Gradle 9.1 / AGP 9 / Compose + libtorrent4j / Media3，0b14991）；
- demo 可用切片：磁力链解析 / 文件清单 / 点视频边下边播（d044b62，
  修告警句柄悬垂 SIGSEGV；落盘改应用内部存储绕开模拟器 FUSE 崩溃）；
- 引擎侧一圈能力：dataSync 前台服务（灭屏继续）、暂停/继续/停止预览
  （convert/hold 两档）、seek 接入、代理限速超时热更新 + 26 单测（d0a914a）；
- UI 接线：设置页（代理/限速/超时/缓存/主题，DataStore 持久化）+ 15 单测（a20525e）；
- 任务持久化与启动恢复：`.tasks.json` 原子写 + `.resume/<ih>.fastresume` +
  启动重建清单 + 46 单测（3fed305，修 v2/v1 哈希键、resume 缺 info-dict、
  并发落盘三处缺陷）；
- 多任务 + 任务操作 + 缓存配额：任务列表/转正/移除双守卫/优先级/LRU 配额
  + 25 单测（45ea60e）；
- 图片画廊：按需下载/缩略图网格/大图缩放翻页 + 20 单测（fea647c）；
- 发布链路：签名 release（20.47MB/仅 arm64）、R8 保 JNI、NDK 剥符号、
  ABI 收窄 + CI workflow（fa02acc）；
- 端到端测试靶场入库（本机做种 + x.pe peer 提示 + 模拟器 adb 驱动，4de1e65）；
- 跨语言对拍夹具 13 组/155 条（523fd1c）；安卓版 README（d1c942d）。

### 修复
- 收口 rmtree 偶发（applyDeletion 透传接缝与失败位，ab198da）；
- CacheQuotaTest flaky：扫描/删除/存在性做成可注入接缝（fb0d847）。

## [2026-09-10] — 桌面 UI 打磨与发布文件

### 新增
- 发布流水线文件入库 + onefile spec 补 ui/assets（d0a7daf）。

### 修复
- 下拉/微调右侧按钮区改 QSS 子控件 + SVG 箭头，废自绘几何（4feee76）；
- 设置面板 v3：复选框行灰带、右侧分区/指示器、标签列对齐、代理联动与
  掩码（c5308f1）。

### 测试
- 下拉/微调按钮区像素断言（满高/居中/等大/无游离线/不越界，f0a36ac）；
  设置面板 v3 像素断言 + 四组标签列宽实测（1a25c27）。

### 合并
- Merge upstream/main 入 copilot-fixes（b096ec5）。
