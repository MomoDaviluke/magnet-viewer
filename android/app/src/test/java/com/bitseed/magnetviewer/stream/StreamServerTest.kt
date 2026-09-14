package com.bitseed.magnetviewer.stream

import com.bitseed.magnetviewer.core.model.PieceMap
import java.io.File
import java.net.Socket
import java.nio.file.Files
import java.util.Collections
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * 流服务的 JVM 单测（真起 socket、真发 HTTP/1.1 请求）。
 *
 * 覆盖四件事（任务书要求）：
 * 1. 就绪不足 → 504，**绝不短写响应**；
 * 2. 就绪区间的字节与真实文件逐字节一致（206 + Content-Range 总长恒为逻辑大小）；
 * 3. 错 token / 错 Host → 403；
 * 4. 路径穿越 → 403。
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

  // ------------------------------------------------------------ 1. 就绪不足绝不短写

  @Test
  fun insufficientAvailability_returns504_andNeverShortWrites() {
    availablePieces = (0..1).toSet() // 只落了头 8192 字节，远不足请求的 32KB
    val resp = request("/video.mp4?t=${server.token}", range = "bytes=0-32767")
    assertEquals(504, resp.status)
    assertEquals("0", resp.header("content-length"))
    assertEquals(0, resp.body.size)
    assertTrue("等待期间应至少点播一次", demanded.isNotEmpty())
  }

  @Test
  fun partiallyAvailableRange_doesNotAnswerEarly() {
    // 请求 [4096, 16383)：仅前 2 块就绪 → 必须等（超时 504），绝不能把 [4096,8191) 当成整段发出去
    availablePieces = (0..1).toSet()
    val resp = request("/video.mp4?t=${server.token}", range = "bytes=4096-16383")
    assertEquals(504, resp.status)
    assertEquals(0, resp.body.size)
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
    assertEquals(504, resp.status) // PieceSource 返回 null → 按不可用处理，绝不整文件可用
  }

  // ------------------------------------------------------------ 工具

  private data class Resp(val status: Int, val headers: Map<String, String>, val body: ByteArray) {
    fun header(name: String): String? = headers[name.lowercase()]
  }

  private fun request(
    pathAndQuery: String,
    range: String? = null,
    host: String? = null,
    method: String = "GET",
    srv: StreamServer = server,
  ): Resp {
    Socket("127.0.0.1", srv.port).use { s ->
      s.soTimeout = 20_000
      val sb = StringBuilder()
      sb.append("$method $pathAndQuery HTTP/1.1\r\n")
      sb.append("Host: ").append(host ?: "127.0.0.1:${srv.port}").append("\r\n")
      if (range != null) sb.append("Range: ").append(range).append("\r\n")
      sb.append("Connection: close\r\n\r\n")
      val out = s.getOutputStream()
      out.write(sb.toString().toByteArray(Charsets.ISO_8859_1))
      out.flush()
      val raw = s.getInputStream().readBytes()
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
