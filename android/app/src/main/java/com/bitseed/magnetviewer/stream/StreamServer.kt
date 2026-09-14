package com.bitseed.magnetviewer.stream

import com.bitseed.magnetviewer.core.http.RangeSpec
import com.bitseed.magnetviewer.core.http.contentRangeHeader
import com.bitseed.magnetviewer.core.http.contentTypeFor
import com.bitseed.magnetviewer.core.http.parseRangeHeader
import com.bitseed.magnetviewer.core.http.unsatisfiedRangeHeader
import com.bitseed.magnetviewer.core.model.PieceMap
import com.bitseed.magnetviewer.core.model.contiguousBytes
import com.bitseed.magnetviewer.core.model.rangeAvailable
import com.bitseed.magnetviewer.core.model.readyUntil
import com.bitseed.magnetviewer.core.model.safeRelPath
import java.io.BufferedOutputStream
import java.io.Closeable
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.io.RandomAccessFile
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.URLDecoder
import java.security.MessageDigest
import java.security.SecureRandom
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * 只绑 127.0.0.1 的本地 HTTP 流服务：把（下载中的）文件按 Range 供给播放器。
 *
 * 设计要求（照搬桌面 `core/stream_server.py` 的语义 + 本工程的任务约束）：
 * 1. **鉴权**：URL 参数 token 常量时间比较 + Host 头白名单（127.0.0.1 / localhost / ::1），
 *    不符一律 403；
 * 2. **路径**：safeRelPath 净化 + 根目录包含性双重校验，越界 403；
 * 3. **Range**：`bytes=a-b` 阻塞等到 [a,b] 全落盘再回 206（循环点播 + 睡 [pollMs] 重查，
 *    最长 [waitTimeoutMs] 超时回 504）；**绝不短写响应**（Content-Length 恒等于实际发送
 *    字节数；万一落盘数据不足，直接断连而不是干净收尾——短响应会被 ExoPlayer 当流结束）；
 * 4. **可用性判不出来就按不可用处理**（找不到任务/句柄 → 等待 → 504），绝不当整文件可用；
 * 5. Content-Range 的总长恒为**逻辑文件大小**，并带 `Cache-Control: no-store`
 *    与 `X-Content-Type-Options: nosniff`。
 *
 * 纯 `java.net` 实现（无 Android / libtorrent 依赖），因此能在 JVM 单测里真跑。
 */
class StreamServer(
  roots: List<File>,
  private val pieceSource: PieceSource?,
  val token: String = newToken(),
  private val waitTimeoutMs: Long = DEFAULT_WAIT_TIMEOUT_MS,
  private val pollMs: Long = DEFAULT_POLL_MS,
  private val demandRenewMs: Long = DEFAULT_DEMAND_RENEW_MS,
  private val openEndedCapBytes: Long = DEFAULT_OPEN_ENDED_CAP_BYTES,
  private val openingMinBytes: Long = DEFAULT_OPENING_MIN_BYTES,
  maxConcurrency: Int = 8,
  private val log: (String) -> Unit = {},
) : Closeable {

  private val rootList: List<File> =
    roots.map { r ->
      try {
        r.canonicalFile
      } catch (e: IOException) {
        r.absoluteFile
      }
    }
  private val server: ServerSocket = ServerSocket(0, 32, InetAddress.getByName("127.0.0.1"))
  private val pool = Executors.newFixedThreadPool(maxOf(1, maxConcurrency))
  private val running = AtomicBoolean(false)
  private var acceptThread: Thread? = null

  val port: Int
    get() = server.localPort

  fun start() {
    if (!running.compareAndSet(false, true)) return
    acceptThread =
      thread(name = "stream-accept", isDaemon = true) {
        while (running.get()) {
          val socket =
            try {
              server.accept()
            } catch (e: IOException) {
              break
            }
          try {
            pool.execute { handle(socket) }
          } catch (e: Throwable) {
            runCatching { socket.close() }
          }
        }
      }
  }

  override fun close() {
    running.set(false)
    runCatching { server.close() }
    pool.shutdownNow()
    acceptThread?.interrupt()
  }

  /** 播放器 / 客户端用的本地 URL（带鉴权 token）。 */
  fun urlFor(relPath: String): String {
    val path = relPath.replace('\\', '/').trimStart('/')
    return "http://127.0.0.1:$port/${encodePath(path)}?$TOKEN_PARAM=$token"
  }

  // ---------------------------------------------------------------- 请求处理

  private fun handle(socket: Socket) {
    try {
      socket.use { s ->
        s.tcpNoDelay = true
        val input = s.getInputStream().buffered()
        val requestLine = readLine(input) ?: return
        val headers = readHeaders(input)
        val out = BufferedOutputStream(s.getOutputStream(), 64 * 1024)
        val parts = requestLine.trim().split(" ")
        if (parts.size < 2) {
          writeStatus(out, 400, "Bad Request")
          return
        }
        val method = parts[0].uppercase(Locale.ROOT)
        val target = parts[1]
        if (method != "GET" && method != "HEAD") {
          writeStatus(out, 405, "Method Not Allowed")
          return
        }
        if (!authorized(target, headers)) {
          writeStatus(out, 403, "Forbidden")
          return
        }
        val resolved = resolvePath(target)
        if (resolved == null) {
          writeStatus(out, 403, "Forbidden")
          return
        }
        val file = File(resolved)
        if (!file.isFile) {
          writeStatus(out, 404, "Not Found")
          return
        }
        serveFile(out, file, headers["range"], sendBody = method == "GET")
      }
    } catch (e: Throwable) {
      // 播放器中断请求属正常行为（连接被对端关闭/超时），静默即可
      log("连接异常（忽略）：${e.javaClass.simpleName}: ${e.message}")
    }
  }

  private fun serveFile(out: OutputStream, file: File, rangeHeader: String?, sendBody: Boolean) {
    val logical = file.length()
    when (val spec = parseRangeHeader(rangeHeader, logical)) {
      is RangeSpec.Unsatisfiable -> writeStatus(out, 416, "Range Not Satisfiable", listOf("Content-Range" to unsatisfiedRangeHeader(logical)))
      is RangeSpec.None -> serveOpening(out, file, logical, sendBody)
      is RangeSpec.Bounded -> {
        val start = spec.start
        if (start >= logical) {
          writeStatus(out, 416, "Range Not Satisfiable", listOf("Content-Range" to unsatisfiedRangeHeader(logical)))
          return
        }
        // 开区间（`bytes=a-`）必须自己定一个有界终点：无上界的请求永远等不到「全落盘」。
        // 先等 [start, start+cap) 落盘，再把「已经连续就绪」的部分一并发出（不多等、不短写）。
        var end = if (spec.openEnded) minOf(logical - 1, start + openEndedCapBytes - 1) else spec.endInclusive
        if (!waitForRange(file, start, end + 1)) {
          writeStatus(out, 504, "Gateway Timeout")
          return
        }
        if (spec.openEnded && end < logical - 1) {
          val ready = readyUntilCached(file, start, logical)
          if (ready - 1 > end) end = minOf(logical - 1, ready - 1)
        }
        writeRange(out, file, start, end, logical, sendBody)
      }
    }
  }

  /**
   * 无 Range 请求（播放器探测/首段读取）：等一个小的开播前缀就绪后，
   * 回**有界**的 206（Content-Range 总长仍是逻辑大小），绝不短写、绝不用 200 谎报整文件。
   */
  private fun serveOpening(out: OutputStream, file: File, logical: Long, sendBody: Boolean) {
    if (logical <= 0) {
      writeStatus(out, 504, "Gateway Timeout")
      return
    }
    val want = minOf(logical, openingMinBytes)
    if (want > 0 && !waitForRange(file, 0, want)) {
      writeStatus(out, 504, "Gateway Timeout")
      return
    }
    val contiguous = contiguousFromZero(file, logical)
    if (contiguous <= 0) {
      writeStatus(out, 504, "Gateway Timeout")
      return
    }
    if (contiguous >= logical) {
      writeFull(out, file, logical, sendBody)
    } else {
      writeRange(out, file, 0, contiguous - 1, logical, sendBody)
    }
  }

  /** 从文件头起连续就绪的字节数；判不出来按 0（不可用）。 */
  private fun contiguousFromZero(file: File, logical: Long): Long {
    val pm = pieceMapOf(file) ?: return 0
    return minOf(logical, contiguousBytes(pm))
  }

  /** 从 [start] 起连续就绪的末端（开区间）；判不出来按 [start]（不可用）。 */
  private fun readyUntilCached(file: File, start: Long, logical: Long): Long {
    val pm = pieceMapOf(file) ?: return start
    return readyUntil(pm, start, logical)
  }

  private fun pieceMapOf(file: File): PieceMap? {
    val src = pieceSource ?: return null
    return try {
      src.pieceMap(file.path)
    } catch (e: Throwable) {
      log("pieces_cb 异常（按不可用处理）：${file.path}: ${e.message}")
      null
    }
  }

  /**
   * 阻塞等待 `[start, endExcl)` 全部落盘。等待期间每 [demandRenewMs] 重新点播一次
   * （幂等续期），每 [pollMs] 重查；超过 [waitTimeoutMs] 返回 false（调用方回 504）。
   */
  private fun waitForRange(file: File, start: Long, endExcl: Long): Boolean {
    if (endExcl <= start) return true
    val deadline = System.currentTimeMillis() + maxOf(0L, waitTimeoutMs)
    var nextDemand = 0L
    while (true) {
      val pm = pieceMapOf(file)
      if (pm != null && rangeAvailable(pm, start, endExcl)) return true
      val now = System.currentTimeMillis()
      if (now >= deadline) return false
      if (now >= nextDemand) {
        try {
          pieceSource?.onDemand(file.path, start, endExcl)
        } catch (e: Throwable) {
          log("demand_cb 异常：${file.path} $start-$endExcl：${e.message}")
        }
        nextDemand = now + maxOf(1L, demandRenewMs)
      }
      try {
        Thread.sleep(maxOf(1L, pollMs))
      } catch (e: InterruptedException) {
        Thread.currentThread().interrupt()
        return false
      }
    }
  }

  // ---------------------------------------------------------------- 响应

  private fun commonHeaders(): List<Pair<String, String>> =
    listOf(
      "Accept-Ranges" to "bytes",
      "Cache-Control" to "no-store",
      "X-Content-Type-Options" to "nosniff",
      "Connection" to "close",
    )

  private fun writeStatus(out: OutputStream, status: Int, reason: String, extra: List<Pair<String, String>> = emptyList()) {
    val headers = commonHeaders() + extra + listOf("Content-Length" to "0")
    writeHead(out, status, reason, headers)
  }

  private fun writeHead(out: OutputStream, status: Int, reason: String, headers: List<Pair<String, String>>) {
    val sb = StringBuilder(256)
    sb.append("HTTP/1.1 ").append(status).append(' ').append(reason).append("\r\n")
    for ((k, v) in headers) sb.append(k).append(": ").append(v).append("\r\n")
    sb.append("\r\n")
    out.write(sb.toString().toByteArray(Charsets.ISO_8859_1))
    out.flush()
  }

  private fun writeFull(out: OutputStream, file: File, logical: Long, sendBody: Boolean) {
    val headers =
      commonHeaders() +
        listOf(
          "Content-Type" to contentTypeFor(file.name),
          "Content-Length" to logical.toString(),
        )
    writeHead(out, 200, "OK", headers)
    if (sendBody) sendFileBody(out, file, 0, logical)
  }

  private fun writeRange(out: OutputStream, file: File, start: Long, endInclusive: Long, logical: Long, sendBody: Boolean) {
    val length = endInclusive - start + 1
    val headers =
      commonHeaders() +
        listOf(
          "Content-Type" to contentTypeFor(file.name),
          "Content-Length" to length.toString(),
          "Content-Range" to contentRangeHeader(start, endInclusive, logical),
        )
    writeHead(out, 206, "Partial Content", headers)
    if (sendBody) sendFileBody(out, file, start, length)
  }

  /**
   * 发送文件体：**必须**恰好发出 [length] 字节。
   *
   * 少一个字节都不能干净收尾：短响应会被 ExoPlayer 判为「流结束」（画面停在半路），
   * 因此这里直接把连接断掉（调用方 catch 后关闭 socket），让播放器看到的是
   * 网络错误而不是正常 EOF。
   */
  private fun sendFileBody(out: OutputStream, file: File, start: Long, length: Long): Boolean {
    var remaining = length
    try {
      RandomAccessFile(file, "r").use { raf ->
        raf.seek(start)
        val buf = ByteArray(CHUNK)
        while (remaining > 0) {
          val want = minOf(buf.size.toLong(), remaining).toInt()
          val n = raf.read(buf, 0, want)
          if (n <= 0) break
          out.write(buf, 0, n)
          remaining -= n.toLong()
        }
        out.flush()
      }
    } catch (e: IOException) {
      log("发送文件体失败：${file.path} @$start+$length：${e.message}")
      return false
    }
    if (remaining > 0L) {
      log("磁盘数据不足 ${file.path} @$start+$length（还差 $remaining 字节）：立即断连，绝不短写")
      return false
    }
    return true
  }

  // ---------------------------------------------------------------- 鉴权 / 路径

  private fun authorized(target: String, headers: Map<String, String>): Boolean {
    val got = queryParam(target, TOKEN_PARAM)
    if (!constantTimeEquals(got, token)) return false
    val host = headers["host"] ?: return false
    return hostAllowed(host)
  }

  /**
   * 路径解析：先按**未净化**路径做根目录包含性校验（`..`/绝对路径越界 → null → 403），
   * 再走 safeRelPath 净化后二次校验（双重校验，对齐任务约束）。
   */
  internal fun resolvePath(target: String): String? {
    val rawPath = target.substringBefore('?').substringBefore('#')
    if (rawPath.isEmpty()) return null
    val decoded =
      try {
        URLDecoder.decode(rawPath, "UTF-8")
      } catch (e: Throwable) {
        return null
      }
    if (decoded.indexOf('\u0000') >= 0) return null
    val root = rootList.firstOrNull() ?: return null

    val rawRel = decoded.trimStart('/')
    val rawCanon = canonicalOrNull(File(root, rawRel.replace('/', File.separatorChar))) ?: return null
    if (!withinAnyRoot(rawCanon)) return null

    val safeRel = safeRelPath(rawRel)
    val safeCanon = canonicalOrNull(File(root, safeRel.replace('/', File.separatorChar))) ?: return null
    if (!withinAnyRoot(safeCanon)) return null
    return safeCanon.path
  }

  private fun canonicalOrNull(f: File): File? = try { f.canonicalFile } catch (e: IOException) { null }

  private fun withinAnyRoot(path: File): Boolean = rootList.any { root -> isWithin(root, path) }

  companion object {
    const val TOKEN_PARAM: String = "t"
    val ALLOWED_HOSTS: Set<String> = setOf("127.0.0.1", "localhost", "::1")
    const val DEFAULT_WAIT_TIMEOUT_MS: Long = 30_000L
    const val DEFAULT_POLL_MS: Long = 200L
    const val DEFAULT_DEMAND_RENEW_MS: Long = 2_000L
    const val DEFAULT_OPEN_ENDED_CAP_BYTES: Long = 1L * 1024 * 1024
    const val DEFAULT_OPENING_MIN_BYTES: Long = 1L * 1024 * 1024
    private const val CHUNK = 256 * 1024

    /** 每会话随机 token（URL 鉴权参数，防 DNS rebinding / 同机进程直连）。 */
    fun newToken(): String {
      val b = ByteArray(16)
      SecureRandom().nextBytes(b)
      val sb = StringBuilder(b.size * 2)
      for (x in b) sb.append(String.format(Locale.ROOT, "%02x", x))
      return sb.toString()
    }

    /** 严格位于 root 之内（前缀比较补一个分隔符，避免 `cache` 与 `cache_evil` 混淆）。 */
    fun isWithin(root: File, path: File): Boolean {
      val r = try { root.canonicalPath } catch (e: IOException) { root.absolutePath }
      val p = try { path.canonicalPath } catch (e: IOException) { path.absolutePath }
      if (p == r) return true
      val sep = File.separator
      return p.startsWith(if (r.endsWith(sep)) r else r + sep)
    }

    /** Host 头白名单判定：解析掉端口（兼容 `[::1]:8080` 这种写法）。 */
    fun hostAllowed(hostHeader: String): Boolean {
      val h = hostHeader.trim()
      if (h.isEmpty()) return false
      val name =
        if (h.startsWith("[")) {
          h.substringAfter('[').substringBefore(']')
        } else {
          val i = h.lastIndexOf(':')
          if (i >= 0) h.substring(0, i) else h
        }
      return name.trim().lowercase(Locale.ROOT) in ALLOWED_HOSTS
    }

    /** 常量时间比较（长度不同也走 isEqual 的固定路径）。 */
    fun constantTimeEquals(a: String, b: String): Boolean {
      if (b.isEmpty()) return false
      return MessageDigest.isEqual(a.toByteArray(Charsets.UTF_8), b.toByteArray(Charsets.UTF_8))
    }

    internal fun queryParam(target: String, name: String): String {
      val q = target.substringAfter('?', "")
      if (q.isEmpty()) return ""
      for (kv in q.split('&')) {
        val i = kv.indexOf('=')
        if (i <= 0) continue
        if (kv.substring(0, i) == name) {
          val v = kv.substring(i + 1)
          return try { URLDecoder.decode(v, "UTF-8") } catch (e: Throwable) { v }
        }
      }
      return ""
    }

    private fun encodePath(rel: String): String {
      val sb = StringBuilder(rel.length)
      for (ch in rel) {
        val ok = ch.isLetterOrDigit() || ch == '/' || ch == '-' || ch == '_' || ch == '.' || ch == '~'
        if (ok) sb.append(ch) else sb.append(String.format(Locale.ROOT, "%%%02X", ch.code))
      }
      return sb.toString()
    }

    private const val MAX_LINE = 8 * 1024

    private fun readLine(input: InputStream): String? {
      val sb = StringBuilder(64)
      while (true) {
        val c = input.read()
        if (c < 0) return if (sb.isEmpty()) null else sb.toString()
        if (c == '\n'.code) return sb.toString().trimEnd('\r')
        if (sb.length >= MAX_LINE) return sb.toString()
        sb.append(c.toChar())
      }
    }

    private fun readHeaders(input: InputStream): Map<String, String> {
      val out = HashMap<String, String>()
      while (true) {
        val line = readLine(input) ?: break
        if (line.isEmpty()) break
        val i = line.indexOf(':')
        if (i <= 0) continue
        out[line.substring(0, i).trim().lowercase(Locale.ROOT)] = line.substring(i + 1).trim()
      }
      return out
    }
  }
}
