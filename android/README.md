# MagnetViewer for Android（安卓版）

磁力链预览器的安卓实现：**输入磁力链或 .torrent，不下载资源本体就能看到完整文件清单**；对视频支持边下边播，对图片支持即点即看（内嵌画廊）。

与桌面版（仓库根的 Python/PySide6 实现）的关系：**桌面版是行为参照，安卓版是原生重写**。
调度参数（播放窗口按字节、递增 deadline、moov 尾部入口块、点播上限）、落盘布局、设置键名都与桌面版逐条对齐。

---

## 当前能力

| 能力 | 说明 |
|---|---|
| 磁力链解析 | DHT + BEP-9 拿元数据（几十 KB），**不下载本体**；支持 v1 与 v1+v2 混合种子 |
| 文件清单 | 目录层级、单文件大小、已下载字节、优先级 |
| 视频边下边播 | 单文件锁定 + 顺序分块 + moov 尾部优先 → 本地 HTTP 流服务（仅 127.0.0.1，带 token）→ Media3/ExoPlayer 播放；进度条可拖动跳转 |
| 图片画廊 | 缩略图网格（已下载显示图片、未下载显示占位+进度）→ 大图缩放/翻页；翻到未下载的图会自动把下载目标切过去 |
| 多任务 | 任务列表；暂停/继续、**转正**（`.preview/<ih>` → `downloads/<ih>`）、**移除**（保留数据 / 删数据，双守卫） |
| 缓存配额 | `cache_limit_mb` 超限按 LRU 清理 `.preview/`（保护活跃任务），设置页可「立即清理」并看当前占用 |
| 持久化 | `.tasks.json` 原子写 + `.resume/<ih>.fastresume`；**杀进程/重启后清单自动恢复**，可续传 |
| 后台运行 | `dataSync` 前台服务 + 通知（暂停/继续/停止）；灭屏继续下载 |
| 设置 | 代理（none/socks5/http + 账号密码 + peer 走代理）、下载限速、元数据超时、缓存上限、预览缓存模式（convert/hold）、主题（浅色/深色/跟随系统）、日志开关；DataStore 持久化，保存即生效 |

## 落盘布局（与桌面一致）

```
<应用内部存储>/magnet/cache/
├── .preview/<infohash>/…     # 预览/转正前的数据（配额只管这一层）
├── downloads/<infohash>/…    # 转正后的下载任务
├── .tasks.json               # 任务清单（原子写）
└── .resume/<infohash>.fastresume
```

> 用**应用内部存储**（不是 `getExternalFilesDir`）：外部专属目录位于 `/storage/emulated/0`，读写要过 MediaProvider 的 FUSE 层，
> 在 Android 16 模拟器上会触发 MediaProvider 自身 SIGABRT 并连带杀掉本应用（实测）。

## 构建

```bash
export JAVA_HOME='C:\Users\Administrator\AppData\Local\Android\jdk-21\jdk-21.0.12.1+1'   # 见 ENV.md
export ANDROID_HOME='C:\Users\Administrator\AppData\Local\Android\Sdk'
cd android
G=/c/Users/Administrator/AppData/Local/Android/tools/gradle-9.1.0/bin/gradle.bat
"$G" :app:testDebugUnitTest      # JVM 单测
"$G" :app:assembleDebug          # 调试包（arm64 + x86_64）
"$G" :app:assembleRelease        # 发布包（仅 arm64、R8、需 app/keystore.properties）
```

⚠️ **本机不要用 `./gradlew`**：bash 版 wrapper 会把 MSYS 路径直接喂给 Windows 的 `java.exe`，报 `ClassNotFoundException: GradleWrapperMain`。
本机用 `gradle.bat`；CI（Linux）用 wrapper 正常。更多环境细节见 `ENV.md`。

## 安装到手机

```bash
adb install -r android/app/build/outputs/apk/release/app-release.apk
```

发布包约 **20.5 MiB**：R8 混淆 + 仅 `arm64-v8a`（现代手机都是这个）。签名为**开发用 keystore**
（`android/app/keystore.properties`，不入库）——正式分发前请换成自己的凭据，见 `app/keystore.properties.example`。

首次启动会请求通知权限（拒绝也能用，只是没有前台服务通知）。

## 使用步骤

1. 粘贴磁力链 → 「解析」→ 出现文件清单（此步**不下载本体**）；
2. 点视频行「预览」→ 边下边播；进度条可拖动（会按需补拉目标区间）；
3. 点图片行的「图片 · 画廊」→ 缩略图网格 → 点开大图（缩放/翻页）；
4. 任务列表里可暂停/继续、转正到 `downloads/`、移除（可选是否删数据）；
5. 退出后重进：清单与任务状态自动恢复。

## 已知限制（如实说明）

- **纯 v2 种子**（只有 `file tree`、无 v1 哈希）不支持——与桌面版同。
- **AVI/WMV 等容器**：安卓系统解码器覆盖比桌面 FFmpeg 窄，可能放不了；可把流地址交给外部播放器。
- **前台服务时长**：Android 15+ 对 `dataSync` 类型有「24 小时内累计 6 小时」上限，到点系统会回调 `onTimeout()`，我们优雅暂停并提示。
- **下载限速**在部分场景不生效（libtorrent 限速器与大块/本地通道交互问题，桌面版同源），做种端侧限速在本机回环上也不稳定。
- **工作目录在应用内部存储**：文件对文件管理器不可见，卸载即删。需要交付给用户时走导出（后续阶段）或先转到 `downloads/` 目录。
- 模拟器上读写 `/storage/emulated/0` 会触发系统 MediaProvider 崩溃（见上）；真机不受影响，但代码已统一走内部存储。

## 测试与验证

| 手段 | 现状 |
|---|---|
| JVM 单测 | **181 个**（流服务语义、调度纯函数、持久化/恢复、配额 LRU、任务操作守卫、画廊逻辑等），无 `@Ignore`、无空断言 |
| 跨语言对拍 | `tools/gen_parity_fixtures.py` 由桌面 Python 生成 golden 夹具（13 组 / 155 条），Kotlin 侧逐条断言一致；`--check` 可作 CI 门禁 |
| 端到端靶场 | `tools/testrig/`：本机做种 + 磁力链 `x.pe` peer 提示 + 模拟器 `adb` 驱动，**不依赖公网**；含视频/大文件/图片三套夹具 |
| 发布包实测 | release APK 在模拟器上跑通「解析 → 清单 → 预览出画面」；签名/16KB 对齐/ABI 三项均过 |
| CI | `.github/workflows/android.yml`（构建+单测+上传产物，另含对拍夹具一致性门禁）；**尚未在 GitHub 上真跑过** |
