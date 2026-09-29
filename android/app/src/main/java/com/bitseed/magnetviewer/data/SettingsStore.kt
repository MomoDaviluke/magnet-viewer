package com.bitseed.magnetviewer.data

import android.content.Context
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.MutablePreferences
import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import com.bitseed.magnetviewer.core.PREVIEW_CACHE_CONVERT
import com.bitseed.magnetviewer.core.normalizePreviewCacheMode
import com.bitseed.magnetviewer.core.states.METADATA_TIMEOUT_SECONDS
import com.bitseed.magnetviewer.engine.PROXY_DEFAULT_PORT
import com.bitseed.magnetviewer.engine.ProxyConfig
import java.io.File
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

/**
 * 应用设置（**键名与桌面 `core/config.py: DEFAULTS` 一一对应**，DataStore Preferences 持久化）。
 *
 * 本文件是「设置项 → 实际生效路径」的唯一入口：
 * ```
 * AppSettings ─ toProxyConfig() ─→ BtEngine.applyProxy(ProxyConfig)
 *            ├ downloadRateLimit ─→ BtEngine.applyRateLimit(kbps)
 *            ├ metadataTimeout ──→ BtEngine.applyMetadataTimeout(seconds)
 *            ├ previewCacheMode ─→ BtEngine.setPreviewCacheMode(mode)
 *            ├ portMapping ──────→ BtEngine.applyPortMapping(enabled)      （热生效）
 *            ├ extraTrackers ────→ BtEngine.applyExtraTrackers(enabled)    （下次加种生效）
 *            ├ uiTheme ──────────→ isDarkTheme() → MagnetViewerTheme（即时换肤）
 *            └ （cacheLimitMb / loggingEnabled 见下方注释：Android 侧只持久化，见「未接线」说明）
 * ```
 */

// ---------------------------------------------------------------- 值域常量（桌面 DEFAULTS 同款）

const val PROXY_TYPE_NONE = "none"
const val PROXY_TYPE_SOCKS5 = "socks5"
const val PROXY_TYPE_HTTP = "http"

/** 代理类型值域（桌面：`none | socks5 | http`）。 */
val PROXY_TYPES: List<String> = listOf(PROXY_TYPE_NONE, PROXY_TYPE_SOCKS5, PROXY_TYPE_HTTP)

const val THEME_LIGHT = "light"
const val THEME_DARK = "dark"
const val THEME_SYSTEM = "system"

/** 主题模式值域（桌面：`light（默认）| dark | system`）。 */
val THEME_MODES: List<String> = listOf(THEME_LIGHT, THEME_DARK, THEME_SYSTEM)

/** 预览缓存上限默认值（MB；桌面 `cache_limit_mb = 2048`，0 = 不限）。 */
const val CACHE_LIMIT_MB_DEFAULT: Int = 2048

/** 键名（**与桌面 QSettings 键同名**：`core/config.py: DEFAULTS`）。 */
object SettingsKeys {
  const val PROXY_TYPE = "proxy_type"
  const val PROXY_HOST = "proxy_host"
  const val PROXY_PORT = "proxy_port"
  const val PROXY_USER = "proxy_user"
  const val PROXY_PASS = "proxy_pass"
  const val PROXY_PEER = "proxy_peer"
  const val DOWNLOAD_RATE_LIMIT = "download_rate_limit"
  const val METADATA_TIMEOUT = "metadata_timeout"
  const val CACHE_LIMIT_MB = "cache_limit_mb"
  const val PREVIEW_CACHE_MODE = "preview_cache_mode"
  const val UI_THEME = "ui_theme"
  const val LOGGING_ENABLED = "logging_enabled"

  /** 入站能力（诊断 D-2026-09-18，安卓侧同步桌面）：UPnP/NAT-PMP 端口映射。 */
  const val PORT_MAPPING = "port_mapping"

  /** peer 发现（诊断 D-2026-09-18，安卓侧同步桌面）：追加 HTTP/HTTPS 兜底 tracker。 */
  const val EXTRA_TRACKERS = "extra_trackers"

  /** 全部键名（顺序 = 桌面 DEFAULTS 顺序），供单测逐键断言。 */
  val ALL: List<String> =
    listOf(
      PROXY_TYPE,
      PROXY_HOST,
      PROXY_PORT,
      PROXY_USER,
      PROXY_PASS,
      PROXY_PEER,
      DOWNLOAD_RATE_LIMIT,
      METADATA_TIMEOUT,
      CACHE_LIMIT_MB,
      PREVIEW_CACHE_MODE,
      UI_THEME,
      LOGGING_ENABLED,
      PORT_MAPPING,
      EXTRA_TRACKERS,
    )
}

// 用户可见文案（设置页下拉）
const val PROXY_TYPE_NONE_LABEL = "不使用代理"
const val PROXY_TYPE_SOCKS5_LABEL = "SOCKS5"
const val PROXY_TYPE_HTTP_LABEL = "HTTP"
const val THEME_LIGHT_LABEL = "浅色"
const val THEME_DARK_LABEL = "深色"
const val THEME_SYSTEM_LABEL = "跟随系统"
const val CACHE_MODE_CONVERT_LABEL = "convert（转正继续缓存）"
const val CACHE_MODE_HOLD_LABEL = "hold（冻结暂停）"

// ---------------------------------------------------------------- 值域处理（纯函数，JVM 可测）

/** 代理类型归一化：值域之外回落 `none`（桌面 `lt_proxy_settings` 的直连分支同款）。 */
fun normalizeProxyType(v: String?): String {
  val t = v?.trim()?.lowercase().orEmpty()
  return if (t in PROXY_TYPES) t else PROXY_TYPE_NONE
}

/** 主题模式归一化：值域之外回落 `light`（桌面默认浅色）。 */
fun normalizeThemeMode(v: String?): String {
  val t = v?.trim()?.lowercase().orEmpty()
  return if (t in THEME_MODES) t else THEME_LIGHT
}

/**
 * 下载限速值域：**负数按 0（不限）处理**（与 `BtEngine.applyRateLimit` 的 `maxOf(0, kbps)`
 * 同一条规则；0 = 不限是桌面 `download_rate_limit` 的约定）。
 */
fun sanitizeRateLimitKbps(v: Int): Int = if (v < 0) 0 else v

/** 代理端口值域：`<= 0` 回落 [PROXY_DEFAULT_PORT]（1080，桌面 `int(p.get("port") or 1080)`）。 */
fun sanitizeProxyPort(v: Int): Int = if (v <= 0) PROXY_DEFAULT_PORT else v

/** 元数据超时值域：`<= 0` 视为无效输入 → 回落默认 [METADATA_TIMEOUT_SECONDS]（桌面 `if metadata_timeout:` 真值判断同款）。 */
fun sanitizeMetadataTimeout(seconds: Int): Int =
  if (seconds <= 0) METADATA_TIMEOUT_SECONDS else seconds

/** 预览缓存上限值域：负数按 0（不限）处理（桌面 `cache_limit_mb`，0 = 不限）。 */
fun sanitizeCacheLimitMb(v: Int): Int = if (v < 0) 0 else v

/**
 * 主题模式 + 系统是否深色 → 是否用深色配色（纯函数；`light` 恒定浅色，`dark` 恒定深色，
 * `system` 跟随 `isSystemInDarkTheme()`）。
 */
fun isDarkTheme(themeMode: String?, systemDark: Boolean): Boolean =
  when (normalizeThemeMode(themeMode)) {
    THEME_DARK -> true
    THEME_SYSTEM -> systemDark
    else -> false
  }

// ---------------------------------------------------------------- 设置模型

/**
 * 一份设置快照。默认值 = 桌面 `core/config.py: DEFAULTS`（本 demo 只取安卓侧真正用到的那几个键）。
 *
 * 执行点（每一项都有接线，见各字段注释）：
 * - `cacheLimitMb` → `MagnetViewModel.enforceCacheLimit`（逐次设置变更 + 启动恢复后）；
 * - `loggingEnabled` → `AppLog.syncFromSettings`（**三处日志出口的总开关**，修 I-02）；
 * - 其余项 → `MagnetViewModel.applySettings`（代理/限速/超时/缓存模式热更新，
 *   以及 `portMapping` → `BtEngine.applyPortMapping`、
 *   `extraTrackers` → `BtEngine.applyExtraTrackers`）。
 */
data class AppSettings(
  val proxyType: String = PROXY_TYPE_NONE,
  val proxyHost: String = "",
  val proxyPort: Int = PROXY_DEFAULT_PORT,
  val proxyUser: String = "",
  val proxyPass: String = "",
  val proxyPeer: Boolean = true,
  val downloadRateLimit: Int = 0,
  val metadataTimeout: Int = METADATA_TIMEOUT_SECONDS,
  val cacheLimitMb: Int = CACHE_LIMIT_MB_DEFAULT,
  val previewCacheMode: String = PREVIEW_CACHE_CONVERT,
  val uiTheme: String = THEME_LIGHT,
  val loggingEnabled: Boolean = true,
  /**
   * UPnP/NAT-PMP 端口映射（诊断 D-2026-09-18）。默认 false —— 与桌面
   * `DEFAULTS["port_mapping"]` 一致：本应用默认只「收」不做种，端口映射徒增暴露面。
   * 关闭时会连不上同为 NAT 后的 peer（swarm 里占比很高），做种时也无人能连入。
   * 保存设置即热生效（`BtEngine.applyPortMapping`）。
   */
  val portMapping: Boolean = false,
  /**
   * 追加 HTTP/HTTPS 兜底 tracker（诊断 D-2026-09-18）。默认 false —— 与桌面
   * `DEFAULTS["extra_trackers"]` 一致：默认路径仍是既有 5 条 udp。
   * 开启条件：UDP 出站受阻（实测公共 DHT 引导节点 5 个仅 1 个响应），
   * 且磁力链不带 `tr=` 时 peer 发现只剩 DHT 一条路。
   * **保存即生效**：加种路径注入 5/10 条，已在跑的任务由
   * `BtEngine.applyExtraTrackers` 直接追加缺失条目（只做加法，不必重新添加任务）。
   */
  val extraTrackers: Boolean = false,
) {

  /** 值域归一（可反复调用，幂等）。 */
  fun sanitized(): AppSettings =
    copy(
      proxyType = normalizeProxyType(proxyType),
      proxyHost = proxyHost.trim(),
      proxyPort = sanitizeProxyPort(proxyPort),
      proxyUser = proxyUser.trim(),
      downloadRateLimit = sanitizeRateLimitKbps(downloadRateLimit),
      metadataTimeout = sanitizeMetadataTimeout(metadataTimeout),
      cacheLimitMb = sanitizeCacheLimitMb(cacheLimitMb),
      previewCacheMode = normalizePreviewCacheMode(previewCacheMode) ?: PREVIEW_CACHE_CONVERT,
      uiTheme = normalizeThemeMode(uiTheme),
    )

  /**
   * 设置 → 引擎代理配置（**唯一的映射点**，字段名/顺序与 [ProxyConfig] 冻结签名一致）。
   * 未启用代理时 host 依旧原样带过去——`proxySettingsFor` 负责回落直连（三条一起）。
   */
  fun toProxyConfig(): ProxyConfig =
    ProxyConfig(
      type = normalizeProxyType(proxyType),
      host = proxyHost.trim(),
      port = sanitizeProxyPort(proxyPort),
      user = proxyUser.trim(),
      pass = proxyPass,
      peer = proxyPeer,
    )

  /** 「是否真的启用代理」：类型在值域内且主机非空（与引擎的直连回落判据一致）。 */
  fun proxyEnabled(): Boolean =
    normalizeProxyType(proxyType) != PROXY_TYPE_NONE && proxyHost.trim().isNotEmpty()

  companion object {
    /** 默认设置（桌面 DEFAULTS）。 */
    fun default(): AppSettings = AppSettings()

    /** DataStore 快照 → 设置（缺键一律取默认值）。 */
    fun from(p: Preferences): AppSettings {
      val d = AppSettings()
      return AppSettings(
        proxyType = normalizeProxyType(p[stringPreferencesKey(SettingsKeys.PROXY_TYPE)] ?: d.proxyType),
        proxyHost = p[stringPreferencesKey(SettingsKeys.PROXY_HOST)] ?: d.proxyHost,
        proxyPort = p[intPreferencesKey(SettingsKeys.PROXY_PORT)] ?: d.proxyPort,
        proxyUser = p[stringPreferencesKey(SettingsKeys.PROXY_USER)] ?: d.proxyUser,
        proxyPass = p[stringPreferencesKey(SettingsKeys.PROXY_PASS)] ?: d.proxyPass,
        proxyPeer = p[booleanPreferencesKey(SettingsKeys.PROXY_PEER)] ?: d.proxyPeer,
        downloadRateLimit = p[intPreferencesKey(SettingsKeys.DOWNLOAD_RATE_LIMIT)] ?: d.downloadRateLimit,
        metadataTimeout = p[intPreferencesKey(SettingsKeys.METADATA_TIMEOUT)] ?: d.metadataTimeout,
        cacheLimitMb = p[intPreferencesKey(SettingsKeys.CACHE_LIMIT_MB)] ?: d.cacheLimitMb,
        previewCacheMode =
          normalizePreviewCacheMode(p[stringPreferencesKey(SettingsKeys.PREVIEW_CACHE_MODE)])
            ?: d.previewCacheMode,
        uiTheme = normalizeThemeMode(p[stringPreferencesKey(SettingsKeys.UI_THEME)] ?: d.uiTheme),
        loggingEnabled = p[booleanPreferencesKey(SettingsKeys.LOGGING_ENABLED)] ?: d.loggingEnabled,
        portMapping = p[booleanPreferencesKey(SettingsKeys.PORT_MAPPING)] ?: d.portMapping,
        extraTrackers = p[booleanPreferencesKey(SettingsKeys.EXTRA_TRACKERS)] ?: d.extraTrackers,
      )
    }
  }
}

/** 设置 → Preferences（**纯函数**：写盘与单测共用同一条路径）。 */
fun writeSettings(p: MutablePreferences, s: AppSettings): MutablePreferences {
  val v = s.sanitized()
  p[stringPreferencesKey(SettingsKeys.PROXY_TYPE)] = v.proxyType
  p[stringPreferencesKey(SettingsKeys.PROXY_HOST)] = v.proxyHost
  p[intPreferencesKey(SettingsKeys.PROXY_PORT)] = v.proxyPort
  p[stringPreferencesKey(SettingsKeys.PROXY_USER)] = v.proxyUser
  p[stringPreferencesKey(SettingsKeys.PROXY_PASS)] = v.proxyPass
  p[booleanPreferencesKey(SettingsKeys.PROXY_PEER)] = v.proxyPeer
  p[intPreferencesKey(SettingsKeys.DOWNLOAD_RATE_LIMIT)] = v.downloadRateLimit
  p[intPreferencesKey(SettingsKeys.METADATA_TIMEOUT)] = v.metadataTimeout
  p[intPreferencesKey(SettingsKeys.CACHE_LIMIT_MB)] = v.cacheLimitMb
  p[stringPreferencesKey(SettingsKeys.PREVIEW_CACHE_MODE)] = v.previewCacheMode
  p[stringPreferencesKey(SettingsKeys.UI_THEME)] = v.uiTheme
  p[booleanPreferencesKey(SettingsKeys.LOGGING_ENABLED)] = v.loggingEnabled
  p[booleanPreferencesKey(SettingsKeys.PORT_MAPPING)] = v.portMapping
  p[booleanPreferencesKey(SettingsKeys.EXTRA_TRACKERS)] = v.extraTrackers
  return p
}

// ---------------------------------------------------------------- DataStore 封装

/**
 * 设置的持久化层（DataStore Preferences）。
 *
 * 落盘位置：`context.filesDir/settings.preferences_pb`（应用内部存储，与预览缓存同一根）。
 * 读：[settings]（Flow，UI 订阅 → 保存即生效）；写：[save]（幂等、值域归一）。
 */
class SettingsStore(private val dataStore: DataStore<Preferences>) {

  /** 设置流：任意一次保存都会推给所有订阅者（UI 从这里做「保存即生效」）。 */
  val settings: Flow<AppSettings> = dataStore.data.map { AppSettings.from(it) }

  /** 当前值（挂起一次读取）。 */
  suspend fun load(): AppSettings = settings.first()

  /** 写入（先归一值域，再落盘）。 */
  suspend fun save(s: AppSettings) {
    dataStore.edit { prefs -> writeSettings(prefs, s) }
  }

  companion object {
    const val FILE_NAME = "settings.preferences_pb"

    /** 进程内单例（DataStore 不允许同一文件多实例：配置变化/多入口都要复用同一个）。 */
    @Volatile private var singleton: SettingsStore? = null

    fun instance(context: Context): SettingsStore =
      singleton
        ?: synchronized(this) {
          singleton ?: create(context.applicationContext).also { singleton = it }
        }

    /**
     * 构造 App 用的实例（进程内单例：DataStore 不允许同一文件多实例）。
     * 文件路径与桌面 QSettings 的「一个用户一份设置」语义对应，只是落在应用私有目录。
     */
    fun create(context: Context): SettingsStore =
      SettingsStore(
        PreferenceDataStoreFactory.create(produceFile = { File(context.filesDir, FILE_NAME) })
      )
  }
}
