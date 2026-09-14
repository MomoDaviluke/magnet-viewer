package com.bitseed.magnetviewer.engine

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 代理映射单测（JVM）：桌面 `core/config.py:lt_proxy_settings` 的逐分支对齐。
 *
 * 重点是那个**历史缺陷**：未启用代理 / 主机为空时必须**三条一起**回落
 * （`proxy_type=none` **且** `proxy_peer_connections=false` **且**
 * `proxy_tracker_connections=false`）——只重置 proxy_type 会让 tracker 继续走旧代理。
 *
 * 本文件只碰纯函数 [proxySettingsFor]，不碰 swig（swig 翻译在 ProxySwig.kt，只在设备上跑）。
 */
class ProxyMappingTest {

  private fun cfg(
    type: String = "socks5",
    host: String = "127.0.0.1",
    port: Int = 1080,
    user: String = "",
    pass: String = "",
    peer: Boolean = true,
  ) = ProxyConfig(type = type, host = host, port = port, user = user, pass = pass, peer = peer)

  // ------------------------------------------------------------ 直连回落（三条一起）

  @Test
  fun disabledType_fallsBackToDirect_andResetsAllThree() {
    val s = proxySettingsFor(cfg(type = "none", host = "127.0.0.1", port = 1080, user = "u", pass = "p", peer = true))

    assertEquals(LtProxyType.NONE, s.proxyType)
    assertFalse("peer 连接开关必须回落 false", s.peerConnections)
    assertFalse("tracker 连接开关必须回落 false（历史缺陷点）", s.trackerConnections)
    assertNull("直连分支不下发主机", s.hostname)
    assertNull("直连分支不下发账号", s.username)
  }

  @Test
  fun emptyHost_fallsBackToDirect_andResetsAllThree() {
    val s = proxySettingsFor(cfg(type = "socks5", host = "   ", port = 1080, user = "u", pass = "p", peer = true))

    assertEquals(LtProxyType.NONE, s.proxyType)
    assertFalse(s.peerConnections)
    assertFalse(s.trackerConnections)
  }

  @Test
  fun unknownTypeWithHost_fallsBackToDirect() {
    val s = proxySettingsFor(cfg(type = "wireguard", host = "10.0.0.1", port = 1234))

    assertEquals(LtProxyType.NONE, s.proxyType)
    assertFalse(s.peerConnections)
    assertFalse(s.trackerConnections)
  }

  @Test
  fun nullConfig_fallsBackToDirect_andResetsAllThree() {
    val s = proxySettingsFor(null)

    assertEquals(LtProxyType.NONE, s.proxyType)
    assertFalse(s.peerConnections)
    assertFalse(s.trackerConnections)
  }

  @Test
  fun typeIsCaseAndSpaceInsensitive() {
    val s = proxySettingsFor(cfg(type = "  SOCKS5 ", host = " h ", port = 1081))

    assertEquals(LtProxyType.SOCKS5, s.proxyType)
    assertEquals("h", s.hostname)
  }

  // ------------------------------------------------------------ socks5 两档

  @Test
  fun socks5WithAuth_usesPasswordVariant_andTrackerAlwaysProxied() {
    val s = proxySettingsFor(cfg(type = "socks5", host = "proxy.local", port = 9050, user = "alice", pass = "s3cr3t", peer = true))

    assertEquals(LtProxyType.SOCKS5_PW, s.proxyType)
    assertEquals("proxy.local", s.hostname)
    assertEquals(9050, s.port)
    assertEquals("alice", s.username)
    assertEquals("s3cr3t", s.password)
    assertTrue("peer 走代理由配置决定", s.peerConnections)
    assertTrue("tracker 恒走代理", s.trackerConnections)
  }

  @Test
  fun socks5WithoutAuth_usesPlainSocks5_andNoCredentials() {
    val s = proxySettingsFor(cfg(type = "socks5", host = "proxy.local", port = 9050, peer = false))

    assertEquals(LtProxyType.SOCKS5, s.proxyType)
    assertNull(s.username)
    assertNull(s.password)
    assertFalse(s.peerConnections)
    assertTrue(s.trackerConnections)
  }

  // ------------------------------------------------------------ http 两档

  @Test
  fun httpWithAuth_usesHttpPwVariant() {
    val s = proxySettingsFor(cfg(type = "http", host = "10.1.2.3", port = 8080, user = "bob", pass = "pw"))

    assertEquals(LtProxyType.HTTP_PW, s.proxyType)
    assertEquals("bob", s.username)
    assertEquals("pw", s.password)
    assertEquals(8080, s.port)
  }

  @Test
  fun httpWithoutAuth_usesPlainHttpVariant() {
    val s = proxySettingsFor(cfg(type = "http", host = "10.1.2.3", port = 8080))

    assertEquals(LtProxyType.HTTP, s.proxyType)
    assertNull(s.username)
  }

  // ------------------------------------------------------------ 端口

  @Test
  fun invalidPort_fallsBackToDefault1080() {
    assertEquals(PROXY_DEFAULT_PORT, proxySettingsFor(cfg(port = 0)).port)
    assertEquals(PROXY_DEFAULT_PORT, proxySettingsFor(cfg(port = -1)).port)
    assertEquals(65535, proxySettingsFor(cfg(port = 65535)).port)
  }
}
