import java.util.Properties

plugins {
  alias(libs.plugins.android.application)
  alias(libs.plugins.compose.compiler)
  alias(libs.plugins.kotlin.serialization)
}

// ---- 发布签名（阶段 7）------------------------------------------------------
// 凭据读自 app/keystore.properties（不入库；模板见 app/keystore.properties.example），
// keystore 本体放在 android/ 下、同样不入库。storeFile 相对**本模块**（android/app/）解析。
// 文件缺失时（干净的 CI checkout / 新机器）不注册签名配置：debug 不受影响，
// release 退化成未签名包，而不是让整个构建直接红。
val keystorePropertiesFile = file("keystore.properties")
val keystoreProperties = Properties().apply {
    if (keystorePropertiesFile.exists()) {
        keystorePropertiesFile.inputStream().use { load(it) }
    }
}
val hasReleaseSigning = keystorePropertiesFile.exists() &&
    !keystoreProperties.getProperty("storeFile").isNullOrBlank()

// ---- NDK：release 剥符号（阶段 7）--------------------------------------------
// 本机装了 NDK r29（见 android/ENV.md）：显式声明后 AGP 才会用它的 llvm-strip 剥 .so
// （否则每个包都告警 "Unable to strip … packaging them as they are"）。
// 实测收益比预期小得多：libtorrent4j 的预编译 .so 本就接近全剥，llvm-strip 只再去掉
// 约 41 KB/ABI（release 单 ABI：46,944,470 → 46,902,334 字节）。真正的体积大头是
// ABI 收窄与 R8（见下）。所以这条的意义是"消除告警 + 让流程与发布实践一致"。
// 注意是**有这份 NDK 时才声明**：CI / 干净 checkout 上没装 NDK，硬声明会把构建直接
// 打红；那种环境下只告警、产物偏大一点，不影响正确性。
val preferredNdkVersion = "29.0.14206865"
val sdkDirFromLocalProperties: String? = rootProject.file("local.properties")
    .takeIf { it.isFile }
    ?.let { f -> Properties().apply { f.inputStream().use { load(it) } }.getProperty("sdk.dir") }
val detectedSdkDir: File? = sequenceOf(
    sdkDirFromLocalProperties,
    System.getenv("ANDROID_HOME"),
    System.getenv("ANDROID_SDK_ROOT"),
).filterNotNull().map { File(it) }.firstOrNull { it.isDirectory }
val hasPreferredNdk = detectedSdkDir?.resolve("ndk/$preferredNdkVersion")?.isDirectory == true

android {
    if (hasPreferredNdk) {
        ndkVersion = preferredNdkVersion
    }
    namespace = "com.bitseed.magnetviewer"
    compileSdk = 36
    defaultConfig {
        applicationId = "com.bitseed.magnetviewer"
        minSdk = 28
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        // ABI 按构建类型给（见 buildTypes）：真机 arm64 + 模拟器 x86_64（debug），
        // release 只出 arm64-v8a。放在 buildType 里是因为 defaultConfig 的 abiFilters
        // 与 buildType 的是**取并集**的，写在 defaultConfig 里就没法在 release 收窄。
    }

    signingConfigs {
        if (hasReleaseSigning) {
            create("release") {
                storeFile = file(keystoreProperties.getProperty("storeFile"))
                storePassword = keystoreProperties.getProperty("storePassword")
                keyAlias = keystoreProperties.getProperty("keyAlias")
                keyPassword = keystoreProperties.getProperty("keyPassword")
            }
        }
    }

    buildTypes {
        debug {
            // 模拟器（x86_64）与真机（arm64-v8a）都要能装
            ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
        }
        release {
            isMinifyEnabled = true        // R8：混淆 + 摇树 + 去符号
            isShrinkResources = true      // 资源收缩（依赖 minify）
            // 只出 arm64-v8a：少打包一份 ~16MB 的 .so（模拟器靠 libndk_translation 跑 arm64）
            ndk { abiFilters += listOf("arm64-v8a") }
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
            signingConfig = signingConfigs.findByName("release")
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures {
      compose = true
      aidl = false
      buildConfig = false
      shaders = false
    }

    packaging {
      resources {
        excludes += "/META-INF/{AL2.0,LGPL2.1}"
      }
    }
}

kotlin {
    jvmToolchain(17)
}

dependencies {
  val composeBom = platform(libs.androidx.compose.bom)
  implementation(composeBom)
  androidTestImplementation(composeBom)

  // Core Android dependencies
  implementation(libs.androidx.core.ktx)
  implementation(libs.androidx.lifecycle.runtime.ktx)
  implementation(libs.androidx.activity.compose)

  // Arch Components
  implementation(libs.androidx.lifecycle.runtime.compose)
  implementation(libs.androidx.lifecycle.viewmodel.compose)

  // Compose
  implementation(libs.androidx.compose.ui)
  implementation(libs.androidx.compose.ui.tooling.preview)
  implementation(libs.androidx.compose.material3)
  // Tooling
  debugImplementation(libs.androidx.compose.ui.tooling)
  // Instrumented tests
  androidTestImplementation(libs.androidx.compose.ui.test.junit4)
  debugImplementation(libs.androidx.compose.ui.test.manifest)

  // Local tests: jUnit, coroutines, Android runner
  testImplementation(libs.junit)
  testImplementation(libs.kotlinx.coroutines.test)

  // Instrumented tests: jUnit rules and runners
  androidTestImplementation(libs.androidx.test.core)
  androidTestImplementation(libs.androidx.test.ext.junit)
  androidTestImplementation(libs.androidx.test.runner)
  androidTestImplementation(libs.androidx.test.espresso.core)

  // Navigation
  implementation(libs.androidx.navigation3.ui)
  implementation(libs.androidx.navigation3.runtime)
  implementation(libs.androidx.lifecycle.viewmodel.navigation3)

  // BT 引擎（libtorrent4j）+ 播放器（Media3）
  implementation(libs.libtorrent4j.core)
  implementation(libs.libtorrent4j.android.arm64)
  implementation(libs.libtorrent4j.android.x86)
  implementation(libs.media3.exoplayer)
  implementation(libs.media3.ui)

  // 对拍夹具解析（JVM 单测）
  testImplementation(libs.kotlinx.serialization.json)

  // 设置持久化（DataStore Preferences；键名与桌面 core/config.py 一致）
  implementation(libs.androidx.datastore.preferences)
}
