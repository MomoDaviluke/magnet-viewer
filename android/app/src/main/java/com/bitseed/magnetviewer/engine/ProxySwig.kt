package com.bitseed.magnetviewer.engine

import org.libtorrent4j.SettingsPack
import org.libtorrent4j.swig.settings_pack

/**
 * [ProxySettings] → libtorrent `SettingsPack` 的 swig 翻译层。
 *
 * 与 [ProxyConfig.kt] 分家是刻意的：那里是**纯函数**（JVM 单测逐分支断言），
 * 这里一碰 swig 常量就会加载原生库，只能在运行时跑。
 *
 * 三条一起回落（`proxy_type` / `proxy_peer_connections` / `proxy_tracker_connections`）
 * 在 [applyProxySettings] 里显式写死，不依赖 `SettingsPack` 的默认值。
 */
internal fun applyProxySettings(pack: SettingsPack, s: ProxySettings) {
  pack.setInteger(settings_pack.int_types.proxy_type.swigValue(), nativeProxyType(s.proxyType))
  if (s.proxyType == LtProxyType.NONE) {
    // 直连：peer 与 tracker 都显式关掉旧代理路径（只改 proxy_type 会留残留，见 ProxyConfig.kt）
    pack.setBoolean(settings_pack.bool_types.proxy_peer_connections.swigValue(), false)
    pack.setBoolean(settings_pack.bool_types.proxy_tracker_connections.swigValue(), false)
    return
  }
  s.hostname?.let { pack.setString(settings_pack.string_types.proxy_hostname.swigValue(), it) }
  s.port?.let { pack.setInteger(settings_pack.int_types.proxy_port.swigValue(), it) }
  s.username?.let { pack.setString(settings_pack.string_types.proxy_username.swigValue(), it) }
  s.password?.let { pack.setString(settings_pack.string_types.proxy_password.swigValue(), it) }
  pack.setBoolean(settings_pack.bool_types.proxy_peer_connections.swigValue(), s.peerConnections)
  pack.setBoolean(settings_pack.bool_types.proxy_tracker_connections.swigValue(), s.trackerConnections)
}

/** 符号枚举 → swig `settings_pack.proxy_type_t` 整数值。 */
internal fun nativeProxyType(t: LtProxyType): Int =
  when (t) {
    LtProxyType.NONE -> settings_pack.proxy_type_t.none.swigValue()
    LtProxyType.SOCKS5 -> settings_pack.proxy_type_t.socks5.swigValue()
    LtProxyType.SOCKS5_PW -> settings_pack.proxy_type_t.socks5_pw.swigValue()
    LtProxyType.HTTP -> settings_pack.proxy_type_t.http.swigValue()
    LtProxyType.HTTP_PW -> settings_pack.proxy_type_t.http_pw.swigValue()
  }
