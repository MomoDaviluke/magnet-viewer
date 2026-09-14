# LibreTorrent 调研笔记：构建配置、清单与 libtorrent4j 2.1.0-39 符号核对

> 调研日期：2026-09-14 ｜ 方式：只读调研（未改动任何代码，未做任何 git 操作）
> 用途：本项目 `android/`（Kotlin + Jetpack Compose + libtorrent4j）的事实核对源。
> 许可证提醒：LibreTorrent 是 **GPL-3.0**，本报告只摘录「构建配置 / 清单 / 设置映射」这类事实性片段用于核对，**不要复制其 Java 源码**。

## 0. 调研对象、版本与取证方式

| 项 | 值 |
|---|---|
| 上游主仓（GitLab） | https://gitlab.com/proninyaroslav/libretorrent |
| GitHub 镜像 | https://github.com/proninyaroslav/libretorrent |
| 快照提交 | `master` @ `ae96f20b9852d2140fd704a3f2451ed7679e35a0`（2026-09-14 经 `api.github.com/repos/proninyaroslav/libretorrent/branches/master` 读取） |
| 语言 | **纯 Java**（仓库文件表内 0 个 `.kt` 文件） |

**取证方式**：本机解析不到 `raw.githubusercontent.com`（`curl: (6) Could not resolve host`），文件正文经 `cdn.jsdelivr.net/gh/proninyaroslav/libretorrent@master/<path>` 与 `api.github.com` 拉取。下文「来源」给的是对应网页链接；GitHub 形如 `https://github.com/proninyaroslav/libretorrent/blob/master/<path>`，GitLab 等价路径为 `https://gitlab.com/proninyaroslav/libretorrent/-/blob/master/<path>`。

**必须先注意的版本差异**：LibreTorrent master 用的 libtorrent4j 是 **2.1.0-38**，不是本项目的 **2.1.0-39**（原因见 §1.3）。因此本报告把「LibreTorrent 的配置写法」与「libtorrent4j 2.1.0-39 的真实 API（javadoc）」分开取证。

---

## 1. libtorrent4j 依赖怎么声明的

### 1.1 artifact 与版本（版本目录 + 模块依赖）

文件：`gradle/libs.versions.toml`

```toml
[versions]
libtorrent4j = "2.1.0-38"
...
[libraries]
libtorrent4j = { group = "org.libtorrent4j", name = "libtorrent4j", version.ref = "libtorrent4j" }
libtorrent4j-android-arm = { group = "org.libtorrent4j", name = "libtorrent4j-android-arm", version.ref = "libtorrent4j" }
libtorrent4j-android-arm64 = { group = "org.libtorrent4j", name = "libtorrent4j-android-arm64", version.ref = "libtorrent4j" }
libtorrent4j-android-x86 = { group = "org.libtorrent4j", name = "libtorrent4j-android-x86", version.ref = "libtorrent4j" }
libtorrent4j-android-x86_x4 = { group = "org.libtorrent4j", name = "libtorrent4j-android-x86_64", version.ref = "libtorrent4j" }
```

文件：`app/build.gradle`（第 196–201 行）

```groovy
    /* libtorrent wrapper */
    implementation libs.libtorrent4j
    implementation libs.libtorrent4j.android.arm
    implementation libs.libtorrent4j.android.arm64
    implementation libs.libtorrent4j.android.x86
    implementation libs.libtorrent4j.android.x86.x4
```

**结论**：共 5 个 artifact —— core = `org.libtorrent4j:libtorrent4j`；ABI 包 = `libtorrent4j-android-arm`（armeabi-v7a）、`libtorrent4j-android-arm64`、`libtorrent4j-android-x86`、`libtorrent4j-android-x86_64`；版本号由版本目录统一为 `2.1.0-38`；四个 ABI **全量声明**（不做"按变体挑 ABI"）。

来源：
- https://github.com/proninyaroslav/libretorrent/blob/master/gradle/libs.versions.toml
- https://github.com/proninyaroslav/libretorrent/blob/master/app/build.gradle

### 1.2 ABI split / abiFilters（两处都配了）

文件：`app/build.gradle`（第 30–32 行）

```groovy
        ndk {
            abiFilters 'armeabi-v7a', 'arm64-v8a', 'x86', 'x86_64'
        }
```

文件：`app/build.gradle`（第 92–101 行）

```groovy
    // https://issuetracker.google.com/issues/402800800
    def isBuildingBundle = gradle.startParameter.taskNames.any { it.toLowerCase().contains("bundle") }
    splits {
        abi {
            enable = !isBuildingBundle
            reset()
            include 'armeabi-v7a', 'arm64-v8a', 'x86', 'x86_64'
            universalApk = true
        }
    }
```

另有第 132–141 行的 per-ABI versionCode 偏移：`project.ext.versionCodes = ['armeabi-v7a': 1, 'arm64-v8a': 2, 'x86': 3, 'x86_64': 4]`。

**结论**：出 APK 时按 ABI 拆包并**额外产 universal APK**；出 App Bundle 时关闭 split（上游注释指向 Google issuetracker 402800800）。`ndk.abiFilters` 与 `splits.abi` 同时存在，构成"双保险"。

来源：https://github.com/proninyaroslav/libretorrent/blob/master/app/build.gradle

### 1.3 为什么是 -38 而不是 -39（直接影响我们）

- 文件 `NEWS.md`，`### Version 4.1.1 (2026-07-24)` 条目：`Temporarily downgrade libtorrent4j to maintain support for Android 8.1 and below`
- `gradle/libs.versions.toml` 该行最近提交（commit `f5b61de8d2e7c6c70780a2714e05549a7fab7e31`）标题：`Temporarily downgrade libtorrent4j to maintain support for Android 8.…`
- libtorrent4j `changelog.txt` 的 `2.1.0-39` 段第一条：`minimum supported Android API >= 28`

**结论**：2.1.0-39 把最低 API 抬到 28，LibreTorrent 为保 `minSdk 24` 而停在 -38。本项目 `minSdk=28`，用 -39 无冲突（与本项目现状一致）。

来源：
- https://github.com/proninyaroslav/libretorrent/blob/master/NEWS.md
- https://github.com/aldenml/libtorrent4j/blob/master/changelog.txt

---

## 2. minSdk / compileSdk / targetSdk 与工具链版本

文件：`app/build.gradle`（第 17–27 行）

```groovy
    defaultConfig {
        applicationId 'org.proninyaroslav.libretorrent'
        minSdkVersion 24
        targetSdkVersion 35
        compileSdkVersion 36
        // Version code format: 0000xx0
        versionCode 9000350
        versionName '4.1.1'
```

文件：`gradle/libs.versions.toml`

```toml
agp = "9.2.1"
...
[plugins]
android-application = { id = "com.android.application", version.ref = "agp" }
navigation-safeargs = { id = "androidx.navigation.safeargs", version.ref = "navigationSafeargs" }
```

文件：`gradle/wrapper/gradle-wrapper.properties`（全文）

```
distributionBase=GRADLE_USER_HOME
distributionPath=wrapper/dists
distributionUrl=https://services.gradle.org/distributions/gradle-9.6.1-bin.zip
networkTimeout=10000
...
```

文件：`gradle/gradle-daemon-jvm.properties`（末两行）：`toolchainVendor=JETBRAINS`、`toolchainVersion=21`

文件：`app/build.gradle`（第 103–108 行）

```groovy
    compileOptions {
        encoding = 'UTF-8'
        coreLibraryDesugaringEnabled = true
        sourceCompatibility JavaVersion.VERSION_17
        targetCompatibility JavaVersion.VERSION_17
    }
```

| 项 | 值 |
|---|---|
| minSdk | **24** |
| compileSdk | **36** |
| targetSdk | **35** |
| Java 源码/字节码 | **17**（启用 core library desugaring，`desugar_jdk_libs = "2.1.5"`） |
| AGP | **9.2.1** |
| Gradle wrapper | **9.6.1** |
| Gradle daemon JVM | 工具链 **21**（vendor = JETBRAINS） |
| Kotlin | **不存在**：全仓库 0 个 `.kt` 文件；plugins 仅 `com.android.application` 与 `androidx.navigation.safeargs`（`build.gradle` 第 1–4 行）。故「Kotlin 版本」在 LibreTorrent 侧无从谈起（仅有 `lifecycle-viewmodel-ktx` 等传递引入的 kotlin-stdlib） |

补充（非阻塞）：`.gitlab-ci.yml` 用 `eclipse-temurin:17-jdk-jammy`，但变量 `ANDROID_COMPILE_SDK: "34"` / `ANDROID_BUILD_TOOLS: "34.0.0"` 与 compileSdk 36 不一致，属上游 CI 遗留，不影响构建结论。

来源：
- https://github.com/proninyaroslav/libretorrent/blob/master/app/build.gradle
- https://github.com/proninyaroslav/libretorrent/blob/master/gradle/libs.versions.toml
- https://github.com/proninyaroslav/libretorrent/blob/master/gradle/wrapper/gradle-wrapper.properties
- https://github.com/proninyaroslav/libretorrent/blob/master/gradle/gradle-daemon-jvm.properties

---

## 3. 前台服务、通知与权限

### 3.1 服务声明（Manifest）

文件：`app/src/main/AndroidManifest.xml`（第 133–139 行）

```xml
        <service
            android:name=".service.TorrentService"
            android:foregroundServiceType="dataSync|specialUse">

            <property android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
                android:value="This service implements the BitTorrent protocol, which allows you to download and share files to other people. BitTorrent was created to share files with people over a network and requires listening to the network interface for a time that the user chooses. Since BitTorrent sharing files over the network from a device is a continuous process and cannot be limited to 6 hours a day, special permission is required so that the user can get the best experience with the application."/>
        </service>
```

**结论**：`foregroundServiceType="dataSync|specialUse"`，并用 `PROPERTY_SPECIAL_USE_FGS_SUBTYPE` 提供 specialUse 的用途说明（申报文本已直接写成"BT 需要长期监听网络，不能限制在每天 6 小时"）。

### 3.2 运行时 startForeground 分档

文件：`app/src/main/java/org/proninyaroslav/libretorrent/service/TorrentService.java`（第 432–440 行）

```java
    private void startForegroundNotification(Notification notification) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            startForeground(SERVICE_STARTED_NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(SERVICE_STARTED_NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(SERVICE_STARTED_NOTIFICATION_ID, notification);
        }
    }
```

（`SERVICE_STARTED_NOTIFICATION_ID = -1`，见该类第 77 行）

**结论**：Android 14+（UPSIDE_DOWN_CAKE）走 **SPECIAL_USE**；API 29–33 走 DATA_SYNC；更低不打类型。这是对「Android 15+ `dataSync` FGS 24 小时内累计 6 小时上限」的实现级规避——34+ 根本不用 dataSync 类型，改走 specialUse（代价是需要上架说明/审核）。

### 3.3 通知渠道

文件：`app/src/main/java/org/proninyaroslav/libretorrent/ui/TorrentNotifier.java`（第 51–53、85–113 行）

```java
    public static final String FOREGROUND_NOTIFY_CHAN_ID = "org.proninyaroslav.libretorrent.FOREGROUND_NOTIFY_CHAN";
    public static final String DEFAULT_NOTIFY_CHAN_ID = "org.proninyaroslav.libretorrent.DEFAULT_NOTIFY_CHAN_ID";
    public static final String FINISH_NOTIFY_CHAN_ID = "org.proninyaroslav.libretorrent.FINISH_NOTIFY_CHAN_ID";
...
        NotificationChannel foregroundChan = new NotificationChannel(
                FOREGROUND_NOTIFY_CHAN_ID,
                appContext.getString(R.string.foreground_notification),
                NotificationManager.IMPORTANCE_LOW
        );
        foregroundChan.setShowBadge(false);
...
        notifyManager.createNotificationChannels(channels);
```

**结论**：三个渠道，一次性 `createNotificationChannels` 建好——前台渠道 `IMPORTANCE_LOW`（静音、无角标），默认渠道与完成渠道 `IMPORTANCE_DEFAULT`（完成渠道开灯/振动）。

### 3.4 通知的构建、内容与动作按钮

文件：`TorrentService.java`（第 382–405 行，建通知）

```java
        foregroundNotify = new NotificationCompat.Builder(getApplicationContext(),
                TorrentNotifier.FOREGROUND_NOTIFY_CHAN_ID)
                .setSmallIcon(R.drawable.ic_app_notification)
                .setContentIntent(startupPendingIntent)
                .setContentTitle(getString(R.string.app_running_in_the_background))
                .setTicker(getString(R.string.app_running_in_the_background))
                .setWhen(System.currentTimeMillis());

        setForegroundNotifyActions(pref.foregroundNotifyCombinedPauseButton());
        foregroundNotify.setCategory(Notification.CATEGORY_SERVICE);
        startForegroundNotification(foregroundNotify.build());
```

内容更新（第 442–526 行 `makeDetailNotifyInboxStyle`）：

```java
        var inboxStyle = new NotificationCompat.InboxStyle();
        ...
            if (code == TorrentStateCode.DOWNLOADING) {
                downloadingTorrents.add(state);
                inboxStyle.addLine(getString(R.string.downloading_torrent_notify_template,
                        state.progress,
                        state.ETA >= TorrentInfo.MAX_ETA
                                ? Utils.INFINITY_SYMBOL
                                : DateUtils.formatElapsedTime(state.ETA),
                        Formatter.formatFileSize(this, state.downloadSpeed),
                        state.name)
                );
```

**进度呈现方式**：**不是** `setProgress()` 进度条，而是 `NotificationCompat.InboxStyle` 的**文本行**（每行 = 单个种子的 百分比 / ETA / 速度 / 名字）；只有单一下载中种子时，标题用种子名、正文用单条模板；无任务时显示"后台运行中"。

动作按钮（第 528–563 行 `setForegroundNotifyActions`）：

```java
        if (combinedPauseButton) {
            foregroundNotify.addAction(new NotificationCompat.Action.Builder(
                    paused ? R.drawable.ic_play_arrow_24px : R.drawable.ic_pause_24px,
                    paused ? getString(R.string.resume_all) : getString(R.string.pause_all),
                    pauseResumePendingIntent).build());
        } else {
            foregroundNotify.addAction(... R.drawable.ic_pause_24px, getString(R.string.pause_all), pauseButtonPendingIntent ...);
            foregroundNotify.addAction(... R.drawable.ic_play_arrow_24px, getString(R.string.resume_all), resumeButtonPendingIntent ...);
        }
        foregroundNotify.addAction(new NotificationCompat.Action.Builder(
                R.drawable.ic_power_settings_new_24px, getString(R.string.shutdown), shutdownPendingIntent).build());
```

PendingIntent 一律 `PendingIntent.getBroadcast(..., NotificationReceiver.class, <action>, FLAG_UPDATE_CURRENT | FLAG_IMMUTABLE)`（第 570–596 行）。

接收器：`app/src/main/java/org/proninyaroslav/libretorrent/receiver/NotificationReceiver.java`（第 36–40 行）定义 5 个 action 常量：`NOTIFY_ACTION_SHUTDOWN_APP` / `NOTIFY_ACTION_ADD_TORRENT` / `NOTIFY_ACTION_PAUSE_ALL` / `NOTIFY_ACTION_RESUME_ALL` / `NOTIFY_ACTION_PAUSE_RESUME_ALL`；Manifest 第 141–148 行注册该 receiver（`android:exported="false"`，intent-filter 列前两个 action）。

其它实现细节：刷新节流 `FOREGROUND_NOTIFY_UPDATE_DELAY = 1000`（ms，第 78 行）；服务返回 `START_STICKY`（第 199 行）；按设置可选 `PARTIAL_WAKE_LOCK` 保活（第 247–266 行）。

### 3.5 Manifest 权限清单（全量，逐条）

文件：`app/src/main/AndroidManifest.xml`（第 6–28 行）

```xml
    <uses-permission android:name="android.permission.INTERNET" />
    <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
    <uses-permission android:name="android.permission.WAKE_LOCK" tools:node="replace" />
    <uses-permission android:name="android.permission.RECEIVE_BOOT_COMPLETED" />
    <uses-permission android:name="android.permission.QUICKBOOT_POWERON" />
    <uses-permission android:name="android.permission.ACCESS_WIFI_STATE" />
    <uses-permission android:name="android.permission.CHANGE_WIFI_STATE" />
    <uses-permission android:name="android.permission.VIBRATE" />
    <uses-permission android:name="com.android.launcher.permission.INSTALL_SHORTCUT" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE_DATA_SYNC" android:maxSdkVersion='33'  />
    <uses-permission android:name="android.permission.FOREGROUND_SERVICE_SPECIAL_USE" />
    <uses-permission android:name="android.permission.READ_EXTERNAL_STORAGE" />
    <uses-permission android:name="android.permission.WRITE_EXTERNAL_STORAGE" tools:ignore="ScopedStorage" />
    <uses-permission android:name="android.permission.MANAGE_EXTERNAL_STORAGE" tools:ignore="ScopedStorage" />
    <uses-permission android:name="android.permission.SCHEDULE_EXACT_ALARM" />
    <uses-permission android:name="android.permission.POST_NOTIFICATIONS" />
```

注意 `FOREGROUND_SERVICE_DATA_SYNC` 带 `android:maxSdkVersion='33'`（只在 ≤33 生效），与 §3.2 的分档一致。另有 `android:usesCleartextTraffic="true"`（第 51 行）。

来源：
- https://github.com/proninyaroslav/libretorrent/blob/master/app/src/main/AndroidManifest.xml
- https://github.com/proninyaroslav/libretorrent/blob/master/app/src/main/java/org/proninyaroslav/libretorrent/service/TorrentService.java
- https://github.com/proninyaroslav/libretorrent/blob/master/app/src/main/java/org/proninyaroslav/libretorrent/ui/TorrentNotifier.java
- https://github.com/proninyaroslav/libretorrent/blob/master/app/src/main/java/org/proninyaroslav/libretorrent/receiver/NotificationReceiver.java

---

## 4. 存储与权限策略（SAF / 应用专属目录 / MANAGE_EXTERNAL_STORAGE）

LibreTorrent 是**三条路线并存**，不是单选。

### 4.1 全文件访问（base flavor 默认）

- `app/src/main/AndroidManifest.xml`：声明 `MANAGE_EXTERNAL_STORAGE`、`WRITE_EXTERNAL_STORAGE`（`tools:ignore="ScopedStorage"`）、并在 `<application>` 上 `android:requestLegacyExternalStorage="true"`。
- **Play flavor 显式删除**：`app/src/play/AndroidManifest.xml`（全文 9 行）

```xml
<manifest ...>
    <!-- Remove MANAGE_EXTERNAL_STORAGE for Google Play -->
    <uses-permission android:name="android.permission.MANAGE_EXTERNAL_STORAGE"
        tools:node="remove"
        tools:ignore="ScopedStorage" />
</manifest>
```

- 运行时申请：`ui/PermissionManager.java`（第 96–116、130–137 行）

```java
    public void requestPermissions() {
        ArrayList<String> permissionsList = new ArrayList<>();
        if (!checkStoragePermissions()) {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && hasManageExternalStoragePermission) {
                if (pref.askManageAllFilesPermission()) {
                    var uri = Uri.fromParts("package", appContext.getPackageName(), null);
                    var i = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION).setData(uri);
                    manageExternalStoragePermission.launch(i);
                }
            } else {
                permissionsList.add(Manifest.permission.WRITE_EXTERNAL_STORAGE);
            }
        }
        ...
    }

    public boolean checkStoragePermissions() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R && hasManageExternalStoragePermission) {
            return Environment.isExternalStorageManager();
        } else {
            return ContextCompat.checkSelfPermission(appContext, Manifest.permission.WRITE_EXTERNAL_STORAGE) == PackageManager.PERMISSION_GRANTED;
        }
    }
```

其中 `hasManageExternalStoragePermission` 由 `Utils.hasManageExternalStoragePermission()`（`core/utils/Utils.java:725`）读 `PackageManager.GET_PERMISSIONS` 判断——即**先看清单里有没有声明该权限**（Play flavor 被 remove 掉，走不到这条分支），这正好对应注释 `// MANAGE_EXTERNAL_STORAGE is disabled for the Google Play version so we need to check before requesting permission`。

### 4.2 SAF 路线（无全文件访问时靠它）

- `core/system/SafFileSystem.java`：SAF（`Intent.ACTION_OPEN_DOCUMENT_TREE` 得到的 tree URI）封装，核心是 **FakePath** 机制——把 SAF 路径编码成"像普通路径"的字符串交给 libtorrent（该文件类注释原文）：

```
 * Fake path example: '/saf_root(com.android.externalstorage.documents/tree/volumeId/);/foo/bar.txt'
 * 'saf_root()'  - a special path format that encapsulates the SAF path and helps
 *                 distinguish it from the rest of the path.
 * ... This is makes for compatibility with `libtorrent` (via `LibTorrentSafAdapter`), which works with the classic `File` object.
```

  以及把原生 fd 交出去（`openFD` → `ParcelFileDescriptor.detachFd()`）：

```java
        ContentResolver cr = appContext.getContentResolver();
        try (ParcelFileDescriptor fd = cr.openFileDescriptor(filePath, mode)) {
            if (fd == null) return -1;
            return fd.detachFd();
```

- `core/system/FsModuleResolverImpl.java`（第 42–50 行）：按 URI scheme 路由——`content://` → SAF 模块，文件系统路径 → 默认模块：

```java
    public FsModule resolveFsByUri(@NonNull Uri uri) throws UnknownUriException {
        if (Utils.isSafPath(appContext, uri)) {
            return safModule;
        } else if (Utils.isFileSystemPath(uri)) {
            return defaultModule;
        } else {
            throw new UnknownUriException("Cannot resolve file system for the given uri: " + uri);
        }
    }
```

- `core/system/SafFsModule.java`：SAF 侧的 FsModule 实现；磁盘可用空间用 `Os.fstatvfs`（`getDirAvailableBytes`）。
- 文件选择：`ui/filemanager/FileManagerFragment.java` 用 `ActivityResultContracts.OpenDocument()` + `takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION)`，并有一段解释性注释：

```java
    /*
     * Without MANAGE_EXTERNAL_STORAGE (e.g. the Play flavor), the in-app browser can only
     * see the directory structure of public folders like Downloads on Android 10+, not the
     * files inside them (scoped storage). The system document picker doesn't have this
     * limitation, so it's used instead for picking a file to open/read.
     */
```

### 4.3 默认落盘目录

`core/system/FileSystemFacadeImpl.java`（第 78–90 行）：默认下载目录 = **公共 Downloads 目录**（不是应用专属目录）：

```java
    public String getDefaultDownloadPath() {
        String path = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS).getAbsolutePath();
        ...
    }
```

（用户自选目录以 `content://` URI 字符串存进偏好，如 `pref.saveTorrentsIn(uri.toString())`，见 `ui/settings/pages/StorageSettingsFragment.java`）

### 4.4 针对 Android 11+ 分区存储的处理（总结）

1. base/F-Droid/直装版：targetSdk ≥ 30 后 `requestLegacyExternalStorage` 失效 → 改走 **MANAGE_EXTERNAL_STORAGE**（R+ 用 `Environment.isExternalStorageManager()` 判定 + `ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION` 申请）；因此默认能用真实 POSIX 路径写公共目录。
2. Play 版：清单里 remove 掉 MANAGE_EXTERNAL_STORAGE → 退回 **SAF**（用户授权 tree URI / 系统文档选择器）+ **原生 fd**（`detachFd`）喂给 libtorrent；README 明确写：`**[Note for Android 12+]**: Google Play version doesn't have permission to access all files, use another version (F-Droid or direct APK).`
3. **没有使用"应用专属目录"作为默认落盘**（这是与本项目计划的一处策略差异：我们计划默认落应用专属外部目录、免权限；LibreTorrent 落公共目录 + 全文件访问/SAF）。

### 4.5 未查证

- **未查证：SAF 目录授权（`ACTION_OPEN_DOCUMENT_TREE` / `ActivityResultContracts.OpenDocumentTree`）的具体启动点。** 已检索 `FileManagerFragment.java`、`FileManagerViewModel.java`、`MainActivity.java`、`NavBarFragment.java`、`StorageSettingsFragment.java` 均未命中（`FileManagerFragment` 中只找到 `OpenDocument()` 与 `takePersistableUriPermission`，用于"选文件"场景）。已确认的相关事实：目录选择通过对 `FileManagerConfig.Mode.DIR_CHOOSER` 的 `dirChooseDialog(...)` 进入内置文件管理器（`StorageSettingsFragment.java:272-287`），结果 URI 写回偏好。

来源：
- https://github.com/proninyaroslav/libretorrent/blob/master/app/src/play/AndroidManifest.xml
- https://github.com/proninyaroslav/libretorrent/blob/master/app/src/main/java/org/proninyaroslav/libretorrent/ui/PermissionManager.java
- https://github.com/proninyaroslav/libretorrent/blob/master/app/src/main/java/org/proninyaroslav/libretorrent/core/system/SafFileSystem.java
- https://github.com/proninyaroslav/libretorrent/blob/master/app/src/main/java/org/proninyaroslav/libretorrent/core/system/SafFsModule.java
- https://github.com/proninyaroslav/libretorrent/blob/master/app/src/main/java/org/proninyaroslav/libretorrent/core/system/FsModuleResolverImpl.java
- https://github.com/proninyaroslav/libretorrent/blob/master/app/src/main/java/org/proninyaroslav/libretorrent/ui/filemanager/FileManagerFragment.java

---

## 5. 16KB 页对齐相关配置

**结论：LibreTorrent 仓库内不存在任何 16KB 页 / 原生打包相关配置。** 逐项证据：

- `app/build.gradle`（全文 213 行）无 `packaging` / `packagingOptions` 块、无 `jniLibs`、无 `useLegacyPackaging`、无 `extractNativeLibs`（对全文 grep 该组关键词：唯一命中是无关的 `implementation libs.viewpager2`）。
- 无 `externalNativeBuild`；仓库文件表里没有任何 `.c/.cpp/CMakeLists.txt`（app 自身不含原生代码，libtorrent 全部来自预编译 AAR）。
- `gradle.properties`（全文 6 行）：

```
android.dependency.useConstraints=true
android.nonTransitiveRClass=false
android.r8.strictFullModeForKeepRules=false
android.uniquePackageNames=false
android.useAndroidX=true
org.gradle.jvmargs=-Xmx2048m
```

  无任何页对齐/打包开关。
- `.gitlab-ci.yml` 只安装 SDK platform、platform-tools、build-tools，**不装 NDK**（NDK 由 AGP 按需下载或被 AAR 绕开）。
- 唯一与"原生打包"沾边的配置只有 §1.2 的 `ndk { abiFilters ... }` 与 `splits { abi { ... } }`。

**16KB 达标来源**：libtorrent4j 的预编译 AAR。`changelog.txt` → `2.1.0-39` 段：`update android NDK r29`（NDK r28+ 默认 16KB 页对齐），与任务背景中的已知事实一致。即：**使用方不需要为此写任何 Gradle 配置**，只需在打包后实测（本项目计划 V8 的 `zipalign -c -P 16 -v 4 <apk>` 验收项）。

- **未查证（范围外）**：F-Droid 的 fdroiddata 构建元数据是否追加了额外 flags——不在 LibreTorrent 仓库范围内，本次未查。

来源：https://github.com/proninyaroslav/libretorrent/blob/master/app/build.gradle ｜ https://github.com/proninyaroslav/libretorrent/blob/master/gradle.properties ｜ https://github.com/aldenml/libtorrent4j/blob/master/changelog.txt

---

## 6. libtorrent4j 符号核对表（javadoc 版本：**2.1.0-39**）

核对方法：
- 「签名」取自 javadoc.io 静态页面 `https://javadoc.io/static/org.libtorrent4j/libtorrent4j/2.1.0-39/org/libtorrent4j/<类>.html`（下文每条链接都带已核实的锚点，`id=` 已逐条在页面 HTML 中确认存在）。
- 「枚举数值」javadoc 不显示，取自 Maven Central 的源码包 `org.libtorrent4j:libtorrent4j:2.1.0-39` → `libtorrent4j-2.1.0-39-sources.jar`（`Priority.java` 等），并在条目中注明。
- 格式：`类.方法(参数)` ← 来源链接。

### 6.1 SessionManager

- `SessionManager.start(SessionParams params)` → void ← https://javadoc.io/static/org.libtorrent4j/libtorrent4j/2.1.0-39/org/libtorrent4j/SessionManager.html#start(org.libtorrent4j.SessionParams)
- `SessionManager.start()` → void ← …/SessionManager.html#start()
- `SessionManager.stop()` → void（javadoc 警告：会阻塞在 native session 销毁，"don't call this from the UI thread"） ← …/SessionManager.html#stop()
- `SessionManager.isRunning()` → boolean ← …/SessionManager.html#isRunning()
- `SessionManager.stats()` → **SessionStats** ← …/SessionManager.html#stats()
- `SessionManager.applySettings(SettingsPack sp)` → void ← …/SessionManager.html#applySettings(org.libtorrent4j.SettingsPack)
- `SessionManager.addListener(AlertListener listener)` → void ← …/SessionManager.html#addListener(org.libtorrent4j.AlertListener)
- 另（项目会用到的）：`removeListener(AlertListener)`、`settings()` → SettingsPack、`saveState()` → byte[]、`find(Sha1Hash)` → TorrentHandle、`download(String magnetUri, File saveDir, torrent_flags_t flags)`、`remove(TorrentHandle, remove_flags_t)`、`fetchMagnet(String, int, File)` → byte[] ← …/SessionManager.html

### 6.2 AddTorrentParams

- `static AddTorrentParams AddTorrentParams.parseMagnetUri(String uri)` ← https://javadoc.io/static/org.libtorrent4j/libtorrent4j/2.1.0-39/org/libtorrent4j/AddTorrentParams.html#parseMagnetUri(java.lang.String)
- `void AddTorrentParams.setSavePath(String value)` / `String getSavePath()` ← …/AddTorrentParams.html#setSavePath(java.lang.String)
- `void AddTorrentParams.setTrackers(List<String> value)` / `List<String> getTrackers()` ← …/AddTorrentParams.html#setTrackers(java.util.List)
- `void AddTorrentParams.peers(List<TcpEndpoint> value)` / `ArrayList<TcpEndpoint> peers()` ← …/AddTorrentParams.html#peers(java.util.List)
- `void AddTorrentParams.setFlags(torrent_flags_t flags)` / `torrent_flags_t getFlags()`（参数类型是 `org.libtorrent4j.swig.torrent_flags_t`） ← …/AddTorrentParams.html#setFlags(org.libtorrent4j.swig.torrent_flags_t)
- 另：`setTorrentInfo(TorrentInfo)`、`setInfoHashes(InfoHash)`、`filePriorities(Priority[])`、`setStorageMode(StorageMode)`、`static byte[] writeResumeDataBuf(AddTorrentParams)` ← …/AddTorrentParams.html

### 6.3 TorrentHandle（逐条核对）

- `void TorrentHandle.setPieceDeadline(int index, int deadline)` ← https://javadoc.io/static/org.libtorrent4j/libtorrent4j/2.1.0-39/org/libtorrent4j/TorrentHandle.html#setPieceDeadline(int,int)
- `void TorrentHandle.clearPieceDeadlines()` ← …/TorrentHandle.html#clearPieceDeadlines()
- `void TorrentHandle.resetPieceDeadline(int index)` ← …/TorrentHandle.html#resetPieceDeadline(int)
- `void TorrentHandle.prioritizeFiles(Priority[] priorities)` ← …/TorrentHandle.html#prioritizeFiles(org.libtorrent4j.Priority[])
- `boolean TorrentHandle.havePiece(int piece)` ← …/TorrentHandle.html#havePiece(int)
- `long[] TorrentHandle.fileProgress()` ← …/TorrentHandle.html#fileProgress()
- `TorrentInfo TorrentHandle.torrentFile()` ← …/TorrentHandle.html#torrentFile()
- `TorrentStatus TorrentHandle.status()` ← …/TorrentHandle.html#status()
- `TorrentStatus TorrentHandle.status(status_flags_t flags)`（要拿最新分块位图必须用这个重载） ← …/TorrentHandle.html#status(org.libtorrent4j.swig.status_flags_t)
- `void TorrentHandle.pause()` ← …/TorrentHandle.html#pause()
- `void TorrentHandle.resume()` ← …/TorrentHandle.html#resume()
- `void TorrentHandle.setFlags(torrent_flags_t flags)` ← …/TorrentHandle.html#setFlags(org.libtorrent4j.swig.torrent_flags_t)
- `void TorrentHandle.unsetFlags(torrent_flags_t flags)` ← …/TorrentHandle.html#unsetFlags(org.libtorrent4j.swig.torrent_flags_t)
- `void TorrentHandle.saveResumeData()` ← …/TorrentHandle.html#saveResumeData()
- `void TorrentHandle.forceRecheck()` ← …/TorrentHandle.html#forceRecheck()
- 另：`Priority filePriority(int)` / `void filePriority(int, Priority)`、`Priority[] filePriorities()`、`void setSequentialRange(int)` / `(int,int)`、`void moveStorage(String)`、`String savePath()`、`void setDownloadLimit(int)` / `setUploadLimit(int)`
- 状态位图开关常量：`TorrentHandle.QUERY_PIECES`（类型 `status_flags_t`，"includes `pieces`"） ← …/TorrentHandle.html#QUERY_PIECES；同类还有 `QUERY_VERIFIED_PIECES`/`QUERY_TORRENT_FILE`/`QUERY_SAVE_PATH`/`QUERY_NAME`/`QUERY_LAST_SEEN_COMPLETE`/`QUERY_DISTRIBUTED_COPIES`/`QUERY_ACCURATE_DOWNLOAD_COUNTERS`

### 6.4 TorrentInfo（元数据：块长 / 文件数 / 文件列表）

- `int TorrentInfo.pieceLength()`（块大小，字节） ← https://javadoc.io/static/org.libtorrent4j/libtorrent4j/2.1.0-39/org/libtorrent4j/TorrentInfo.html#pieceLength()
- `int TorrentInfo.numFiles()` ← …/TorrentInfo.html#numFiles()
- `FileStorage TorrentInfo.files()` ← …/TorrentInfo.html#files()（**注意**：文件列表不是 `List<TorrentFile>`，而是 `FileStorage` 对象；`TorrentFile` 在 2.1.0-39 的 core javadoc 里不存在）
- 另：`long totalSize()`、`long sizeOnDisk()`、`String name()`、`Sha1Hash infoHash()`、`InfoHash infoHashes()`、`boolean isValid()`、`int numPieces()`、`int pieceSize(int)`、`ArrayList<FileSlice> mapBlock(int,long,int)`、`PeerRequest mapFile(int,long,int)`、`static TorrentInfo bdecode(byte[])`

**每个文件的 path / size / offset / 首末 piece**（在 `FileStorage` 上）：

- `int FileStorage.numFiles()` ← https://javadoc.io/static/org.libtorrent4j/libtorrent4j/2.1.0-39/org/libtorrent4j/FileStorage.html#numFiles()
- `String FileStorage.filePath(int index)`（相对路径）与 `String filePath(int index, String savePath)`（拼接后完整路径） ← …/FileStorage.html#filePath(int) / …/FileStorage.html#filePath(int,java.lang.String)
- `String FileStorage.fileName(int index)` ← …/FileStorage.html#fileName(int)
- `long FileStorage.fileSize(int index)` ← …/FileStorage.html#fileSize(int)
- `long FileStorage.fileOffset(int index)` ← …/FileStorage.html#fileOffset(int)
- `int FileStorage.pieceIndexAtFile(int fileIndex)`（该文件**首** piece） ← …/FileStorage.html#pieceIndexAtFile(int)
- `int FileStorage.lastPieceIndexAtFile(int fileIndex)`（该文件**末** piece） ← …/FileStorage.html#lastPieceIndexAtFile(int)
- 反向映射：`int fileIndexAtPiece(int piece)`、`int lastFileIndexAtPiece(int piece)`、`int fileIndexAtOffset(long offset)` ← …/FileStorage.html
- 另：`boolean padFileAt(int)`、`file_flags_t fileFlags(int)`、`long totalSize()`、`int pieceLength()`、`int numPieces()`、`boolean isValid()`

### 6.5 TorrentStatus（分块位图）

- `PieceIndexBitfield TorrentStatus.pieces()` ← https://javadoc.io/static/org.libtorrent4j/libtorrent4j/2.1.0-39/org/libtorrent4j/TorrentStatus.html#pieces()
  javadoc 原文（关键）：

  > IMPORTANT: If you are not getting up to date information about pieces remember that PieceIndexBitfield data is considered augmented data for a torrentHandle.status() call, meaning, if you want to get the latest piece data, **you must use the TorrentHandle.QUERY_PIECES flag** when invoking `torrentHandle.status(TorrentHandle.QUERY_PIECES)`. Keep in mind this is an expensive call, therefore not part of the default flags. A bitmask that represents which pieces we have (set to true) and the pieces we don't have. It's a pointer and may be set to 0 if the torrent isn't downloading or seeding.

- `PieceIndexBitfield TorrentStatus.verifiedPieces()`（仅 seed mode 有效） ← …/TorrentStatus.html#verifiedPieces()
- 另：`int numPieces()`、`long totalDone()`、`long total()`、`long totalWanted()`/`totalWantedDone()`、`float progress()`、`int progressPpm()`、`TorrentStatus.State state()`、`boolean hasMetadata()`、`boolean isSeeding()`、`boolean isFinished()`、`int downloadRate()`、`int numPeers()`、`int blockSize()`、`torrent_flags_t flags()`

`PieceIndexBitfield` 的取值方法：

- `boolean PieceIndexBitfield.getBit(int index)` ← https://javadoc.io/static/org.libtorrent4j/libtorrent4j/2.1.0-39/org/libtorrent4j/PieceIndexBitfield.html#getBit(int)
- `void setBit(int)` / `void clearBit(int)` ← …/PieceIndexBitfield.html#setBit(int) / …/PieceIndexBitfield.html#clearBit(int)
- `int count()`（置 1 的位数）、`int size()`（比特数）、`boolean isEmpty()`、`boolean isAllSet()`、`boolean isNoneSet()`、`int findFirstSet()`、`int findLastClear()` ← …/PieceIndexBitfield.html

### 6.6 Priority 枚举常量（「不下载 = 0」与「最高优先级 = ?」）

javadoc：https://javadoc.io/static/org.libtorrent4j/libtorrent4j/2.1.0-39/org/libtorrent4j/Priority.html
数值来自源码包 `Priority.java`（Maven Central sources jar，`org.libtorrent4j.Priority`）：

```java
public enum Priority {
    /** piece or file is not downloaded at all */
    IGNORE(0),
    LOW(1),
    /** higher than normal priority. … */
    TWO(2),
    /** pieces are as likely to be picked as partial pieces. */
    THREE(3),
    /** pieces are preferred over partial pieces, but not over pieces with lower availability */
    DEFAULT(4),
    FIVE(5),
    SIX(6),
    /** maximum priority, availability is disregarded, … */
    TOP_PRIORITY(7);
```

| 语义 | 常量 | 值 |
|---|---|---|
| 不下载（ignore） | `Priority.IGNORE` | 0 ← https://javadoc.io/static/org.libtorrent4j/libtorrent4j/2.1.0-39/org/libtorrent4j/Priority.html#IGNORE |
| 低 | `Priority.LOW` | 1 |
| 中低 | `Priority.TWO` | 2 |
| 中 | `Priority.THREE` | 3 |
| 默认/普通 | `Priority.DEFAULT` | 4 ← …/Priority.html#DEFAULT |
| 偏高 | `Priority.FIVE` | 5 |
| 高 | `Priority.SIX` | 6 |
| **最高** | `Priority.TOP_PRIORITY` | **7** ← …/Priority.html#TOP_PRIORITY |

⚠️ **对任务书里「最高优先级=4」的直接更正（这是一个会踩的坑）**：在 libtorrent4j 中 **4 = `Priority.DEFAULT`（普通优先级）**，真正的"最高"是 **`Priority.TOP_PRIORITY`（7）**。所以从桌面 Python 侧照搬"优先级 4"的写法到 libtorrent4j 时要写 `Priority.TOP_PRIORITY`（或 `Priority.SIX`/`Priority.FIVE`，按意图），**直接写数值 4 会退化成默认优先级**，加急语义丢失。证据：`Priority` 的枚举定义与注释（sources jar）+ javadoc 页面。

另：`byte Priority.swig()` 取原生值、`static Priority fromSwig(int)` 反查、`static Priority[] Priority.array(Priority value, int size)` 批量建数组（对 `prioritizeFiles` 有用）。

### 6.7 TorrentFlags 常量（**不是 enum**，常量类型为 `org.libtorrent4j.swig.torrent_flags_t`）

javadoc：https://javadoc.io/static/org.libtorrent4j/libtorrent4j/2.1.0-39/org/libtorrent4j/TorrentFlags.html （每个常量形如 `public static final torrent_flags_t DISABLE_DHT = libtorrent.getDisable_dht();`，见源码包 `TorrentFlags.java`）

全部常量：`SEED_MODE`、`UPLOAD_MODE`、`SHARE_MODE`、`APPLY_IP_FILTER`、`PAUSED`、`AUTO_MANAGED`、`DUPLICATE_IS_ERROR`、`UPDATE_SUBSCRIBE`、`SUPER_SEEDING`、`SEQUENTIAL_DOWNLOAD`、`STOP_WHEN_READY`、`NEED_SAVE_RESUME`、`DISABLE_DHT`、`DISABLE_LSD`、`DISABLE_PEX`、`NO_VERIFY_FILES`、`DEFAULT_DONT_DOWNLOAD`、`I2P_TORRENT`、`ALL`。

本项目最相关三个：
- `TorrentFlags.DEFAULT_DONT_DOWNLOAD`（javadoc：Default all file priorities to dont_download → 加 magnet 时"只列清单不下载"的开关） ← …/TorrentFlags.html#DEFAULT_DONT_DOWNLOAD
- `TorrentFlags.SEQUENTIAL_DOWNLOAD` ← …/TorrentFlags.html#SEQUENTIAL_DOWNLOAD
- `TorrentFlags.AUTO_MANAGED` / `TorrentFlags.PAUSED` ← …/TorrentFlags.html#AUTO_MANAGED

### 6.8 SessionParams 与 SettingsPack：设置项方法名对照

**先记一个硬事实：2.1.0-39 的 `SessionParams` 只有 9 个成员**（javadoc 全量枚举）——构造器 `SessionParams()` / `SessionParams(SettingsPack)` / `SessionParams(session_params)` / `SessionParams(File)` / `SessionParams(byte[])`、`SettingsPack getSettings()`、`void setSettings(SettingsPack)`、`void setPosixDiskIO()`、`void setDefaultDiskIO()` ← https://javadoc.io/static/org.libtorrent4j/libtorrent4j/2.1.0-39/org/libtorrent4j/SessionParams.html
→ **没有** `enableDht` / `enableLsd` / `connectionsLimit` / `alertMask` 这类 setter；DHT/LSD/UPnP/NAT-PMP/连接数/alert 掩码/代理/限速**全部在 `SettingsPack` 上**，经 `new SessionParams(settingsPack)`（或 `setSettings(...)`）生效。

| 需求 | 2.1.0-39 的真实方法 | 备注 / 真实用例 |
|---|---|---|
| 启用 DHT | `void SettingsPack.setEnableDht(boolean value)`（读取用 `boolean isEnableDht()`）；低层等价 `setBoolean(settings_pack.bool_types.enable_dht.swigValue(), v)` | javadoc 专用方法；LibreTorrent：`sp.setEnableDht(settings.dhtEnabled && !proxyKillSwitch)` ← …/SettingsPack.html#setEnableDht(boolean) |
| 启用 LSD | `void SettingsPack.setEnableLsd(boolean value)` / `boolean isEnableLsd()` | LibreTorrent：`sp.setBoolean(settings_pack.bool_types.enable_lsd.swigValue(), ...)`（两种写法都真实存在） ← …/SettingsPack.html#setEnableLsd(boolean) |
| 禁用 UPnP | **无专用方法** → `SettingsPack.setBoolean(settings_pack.bool_types.enable_upnp.swigValue(), false)` | LibreTorrent 实测写法（TorrentSessionImpl.applySettings） |
| 禁用 NAT-PMP | **无专用方法** → `SettingsPack.setBoolean(settings_pack.bool_types.enable_natpmp.swigValue(), false)` | 同上 |
| 连接数上限 | `SettingsPack SettingsPack.connectionsLimit(int value)` / `int connectionsLimit()` | LibreTorrent：`sp.connectionsLimit(settings.connectionsLimit)` ← …/SettingsPack.html#connectionsLimit(int) |
| alert 掩码 | **无专用方法** → `SettingsPack.setInteger(settings_pack.int_types.alert_mask.swigValue(), mask)` | LibreTorrent：`sp.setInteger(settings_pack.int_types.alert_mask.swigValue(), getAlertMask(settings).to_int())`；libtorrent4j 自身 `SessionManager.java:135` 也这么写 ← …/SettingsPack.html#setInteger(int,int) |
| 代理类型 | `setInteger(settings_pack.int_types.proxy_type.swigValue(), v)`，v 取 `settings_pack.proxy_type_t.{none,socks4,socks5,socks5_pw,http,http_pw}.swigValue()` | LibreTorrent `convertProxyType()`（见下） |
| 代理主机 | `setString(settings_pack.string_types.proxy_hostname.swigValue(), host)` ← …/SettingsPack.html#setString(int,java.lang.String) | swig 常量名已核实：`proxy_hostname`（swig/settings_pack.java:156） |
| 代理端口 | `setInteger(settings_pack.int_types.proxy_port.swigValue(), port)` | |
| 代理用户名 | `setString(settings_pack.string_types.proxy_username.swigValue(), user)` | |
| 代理密码 | `setString(settings_pack.string_types.proxy_password.swigValue(), pass)` | |
| peer 走代理 | `setBoolean(settings_pack.bool_types.proxy_peer_connections.swigValue(), true)`（tracker 侧另有 `proxy_tracker_connections`，resolver 侧 `proxy_hostnames`） | LibreTorrent 三个都设 |
| 下载限速 | `SettingsPack SettingsPack.downloadRateLimit(int value)`（单位 **bytes/s**，0 = 不限） | LibreTorrent：`sp.downloadRateLimit(settings.downloadRateLimit)` ← …/SettingsPack.html#downloadRateLimit(int)；上传对应 `uploadRateLimit(int)` |

LibreTorrent 的代理映射原文（`core/model/session/TorrentSessionImpl.java`，第 1218–1233、1271–1281 行）：

```java
    private void applyProxy(SessionSettings settings, SettingsPack sp) {
        int proxyType = convertProxyType(settings.proxyType, settings.proxyRequiresAuth);
        sp.setInteger(settings_pack.int_types.proxy_type.swigValue(), proxyType);
        if (settings.proxyType != SessionSettings.ProxyType.NONE) {
            sp.setInteger(settings_pack.int_types.proxy_port.swigValue(), settings.proxyPort);
            sp.setString(settings_pack.string_types.proxy_hostname.swigValue(), settings.proxyAddress);
            if (settings.proxyRequiresAuth) {
                sp.setString(settings_pack.string_types.proxy_username.swigValue(), settings.proxyLogin);
                sp.setString(settings_pack.string_types.proxy_password.swigValue(), settings.proxyPassword);
            }
            sp.setBoolean(settings_pack.bool_types.proxy_peer_connections.swigValue(),
                    settings.proxyPeersToo || settings.proxyRequireAllConnections);
            sp.setBoolean(settings_pack.bool_types.proxy_tracker_connections.swigValue(), true);
            sp.setBoolean(settings_pack.bool_types.proxy_hostnames.swigValue(), true);
        }
    }

    private int convertProxyType(SessionSettings.ProxyType mode, boolean authRequired) {
        return switch (mode) {
            case SOCKS4 -> settings_pack.proxy_type_t.socks4.swigValue();
            case SOCKS5 -> (authRequired ? settings_pack.proxy_type_t.socks5_pw.swigValue()
                                         : settings_pack.proxy_type_t.socks5.swigValue());
            case HTTP   -> (authRequired ? settings_pack.proxy_type_t.http_pw.swigValue()
                                         : settings_pack.proxy_type_t.http.swigValue());
            default     -> settings_pack.proxy_type_t.none.swigValue();
        };
    }
```

其余可直接复用的 `SettingsPack` 专用方法（javadoc 已核实）：`activeDownloads(int)`、`activeSeeds(int)`、`activeChecking(int)`、`activeLimit(int)`、`maxPeerlistSize(int)`、`tickInterval(int)`、`inactivityTimeout(int)`、`seedingOutgoingConnections(boolean)`、`anonymousMode(boolean)`、`setMaxMetadataSize(int)`（magnet 元数据上限）、`setDhtBootstrapNodes(String)`、`listenInterfaces(String)`、`alertQueueSize(int)`、`stopTrackerTimeout(int)`、`static SettingsPack SettingsPack.defaultSettings()`、以及通用入口 `setBoolean/setInteger/setString(int name, …)`、`clear()`、`hasValue(int)` ← https://javadoc.io/static/org.libtorrent4j/libtorrent4j/2.1.0-39/org/libtorrent4j/SettingsPack.html

**提示（推断，非 LibreTorrent 事实）**：本项目桌面设置键 `download_rate_limit` 的单位是 KB/s，而 `SettingsPack.downloadRateLimit(int)` 的单位是 bytes/s（javadoc 原文 "in bytes per second"）——移植时需要 ×1024 换算。

来源（本节）：
- javadoc 各页（链接已逐条内联）
- 源码包（枚举值/常量定义）：`https://repo1.maven.org/maven2/org/libtorrent4j/libtorrent4j/2.1.0-39/libtorrent4j-2.1.0-39-sources.jar`
- LibreTorrent `TorrentSessionImpl.java`：https://github.com/proninyaroslav/libretorrent/blob/master/app/src/main/java/org/proninyaroslav/libretorrent/core/model/session/TorrentSessionImpl.java

---

## 7. 对本项目的直接结论（只列差异与动作项）

1. **minSdk**：LibreTorrent 是 24（因此停在 libtorrent4j -38）；本项目 `minSdk=28` + **-39** 的组合没有矛盾（-39 changelog 明写 API ≥ 28）。
2. **依赖声明可照抄形态**：core + 4 个 `libtorrent4j-android-<abi>` 全量声明；但本项目 release 只出 arm64（按计划），debug 保留 arm64 + x86_64 —— 这是本项目自己的取舍，LibreTorrent 是四 ABI 全量 + `splits.abi` + universalApk。
3. **16KB**：无需任何 Gradle 配置（LibreTorrent 也没有）；达标来自 AAR（NDK r29），验收靠 `zipalign -c -P 16`。
4. **前台服务**：Android 14+ 用 `specialUse`（+ `PROPERTY_SPECIAL_USE_FGS_SUBTYPE` 说明）可以绕开 dataSync 的 6 小时累计上限；若本项目坚持 dataSync，就必须实现 `Service.onTimeout()`。两条路都可见先例/官方约束。
5. **存储**：LibreTorrent 走"公共目录 + MANAGE_EXTERNAL_STORAGE（Play 版去掉→SAF）"，**没有**应用专属目录方案；本项目计划走"应用专属外部目录（免权限）+ 导出"，属于更保守的独立选择，无需跟随 LibreTorrent。
6. **API 写法风险点**（务必按 §6 写）：
   - 拿分块位图必须 `handle.status(TorrentHandle.QUERY_PIECES)`，否则是缓存值；
   - 设置项全部在 `SettingsPack`（`SessionParams` 只是个壳）；
   - UPnP/NAT-PMP/alert_mask/代理 没有专用 setter，用 `setBoolean/setInteger/setString(settings_pack.*.swigValue(), v)`；
   - 加急优先级在 libtorrent4j 里是 `Priority.TOP_PRIORITY`（7），不是 4（4 = DEFAULT）。

## 附：本报告的 5 问查证计数

- **5 个问题均已给出带【文件路径 + 原始片段/数值 + 来源链接】的结论：已查证 5 / 未查证 0。**
- 另有 2 处**子项级「未查证」**（已在正文标出）：
  1. §4.5 — SAF 目录树授权（`ACTION_OPEN_DOCUMENT_TREE`）的具体启动点（已查 5 个候选文件，未命中）；
  2. §5 — F-Droid fdroiddata 是否追加 16KB 相关构建 flag（范围外，不在 LibreTorrent 仓库内）。
