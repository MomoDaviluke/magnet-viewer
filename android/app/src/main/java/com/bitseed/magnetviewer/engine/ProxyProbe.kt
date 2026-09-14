package com.bitseed.magnetviewer.engine

import java.net.InetSocketAddress
import com.bitseed.magnetviewer.data.PROXY_TYPE_HTTP
import com.bitseed.magnetviewer.data.PROXY_TYPE_NONE
import com.bitseed.magnetviewer.data.PROXY_TYPE_SOCKS5
import java.net.Socket

/**
 * **代理探活 + 生效决策**（I-03 的修复落点；纯 Kotlin，JVM 可测，不碰 libtorrent）。
 *
 * 现场（QA-2026-09-14）：填 `socks5 10.0.2.2:1`（不可达）保存后，应用照单全收并对外宣称
 * 「代理已更新」——日志里既没有回落直连也没有探活失败，用户以为代理生效了。
 *
 * 现在的口径（报告里的方案 (a)）：
 * 1. 保存/加载设置时对**启用中的**代理做一次真实握手探活（[probeProxy]）：
 *    - SOCKS5：RFC1928 方法协商（`05 01 00` → 应答 `05 xx`，`xx=00` 无鉴权 / `FF` 要鉴权都算活）；
 *    - HTTP：`CONNECT host:port HTTP/1.1` → 应答 1xx/2xx 算活；
 * 2. 探活失败 → **回退直连**（`proxy_type=none`），状态行明说
 *    「代理不可达（host:port）：已回退直连（原因）」（[ProxyDecision.note]）；
 * 3. 探活成功 → 原样下发，**不额外宣称**（引擎自己的 `代理已更新：host:port` 才是那句话）。
 */

/** 探活结果。 */
data class ProxyReachability(val ok: Boolean, val detail: String)

/** 生效决策：真正下发给引擎的配置 + 需要上屏的说明（null = 无需特别说明）。 */
data class ProxyDecision(
  /** 实际生效的代理配置（探活失败时是直连）。 */
  val effective: ProxyConfig,
  /** 状态行文案（可达时为 null）。 */
  val note: String?,
  /** 探活是否通过（直连恒 true）。 */
  val ok: Boolean,
)

/** 探活超时（毫秒）。够本地回环/局域网代理应答，又不至于让「保存设置」卡住。 */
const val PROXY_PROBE_TIMEOUT_MS: Int = 1500

/**
 * 一次真实握手探活。**绝不抛**（失败一律变成 `ok=false` + 原因文本）。
 *
 * [dial] 可注入（默认真实 TCP 连接）——单测既能用真实 loopback socket 验协议，
 * 也能用替身验异常分支。
 */
fun probeProxy(
  type: String,
  host: String,
  port: Int,
  timeoutMs: Int = PROXY_PROBE_TIMEOUT_MS,
  hasAuth: Boolean = false,
  dial: (String, Int, Int) -> Socket = ::dialTcp,
): ProxyReachability {
  val t = type.trim().lowercase()
  val h = host.trim()
  if (h.isEmpty() || port <= 0) return ProxyReachability(ok = false, detail = "代理主机/端口为空（$h:$port）")
  return when (t) {
    "socks5" -> probeSocks5(h, port, timeoutMs, hasAuth, dial)
    "http" -> probeHttp(h, port, timeoutMs, dial)
    else -> ProxyReachability(ok = false, detail = "未知代理类型：$type（不探活）")
  }
}

/**
 * SOCKS5 方法协商（RFC1928 §3）：发 `05 01 00`（版本 5 / 1 种方法 / 无鉴权），
 * 应答必须是 `05 xx`（`xx=00` 无需鉴权，`FF` 需要鉴权——服务器活着，我们带账号密码时可继续）。
 */
private fun probeSocks5(
  host: String,
  port: Int,
  timeoutMs: Int,
  hasAuth: Boolean,
  dial: (String, Int, Int) -> Socket,
): ProxyReachability =
  runCatching {
      dial(host, port, timeoutMs).use { s ->
        s.soTimeout = timeoutMs
        // 方法列表按配置给：有账号密码就同时提供 user/pass(0x02)，否则只提供 no-auth(0x00)。
        // 服务器只能从**我们提供的**方法里挑（RFC1928 §3），所以 0xFF = 我们的方法都不被接受。
        val methods = if (hasAuth) byteArrayOf(0x05, 0x02, 0x00, 0x02) else byteArrayOf(0x05, 0x01, 0x00)
        s.getOutputStream().write(methods)
        s.getOutputStream().flush()
        val reply = ByteArray(2)
        var got = 0
        while (got < 2) {
          val n = s.getInputStream().read(reply, got, 2 - got)
          if (n <= 0) break
          got += n
        }
        when {
          got < 2 -> ProxyReachability(ok = false, detail = "握手无应答（代理 $host:$port 未按 SOCKS5 回应）")
          reply[0].toInt() != 0x05 -> ProxyReachability(
            ok = false,
            detail = "应答不是 SOCKS5（首字节 ${reply[0].toInt()}，主机 $host:$port）",
          )
          reply[1] == 0x00.toByte() -> ProxyReachability(ok = true, detail = "SOCKS5 握手成功（无需鉴权）")
          reply[1] == 0x02.toByte() && hasAuth ->
            ProxyReachability(ok = true, detail = "SOCKS5 握手成功（服务器选择账号密码鉴权，已配置凭据）")
          reply[1] == 0xFF.toByte() -> ProxyReachability(
            ok = false,
            detail = if (hasAuth) "代理拒绝了提供的鉴权方法（$host:$port）" else "代理要求账号密码鉴权，但当前未配置",
          )
          else -> ProxyReachability(
            ok = false,
            detail = "代理选择了未提供的方法（${reply[1].toInt() and 0xFF}，$host:$port）",
          )
        }
      }
    }
    .getOrElse { e -> ProxyReachability(ok = false, detail = e.javaClass.simpleName + ": " + (e.message ?: "连接失败")) }

/** HTTP 代理探活：`CONNECT host:port HTTP/1.1` → 状态行 1xx/2xx 即视为可用。 */
private fun probeHttp(
  host: String,
  port: Int,
  timeoutMs: Int,
  dial: (String, Int, Int) -> Socket,
): ProxyReachability =
  runCatching {
      dial(host, port, timeoutMs).use { s ->
        s.soTimeout = timeoutMs
        s.getOutputStream().write("CONNECT $host:$port HTTP/1.1\r\nHost: $host\r\n\r\n".toByteArray())
        s.getOutputStream().flush()
        val line = readLine(s, timeoutMs)
        if (line == null) {
          ProxyReachability(ok = false, detail = "HTTP 代理无应答（$host:$port）")
        } else if (line.startsWith("HTTP/") && (line.contains(" 1") || line.contains(" 2"))) {
          ProxyReachability(ok = true, detail = "HTTP 代理 CONNECT 成功（$line）")
        } else {
          ProxyReachability(ok = false, detail = "HTTP 代理应答异常：$line")
        }
      }
    }
    .getOrElse { e -> ProxyReachability(ok = false, detail = e.javaClass.simpleName + ": " + (e.message ?: "连接失败")) }

private fun readLine(s: Socket, timeoutMs: Int): String? {
  val deadline = System.currentTimeMillis() + timeoutMs
  val sb = StringBuilder()
  while (System.currentTimeMillis() < deadline && sb.length < 256) {
    val b = s.getInputStream().read()
    if (b < 0) break
    val c = b.toChar()
    if (c == '\n') return sb.toString().trimEnd('\r')
    sb.append(c)
  }
  return if (sb.isEmpty()) null else sb.toString()
}

/** 默认拨号器：真实 TCP 连接（带连接超时）。 */
private fun dialTcp(host: String, port: Int, timeoutMs: Int): Socket =
  Socket().apply { connect(InetSocketAddress(host, port), timeoutMs) }

/**
 * **生效决策**（纯函数 + 可注入探针）：探活失败 → 回退直连并把原因交给状态行。
 *
 * @param probe 探活实现（默认 [probeProxy]；单测注入固定结果）
 */
fun decideEffectiveProxy(
  cfg: ProxyConfig,
  probe: (ProxyConfig) -> ProxyReachability = { c ->
    probeProxy(c.type, c.host, c.port, hasAuth = c.user.trim().isNotEmpty())
  },
): ProxyDecision {
  val type = cfg.type.trim().lowercase()
  val host = cfg.host.trim()
  if (type == PROXY_TYPE_NONE || type.isEmpty() || host.isEmpty()) {
    return ProxyDecision(
      effective = ProxyConfig(PROXY_TYPE_NONE, host, cfg.port, cfg.user, cfg.pass, cfg.peer),
      note = "直连（未启用代理）",
      ok = true,
    )
  }
  val r = probe(cfg)  // decideEffectiveProxy 的探活只看「是否可达」
  if (r.ok) return ProxyDecision(effective = cfg, note = null, ok = true)
  return ProxyDecision(
    effective = ProxyConfig(PROXY_TYPE_NONE, host, cfg.port, cfg.user, cfg.pass, cfg.peer),
    note = "代理不可达（$host:${cfg.port}）：已回退直连（${r.detail}）",
    ok = false,
  )
}
