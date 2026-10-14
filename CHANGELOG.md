# 更新日志（Changelog）

本项目的所有重要变更都记录在此文件中。

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
按日期分组（尚未启用语义化版本 tag；`v-baseline-20261009` 基线 tag 计划见
technical_roadmap Phase 0.1）。变更记录自 `git log` 提炼。

---

## [2026-10-12] — 主干统一与工程收敛（W1~W3）

### 变更
- **主干统一**：`main` 以 `--ff-only` 快进至 `arch/roadmap-2026q4` 尖端（2b9021c，
  +120 提交 / 47→272 受控文件），安卓端 141 文件（95 `.kt`）随树入库——`*.jks` /
  `keystore.properties` / `local.properties` 经 `android/.gitignore` 排除，0 真实私钥入库
  （唯一 keystore 匹配为有意入库的占位符模板 `keystore.properties.example`）。
- **缺口收割**：G2 六项缺口复核——S-1~S-6 随合并自动闭合（fetcher 891 行、限速/缓存/日志
  三接线、流服务限流、UI 封装违规 0 命中）；S-10 静默吞异常清零（见下）。
- **S-10**：`ui/main_window.py` 两处 `except Exception: pass` 修复（画廊预览失败改
  `QMessageBox` 反馈对齐视频路径模式；关窗计时器收窄 `RuntimeError`）；
  `core/logutil.py` 日志器自守卫注释登记为唯一豁免。
- **安卓验证**：现有 release APK 免重编判定（时序实证：最后 android 提交早于构建时刻）+
  R8 mapping/dex 混淆名回查等价证据链 + `zipalign -P 16` 通过；dist 安卓交付副本刷新
  （09-29 版字节数与 10-11 版相同但内容差 12 天提交，旧版存档 `*-20260929.bak.apk`）。
- **文档同步**：README 结构树补齐 core/ui/根目录缺失条目、契约项数 169→208、
  gui_feature 计数 126→275（实测）；`regression_run.py`/`coverage_run.py`/`ci.yml`
  套件计数口径统一为 27；`technical_roadmap` 状态收口；`ARCHITECTURE` 待产出注删除；
  `contract_snapshot` 补最新核对日期。
- **D-3 正式签名落地**：生成正式 keystore（CN=MagnetViewer，RSA-2048/PKCS12，有效期
  10000 天）并切换 `android/app/keystore.properties`（gitignored，不入库）；重签 release
  APK 后 `release_check.py` **无放行全绿**（R1 正式凭据 / R2 16KB 对齐 / R3 ABI 收窄 /
  R4 产物对账）。交付副本 `dist/MagnetViewer-1.0-android-arm64.apk` 更新为正式签名版
  （sha256 `c32dee75…`），开发签名版存档；凭据资产备份于仓库外目录。
- **CI 修复**：`android.yml` 的 `android-actions/setup-android@v3` 在 2026-10 runner
  镜像上因新增 SDK 许可证交互式复核必红（两次实跑实证），替换为 `yes | sdkmanager
  --licenses` 预接受步。首推 run 的 `local_torrent_test` 磁盘一致性断言单点抖动
  （同树本机两轮全绿 + 后续 run 复绿），已重跑核验。

### 验证
- 回归 27/27 全绿 ×3（ff 后、S-10 后本机、CI runner @ f31a454）；coverage 81% ≥70；
  contract_check 208/208；推送均经 ls-remote 验证。

### 发布后补强（同日 2026-10-12 晚）
- **安卓升级链闭环**：`versionCode`/`versionName` 参数化（`-PappVersionName`/`-PappVersionCode`
  覆盖，缺省=历史值/commit 计数）+ `android/tools/publish_apk.py` 一键发版链
  （gradle 正式签名构建 → release_check 硬门禁 → 刷 dist → curl 上传 GitHub Release，
  同名资产先删后传可重跑）+ `android/build_release.bat` 壳。三缺口清零：Release 页此前
  不挂 APK、versionCode 硬编码 1 永拒覆盖安装、发包全手工。
- Release v1.1.0 已补挂正式签名 `MagnetViewer-v1.1.0-android-arm64.apk`
  （versionName 1.1.0，sha256 `5177ed0f…`），与桌面件同页分发。

---

## [2026-10-14] — v1.1.1：性能实测优化 + 安卓播放器横竖屏/全屏 + UI 精修

### 变更（v1.1.1，提交 `543a1a2`/`dbf6f20`/`533e847`/`a53174c`）
- **性能（实测驱动，指定种子 `ae0157c0…` 定位）**：
  - `port_mapping` 双端默认**开启**（桌面 `core/config.py` + 安卓 `SettingsStore`/
    `BtEngine`）：实测关闭时前 66s 仅 1–10KB/s（连不上 NAT 后做种端），开启后产品栈
    预览峰值 3MB/s、peers 2→28——入站可达是下载速率第一瓶颈；对齐主流 BT 客户端默认。
  - 安卓 `EngineTuning` 补齐桌面第二/三批吞吐参数（recv/send socket 缓冲、
    aio/hashing 线程、max_out_request_queue、request_queue_time、重连节奏、
    piece_extent_affinity、announce_to_all_*；全部键经 javap 核对 libtorrent4j
    2.1.0-39 存在）；`BtEngine` 补 `setDhtBootstrapNodes` 6 节点冗余（对齐桌面）。
- **新增（安卓播放器横竖屏/全屏）**：Manifest `configChanges` 防旋转重建；ExoPlayer
  实例提升到内容作用域共享（横屏覆盖层与竖屏内嵌卡同一实例，切换播放不中断）；
  FULL_SENSOR 重力感应自动横竖屏 + 竖屏「全屏」按钮 + 沉浸系统栏 + 顶部控制条
  （暂停/退出播放/退出全屏）+ 返回键分级退出。模拟器（mv-demo）实测：旋转 pid 恒定、
  requestedOrientation 链路正确、覆盖层渲染、沉浸生效。
- **UI 精修（桌面下载页）**：进度条自绘重做——旧系统样式百分比压条、窄列裁切，
  改「圆角轨道 + 右侧独立百分比」（对齐 qBittorrent/IDM 惯例），paint 现读
  `ui.theme` 跟随深浅主题热切换。

### 验证
- 桌面回归 27/27 全绿；安卓单测 364/364 全绿；release APK 构建 + lintVital 通过。
- 产品级真实磁力端到端（SessionManager 栈）：元数据 1.2–12.6s、预览峰值 3MB/s。
- 回环吞吐 T4（千兆口径）：本程序栈峰值 78.3MB/s（≥60MB/s 判据）、首字节 0.10s、
  UI 轮询中位 0.49ms 零拖累——软件栈无瓶颈。

---

## [Unreleased] — 架构重构 Wave 2 / Wave 3 / Wave 4 / Wave 5（arch/roadmap-2026q4）

### 新增
- 契约层：`core/interfaces.py`——Resolver / StreamTransport / CachePolicy /
  ThemeSource 四个 `runtime_checkable` Protocol（16 方法签名，只声明不重构、
  实现零改动）；contract_check [5] 增加结构满足 + 签名指纹对拍门禁（ADR-0005）。
- 配置注册机制：`core/config.py` `register_config_key(key, default,
  validator=None)`——新键经注册入口进 DEFAULTS；validator 只在读取侧生效、
  非法值回落 default 绝不抛；重复注册首注册者胜（ADR-0006；既有键集中
  声明不物理分散，权衡见 ADR）。
- 主题注册表：`ui/theme.py` `THEMES` dict + `register_theme(name, palette)`
  ——LIGHT/DARK 走注册路径进表，未来自定义主题同走此入口；键集校验挡在
  注册点、重复名首注册者胜（与配置注册同一纪律）；`PALETTES` 保留为同一
  对象别名。
- 文档：ARCHITECTURE.md 定稿更新（Wave 2/3 演进同步，行数/计数全部
  2026-10-10 实测复核）+ ADR-0005/0006/0007；ADR-0001/0002/0003/0004
  过时快照修正（taskops 行号漂移、facade 瘦身落地状态、越层收敛现状、
  对拍夹具计数）。

### 变更
- fetcher facade 化（7008cda）：相对 import 22→10；删纯历史再导出
  （STATE_*/taskstore 函数/METADATA_TIMEOUT 等，调用方改直连源模块）；
  定义 `__all__`（7 符号）；SessionManager 补 7 薄委托方法
  （preview_root / enforce_preview_cache_limit / preview_downloaded_bytes /
  proxy_hint / inbound_selfcheck / inbound_hint_text / create_stream_server），
  UI 越层 import 9→**0**（§2.2 白名单落地）。
- main_window 拆分（fe71a02）：1365→838 行，按内聚簇抽出四协作者
  `ui/cache_host.py`（CacheHost：清理/配额/占用）、`ui/stream_host.py`
  （StreamHost：流服务生命周期/开播门控/重试链）、`ui/task_controller.py`
  （TaskController：添加下载/任务 CRUD 槽）、`ui/inbound_host.py`
  （InboundHost：入站探测链含 `_InboundBridge`）；行为零变化，白盒测试
  spy 路径同步迁移。
- 测试目录化（e5d794c）：29 个套件脚本迁入 `tests/`（SUITES 27 套 +
  live_test + pack_check）；`regression_run.py` / `test_support.py` /
  `coverage_run.py` / `diag_*` 留仓库根；31 处路径引导修复。

### 修复
- metacache 剪枝锁内 I/O（Phase 5.1 残留，Wave 3 A7）：`maybe_prune`
  剪枝后的重扫计数 `_count_entries`（scandir）原写在 `_PRUNE_LOCK` 段内，
  违反「锁内禁 I/O」纪律（2026-09-29 审计只外移了主 rescan 路径，此处
  同型漏修）；改为先锁外扫描、回锁只写结果，concurrency_test §E 同型
  AST 断言钉住防再犯。
- 安卓跨平台行为差异闭环（Wave 4 A5c，12 项，桌面为契约基线对齐）：
  C21 点播窗口 60 块常量→`windowPieces` 字节口径（4MB 块 240MB 洪泛→16MB）；
  fastresume 三件套 C41 60s 周期脏写 / C42 退出 drain 去「已有文件跳过」+
  mtime 新鲜度等待 / C43 恢复回退 `.meta` 缓存；C11 元数据超时先 pause 句柄、
  C12 完成自动停止（D3 对齐）、C32 流服务等待/服务双池额度分账；低优先
  C22/C23/C33/C44/C45 顺带。C61 深色板豁免（用户裁决，ADR-0004 双轨）；
  C31/C13/C34 登记真机回归待办。gradlew `--rerun-tasks` 364/364 绿。

### Wave 4 / Wave 5 新增
- 跨平台行为契约：`docs/contract_spec.md`——6 大核心流程（解析状态机/边下边播
  调度/Range 流语义/持久化 fastresume/缓存配额/主题令牌）平台无关伪代码 +
  双端实现位置逐节核实；登记 28 条差异（审计旧 4 项假设核实**全部已修复**，
  与 Wave 3 的 5.2/5.3 同型——审计基线第三次过时）。
- 共享数据模型：`shared/task-schema.json`（draft-07，15 任务字段 + 6 文件字段）；
  桌面 `taskstore.py` 与安卓 `TaskStore.kt` 各自读写 `schema_version`，
  旧文件缺字段容忍（版本 0，绝不因缺字段拒读）。
- CI 覆盖率门禁：`.github/workflows/ci.yml` 新增 `fail-under=70` 硬门禁
  （实测基线 **81%** branch 口径，core 87.7% / ui 75.3%）；CONTRIBUTING 记录
  门槛决策与最低 5 模块爬坡建议。

### Wave 5 收尾验收
- 桌面重打包：`build.bat` → PyInstaller onedir `dist\MagnetViewer`（104.4 MB），
  `tests\pack_check.py` 必需组件 10 项齐全 / 瘦身 4/4 剔除，exe 冒烟启动存活。
- 安卓重打包：`gradlew assembleRelease` → `app-release.apk`（20.7 MB），
  R8 混淆 + lintVital 通过，BUILD SUCCESSFUL。
- 全量回归终态：27/27 全绿（smoke_test 首跑瞬态 `ConnectionResetError 10054`
  经单跑 + 重跑双重复验为环境瞬态，非代码缺陷）。

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
