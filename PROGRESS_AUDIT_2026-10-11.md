# magnet-viewer 开发进度审计（2026-10-11）

> 审计对象：工作区 `C:/Users/Administrator/Desktop/magnet-viewer`
> 审计基准：磁盘实际文件 + git 对象库 + README.md / REVIEW.md / audit_report.md / contract_snapshot.md / plan/*.md
> 实证方式：文件存在性、行数、grep 符号命中、实际运行回归（`.venv/Scripts/python.exe regression_run.py`）

---

## 0. 结论摘要（先看这两条）

### 0.1 进度分裂：工作树落后主线 120 个提交

当前工作树 checkout 在 `main`（HEAD=`e974d68`），而真实主线在 `arch/roadmap-2026q4`。

| 指标 | main（当前工作树） | arch/roadmap-2026q4 |
|------|------------------|---------------------|
| 纳入版本控制的文件数 | **47** | **272** |
| 相对 main 的独有提交 | — | **120** |
| `core/fetcher.py` 行数 | **1579** | **891** |
| 测试套件数 | 8（散落根目录，`regression_run.py` 硬编码） | **29（全部位于 `tests/`）** |
| CI 工作流 | 1（ci.yml） | 3（ci / android / release） |
| 架构文档 | 无 | `docs/ARCHITECTURE.md` + `docs/adr/` + `CHANGELOG.md` + `CONTRIBUTING.md` |
| 安卓源码 | 磁盘上**已被删除** | 141 个文件（其中 95 个 `.kt`） |

`git branch --merged main` 的输出**只有 `main` 本身**——5 个分支（`arch/roadmap-2026q4` / `copilot-fixes` / `refactor-fetcher` / `release-onefile` / `settings-wiring`）**全部未合并**。
`git rev-list --count arch/roadmap-2026q4..<branch>` 对 copilot-fixes / release-onefile / settings-wiring 均为 **0**，对 refactor-fetcher 为 3 —— 说明 **roadmap 分支是事实上的集成尖端**。

### 0.2【P0 风险】安卓端源码在工作树中已不存在

```
android/app/src/main/           ← 不存在（无 AndroidManifest.xml、无 Kotlin 源码）
android/app/src/androidTest/    ← 空目录
android/app/src/test/           ← 空目录
android/app/build.gradle.kts    ← 不存在
android/build.gradle.kts        ← 不存在
android/settings.gradle.kts     ← 不存在
android/gradlew                 ← 不存在
```

`find android -type f | grep -v build | grep -v .gradle` 仅剩 21 个文件：`keystore.properties`、`local.properties`、`magnetviewer-dev.jks` 与 `tools/testrig/` 下的测试夹具。

**成因**：`android/` 在 `main` 上从未被跟踪（`git status --porcelain android` = `?? android/`，`git log --oneline -- android` 为空），但在 `arch/roadmap-2026q4` 上被跟踪（141 个文件）。从 roadmap 切回 main 时，git 把被跟踪的 android 文件全部删除，只留下未被跟踪的编译产物。

**可恢复**：`git checkout arch/roadmap-2026q4 -- android/`。roadmap 上完整存在：
`android/build.gradle.kts`、`android/settings.gradle.kts`、`android/gradlew`、
`android/app/src/main/AndroidManifest.xml`、95 个 `.kt`（`engine/PreviewScheduler.kt`、`engine/PieceView.kt`、`engine/BtEngine.kt`、`stream/PieceSource.kt`、`data/TaskStore.kt` 等）、
28 个 JVM 单测类、`android/tools/release_check.py`、`android/tools/gen_parity_fixtures.py`、
`android/app/src/test/resources/parity/parity.json`。

**为什么能断定「曾完成过」而不是「没做完」**（三条独立证据）：

1. **R8 产物反推**：`android/app/build/intermediates/mapping/release/mapping.txt`（51 MB）记录原始类 **120 个 → 混淆后存活 91 个**，分布在 15 个包：`engine` 45、`data` 29、`theme` 8、`ui.magnet` 8、`core.model` 6、`ui.gallery` 5、`ui.main` 4、`ui.components` 5、`core.http` 2、`core.sched` 2、`stream` 2、`service` 1 等。
2. **历史测试全绿**：`android/app/build/test-results/testDebugUnitTest/` 下 35 个 JUnit XML，**364 个用例 / 失败 0 / 跳过 0**，含 `TaskStoreTest` 22、`StreamServerTest` 22、`TaskProgressTest` 21、`PieceMapTest` 21、`TrackerPushTest` 19、`CacheQuotaTest` 18、`RestorePlanTest` 17、`ProxyProbeTest` 15、`GalleryLogicTest` 15、`SettingsStoreTest` 15 等。
3. **构建日志与产物**：`.android_release.log:42-43` 出现 `BtEngine.kt:1384` / `:2094` 的编译告警（仅此文件即 ≥2094 行）；`android/app/build/outputs/apk/release/app-release.apk`（21,728,460 B）时间戳为 **2026-10-11 00:03:24** —— 即源码直到今天 00:03 仍在盘上，00:22 的分支切换才将其清除。

**已确认的安卓端模块完成度（依据 R8 mapping + 历史用例，非源码）**

| 子系统 | 判定 | 证据 |
|--------|------|------|
| 磁力链解析 / DHT / tracker | ✅ 已实现 | dex 含 `org/libtorrent4j/{DhtLookup, DhtRoutingBucket, AnnounceEntry}`；`engine.TrackerPushKt`、`TrackerPushTest` 19 + `TrackerFallbackTest` 11 |
| 下载管理与任务持久化 | ✅ 已实现（JSON 落盘，非 Room） | `data.{TaskStore, ResumeStore, MetaCache, RestorePlan, MagnetStorage}`；`TaskStoreTest` 22 含原子写/并发/损坏降级/防穿越 |
| 边下边播 —— 本地 HTTP Server | ✅ 已实现且质量高 | `stream.StreamServer` + `PieceSource`；`StreamServerTest` 22 含 206/416/503/鉴权/防穿越/有界队列/读超时回收 |
| 边下边播 —— 分块调度与优先级 | ✅ 已实现 | `engine.PreviewScheduler`、`PreviewGateKt`、`PieceDeadline`；`PreviewControlTest` 11、`PieceViewTest` 9 |
| 边下边播 —— 播放器 | ⚠️ **存疑** | dex 中 `exoplayer` 仅 22 次、`media3` 26 次（疑似传递依赖残留），`android/media/MediaPlayer` = 0、`widget/VideoView` = 0，Manifest 无 `FOREGROUND_SERVICE_MEDIA_PLAYBACK` 与 VIEW intent-filter → 推测走 `ACTION_VIEW` 拉起外部播放器。**需源码恢复后核实** |
| 文件树 / 画廊 / 预览 UI | ✅ 已实现 | 纯 Compose 单 Activity（零 Fragment、零自研 XML layout）；`ui.main` / `ui.magnet` / `ui.gallery` / `ui.settings` / `ui.components`；`GalleryLogicTest` 15 |
| 设置项 5 项（限速 / 缓存配额 / 代理 / 端口映射 / extra trackers） | ✅ 全部已实现 | `RateLimitTest` 5、`CacheQuotaTest` 18、`ProxyProbeTest` 15 + `ProxyMappingTest` 10、`PortMappingTest` 10、`TrackerPushTest` 19 |

**安卓端构建配置（从 Manifest / output-metadata.json / dex 反推）**：applicationId `com.bitseed.magnetviewer`、versionName 1.0 / versionCode 1、minSdk **28**、targetSdk **36**、R8 + resource shrink 已开、单 ABI `arm64-v8a`、**16 KB page size 对齐已满足**（`libtorrent4j.so` 三个 LOAD 段 `p_align` = 16384、`extractNativeLibs=false`）、签名用本机 `magnetviewer-dev.jks`（**开发签名，非正式发布凭据**）。

⚠️ **附带安全风险**：`android/.gitignore` 已随源码一同消失，`android/magnetviewer-dev.jks` 与明文弱口令 `android/app/keystore.properties`（`storePassword=keyAlias=keyPassword=magnetviewer-dev`）目前处于**未被忽略且未跟踪**状态。若直接 `git add android/` 会把私钥入库。恢复源码后须先补回 `android/.gitignore`。

---

## 1. 已完成模块与功能（当前工作树实证）

### 1.1 桌面核心（`core/`，共 11 文件 / 3244 行）

| 模块 | 行数 | 完成度 | 实证 |
|------|------|--------|------|
| `core/parser.py` | 200 | ✅ 完整 | bencode 编解码（深度/整数/长度 DoS 上限）、`.torrent` 与 magnet 解析、`torrent_info_hash()`（v1=SHA-1 / v2=SHA-256 前 20 字节）、`is_pure_v2()` 明确报错 |
| `core/models.py` | 189 | ✅ 完整 | `TorrentFile` / `ParseResult` / `safe_rel_path()` / `file_disk_path()` / `PieceMap` / `contiguous_bytes` / `range_available` / `disk_root` |
| `core/fetcher.py` | 1579 | ✅ 功能完整、⚠️ 体积未治理 | 2 个类（`TaskRecord` :63、`SessionManager` :97）、59 个方法；会话管理、元数据看门狗、代理热更新、任务注册表、持久化、预览调度委托 |
| `core/scheduler.py` | 206 | ✅ 完整 | 单文件锁定 + 顺序分块 + moov 尾部优先 + `request_range()` 幂等点播 + `seek_to_byte` |
| `core/stream_server.py` | 346 | ✅ 功能完整、⚠️ 无限流 | `_is_within()` 防穿越、token/Host 鉴权、Range 206/416、`_wait_ready()` 点播+等待、`_QuietHTTPServer` |
| `core/taskstore.py` | 260 | ✅ 完整 | `.tasks.json` 原子写（`smoke_test` [2d] 断言替换失败旧文件完好、成功后无残留） |
| `core/resume.py` | 66 | ✅ 完整 | fastresume 路径约定 / 编解码 roundtrip / 损坏容错 / 与 libtorrent 互通 |
| `core/cache_guard.py` | 108 | ✅ 完整 | `CACHE_MARKER`、高风险目录拒绝、保留名单 `{downloads, .tasks.json, .resume, marker}` |
| `core/config.py` | 125 | ✅ 完整（12 键全部有读写） | `DEFAULTS` 12 键，grep 命中数最低 4 次，无孤儿键；`lt_proxy_settings()` 含 tracker 重置 |
| `core/logutil.py` | 142 | ✅ 模块完整、⚠️ 开关未接线 | 滚动 1MB×3、线程安全、`_write()` 懒调 `setup()`（故日志实际生效） |

### 1.2 桌面 UI（`ui/`，共 9 文件 / 1979 行）

| 文件 | 行数 | 状态 |
|------|------|------|
| `ui/main_window.py` | 626（33 方法） | 双页签 + 下载页；拖放、输入历史、700ms 状态轮询、closeEvent 清理 |
| `ui/downloads_pane.py` | 319 | 已接入（`main_window.py:206`） |
| `ui/settings_dialog.py` | 183 | **11 个控件全部接线**（`:171-182` 逐一 `cfg.set`） |
| `ui/preview_player.py` | 203 | QMediaPlayer 内嵌播放 + 重试 |
| `ui/gallery.py` | 182 | 图片画廊，按 `save_subdir` 隔离路径加载 |
| `ui/file_tree.py` | 135 | 三级展开 |
| `ui/add_download_dialog.py` | 112 | 已接入（`main_window.py:299`） |
| `ui/status_panel.py` / `preview_pane.py` | 54 / 71 | 状态栏 / 预览容器 |

下载管理模块（README 未列但已实现）：`ui/downloads_pane.py` + `ui/add_download_dialog.py` + `core/taskstore.py` + `core/resume.py`，对应 `plan/t1~t4`。

### 1.3 测试与工程（本次实测）

- **实测回归 8/8 PASS，总耗时 3m23s**（`.venv/Scripts/python.exe regression_run.py`，退出码 0）：
  smoke 6.0s / local_magnet 9.4s / local_torrent 4.1s / single_file 6.9s / gui_feature 1.4s / moov 22.9s / qt_stream_open 27.8s / download_mgr 123.0s
- 冒烟测试单独跑同样 EXIT=0，覆盖 20 个断言段（含 bencode 防御、穿越防护、鉴权、点播+等待、代理映射、多根流服务）。
- `.github/workflows/ci.yml`：windows-latest + Py3.13 + `PYTHONUTF8=1`，跑 `regression_run.py`，上传 `reg_*.log`。
- 打包产物存在：`dist/MagnetViewer.exe`（44,254,859 B）、`dist/MagnetViewer-1.0-android-arm64.apk`（21,728,460 B）。

---

## 2. 进行中 / 部分实现（接口存在，逻辑缺失或未接线）

| # | 项 | 证据 | 缺口性质 |
|---|----|------|---------|
| S-1 | **fetcher.py 未拆分** | 1579 行 / 59 方法 / 2 类；`grep -c "^class " core/fetcher.py` = 2 | 体积。roadmap 已拆为 `fetcher.py` 891 + `session.py` 827 + `taskops.py` 614 + `resolver.py` 466 + `persist.py` 453 + `metacache.py` 532 + `pieceview.py` 351 等，**未合并** |
| S-2 | **日志开关未接线** | `core/logutil.py:20` docstring 声明「由配置 `logging_enabled` 控制」，但 `config.DEFAULTS` 12 键中**无此键**；`set_enabled`/`is_enabled`/`log_path` 在 core/ ui/ 中**零调用点** | 文档超前于实现（audit P2-18，仍未修）。注：日志本身生效——`_write()` 在未 configure 时懒调 `setup()` |
| S-3 | **下载限速无 API 无 UI** | `grep -rn "apply_rate_limit\|download_rate_limit" core/ ui/` = **0 命中**；设置面板 11 控件中无限速项；`download_mgr_test.py:53` 直接 `mgr._ses.apply_settings({"download_rate_limit": ...})` 绕过公开接口 | 底层能力存在，产品侧入口缺失。roadmap 的 `contract_check` 日志含 `[OK] SessionManager.apply_rate_limit` |
| S-4 | **缓存上限 / LRU 未实现** | `core/` 无 `cache_quota.py`（roadmap 有，198 行）；设置面板无缓存上限控件 | REVIEW 第三段第 8 项标记「❌ 未实现」，本次复核仍未做 |
| S-5 | **元数据超时写入越界** | `ui/main_window.py:224` 直写 `self.session._metadata_timeout = ...`；`core/fetcher.py` 只有 `metadata_timeout` property（:195），**无 `apply_metadata_timeout()`** | UI 破 core 封装（audit P2-10 同类） |
| S-6 | **流服务并发无限流** | `core/stream_server.py:288` `class _QuietHTTPServer(ThreadingHTTPServer)`、:330 `daemon_threads = True`；全项目 `grep Semaphore` = **0 命中** | audit P2-5「未做」，本次复核仍未做 |
| S-7 | **死代码** | `core/fetcher.py:1062 current_result()` 全项目零调用；`ui/main_window.py:354-356` 的 `hasattr(self.session,"set_priority")` 分支永不触发（`fetcher.py:704` 有 `set_priority`），「优先级调整暂未开放」提示是死文案 | 低危 |
| S-8 | **异常层级未建立** | `core/` 无 `exceptions.py`；`grep "class .*Error" core/` = 0 命中；`core/`+`ui/` 共 **85 处 `except Exception`** | roadmap 已建 `core/exceptions.py`（83 行，7 类）并收窄至 26 处，**未合并** |
| S-9 | **增量可用性 / 元缓存 / 代理自检缺失** | `grep -c` 于 core/：`PieceView`=0、`metacache`=0、`proxy_probe`=0、`dht_state`=0、`request_deadline`=0、`AtomicSessionContext`=0 | roadmap 均有对应模块（`pieceview.py` 351 / `metacache.py` 532 / `proxycheck.py` 148），**未合并** |
| S-10 | **静默吞异常仍有残留** | `ui/main_window.py:508-512` `except Exception: pass`（预览启动失败完全静默） | 可观测性 |

---

## 3. 尚未开始（对照 README / REVIEW / audit_report / plan）

| # | 缺失项 | 出处 | 实证 |
|---|--------|------|------|
| N-1 | 覆盖率度量与门禁 | REVIEW 第四阶段 10、`audit_report` P2-25 | 无 `pytest.ini` / `pyproject.toml` / `tox.ini`；`requirements.txt` 无 `coverage`。（roadmap：CI `fail-under=70`，实测基线 81%） |
| N-2 | 依赖锁定 | `audit_report` P2-24 | `requirements.txt` 仍为 `PySide6>=6.6` / `libtorrent>=2.0.10`，无 hash 锁定；2.0/2.1 API 不兼容风险仍存在 |
| N-3 | 混合 v2 × 两入口测试矩阵 | REVIEW 第四阶段 9 | main 无 `hybrid_v2_test.py`（roadmap 有 `tests/hybrid_v2_test.py`） |
| N-4 | 纯函数快速单元测试 | README「已知限制」、REVIEW 第四阶段 9 | 无独立纯函数套件；roadmap 的 29 套中含 `pieceview_test` / `proxycheck_test` / `registry_test` / `session_test` 等 |
| N-5 | 打包 spec 不在工作树 | — | 根目录无 `*.spec`，但 `build/` 下存在 `magnet-viewer` 与 `magnet-viewer-onefile` 两个目录；roadmap 有 `magnet-viewer.spec` + `magnet-viewer-onefile.spec` |
| N-6 | release / android CI 不在工作树 | — | `.github/workflows/` 只有 `ci.yml`；roadmap 有 `android.yml` + `release.yml` |
| N-7 | 类型检查门禁 | REVIEW 第五阶段 | 无 mypy 配置 |
| N-8 | Windows 260 字符路径限制 | REVIEW 第七节遗留 3 | 未处理 |
| N-9 | 代理密码明文落注册表 | `audit_report` P3 | `config.py` 直接 `setValue("proxy_pass", ...)`（roadmap 有 `core/secretbox.py` 93 行，**未合并**） |
| N-10 | README「项目结构」章节过时 | — | README:46-73 未列 `ui/downloads_pane.py`、`ui/add_download_dialog.py`、`core/taskstore.py`、`core/resume.py`；README:92 仍称「7 套旧测试」，实际 `regression_run.py` 编排 8 套 |

---

## 4. 阻塞项与风险

| 级别 | 风险 | 证据 | 影响 |
|------|------|------|------|
| **P0** | 安卓源码从工作树消失 | `android/app/src/main` 不存在；`find android/app/src -type f` = 0；无 gradle 构建脚本与 wrapper | 安卓端无法编译、无法继续开发；仅靠 roadmap 分支可恢复 |
| **P0** | 安卓端在 main 上从未纳入版本控制 | `git status --porcelain` 显示 `android/` 为 untracked | 一旦工作树被清理即永久丢失（当前正是这种情况） |
| **P0** | 5 个分支全部未合并 | `git branch --merged main` 仅输出 `main` | 120+100+27+28+1 个提交的工作成果不在主干；CI 只对 main/PR 生效，这些代码无 CI 覆盖 |
| **P1** | 文档与最新实现脱节 | `contract_snapshot.md:3` 自称「2026-09-03 改造前基线」；README/REVIEW 描述的是 main 状态 | 按 README 判断进度会低估约 120 个提交 |
| **P1** | 产物与源码不一致 | `dist/MagnetViewer.exe`、`dist/*.apk` 均为 2026-09-29 产物，对应 roadmap 状态 | 从当前 main 重新打包会产出功能更少的版本 |
| **P1** | 85 处宽泛 `except Exception` | grep 计数 | 故障静默；roadmap 已收窄至 26 处但未合并 |
| **P2** | `live_test.py` 沙箱不可用 | README:89 | 公网 DHT 路径无验证 |
| **P2** | 测试临时目录泄漏 | `audit_report` P2-22 | 未在本次实证范围内复测 |

---

## 5. 下一步建议（按优先级）

**P0 — 先救数据与统一主线（本轮必做）**

1. 恢复安卓源码（顺序不可颠倒）：
   ```bash
   git checkout arch/roadmap-2026q4 -- android/     # 先恢复源码 + android/.gitignore
   # 确认 android/.gitignore 已忽略 *.jks / keystore.properties 后
   git add android/
   ```
   **务必先恢复 `.gitignore` 再 `git add`**，否则 `magnetviewer-dev.jks` 与明文 `keystore.properties` 会被提交入库。
2. 决定主干策略：`arch/roadmap-2026q4` 已包含 copilot-fixes / release-onefile / settings-wiring 的全部提交与 refactor-fetcher 除 3 个外的全部提交，建议**以 roadmap 为集成主线合并回 main**（或把 main 快进到 roadmap），消除 5 分支分裂。合并前先在 roadmap 上跑一次 27 套全量回归确认绿。
3. 合并后同步文档：README 项目结构、套件计数、contract_snapshot 基线说明。

**P1 — 补齐「有接口缺入口」的四项（合并后做）**

4. 下载限速：`SessionManager` 补 `apply_rate_limit()` + 设置面板加控件（roadmap 侧的签名已在 contract_check 中固化，照抄即可）。
5. 缓存上限与 LRU：合入 `core/cache_quota.py`，设置面板加控件，状态栏显示占用。
6. 日志开关：`config.DEFAULTS` 补 `logging_enabled`，设置面板加开关并接 `logutil.set_enabled()`。
7. 流服务并发限流：`_QuietHTTPServer` 加有界信号量 + `handler` 超时（audit P2-5）。

**P2 — 质量基建**

8. 覆盖率门禁（roadmap 已 81%，`fail-under=70`）+ 依赖锁定 + mypy。
9. 测试矩阵补齐：混合 v2 × 本地种子/磁力链两入口；纯函数单测抽离。
10. 把 `magnet-viewer.spec` / `magnet-viewer-onefile.spec` / `release.yml` / `android.yml` 落到主干。

**P3 — 低优先级技术债**

11. Windows 260 字符路径、代理密码加密（`secretbox.py`）、异常层级合入、死代码清理（`current_result`、优先级提示分支）。

---

## 附：本次审计对既有结论的证伪记录

| 既有结论 | 实际情况 | 判定 |
|---------|---------|------|
| 「`core/fetcher.py` 已是 766 行纯委托 Facade，拆分已完成」 | main 上 **1579 行**；roadmap 上 891 行 | 结论对应 roadmap 分支，对 main 不成立 |
| 「stream_server 并发限流已修」 | main 上 `grep Semaphore` = 0 | 同上，对 main 不成立 |
| 「设置三处接线已完成（限速 / 缓存上限 / 日志开关）」 | main 的设置面板 11 控件中无此三项，core 无对应模块 | 对应 `settings-wiring` 分支，未合并进 main |
| 「日志 `setup()` 从未被调用 → 日志不生效」 | `logutil._write()` 在未 configure 时懒调 `setup()`，日志实际生效 | 证伪；真正缺口只是「开关未接线」 |
| 「`push_recent` 未接线」 | `ui/main_window.py:271` 有调用 | 证伪 |
