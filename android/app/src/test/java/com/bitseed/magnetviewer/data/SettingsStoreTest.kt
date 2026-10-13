package com.bitseed.magnetviewer.data

import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import androidx.datastore.preferences.core.mutablePreferencesOf
import androidx.datastore.preferences.core.emptyPreferences
import com.bitseed.magnetviewer.core.PREVIEW_CACHE_CONVERT
import com.bitseed.magnetviewer.core.PREVIEW_CACHE_HOLD
import com.bitseed.magnetviewer.core.states.METADATA_TIMEOUT_SECONDS
import com.bitseed.magnetviewer.engine.LtProxyType
import com.bitseed.magnetviewer.engine.PROXY_DEFAULT_PORT
import com.bitseed.magnetviewer.engine.proxySettingsFor
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 设置层单测（JVM，不需要设备）：
 * 1. 默认值 = 桌面 `core/config.py: DEFAULTS`；
 * 2. 读写往返（纯 Preferences 往返 + **真落盘的 DataStore 往返**，含「换一个实例再读」= 重启后仍在）；
 * 3. 设置 → `ProxyConfig` 映射（再喂给引擎的纯函数 `proxySettingsFor`，逐档断言）；
 * 4. 主题模式解析（light/dark/system + 非法值回落）；
 * 5. 限速值域（负值 / 0 / 正常值）。
 */
class SettingsStoreTest {

  // ------------------------------------------------------------ 默认值

  @Test
  fun defaults_matchDesktop() {
    val d = AppSettings()
    assertEquals(PROXY_TYPE_NONE, d.proxyType)
    assertEquals("", d.proxyHost)
    assertEquals(PROXY_DEFAULT_PORT, d.proxyPort)
    assertEquals("", d.proxyUser)
    assertEquals("", d.proxyPass)
    assertTrue("桌面 DEFAULTS: proxy_peer = True", d.proxyPeer)
    assertEquals(0, d.downloadRateLimit)
    assertEquals(METADATA_TIMEOUT_SECONDS, d.metadataTimeout)
    assertEquals(90, d.metadataTimeout)
    assertEquals(CACHE_LIMIT_MB_DEFAULT, d.cacheLimitMb)
    assertEquals(2048, d.cacheLimitMb)
    assertEquals(PREVIEW_CACHE_CONVERT, d.previewCacheMode)
    assertEquals(THEME_LIGHT, d.uiTheme)
    assertTrue(d.loggingEnabled)
    // 诊断 D-2026-09-18 + 2026-10-13 性能实测裁决：
    // （port_mapping=True 开启入站可达——关闭时连不上 NAT 后的 peer，实测是下载
    //   速率第一瓶颈；extra_trackers=False 保持既有 5 条 udp 的行为与相关断言不变）。
    assertTrue("桌面 DEFAULTS: port_mapping = True", d.portMapping)
    assertFalse("桌面 DEFAULTS: extra_trackers = False", d.extraTrackers)
  }

  @Test
  fun keyNames_matchDesktop() {
    assertEquals(
      listOf(
        "proxy_type",
        "proxy_host",
        "proxy_port",
        "proxy_user",
        "proxy_pass",
        "proxy_peer",
        "download_rate_limit",
        "metadata_timeout",
        "cache_limit_mb",
        "preview_cache_mode",
        "ui_theme",
        "logging_enabled",
        "port_mapping",
        "extra_trackers",
      ),
      SettingsKeys.ALL,
    )
  }

  @Test
  fun emptyPreferences_yieldDefaults() {
    assertEquals(AppSettings(), AppSettings.from(emptyPreferences()))
  }

  // ------------------------------------------------------------ 往返（纯 Preferences）

  @Test
  fun roundTrip_writeThenRead_purePreferences() {
    val s =
      AppSettings(
        proxyType = "SOCKS5",
        proxyHost = "  proxy.local  ",
        proxyPort = 9050,
        proxyUser = " alice ",
        proxyPass = "s3cr3t",
        proxyPeer = false,
        downloadRateLimit = 512,
        metadataTimeout = 120,
        cacheLimitMb = 1024,
        previewCacheMode = PREVIEW_CACHE_HOLD,
        uiTheme = "DARK",
        loggingEnabled = false,
        // 诊断 D-2026-09-18：两项新键也要经同一条读写路径往返
        portMapping = true,
        extraTrackers = true,
      )
    val prefs = writeSettings(mutablePreferencesOf(), s)
    assertEquals(s.sanitized(), AppSettings.from(prefs))
  }

  // ------------------------------------------------------------ 往返（真落盘 DataStore）

  @Test
  fun roundTrip_realDataStore_survivesNewInstance() = runBlocking {
    val dir = tempDir("settings-roundtrip")
    val first = openStore(dir)
    try {
      val s =
        AppSettings(
          proxyType = PROXY_TYPE_HTTP,
          proxyHost = "10.1.2.3",
          proxyPort = 8080,
          proxyUser = "bob",
          proxyPass = "pw",
          proxyPeer = true,
          downloadRateLimit = 300,
          metadataTimeout = 45,
          cacheLimitMb = 4096,
          previewCacheMode = PREVIEW_CACHE_HOLD,
          uiTheme = THEME_DARK,
          loggingEnabled = false,
        )
      first.store.save(s)
      assertEquals(s, first.store.load())
      assertTrue(
        "落盘文件必须存在且非空：" + File(dir, SettingsStore.FILE_NAME).path,
        File(dir, SettingsStore.FILE_NAME).exists() && File(dir, SettingsStore.FILE_NAME).length() > 0,
      )
    } finally {
      // 「进程重启」= 旧实例的 scope 先真正结束（DataStore 硬约束：同一文件同时只能有一个活跃实例）
      first.close()
    }
    val second = openStore(dir)
    try {
      assertEquals(
        "重启后必须从同一个文件读回同一份设置",
        AppSettings(
          proxyType = PROXY_TYPE_HTTP,
          proxyHost = "10.1.2.3",
          proxyPort = 8080,
          proxyUser = "bob",
          proxyPass = "pw",
          proxyPeer = true,
          downloadRateLimit = 300,
          metadataTimeout = 45,
          cacheLimitMb = 4096,
          previewCacheMode = PREVIEW_CACHE_HOLD,
          uiTheme = THEME_DARK,
          loggingEnabled = false,
        ),
        second.store.load(),
      )
    } finally {
      second.close()
      dir.deleteRecursively()
    }
  }

  @Test
  fun write_isIdempotent_andSanitizes() = runBlocking {
    val dir = tempDir("settings-sanitize")
    val h = openStore(dir)
    try {
      h.store.save(AppSettings(proxyType = "  WeIrD  ", proxyPort = -5, downloadRateLimit = -9, metadataTimeout = 0, uiTheme = "neon"))
      val v = h.store.load()
      assertEquals(PROXY_TYPE_NONE, v.proxyType)
      assertEquals(PROXY_DEFAULT_PORT, v.proxyPort)
      assertEquals(0, v.downloadRateLimit)
      assertEquals(METADATA_TIMEOUT_SECONDS, v.metadataTimeout)
      assertEquals(THEME_LIGHT, v.uiTheme)
      h.store.save(v)
      assertEquals(v, h.store.load())
    } finally {
      h.close()
      dir.deleteRecursively()
    }
  }

  // ------------------------------------------------------------ 代理：设置 → ProxyConfig → libtorrent 设置

  @Test
  fun proxyMapping_socks5WithAuth_reachesEngineAsSocks5Pw() {
    val s = AppSettings(proxyType = "socks5", proxyHost = "proxy.local", proxyPort = 9050, proxyUser = "alice", proxyPass = "s3cr3t", proxyPeer = true)
    val cfg = s.toProxyConfig()

    assertEquals("socks5", cfg.type)
    assertEquals("proxy.local", cfg.host)
    assertEquals(9050, cfg.port)
    assertEquals("alice", cfg.user)
    assertEquals("s3cr3t", cfg.pass)
    assertTrue(cfg.peer)
    assertTrue(s.proxyEnabled())

    val mapped = proxySettingsFor(cfg)
    assertEquals(LtProxyType.SOCKS5_PW, mapped.proxyType)
    assertEquals("proxy.local", mapped.hostname)
    assertEquals(9050, mapped.port)
    assertEquals("alice", mapped.username)
    assertEquals("s3cr3t", mapped.password)
    assertTrue(mapped.peerConnections)
    assertTrue("tracker 恒走代理", mapped.trackerConnections)
  }

  @Test
  fun proxyMapping_httpWithoutAuth() {
    val mapped = proxySettingsFor(AppSettings(proxyType = "http", proxyHost = "10.0.0.1", proxyPort = 3128).toProxyConfig())
    assertEquals(LtProxyType.HTTP, mapped.proxyType)
    assertEquals(3128, mapped.port)
    assertNull(mapped.username)
  }

  @Test
  fun proxyMapping_noneOrEmptyHost_fallsBackToDirect_bothSwitchesOff() {
    val disabled = proxySettingsFor(AppSettings(proxyType = "none", proxyHost = "proxy.local", proxyPort = 9050).toProxyConfig())
    assertEquals(LtProxyType.NONE, disabled.proxyType)
    assertFalse(disabled.peerConnections)
    assertFalse(disabled.trackerConnections)
    assertFalse(AppSettings(proxyType = "none", proxyHost = "proxy.local").proxyEnabled())

    val emptyHost = proxySettingsFor(AppSettings(proxyType = "socks5", proxyHost = "   ").toProxyConfig())
    assertEquals(LtProxyType.NONE, emptyHost.proxyType)
    assertFalse(emptyHost.trackerConnections)
    assertFalse(AppSettings(proxyType = "socks5", proxyHost = "  ").proxyEnabled())
  }

  @Test
  fun proxyMapping_invalidPort_fallsBackTo1080() {
    assertEquals(PROXY_DEFAULT_PORT, AppSettings(proxyPort = 0).toProxyConfig().port)
    assertEquals(PROXY_DEFAULT_PORT, AppSettings(proxyPort = -1).toProxyConfig().port)
    assertEquals(1080, sanitizeProxyPort(0))
  }

  // ------------------------------------------------------------ 主题模式解析

  @Test
  fun themeModeParsing_valueDomain() {
    assertEquals(THEME_LIGHT, normalizeThemeMode("light"))
    assertEquals(THEME_DARK, normalizeThemeMode("DARK"))
    assertEquals(THEME_SYSTEM, normalizeThemeMode(" System "))
    assertEquals("非法值回落浅色（桌面默认）", THEME_LIGHT, normalizeThemeMode("neon"))
    assertEquals(THEME_LIGHT, normalizeThemeMode(null))
    assertEquals(THEME_LIGHT, normalizeThemeMode(""))
    assertEquals(listOf("light", "dark", "system"), THEME_MODES)
  }

  @Test
  fun darkDecision_lightDarkSystem() {
    assertFalse(isDarkTheme(THEME_LIGHT, systemDark = true))
    assertFalse(isDarkTheme(THEME_LIGHT, systemDark = false))
    assertTrue(isDarkTheme(THEME_DARK, systemDark = false))
    assertTrue(isDarkTheme(THEME_DARK, systemDark = true))
    assertEquals("system 跟随系统", true, isDarkTheme(THEME_SYSTEM, systemDark = true))
    assertEquals("system 跟随系统", false, isDarkTheme(THEME_SYSTEM, systemDark = false))
    assertFalse("非法值 → 浅色", isDarkTheme("weird", systemDark = true))
  }

  // ------------------------------------------------------------ 限速值域

  @Test
  fun rateLimitValueDomain_negativeAndZero() {
    assertEquals(0, sanitizeRateLimitKbps(-1))
    assertEquals(0, sanitizeRateLimitKbps(-999999))
    assertEquals(0, sanitizeRateLimitKbps(0))
    assertEquals(1, sanitizeRateLimitKbps(1))
    assertEquals(300, sanitizeRateLimitKbps(300))
    // 落盘时同样归一：负值写成 0（= 不限）
    val prefs = writeSettings(mutablePreferencesOf(), AppSettings(downloadRateLimit = -5))
    assertEquals(0, AppSettings.from(prefs).downloadRateLimit)
    assertEquals(0, AppSettings(downloadRateLimit = -5).sanitized().downloadRateLimit)
  }

  @Test
  fun metadataTimeoutValueDomain() {
    assertEquals(METADATA_TIMEOUT_SECONDS, sanitizeMetadataTimeout(0))
    assertEquals(METADATA_TIMEOUT_SECONDS, sanitizeMetadataTimeout(-30))
    assertEquals(30, sanitizeMetadataTimeout(30))
  }

  @Test
  fun cacheModeValueDomain_unknownFallsBackToConvert() {
    assertEquals(PREVIEW_CACHE_CONVERT, AppSettings(previewCacheMode = "nonsense").sanitized().previewCacheMode)
    assertEquals(PREVIEW_CACHE_HOLD, AppSettings(previewCacheMode = " HOLD ").sanitized().previewCacheMode)
  }

  // ------------------------------------------------------------ 夹具工具

  private fun tempDir(tag: String): File {
    val d = File(System.getProperty("java.io.tmpdir"), "mv-settings-test-$tag-${System.nanoTime()}")
    d.mkdirs()
    return d
  }

  /** 一个 DataStore 实例 + 它的 scope（关掉必须 cancel 并等它结束，否则同一文件不能重开）。 */
  private class StoreHandle(val store: SettingsStore, private val scope: CoroutineScope) {
    fun close() {
      scope.cancel()
      runBlocking { scope.coroutineContext[Job]?.join() }
    }
  }

  private fun openStore(dir: File): StoreHandle {
    val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    return StoreHandle(
      SettingsStore(
        PreferenceDataStoreFactory.create(
          scope = scope,
          produceFile = { File(dir, SettingsStore.FILE_NAME) },
        )
      ),
      scope,
    )
  }
}
