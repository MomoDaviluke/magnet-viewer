package com.bitseed.magnetviewer.stream

import com.bitseed.magnetviewer.core.model.PieceMap
import java.io.File
import java.net.Socket
import java.nio.file.Files
import java.util.Collections
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * 流服务的 JVM 单测（真起 socket、真发 HTTP/1.1 请求）。
 *
 * 覆盖（2026-09-18 调整见 §1）：
 * 1. **渐进服务**：就绪不足时回**合法短区间**（206 + 就绪子集），
 *    `Content-Length` 恒等于实际发送字节数（**绝不谎报长度**）；
 *    一个字节都没就绪 → **503 + `Retry-After`**（可重试，与桌面同语义）。
 *    注：旧实现要求「整段就绪才应答，否则 504」，而 ExoPlayer 默认读超时约
 *    8s < 服务端 30s ⇒ seek 后必然失败。本套件锁住新语义。
 * 2. **不等满超时**：停滞提前返回与 progressiveMin 达成即返回，
 *    用「耗时远小于 waitTimeoutMs」直接证明（不是只看返回码）。
 * 3. 就绪区间的字节与真实文件逐字节一致（206 + Content-Range 总长恒为逻辑大小）；
 * 4. 错 token / 错 Host / 路径穿越 → 403；未知文件 → 404；
 *    可用性判不出来 → 按不可用（503），绝不整文件可用。
 */
class StreamServerTest {

  private lateinit var root: File
  private lateinit var content: ByteArray
  private lateinit var server: StreamServer
  private val demanded: MutableList<Pair<Long, Long>> = Collections.synchronizedList(ArrayList())

  private val pieceLength = 4096
  private var lastPiece = 15

  @Volatile private var availablePieces: Set<Int> = emptySet()

  @Before
  fun setUp() {
    root = Files.createTempDirectory("magnet-stream-test").toFile()
    content = ByteArray(64 * 1024) { i -> ((i * 31 + 7) and 0xFF).toByte() }
    lastPiece = content.size / pieceLength - 1
    File(root, "video.mp4").writeBytes(content)
    availablePieces = (0..lastPiece).toSet()
    demanded.clear()
    server =
      StreamServer(
        roots = listOf(root),
        pieceSource =
          object : PieceSource {
            override fun pieceMap(diskPath: String): PieceMap? {
              if (!diskPath.endsWith("video.mp4")) return null
              val have = availablePieces
              return PieceMap(pieceLength, 0L, 0, lastPiece, content.size.toLong()) { p -> p in have }
            }

            override fun onDemand(diskPath: String, start: Long, endExcl: Long) {
              demanded.add(start to endExcl)
            }
          },
        waitTimeoutMs = 400L,
        pollMs = 50L,
      )
    server.start()
  }

  @After
  fun tearDown() {
    server.close()
    root.deleteRecursively()
  }

  // ------------------------------------------------------------ 1. 渐进服务与不谎报

  /**
   * 就绪不足 → 回**合法短区间**（就绪多少发多少）。
   *
   * 「绝不短写」的准确含义是**绝不谎报长度**：`Content-Length` 必须等于实际
   * 发送字节数。返回比请求更短的区间本身是 RFC 7233 允许的，也是桌面
   * `core/stream_server.py` 长期在用的渐进机制（FFmpeg 读尽后自动续请求）。
   */
  @Test
  fun insufficientAvailability_servesReadySubset_withHonestLength() {
    availablePieces = (0..1).toSet() // 只落了头 8192 字节，远不足请求的 32KB
    val resp = request("/video.mp4?t=${server.token}", range = "bytes=0-32767")
    assertEquals(206, resp.status)
    assertEquals("bytes 0-8191/65536", resp.header("content-range"))
    // 长度必须说实话（= 实际发送字节数），这是不可动摇的不变量
    assertEquals("8192", resp.header("content-length"))
    assertEquals(resp.header("content-length")!!.toInt(), resp.body.size)
    assertArrayEquals(content.copyOfRange(0, 8192), resp.body)
    assertTrue("等待期间应至少点播一次", demanded.isNotEmpty())
  }

  /**
   * 闭区间请求的部分就绪：只发「从 start 起连续就绪」的那一段，
   * **不越过请求上界**，也不把缺口当已就绪。
   */
  @Test
  fun partiallyAvailableClosedRange_servesContiguousPrefixOnly() {
    // 请求 [4096, 16383)：第 1 块就绪、第 2 块缺 → 只能发 [4096, 8191)
    availablePieces = (0..1).toSet()
    val resp = request("/video.mp4?t=${server.token}", range = "bytes=4096-16383")
    assertEquals(206, resp.status)
    assertEquals("bytes 4096-8191/65536", resp.header("content-range"))
    assertEquals(4096, resp.header("content-length")!!.toInt())
    assertEquals(resp.header("content-length")!!.toInt(), resp.body.size)
    assertArrayEquals(content.copyOfRange(4096, 8192), resp.body)
  }

  /** 一个字节都没就绪 → 503 + `Retry-After`（可重试），而不是 504 判死。 */
  @Test
  fun noDataAtAll_returns503_retryable() {
    availablePieces = emptySet()
    val resp = request("/video.mp4?t=${server.token}", range = "bytes=0-4095")
    assertEquals(503, resp.status)
    assertEquals("1", resp.header("retry-after"))
    assertEquals("0", resp.header("content-length"))
    assertEquals(0, resp.body.size)
    // S8 诊断头：失败可自证——数据未就绪 = no-data，就绪量 0
    assertEquals("no-data", resp.header("x-why"))
    assertEquals("0", resp.header("x-ready-bytes"))
  }

  /** 请求起点越过文件末端 → 416 + `X-Why: range`（S8：区间不合法必须自证）。 */
  @Test
  fun rangeBeyondLogical_returns416_withDiagnosticHeader() {
    availablePieces = (0..lastPiece).toSet() // 数据全就绪也挡不住：区间本身不合法
    val resp = request("/video.mp4?t=${server.token}", range = "bytes=999999-")
    assertEquals(416, resp.status)
    assertEquals("range", resp.header("x-why"))
    assertEquals("bytes */${content.size}", resp.header("content-range"))
  }

  /**
   * C33（对齐桌面 stream_server.py:433-438）：**有前缀但请求起点未就绪 → 416**，
   * 而不是旧安卓的一律 503。头 8KB 就绪、请求从 16KB 起 → 起点无数据但整文件
   * 有前缀 → 416 + `X-Why: range`（播放器据此换请求，而不是无脑重试同一段）。
   */
  @Test
  fun prefixExistsButStartMissing_returns416_not503() {
    availablePieces = (0..1).toSet() // 头 8KB 就绪；16384 起未就绪
    val resp = request("/video.mp4?t=${server.token}", range = "bytes=16384-32767")
    assertEquals("有前缀但起点未就绪必须细分成 416（C33）", 416, resp.status)
    assertEquals("range", resp.header("x-why"))
    assertEquals("bytes */${content.size}", resp.header("content-range"))
  }

  /**
   * C32（对齐桌面 stream_server.py:315-354 等待/服务两类额度分账）：
   * **慢等待者不得饿死可即时服务的请求**。
   *
   * 构造（不靠 sleep 猜时序）：`maxConcurrency=1` → 服务额度只有 1；
   * 先把等待池（上限 1×4=4）用 4 个「起点未就绪」的 video 请求占满（它们
   * 移交等待池后**服务线程立即归还**）；此时对全就绪的 instant.bin 的请求
   * 必须仍能走服务线程快速拿到 200/206 —— 旧实现（固定池 8、等待即占线程）
   * 在这里会被 4 个等待者冻死。第 5 个等待请求 → 503 overloaded（等待额度满）。
   */
  @Test
  fun waitingRequests_doNotStarveServiceableRequests() {
    val instant = File(root, "instant.bin")
    instant.writeBytes(ByteArray(8192) { i -> (i and 0xFF).toByte() })
    val srv =
      StreamServer(
        roots = listOf(root),
        pieceSource =
          object : PieceSource {
            override fun pieceMap(diskPath: String): PieceMap? {
              if (diskPath.endsWith("instant.bin")) {
                return PieceMap(4096, 0L, 0, 1, instant.length()) { true }
              }
              if (!diskPath.endsWith("video.mp4")) return null
              // video 只有头 1 块就绪：任何 ≥4096 起点的请求都要等
              return PieceMap(pieceLength, 0L, 0, lastPiece, content.size.toLong()) { p -> p == 0 }
            }

            override fun onDemand(diskPath: String, start: Long, endExcl: Long) {}
          },
        waitTimeoutMs = 3_000L,
        pollMs = 50L,
        maxConcurrency = 1,
        queueCapacity = 4,
      )
    srv.start()
    try {
      // 4 个慢等待请求占满等待池（它们会阻塞到 waitTimeout，异步发出）
      val waiters =
        (1..4).map {
          val t =
            Thread {
              runCatching { request("/video.mp4?t=${srv.token}", "bytes=4096-8191", srv = srv, readTimeoutMs = 6_000) }
            }
          t.isDaemon = true
          t.start()
          t
        }
      // 给等待池一点时间把 4 个线程建起来
      Thread.sleep(300)
      // 关键断言：服务额度（1）没被等待者占用 → instant.bin 请求必须立即可服务
      val t0 = System.currentTimeMillis()
      val instantResp = request("/instant.bin?t=${srv.token}", srv = srv, readTimeoutMs = 2_000)
      val elapsed = System.currentTimeMillis() - t0
      assertTrue("instant 必须 200/206（实际 ${instantResp.status}）", instantResp.status == 200 || instantResp.status == 206)
      assertTrue("等待者不得饿死服务请求：instant 应即时返回，实测 ${elapsed}ms", elapsed < 1_500)
      // 第 5 个等待请求：等待池饱和 → 503 overloaded（绝不无界排队）
      val fifth = request("/video.mp4?t=${srv.token}", "bytes=4096-8191", srv = srv, readTimeoutMs = 2_000)
      assertEquals("等待额度满必须立即 503", 503, fifth.status)
      assertEquals("overloaded", fifth.header("x-why"))
      waiters.forEach { it.join(10_000) }
    } finally {
      srv.close()
      instant.delete()
    }
  }

  /**
   * 过载（S4）：worker 占满且队列也满时，多出的请求必须回 **503 + Retry-After**，
   * 而不是**静默断连**（旧实现的队列无界，根本不会走到这条；而静默断连会被
   * ExoPlayer 当成「流异常结束」且不重试，比一个可重试状态码难排查得多）。
   *
   * 确定性构造（不靠 sleep 猜时序）：
   * 1. `pieceMap` 阻塞在闩上 → 唯一的 worker 一定被占住；用 `entered` 闩确认已进入处理；
   * 2. `queueCapacity = 1` → 之后再发 2 个请求，**至少 1 个必被拒**（容量只够 1 个）。
   */
  @Test
  fun overload_beyondQueueCapacity_returns503_notSilentClose() {
    val entered = CountDownLatch(1)
    val release = CountDownLatch(1)
    val srv =
      StreamServer(
        roots = listOf(root),
        pieceSource =
          object : PieceSource {
            override fun pieceMap(diskPath: String): PieceMap? {
              entered.countDown()
              release.await(10, TimeUnit.SECONDS)
              return null
            }

            override fun onDemand(diskPath: String, start: Long, endExcl: Long) {}
          },
        waitTimeoutMs = 0L,
        pollMs = 50L,
        maxConcurrency = 1,
        queueCapacity = 1,
      )
    srv.start()
    try {
      val hog = Thread { runCatching { request("/video.mp4?t=${srv.token}", "bytes=0-15", srv = srv, readTimeoutMs = 500) } }
      hog.isDaemon = true
      hog.start()
      assertTrue(
        "首个请求应已进入处理（占住唯一 worker）",
        entered.await(5, TimeUnit.SECONDS),
      )

      val got: MutableList<Resp> = Collections.synchronizedList(ArrayList())
      val more =
        (1..2).map {
          val t =
            Thread {
              got.add(
                request("/video.mp4?t=${srv.token}", "bytes=0-15", srv = srv, readTimeoutMs = 1_500),
              )
            }
          t.isDaemon = true
          t.start()
          t
        }
      more.forEach { it.join(6_000) }

      val rejected = got.firstOrNull { it.status == 503 }
      assertTrue(
        "过载时必须回 503（实际状态=${got.map { it.status }}）",
        rejected != null,
      )
      assertEquals("503 必须是可重试语义（带 Retry-After）", "1", rejected!!.header("retry-after"))
      assertEquals("503 带零长 body", "0", rejected.header("content-length"))
      // S8 诊断头：过载拒绝必须自证 overloaded
      assertEquals("过载 503 必须自证 overloaded", "overloaded", rejected.header("x-why"))
    } finally {
      release.countDown()
      srv.close()
    }
  }

  /**
   * 停滞提前返回：就绪前缀不再增长时**不等满** `waitTimeoutMs`。
   *
   * 这是「seek 不再卡死」的关键 —— 旧实现会耗尽整个超时（30s）再回错误，
   * 而播放器早已放弃。用耗时断言直接证明，不只看返回码。
   */
  @Test
  fun stalledPrefix_returnsEarly_farBeforeTimeout() {
    val srv =
      StreamServer(
        roots = listOf(root),
        pieceSource =
          object : PieceSource {
            override fun pieceMap(diskPath: String): PieceMap? =
              PieceMap(pieceLength, 0L, 0, lastPiece, content.size.toLong()) { p -> p <= 1 }

            override fun onDemand(diskPath: String, start: Long, endExcl: Long) {}
          },
        waitTimeoutMs = 5_000L,
        pollMs = 50L,
        // 阈值设得够高，确保走的是「停滞」而不是「progressiveMin 达成」
        progressiveMinBytes = 1L * 1024 * 1024,
      )
    srv.start()
    try {
      val t0 = System.currentTimeMillis()
      val resp = request("/video.mp4?t=${srv.token}", range = "bytes=0-32767", srv = srv)
      val elapsed = System.currentTimeMillis() - t0
      assertEquals(206, resp.status)
      assertEquals("8192", resp.header("content-length"))
      assertEquals(resp.header("content-length")!!.toInt(), resp.body.size)
      assertTrue(
        "停滞应在 ~0.5s 内返回，实测 ${elapsed}ms（超时上限 5000ms）",
        elapsed < 3_000,
      )
    } finally {
      srv.close()
    }
  }

  /**
   * progressiveMin 达成即返回：不再等「整段就绪」。
   * 同理用耗时证明（阈值 4KB 很快达成，超时上限 5s）。
   */
  @Test
  fun progressiveMinReached_returnsWithoutWaitingFullTimeout() {
    val srv =
      StreamServer(
        roots = listOf(root),
        pieceSource =
          object : PieceSource {
            override fun pieceMap(diskPath: String): PieceMap? =
              PieceMap(pieceLength, 0L, 0, lastPiece, content.size.toLong()) { p -> p <= 1 }

            override fun onDemand(diskPath: String, start: Long, endExcl: Long) {}
          },
        waitTimeoutMs = 5_000L,
        pollMs = 50L,
        progressiveMinBytes = 4_096L,
      )
    srv.start()
    try {
      val t0 = System.currentTimeMillis()
      val resp = request("/video.mp4?t=${srv.token}", range = "bytes=0-32767", srv = srv)
      val elapsed = System.currentTimeMillis() - t0
      assertEquals(206, resp.status)
      assertEquals("8192", resp.header("content-length"))
      assertTrue("阈值达成应立即返回，实测 ${elapsed}ms", elapsed < 1_500)
    } finally {
      srv.close()
    }
  }

  // ------------------------------------------------------------ 2. 区间字节逐字节一致

  @Test
  fun rangeRequest_returns206_withExactBytes() {
    val resp = request("/video.mp4?t=${server.token}", range = "bytes=1024-2047")
    assertEquals(206, resp.status)
    assertEquals("bytes 1024-2047/65536", resp.header("content-range"))
    assertEquals("1024", resp.header("content-length"))
    assertEquals("bytes", resp.header("accept-ranges"))
    assertEquals("no-store", resp.header("cache-control"))
    assertEquals("nosniff", resp.header("x-content-type-options"))
    assertEquals("video/mp4", resp.header("content-type"))
    // Content-Length 必须等于实际发送字节数
    assertEquals(resp.header("content-length")!!.toInt(), resp.body.size)
    assertArrayEquals(content.copyOfRange(1024, 2048), resp.body)
  }

  @Test
  fun rangeRequest_fromMiddleToEnd_isByteExact() {
    val resp = request("/video.mp4?t=${server.token}", range = "bytes=60000-")
    assertEquals(206, resp.status)
    // 到文件尾：总长恒为逻辑大小，实际区间被文件尾截断
    assertEquals("bytes 60000-65535/65536", resp.header("content-range"))
    assertEquals(5536, resp.body.size)
    assertArrayEquals(content.copyOfRange(60000, 65536), resp.body)
  }

  @Test
  fun headRequest_hasNoBodyButCorrectLength() {
    val resp = request("/video.mp4?t=${server.token}", range = "bytes=0-4095", method = "HEAD")
    assertEquals(206, resp.status)
    assertEquals("4096", resp.header("content-length"))
    assertEquals(0, resp.body.size)
  }

  @Test
  fun noRangeRequest_servesFullFileWhenComplete() {
    val resp = request("/video.mp4?t=${server.token}")
    assertEquals(200, resp.status)
    assertEquals("65536", resp.header("content-length"))
    assertArrayEquals(content, resp.body)
  }

  /**
   * 开区间请求 `bytes=a-`：先等有界窗口落盘，再把「已经连续就绪」的部分一并发出
   * （Content-Length 恒等于实际发送字节数，不短写；Content-Range 总长恒为逻辑大小）。
   */
  @Test
  fun openEndedRequest_servesContiguousPrefixBeyondCap() {
    val srv =
      StreamServer(
        roots = listOf(root),
        pieceSource =
          object : PieceSource {
            override fun pieceMap(diskPath: String): PieceMap? =
              PieceMap(pieceLength, 0L, 0, lastPiece, content.size.toLong()) { p -> p <= 3 }

            override fun onDemand(diskPath: String, start: Long, endExcl: Long) {}
          },
        waitTimeoutMs = 2_000L,
        pollMs = 50L,
        openEndedCapBytes = 4096L,
      )
    srv.start()
    try {
      val resp = request("/video.mp4?t=${srv.token}", range = "bytes=0-", srv = srv)
      assertEquals(206, resp.status)
      assertEquals("bytes 0-16383/65536", resp.header("content-range"))
      assertEquals(16384, resp.header("content-length")!!.toInt())
      assertEquals(16384, resp.body.size)
      assertArrayEquals(content.copyOfRange(0, 16384), resp.body)
    } finally {
      srv.close()
    }
  }

  // ------------------------------------------------------------ 3. 鉴权

  @Test
  fun missingOrWrongToken_isForbidden() {
    assertEquals(403, request("/video.mp4").status)
    assertEquals(403, request("/video.mp4?t=deadbeef").status)
    assertEquals(403, request("/video.mp4?t=").status)
  }

  @Test
  fun wrongHostHeader_isForbidden() {
    val resp = request("/video.mp4?t=${server.token}", host = "evil.example.com")
    assertEquals(403, resp.status)
    assertEquals(403, request("/video.mp4?t=${server.token}", host = "127.0.0.1.evil.com:9999").status)
    // 回环白名单放行（含 localhost / ::1）
    assertEquals(206, request("/video.mp4?t=${server.token}", range = "bytes=0-15", host = "localhost:${server.port}").status)
    assertEquals(206, request("/video.mp4?t=${server.token}", range = "bytes=0-15", host = "[::1]:${server.port}").status)
  }

  // ------------------------------------------------------------ 4. 路径穿越

  @Test
  fun pathTraversal_isForbidden() {
    val outside = File(root.parentFile, "secret.txt")
    outside.writeText("secret")
    try {
      assertEquals(403, request("/../secret.txt?t=${server.token}").status)
      assertEquals(403, request("/..%2F..%2Fsecret.txt?t=${server.token}").status)
      assertEquals(403, request("/sub/../../secret.txt?t=${server.token}").status)
    } finally {
      outside.delete()
    }
    // 目录内的正常路径照常服务
    assertEquals(206, request("/video.mp4?t=${server.token}", range = "bytes=0-15").status)
  }

  @Test
  fun unknownFile_isNotFound() {
    assertEquals(404, request("/nope.mp4?t=${server.token}").status)
  }

  // ------------------------------------------------------------ 5. 可用性判不出来 → 不可用

  @Test
  fun unknownPathFromPieceSource_isTreatedUnavailable() {
    val other = File(root, "other.mp4")
    other.writeBytes(ByteArray(16 * 1024))
    val resp = request("/other.mp4?t=${server.token}", range = "bytes=0-4095")
    assertEquals(503, resp.status) // PieceSource 返回 null → 按不可用处理，绝不整文件可用
    assertEquals("1", resp.header("retry-after"))
  }

  // ------------------------------------------------------------ 6. S5 / S7

  /**
   * S5（2026-09-18）：读请求阶段必须有 socket 超时 —— 否则「连上不发请求」的
   * 僵尸连接会**永久占住线程**（固定线程池下几条即可冻结整个服务，
   * 且并发上限约束不到它们）。断言服务端会**主动关闭**该连接。
   */
  @Test
  fun zombieConnection_isReclaimedByReadTimeout() {
    val srv =
      StreamServer(roots = listOf(root), pieceSource = null,
                   readTimeoutMs = 300)
    srv.start()
    try {
      val sock = Socket("127.0.0.1", srv.port)
      sock.soTimeout = 5_000
      val t0 = System.currentTimeMillis()
      val data = try { sock.getInputStream().read() } catch (e: java.io.IOException) { -1 }
      val elapsed = System.currentTimeMillis() - t0
      sock.close()
      assertTrue(
        "僵尸连接应被服务端主动关闭（读到 EOF 或对端复位），实得 $data",
        data == -1,
      )
      assertTrue(
        "应在超时值附近回收，实测 ${elapsed}ms（readTimeoutMs=300）",
        elapsed < 3_000,
      )
    } finally {
      srv.close()
    }
  }

  /**
   * S5 的另一半：**写 body 期间必须取消超时** —— 客户端暂停读取超过
   * readTimeoutMs 时，body 不得被截断（否则播放器缓冲一下就断了）。
   */
  /** 全分块就绪的可用性源（按磁盘文件实际大小构造；不认识的路径返回 null）。 */
  private fun fullMapSource(): PieceSource =
    object : PieceSource {
      override fun pieceMap(diskPath: String): PieceMap? {
        val f = File(diskPath)
        if (!f.isFile) return null
        val pl = 4096
        val n = maxOf(1, ((f.length() + pl - 1) / pl).toInt())
        return PieceMap(pl, 0L, 0, n - 1, f.length()) { true }
      }

      override fun onDemand(diskPath: String, start: Long, endExcl: Long) {}
    }

  @Test
  fun pausedClient_doesNotTruncateBody() {
    val big = File(root, "big.bin")
    val total = 2 * 1024 * 1024
    big.writeBytes(ByteArray(total) { i -> ((i * 17 + 3) and 0xFF).toByte() })
    val srv =
      StreamServer(roots = listOf(root), pieceSource = fullMapSource(),
                   readTimeoutMs = 300)
    srv.start()
    try {
      val sock = Socket("127.0.0.1", srv.port)
      sock.soTimeout = 30_000
      sock.getOutputStream().write((
        "GET /big.bin?t=${srv.token} HTTP/1.1\r\n" +
          "Host: 127.0.0.1:${srv.port}\r\n" +
          "Range: bytes=0-${total - 1}\r\nConnection: close\r\n\r\n"
        ).toByteArray(Charsets.ISO_8859_1))
      var raw = ByteArray(0)
      var sep = indexOfHeaderEnd(raw)
      while (sep < 0) {
        val chunk = sock.getInputStream().readNBytes(1024)
        if (chunk.isEmpty()) break
        raw += chunk
        sep = indexOfHeaderEnd(raw)
      }
      require(sep > 0) { "响应没有 header 结束标记，原始字节数=${raw.size}" }
      val headText = String(raw, 0, sep, Charsets.ISO_8859_1)
      // 故意停读 3× 超时值 —— 若写 body 的超时没被取消，这里必然截断
      Thread.sleep(900)
      var body = raw.copyOfRange(sep + 4, raw.size)
      while (body.size < total) {
        val chunk = sock.getInputStream().readNBytes(65536)
        if (chunk.isEmpty()) break
        body += chunk
      }
      sock.close()
      assertEquals(206, headText.split("\r\n").first().split(" ")[1].toInt())
      assertEquals(
        "客户端停读 900ms 后 body 仍须完整（写 body 期间超时必须已取消）",
        total, body.size,
      )
      // 内容不得错位（生成式与写入端一致：byte i = (i*17+3) & 0xFF）
      assertEquals("首字节", 3, body[0].toInt())
      assertEquals("末字节", ((total - 1) * 17 + 3) and 0xFF,
                   body[total - 1].toInt() and 0xFF)
    } finally {
      srv.close()
      big.delete()
    }
  }

  /**
   * S7（2026-09-18）：多根目录必须**逐根解析**。
   *
   * 旧实现只用第一个根拼路径 —— `download_dir` 在缓存目录之外时，
   * 要么 403，要么**更糟**：第一个根下恰好有同名文件 → 校验全过，
   * 把另一个文件当成请求的资源发出去（内容错了但校验全过）。
   */
  @Test
  fun multiRoot_servesFileFromSecondRoot_andKeeps404() {
    val rootA = Files.createTempDirectory("mv-rootA").toFile()
    val rootB = Files.createTempDirectory("mv-rootB").toFile()
    val srv =
      StreamServer(roots = listOf(rootA, rootB), pieceSource = fullMapSource())
    srv.start()
    try {
      // 情形 1：文件只在第二个根下（旧实现 → 403/404）
      File(rootB, "only-b.bin").writeBytes(ByteArray(2048))
      val r1 = request("/only-b.bin?t=${srv.token}", srv = srv)
      assertEquals("第二个根下的文件必须能被服务（S7）", 200, r1.status)
      assertEquals(2048, r1.body.size)

      // 情形 2：两个根下都有同名文件 → 按 rootList 顺序取**第一个命中**的根。
      // 这里 rootA 在前，故返回 rootA 的内容；重点是「内容与所返回根一致」，
      // 绝不能把 A 的路径配 B 的内容（校验全过却串了文件）。
      File(rootA, "both.bin").writeBytes(ByteArray(1024) { 0x41 })
      File(rootB, "both.bin").writeBytes(ByteArray(1024) { 0x42 })
      val r2 = request("/both.bin?t=${srv.token}", srv = srv)
      assertEquals(200, r2.status)
      assertEquals(1024, r2.body.size)
      assertEquals("第一个根命中 → 应返回 rootA 的内容（顺序确定、不串内容）",
                   0x41.toByte(), r2.body[0])

      // 情形 3：两个根都不存在 → 仍是 404（不能误报成 403 越界）
      val r3 = request("/nope.bin?t=${srv.token}", srv = srv)
      assertEquals(404, r3.status)
    } finally {
      srv.close()
      rootA.deleteRecursively()
      rootB.deleteRecursively()
    }
  }

  private data class Resp(val status: Int, val headers: Map<String, String>, val body: ByteArray) {
    fun header(name: String): String? = headers[name.lowercase()]
  }

  private fun request(
    pathAndQuery: String,
    range: String? = null,
    host: String? = null,
    method: String = "GET",
    srv: StreamServer = server,
    readTimeoutMs: Int = 20_000,
  ): Resp {
    Socket("127.0.0.1", srv.port).use { s ->
      s.soTimeout = readTimeoutMs
      val sb = StringBuilder()
      sb.append("$method $pathAndQuery HTTP/1.1\r\n")
      sb.append("Host: ").append(host ?: "127.0.0.1:${srv.port}").append("\r\n")
      if (range != null) sb.append("Range: ").append(range).append("\r\n")
      sb.append("Connection: close\r\n\r\n")
      val out = s.getOutputStream()
      out.write(sb.toString().toByteArray(Charsets.ISO_8859_1))
      out.flush()
      // 请求被排队（未开始处理）时服务端不会回任何字节 → 读超时返回 status=0，
      // 而不是把测试挂死 20s。
      val raw =
        try {
          s.getInputStream().readBytes()
        } catch (e: java.net.SocketTimeoutException) {
          ByteArray(0)
        }
      if (raw.isEmpty()) return Resp(0, emptyMap(), ByteArray(0))
      return parse(raw)
    }
  }

  private fun parse(raw: ByteArray): Resp {
    val sep = indexOfHeaderEnd(raw)
    require(sep > 0) { "响应没有 header 结束标记，原始字节数=${raw.size}" }
    val head = String(raw, 0, sep, Charsets.ISO_8859_1)
    val lines = head.split("\r\n")
    val status = lines.first().split(" ")[1].toInt()
    val headers = HashMap<String, String>()
    for (line in lines.drop(1)) {
      val i = line.indexOf(':')
      if (i > 0) headers[line.substring(0, i).trim().lowercase()] = line.substring(i + 1).trim()
    }
    val body = raw.copyOfRange(sep + 4, raw.size)
    return Resp(status, headers, body)
  }

  private fun indexOfHeaderEnd(raw: ByteArray): Int {
    for (i in 0..(raw.size - 4)) {
      if (raw[i] == 13.toByte() && raw[i + 1] == 10.toByte() && raw[i + 2] == 13.toByte() && raw[i + 3] == 10.toByte()) return i
    }
    return -1
  }
}
