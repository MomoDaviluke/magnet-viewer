# 本机 Android 构建环境速查（2026-09-14 安装）

> 面向：在这台机器上跑安卓版构建的人或子代理。**先读这一页再动手**，三个坑都踩过了。

## 装机清单

| 用途 | 路径 | 版本 |
|---|---|---|
| IDE | `C:\Program Files\Android\Android Studio` | 2026.1.4.7（自带 JBR 25，仅供 IDE 用） |
| 构建 JDK（跑 Gradle） | `%LOCALAPPDATA%\Android\jdk-21\jdk-21.0.12.1+1` | Temurin 21.0.12.1 |
| 工具链 JDK（模板要 17） | `%LOCALAPPDATA%\Android\jdk-17\jdk-17.0.20.1+1` | Temurin 17.0.20.1 |
| Android SDK | `%LOCALAPPDATA%\Android\Sdk` | platform-tools 37.0.1 / platforms;android-36 / build-tools;36.0.0 |
| 本地 Gradle | `%LOCALAPPDATA%\Android\tools\gradle-9.1.0` | 9.1.0（官方源太慢，改本地安装） |

环境变量（用户级，新开终端即生效）：`JAVA_HOME` = jdk-21；`ANDROID_HOME` / `ANDROID_SDK_ROOT` = SDK 目录；PATH 追加三处 `bin`。git-bash 另写入 `~/.bashrc`。

`android/gradle.properties` 里登记了本地 JDK（`org.gradle.java.installations.paths` + `auto-download=false`）——**不要删**，否则 Gradle 会去慢速 CDN 自动下载 JDK 17。

## 怎么构建

```bash
cd <repo>/android
export JAVA_HOME='C:\Users\Administrator\AppData\Local\Android\jdk-21\jdk-21.0.12.1+1'
export ANDROID_HOME='C:\Users\Administrator\AppData\Local\Android\Sdk'
G="$LOCALAPPDATA/Android/tools/gradle-9.1.0/bin/gradle.bat"
"$G" :app:assembleDebug        # 产物 app/build/outputs/apk/debug/app-debug.apk
"$G" :app:testDebugUnitTest    # JVM 单测
"$G" :app:connectedDebugAndroidTest   # 仪器测试（需连真机/模拟器）
```

SDK 管理用新的 `android` CLI（2026 版已废弃 `sdkmanager`）：

```bash
"$ANDROID_HOME/cmdline-tools/latest/bin/android.exe" sdk install "platforms;android-36"
"$ANDROID_HOME/cmdline-tools/latest/bin/android.exe" sdk list          # 已装清单
"$ANDROID_HOME/cmdline-tools/latest/bin/android.exe" emulator --help   # AVD 管理
```

## 三个坑（踩过，别重踩）

1. **不要用 `./gradlew`**（bash 版）：它把 MSYS 风格路径（`/c/...`）直接传给 Windows 的 `java.exe` → `ClassNotFoundException: GradleWrapperMain`。本机一律用 `gradle.bat`；wrapper 文件保留给 CI（Linux 上正常）。
2. **给原生 Windows 工具（`sdkmanager` / `aapt2` / `zipalign` / `adb`）传路径必须写 `C:/...` 或 `C:\...`**。传 `/c/...` 会被当成当前盘相对路径——曾把 SDK 装到 `C:\c\Users\...\Sdk` 下（已清理）。
3. **慢速 CDN 清单**（本机实测）：`services.gradle.org` 6 分钟 4.6MB、`api.adoptium.net` 7 分钟 6MB。换镜像：Gradle → `https://mirrors.huaweicloud.com/gradle/`；JDK → `https://mirrors.tuna.tsinghua.edu.cn/Adoptium/<大版本>/jdk/x64/windows/`。Google 的 `dl.google.com`（SDK / Maven）直连正常，不用代理。

## 已核实的状态（2026-09-14）

- `:app:assembleDebug` 通过：首次 12m05s（含依赖下载），增量 1m48s。
- 依赖已接入并打进包：libtorrent4j 2.1.0-39（arm64-v8a + x86_64）、Media3 1.11.1、kotlinx-serialization 1.11.0。
- APK 内含 `lib/arm64-v8a/libtorrent4j.so`（15.9MB）、`lib/x86_64/libtorrent4j.so`（16.4MB）。
- `zipalign -c -P 16 -v 4 app-debug.apk` → `Verification successful`（16KB 页对齐达标）。
- `:app:testDebugUnitTest` 通过（JVM 单测链路可用）。
- **未装 NDK**：因此 `stripDebugDebugSymbols` 会报 `Unable to strip … libtorrent4j.so, packaging them as they are`——原样打包未剥符号（debug 60+MB / 双 ABI）。不影响正确性与 16KB 对齐，只影响体积。**发布前（阶段 7）再装 NDK r29**（约 1GB）让 AGP 剥符号；debug 阶段不用管。
- **尚未安装**：模拟器与系统镜像 —— 待定（真机 USB 调试 or 模拟器）。
- 工程骨架来自 `android create empty-activity` 模板：Gradle 9.1 / AGP 9.0.1 / Kotlin 2.3.20 / Compose BOM 2026.03.01；包名 `com.bitseed.magnetviewer`，minSdk 28 / targetSdk 36。

## 待办（下一步）

- 按计划 Task 0.0 读 LibreTorrent 构建配置，产出 `android/NOTES-libtorrent4j.md`（含 libtorrent4j 符号核对表）。
- 按计划 Task 0.3/0.5/0.6 跑三道可行性闸门（会话能否起、ExoPlayer 能否播增长文件、灭屏能否继续）。
