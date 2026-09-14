package com.bitseed.magnetviewer.engine

import com.bitseed.magnetviewer.data.PROXY_TYPE_HTTP
import com.bitseed.magnetviewer.data.PROXY_TYPE_NONE
import com.bitseed.magnetviewer.data.PROXY_TYPE_SOCKS5
import java.io.InputStream
import java.io.OutputStream
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * **I-03 回归**：坏代理不许被静默「照单全收」。
 *
 * QA-2026-09-14 实测：设置页选 SOCKS5、主机 `10.0.2.2`、端口 `1`（不可达）→ 保存后
 * 日志只有 `应用代理：type=SOCKS5 … 会话运行中=true` + `UI 状态：代理已更新：10.0.2.2:1`，
 * 既没有探活失败提示也没有回落直连 —— 用户以为代理生效了，实际上什么都连不上。
 *
 * 修法（报告里的 (a) 方案）：保存/加载设置时对**启用中的代理**做一次真实握手探活
 * （SOCKS5 = RFC1928 方法协商；HTTP = CONNECT），不可达 → **回退直连**并在状态行写清
 * 「代理不可达（已回退直连）」；可达 → 照常下发，不额外宣称。
 *
 * 本测试用**真实 loopback socket**（起一个假代理服务器）验证探针协议本身，
 * 再用注入的探针验证决策分支。
 */
class ProxyProbeTest {

  private fun fakeSocks5Server(reply: ByteArray, onRequest: ((ByteArray) -> Unit)? = null): Pair<ServerSocket, Thread> {
    val server = ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"))
    val t =
      thread(isDaemon = true) {
        try {
          val c = server.accept()
          val input: InputStream = c.getInputStream()
          val out: OutputStream = c.getOutputStream()
          val buf = ByteArray(512)
          val n = input.read(buf)
          if (n > 0) onRequest?.invoke(buf.copyOf(n))
          out.write(reply)
          out.flush()
          c.close()
        } catch (_: Throwable) {
          // 客户端提前断开
        }
      }
    return server to t
  }

  private fun freePort(): Int {
    val s = ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"))
    val p = s.localPort
    s.close()
    return p
  }

  // ------------------------------------------------------------ 探针本体（真实 socket）

  @Test
  fun socks5Probe_succeedsOnNoAuthGreeting() {
    val (server, t) = fakeSocks5Server(byteArrayOf(0x05, 0x00.toByte()))
    val port = server.localPort
    val r = probeProxy(PROXY_TYPE_SOCKS5, "127.0.0.1", port, timeoutMs = 2000)
    server.close()
    t.join(500)
    assertTrue("SOCKS5 无鉴权应答必须判可达：${r.detail}", r.ok)
  }

  @Test
  fun socks5Probe_authRequiredWithCredentials_isReachable() {
    // 服务器从我们提供的方法里选了 0x02（账号密码）→ 我们配了凭据，这就是可达
    val (server, _) = fakeSocks5Server(byteArrayOf(0x05, 0x02.toByte()))
    val r = probeProxy(PROXY_TYPE_SOCKS5, "127.0.0.1", server.localPort, timeoutMs = 2000, hasAuth = true)
    server.close()
    assertTrue("配了凭据时服务器选账号密码鉴权 = 可达：${r.detail}", r.ok)
  }

  @Test
  fun socks5Probe_authRequiredWithoutCredentials_isUnreachable() {
    // 0xFF = 我们提供的方法一个都不接受（没配账号密码时只提供了 no-auth）
    val (server, _) = fakeSocks5Server(byteArrayOf(0x05, 0xFF.toByte()))
    val r = probeProxy(PROXY_TYPE_SOCKS5, "127.0.0.1", server.localPort, timeoutMs = 2000)
    server.close()
    assertFalse("代理要鉴权而我们没配凭据 → 不可用（不许宣称生效）", r.ok)
    assertTrue("原因要写清是鉴权问题：${r.detail}", r.detail.contains("鉴权"))
  }

  @Test
  fun socks5Probe_failsOnGarbageReply() {
    val (server, _) = fakeSocks5Server("HTTP/1.1 400 Bad Request\r\n\r\n".toByteArray())
    val r = probeProxy(PROXY_TYPE_SOCKS5, "127.0.0.1", server.localPort, timeoutMs = 2000)
    server.close()
    assertFalse("握手应答不是 SOCKS5 → 不可达", r.ok)
    assertTrue("原因要写清（便于状态行显示）：${r.detail}", r.detail.isNotEmpty())
  }

  @Test
  fun socks5Probe_sendsTheRfc1928Greeting() {
    var seen: ByteArray? = null
    val (server, t) = fakeSocks5Server(byteArrayOf(0x05, 0x00.toByte())) { seen = it }
    probeProxy(PROXY_TYPE_SOCKS5, "127.0.0.1", server.localPort, timeoutMs = 2000)
    server.close()
    t.join(500)
    val req = seen
    assertNotNull("探针必须真的发握手字节（不是只 connect 一下）", req)
    assertTrue("首字节 VER=5：${req!!.toList()}", req.isNotEmpty() && req[0] == 0x05.toByte())
    assertTrue("NMETHODS>=1", req.size >= 2 && req[1].toInt() >= 1)
    assertTrue("必须提供 no-auth(0x00)", req.any { it == 0x00.toByte() })
    assertEquals("没配凭据时只提供 1 种方法", 1, req[1].toInt())
  }

  @Test
  fun probe_failsWhenNothingListens() {
    val port = freePort()
    val r = probeProxy(PROXY_TYPE_SOCKS5, "127.0.0.1", port, timeoutMs = 800)
    assertFalse("端口没人监听 → 不可达", r.ok)
    assertTrue("原因里要带主机端口或异常类型：${r.detail}", r.detail.isNotEmpty())
  }

  @Test
  fun httpProbe_acceptsConnectSuccessReply() {
    val server = ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"))
    thread(isDaemon = true) {
      try {
        val c = server.accept()
        c.getInputStream().read(ByteArray(512))
        c.getOutputStream().write("HTTP/1.1 200 Connection established\r\n\r\n".toByteArray())
        c.getOutputStream().flush()
        c.close()
      } catch (_: Throwable) {}
    }
    val r = probeProxy(PROXY_TYPE_HTTP, "127.0.0.1", server.localPort, timeoutMs = 2000)
    server.close()
    assertTrue("HTTP 代理回 200 → 可达：${r.detail}", r.ok)
  }

  // ------------------------------------------------------------ 决策分支（生效 / 回退直连）

  @Test
  fun decision_directWhenProxyDisabled() {
    val d =
      decideEffectiveProxy(
        ProxyConfig(PROXY_TYPE_NONE, "", 0, "", "", false),
        probe = { error("直连不该探活") },
      )
    assertTrue(d.ok)
    assertEquals(PROXY_TYPE_NONE, d.effective.type)
    assertTrue("状态行要说清「直连」而不是「代理已生效」：${d.note}", (d.note ?: "").contains("直连"))
  }

  @Test
  fun decision_directWhenHostEmpty() {
    val d = decideEffectiveProxy(ProxyConfig(PROXY_TYPE_SOCKS5, "  ", 1080, "", "", true), probe = { error("空主机不该探活") })
    assertEquals(PROXY_TYPE_NONE, d.effective.type)
    assertTrue(d.note!!.contains("直连"))
  }

  @Test
  fun decision_keepsProxyWhenReachable() {
    val cfg = ProxyConfig(PROXY_TYPE_SOCKS5, "127.0.0.1", 1080, "", "", true)
    val d = decideEffectiveProxy(cfg, probe = { ProxyReachability(ok = true, detail = "SOCKS5 握手成功") })
    assertTrue(d.ok)
    assertEquals("可达就原样生效", cfg, d.effective)
    assertTrue("可达时不要多嘴宣称（引擎会报「代理已更新」）", d.note == null)
  }

  @Test
  fun decision_fallsBackToDirectWhenUnreachable() {
    val cfg = ProxyConfig(PROXY_TYPE_SOCKS5, "10.0.2.2", 1, "", "", true)
    val d = decideEffectiveProxy(cfg, probe = { ProxyReachability(ok = false, detail = "连接被拒绝") })
    assertFalse(d.ok)
    assertEquals("不可达必须回退直连", PROXY_TYPE_NONE, d.effective.type)
    val note = d.note!!
    assertTrue("状态行必须明说「代理不可达」：$note", note.contains("代理不可达"))
    assertTrue("并且说清已回退直连：$note", note.contains("回退直连"))
    assertTrue("带上主机端口，用户才知道是哪一个：$note", note.contains("10.0.2.2") && note.contains("1"))
  }

  @Test
  fun decision_reportsExceptionTextFromProbe() {
    val cfg = ProxyConfig(PROXY_TYPE_SOCKS5, "bad.host.local", 1080, "", "", true)
    val d = decideEffectiveProxy(cfg, probe = { ProxyReachability(ok = false, detail = "UnknownHostException: bad.host.local") })
    assertTrue("原因要进状态行：${d.note}", d.note!!.contains("UnknownHostException"))
  }

  @Test
  fun probeTimeout_isBounded() {
    // 一个只 accept 不回答的「黑洞」代理：探针必须在超时内返回且判不可达
    val server = ServerSocket(0, 1, java.net.InetAddress.getByName("127.0.0.1"))
    thread(isDaemon = true) {
      try {
        Thread.sleep(3000)
        server.close()
      } catch (_: Throwable) {}
    }
    val latch = CountDownLatch(1)
    var ok = true
    thread(isDaemon = true) {
      ok = probeProxy(PROXY_TYPE_SOCKS5, "127.0.0.1", server.localPort, timeoutMs = 700).ok
      latch.countDown()
    }
    assertTrue("探针必须在超时窗口内返回（不许卡住设置保存）", latch.await(2500, TimeUnit.MILLISECONDS))
    assertFalse("不回应的代理 = 不可达", ok)
  }

  @Test
  fun probe_unknownTypeIsNotProbed() {
    val r = probeProxy("bogus", "127.0.0.1", freePort(), timeoutMs = 500)
    assertFalse("未知代理类型不做探活、也不宣称可用", r.ok)
    assertTrue(r.detail.contains("未知"))
  }

  /** 用真实的 `Socket` 兜一道：探针对象本身不抛异常。 */
  @Test
  fun probe_neverThrowsOnWeirdHost() {
    val r = probeProxy(PROXY_TYPE_SOCKS5, "", 0, timeoutMs = 300)
    assertFalse(r.ok)
  }
}
