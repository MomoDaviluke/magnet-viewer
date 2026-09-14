package com.bitseed.magnetviewer.engine

/**
 * 代理配置（**签名冻结**：字段名/顺序/类型都不要再改，UI 侧按这个构造）。
 *
 * 与桌面 `core/config.py:proxy_config()` 的 6 个键一一对应：
 * `type`（none/socks5/http）、`host`、`port`、`user`、`pass`、`peer`（peer 是否走代理）。
 */
data class ProxyConfig(
  val type: String,
  val host: String,
  val port: Int,
  val user: String,
  val pass: String,
  val peer: Boolean,
)

/** 代理未给端口时的默认值（桌面 `lt_proxy_settings`：`int(p.get("port") or 1080)`）。 */
const val PROXY_DEFAULT_PORT: Int = 1080

/**
 * libtorrent 代理类型的**符号化**枚举。
 *
 * 为什么不用 swig 的 `settings_pack.proxy_type_t`：那是 swig 生成类，JVM 单测里
 * 一碰就会去加载原生库。符号枚举让「配置 → libtorrent 设置」的映射成为纯函数，
 * 可以由单测逐分支断言；swig 常量只在运行时翻译（见 `ProxySwig.kt`）。
 */
enum class LtProxyType { NONE, SOCKS5, SOCKS5_PW, HTTP, HTTP_PW }

/**
 * [proxySettingsFor] 的输出：一组**已经决策好**的 libtorrent 代理设置。
 *
 * 全部字段是普通类型 → 映射逻辑可在 JVM 单测里逐条断言（不让 swig 类型泄漏进来）。
 */
data class ProxySettings(
  val proxyType: LtProxyType,
  val hostname: String? = null,
  val port: Int? = null,
  val username: String? = null,
  val password: String? = null,
  val peerConnections: Boolean = false,
  val trackerConnections: Boolean = false,
)

/**
 * 代理配置 → libtorrent 设置（桌面 `core/config.py:lt_proxy_settings` 的逐条移植，纯函数）。
 *
 * 关键语义（**历史缺陷就在这里，不许简化**）：
 * 1. 未启用代理（type 不是 socks5/http）**或主机为空** → 直连：`proxy_type=none`，
 *    **且同时**把 `proxy_peer_connections` / `proxy_tracker_connections` 一起回落为 false。
 *    只重置 proxy_type 会让 tracker 继续走旧代理（桌面注释里已实证的残留缺陷）；
 * 2. 给了用户名 → 自动用带鉴权的类型（socks5 → socks5_pw，http → http_pw），账号密码一起下发；
 * 3. 主机走代理时 tracker 恒为 true；peer 是否走代理由 [ProxyConfig.peer] 决定；
 * 4. 端口非法（<= 0）回落 [PROXY_DEFAULT_PORT]。
 */
fun proxySettingsFor(cfg: ProxyConfig?): ProxySettings {
  val type = (cfg?.type ?: "none").trim().lowercase()
  val host = cfg?.host?.trim().orEmpty()
  if (type != "socks5" && type != "http" || host.isEmpty()) {
    // 直连分支：三条一起回落（proxy_type / proxy_peer_connections / proxy_tracker_connections）
    return ProxySettings(
      proxyType = LtProxyType.NONE,
      peerConnections = false,
      trackerConnections = false,
    )
  }
  val user = cfg?.user.orEmpty()
  val auth = user.isNotEmpty()
  val kind =
    when (type) {
      "socks5" -> if (auth) LtProxyType.SOCKS5_PW else LtProxyType.SOCKS5
      else -> if (auth) LtProxyType.HTTP_PW else LtProxyType.HTTP
    }
  val port = cfg?.port ?: 0
  return ProxySettings(
    proxyType = kind,
    hostname = host,
    port = if (port > 0) port else PROXY_DEFAULT_PORT,
    username = if (auth) user else null,
    password = if (auth) cfg?.pass.orEmpty() else null,
    peerConnections = cfg?.peer == true,
    trackerConnections = true,
  )
}
