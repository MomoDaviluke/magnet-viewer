# magnet-viewer 全项目审计报告（2026-09-29）

## 一、审计方法与覆盖面

- **并行子代理审查**（4 路）：桌面端 Python（core/ 24 模块 + main.py）、安卓端 Kotlin 逻辑（engine/stream/core/data/service）、安卓端 UI（theme/ui/res 全部 12 个文件）、资源引用与兼容性（Manifest/build.gradle/res）。
- **主代理逐条实证**：全部 P0/P1 及关键 P2 均由主代理读源码独立验证后才采信；1 条子代理报告的 P1 经实证为**误报**（已剔除：checkMetadataTimeout「暂停恢复永不超时」——`resumeTask` 第 1872 行对无元数据任务正确重置 `metaStartedMs`，所有进入 `STATE_META_FETCH` 的路径均正确置位）。
- **打包产物核查**：当前 APK（9-27 15:24）经 R8 mapping.txt 实证已含 S6/S8 全部最新代码（`IncrementalAvailability -> u6.a` 混淆入包，`X-Why`/`X-Ready-Bytes` 字符串在 dex 中）。

## 二、总体结论

| 维度 | 结论 |
|---|---|
| 桌面端代码质量 | 高。锁纪律（`*_locked` 约定、不可重入 Lock 调用链）无违规；无 P0；历史遗留的 P2-5（HTTP 并发限流）、P2-18（config 键）均已闭环 |
| 安卓端逻辑质量 | 较高。调度器锁/taskSeq/metaCacheWriter/addTracker/info 注入/Content-Length 等重点项均符合修复后形态；存在 1 个 P1（注解错位）与 5 个 P2 |
| 安卓端 UI | **主要短板**。深色模式 3 处 P0（同色字底不可读）、设计令牌「有定义、无执行」、触摸目标不足、空态按钮未接线 |
| 资源引用 | 无断链。所有 @/R 引用均有定义；mipmap 五档齐全；R8 规则正确覆盖 JNI 边界；1 个 P2（备份规则未接线） |
| 兼容性 | minSdk 28 / targetSdk 36 全部 API 调用有版本守卫；FGS dataSync + onTimeout 已实现 |
| 是否重新打包 | **两端修复后都需重新打包**（详见第五节） |

## 三、问题清单（全部经实证）

### P0 —— 安卓深色模式（3 条，功能受损：文字不可读）

| # | 位置 | 问题 | 证据 |
|---|---|---|---|
| 1 | theme/Theme.kt:105 | 深色下 `okSoft = Pal.ok`（0xFF1A7F47）→「已完成」徽章绿字绿底，文字完全隐形 | TaskCard.kt:188 `"COMPLETED" -> pal.okSoft to pal.ok` |
| 2 | theme/Theme.kt:103 | 深色下 `dangerSoft = Pal.dangerPressed`（0xFFA32C1F）与文字 `danger`（0xFFC0392B）对比度约 1.3:1 → 失败徽章/删除确认/错误条红字红底 | TaskCard.kt:189、DeleteConfirmSheet.kt:60-61、MagnetScreen.kt:320-321 |
| 3 | theme/Theme.kt:100 | 深色下 `altRow = Pal.textMuted` 与 `panel` 同值（0xFF5B6472）→ 斑马纹与卡内 inset 完全失去区分 | Color.kt:21 与 Theme.kt:84 |

### P1 —— 功能受损（3 条）

| # | 端 | 位置 | 问题 | 证据 |
|---|---|---|---|---|
| 4 | 桌面 | cache_guard.py:179 | `keep_fn() or ()` 把 None（名单取用失败信号）吞成空元组，`_norm_keep_dirs(())` 返回**空集合**而非 None → 180-181 行「保守停手」检查成死代码 → 失败场景下按空名单继续删，**活任务目录被删 → 引擎假完成/数据丢失** | 入口 145-146 行 `raise` 尊重 None，循环内复核处不尊重；语义见 `_norm_keep_dirs` docstring |
| 5 | 安卓 | BtEngine.kt:386 | `@Synchronized` 注解错位——386 行的注解实际挂在 398 行 `applyTuning` 上（387-397 的 KDoc 隔开），`ensureStarted`（450 行）**无同步** → 多入口并发加种时 check-then-act 竞态可能双启会话 | 386-398 行布局；450 行无任何锁 |
| 6 | 安卓 UI | MagnetScreen.kt:343 | `EmptyState()` 未传 `onSample`（默认空 lambda）→ 空态「示例磁力链」按钮点击无任何反应 | EmptyState.kt 签名有 `onSample: () -> Unit = {}`，调用处 343 行未传 |

### P2 —— 健壮性/体验（11 条）

| # | 端 | 位置 | 问题 |
|---|---|---|---|
| 7 | 桌面 | taskops.py:448,457 | `remove_task` 裸写 `del reg.torrents[key]` + `reg.gen += 1`，绕过 `drop_record_locked`/`_bump_struct_locked` → PreviewCore 目录索引（_dir_index）陈旧、PieceView 回收延迟 |
| 8 | 桌面 | preview.py:60-70,125 | `_views` 字典无锁读写；流服务探测线程写与回收线程的字典迭代并发 → 偶发 RuntimeError → 偶发 503 |
| 9 | 桌面 | stream_server.py:275,284,454 | `_send_file` 只捕 BrokenPipe/ConnectionReset；`_wait_ready` 循环内 `os.path.getsize` 未捕文件被删的 OSError；`handle_error`（486-489）不吞该类型 → 下载中删除任务时连接异常 |
| 10 | 桌面 | session.py:343-350 | 端口回退后第二次 `lt.session(settings)` 无 try → 回退启动也失败则启动进程直接崩 |
| 11 | 桌面 | stream_server.py:376-381 | 无 Range 请求返回 200 但 Content-Length = 可用前缀 ≠ 文件全长 → RFC 7233 语义错误，播放器可能误判文件截断 |
| 12 | 安卓 | BtEngine.kt:541-563 | `drainResumeBeforeShutdown` 只给焦点任务（`currentHandle ?: return`）落 fastresume → 多任务下非焦点任务正常退出丢续传 |
| 13 | 安卓 | BtEngine.kt:1200-1201 | `writeResumeFromAlert` 在 params 取不到哈希时回退 `currentInfoHash` → 非焦点任务的 resume 字节写进焦点任务的 .fastresume，跨任务污染 |
| 14 | 安卓 | BtEngine.kt:659 | 复用既有任务分支 `onMetadataReady(ih, done, true)` 只传 3 参，第 4 参 `download` 默认 true（签名在 231 行，已实证）→ **仅解析任务重复解析后被改回下载型**（J-01 残留） |
| 15 | 安卓 | BtEngine.kt:655,178-186 | EngineTask 的 state/selected/download/result/source 非 @Volatile 且存在锁外写（655 行 `existing.source = uri`）→ 完成判定可能基于陈旧组合 |
| 16 | 安卓 | StreamServer.kt:184-185 | 读完请求头即 `soTimeout = 0`，写 body 阶段无超时 → 停止读取的连接可永久占满 8 线程池（S5 只护了请求阶段） |
| 17 | 安卓 UI | MagnetScreen.kt:516 | 硬编码 `Color(0xFFD0D4DA)` 13sp 正文（未走 `textMuted`）→ 浅色模式白底上对比度约 1.4:1，几乎不可读 |

### P2 —— 资源与兼容（1 条）

| # | 端 | 位置 | 问题 |
|---|---|---|---|
| 18 | 安卓 | AndroidManifest.xml + res/xml/ | `allowBackup="true"` 但 `backup_rules.xml`/`data_extraction_rules.xml` 均未在 Manifest 引用（孤儿文件）→ targetSdk 36 下磁力任务/fastresume 默认全量云备份，有隐私泄露面 |

### P3 —— 低风险/整洁（16 条，摘要）

| 分组 | 条目 |
|---|---|
| 桌面（4） | taskops.py:438,530 持 reg.lock 调 scheduler.stop 的锁序边与 R-10 决策冲突；persist.py:288-292 超时兜底锁外迭代活字典；registry.py:81 tmp-key id 复用撞键；metacache.py:428-441 剪枝在锁内做 scandir I/O |
| 安卓逻辑（4） | StreamServer.kt:222-234 两处 416 缺 X-Ready-Bytes 诊断头；StreamServer.kt:623-632 readLine 超 MAX_LINE 返回半行且不消费行尾；BtEngine.kt:296 shutdown 未关停 metaCacheWriter（写幂等，仅提示）；EngineActivity.kt:90 心跳主线程走 swig JNI status() 理论可掉帧 |
| 安卓 UI（3） | 约 250 处中文文案硬编码未入 strings.xml（strings.xml 仅 app_name）；MainScreen.kt 为模板死代码（Navigation.kt:13 注释声明保留给单测）；MagnetScreen.kt:228 用中文文案字符串做业务逻辑判断（`statusText.contains("复用既有任务")`，文案一改逻辑即断） |
| 资源（2） | `com/example/` 空残留目录；libs.versions.toml 别名 `libtorrent4j-android-x86` 实为 x86_64 构件（命名误导）；android/gradle_apk.log、gradle_test.log 未入库垃圾 |

## 四、安卓端美化改进方向（重点）

### 4.1 修复深色模式（最高优先，对应 P0×3）

深色语义色不应「复用浅色值」，应为 Dark 色板定义独立的容器色：

```
Dark.okSoft     = 0xFF17352A（暗绿底）    Dark.ok     = 0xFF6EE7A0（亮绿字）
Dark.dangerSoft = 0xFF3B1D18（暗红底）    Dark.danger = 0xFFFF8A75（亮红字）
Dark.altRow     = 0xFF525B68（比 panel 深/浅一档的独立值）
```

### 4.2 设计令牌落地（「有定义、无执行」）

- **间距**：新建 `theme/Spacing.kt`，按 4dp 网格收敛 6 种混用值：`xs=4 / sm=8 / md=12 / lg=16 / xl=24`；6dp 与 10dp 全部归并到 8dp/12dp。现状实测：8dp×30、4dp×17、6dp×16、12dp×12、16dp×10、10dp×8。
- **字号**：Type.kt 刻度已存在，但 ui/ 下有 **102 处裸写 `fontSize = X.sp`**——统一替换为 `MaterialTheme.typography.*` 引用。
- **圆角**：UiShapes 三档（卡 16/按钮 12/芯片全圆）已定义，但 ui/ 下仍有 11 处散落 `RoundedCornerShape`，其中存在命名 `RoundedCornerShape4` 实际值 8dp 的误导项，一并清理。

### 4.3 配色与对齐

- MagnetScreen.kt:516 改 `color = pal.textMuted`；全项目硬编码色仅此 1 处散落（其余 23 处集中在 Color.kt，正常）。
- 列表项/卡片内边距统一 `16.dp`（现 12/16 混用）；状态徽章底-字组合全部走 `LocalPal` 语义对，杜绝直接 `(bg, fg)` 裸配。

### 4.4 屏幕适配

- 引入 WindowSizeClass：`widthDp >= 600` 时任务列表与详情双栏（平板/折叠屏/横屏）。
- 所有 `Text` 按语义补 `overflow = TextOverflow.Ellipsis` / `maxLines`（长文件名场景）。
- 触摸目标：seek 滑条 32dp、部分 chips <40dp——统一提升到 48dp（`Modifier.sizeIn(minWidth = 48.dp, minHeight = 48.dp)` 或加大 padding 保持视觉尺寸不变、命中区扩大）。

### 4.5 交互体验

- 输入框补 `keyboardType = KeyboardType.Uri` + `imeAction = ImeAction.Search`（KeyboardActions 直接触发解析）。
- 空态按钮接线（P1 #6）：`EmptyState(onSample = { vm.loadSample() })`。
- 深色启动闪白：themes.xml 固定 `Theme.Material.Light` → 新增 `res/values-night/themes.xml`（深色 NoActionBar），消除深色系统用户启动瞬间的白屏。
- （可选）Predictive Back 显式启用（BackHandler 已正确拦截，属升级项）。

## 五、是否需要重新打包

| 产物 | 现状 | 判定 |
|---|---|---|
| 安卓 APK（9-27 15:24，21,704,836 B） | 经 mapping.txt 实证已含 S6/S8 全部最新代码，**不是旧包** | 可继续用于测试；但 P0×3 + P1×3 + P2×5 修复后**必须重新打包** |
| 桌面 EXE（9-17） | P1（cache_guard）与 6 条 P2 均在此版本之后发现 | 修复批次完成后**需重新打包** |
| 分发建议 | R1 仍是开发签名 | 正式分发前必须换正式签名（既有结论不变） |

## 六、按优先级的行动清单

### 第一批（阻断性，修复后立即重打包）

1. **修安卓深色模式 3 连**（#1-#3）：Dark 色板独立容器色，改 Theme.kt:100/103/105 + Color.kt 补 4 个深色值。
2. **修 cache_guard.py:179**（#4）：去掉 `or ()`，`keep_fn()` 返回 None 时直接 `return removed`（与入口 145-146 行语义对齐）。
3. **修 BtEngine.kt:386 注解错位**（#5）：`@Synchronized` 移到 `ensureStarted` 上（确认 applyTuning 的调用方已有锁后再决定是否保留）。
4. **接线空态按钮**（#6）：MagnetScreen.kt:343 传入 onSample。
5. **接线备份规则**（#18）：Manifest 补 `android:fullBackupContent="@xml/backup_rules"` + `android:dataExtractionRules="@xml/data_extraction_rules"`。

### 第二批（健壮性，与第一批同窗口）

6. taskops.py remove_task 改走 `drop_record_locked`（#7）。
7. BtEngine.kt:659 复用分支补第 4 参 `existing.download`（#14）。
8. writeResumeFromAlert 去掉 currentInfoHash 回退，哈希缺失直接跳过（#13）。
9. drainResumeBeforeShutdown 扩展到全部有变动的任务（#12）。
10. preview.py `_views` 加锁或改线程安全容器（#8）；stream_server `_send_file`/`_wait_ready` 补 OSError 捕获（#9）。
11. session.py:350 二次 lt.session 包 try + 兜底（#10）。
12. StreamServer 写 body 阶段加写超时/心跳（#16）。

### 第三批（体验与美化，随第二批打包）

13. MagnetScreen.kt:516 颜色改 textMuted（#17）；无 Range 200 语义修正（#11）。
14. Spacing 令牌化 + 6dp/10dp 归并 + 102 处裸 sp 迁 Typography（§4.2）。
15. 触摸目标 48dp + 输入框 IME 配置 + values-night（§4.4/4.5）。

### 第四批（整洁，随时）

16. 删除 com/example/ 空目录、gradle 日志；toml 别名更名。
17. 250 处中文文案入 strings.xml（可分批）；MainScreen.kt 死代码去留决策。
18. P3 桌面 4 条按触达时机顺手修。

## 七、审计边界说明

- 本报告基于静态审查 + 源码实证，未运行真机/UI 自动化；深色 P0 建议修复后真机开深色模式目验一轮。
- 工作区现有 78 个未提交文件（9-27 的 S6/S8 工作）尚未 commit，建议先提交现有工作再开修复批次，避免混流。
