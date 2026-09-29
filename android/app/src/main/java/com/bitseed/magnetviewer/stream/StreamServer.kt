package com.bitseed.magnetviewer.stream

import com.bitseed.magnetviewer.core.http.RangeSpec
import com.bitseed.magnetviewer.core.http.contentRangeHeader
import com.bitseed.magnetviewer.core.http.contentTypeFor
import com.bitseed.magnetviewer.core.http.parseRangeHeader
import com.bitseed.magnetviewer.core.http.unsatisfiedRangeHeader
import com.bitseed.magnetviewer.core.model.PieceMap
import com.bitseed.magnetviewer.core.model.rangeAvailableOf
import com.bitseed.magnetviewer.core.model.readyUntilOf
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
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ScheduledThreadPoolExecutor
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

/**
 * 只绑 127.0.0.1 的本地 HTTP 流服务：把（下载中的）文件按 Range 供给播放器。
 *
 * 设计要求（照搬桌面 `core/stream_server.py` 的语义 + 本工程的任务约束）：
 * 1. **鉴权**：URL 参数 token 常量时间比较 + Host 头白名单（127.0.0.1 / localhost / ::1），
 *    不符一律 403；
 * 2. **路径**：safeRelPath 净化 + 根目录包含性双重校验，越界 403；
 * 3. **Range**：循环点播 + 睡 [pollMs] 重查，最长 [waitTimeoutMs] 回 503。
 *    **「绝不短写」的准确含义（2026-09-18 澄清）**：绝不**谎报**长度 ——
 *    `Content-Length` 恒等于实际发送字节数。但**允许**返回比请求更短的
 *    **合法区间**（RFC 7233 §4.1 允许服务器返回请求区间的子集），这是
 *    「渐进服务」的前提：
 *    - 一旦就绪 `>= [progressiveMinBytes]` 立即回 206（就绪多少发多少）；
 *    - 就绪前缀连续 [STALL_POLLS] 轮不增长即提前交出（不等满超时）。
 *    旧实现把上面两条都当成禁忌，于是**闭区间与开区间都必须整段就绪**才应答，
 *    **等满 30s 才回 504** —— 而 ExoPlayer 当时用默认 ~8s 读超时，先于服务端放弃，
 *    结果是「seek 后必然播放失败」。桌面 `core/stream_server.py` 长期用同一套
 *    渐进机制服务 FFmpeg（`PROGRESSIVE_MIN=256KB` + `STALL_POLLS=10`），
 *    是本条改动的先例与依据。
 *    两端的等待预算已对齐成一条**单调链**（任一处倒挂都会退化成必然失败）：
 *    `停滞 ~0.5s < progressiveMin 观测 < 服务端等待 20s < 播放器读超时 25s`。
 *    注：ExoPlayer 读尽 `Content-Length` 后会从新位置**续发**下一个 Range 请求，
 *    因此短响应不会被当成媒体结束（`ProgressiveMediaPeriod` 会从
 *    `loadPosition` 继续加载）—— 真正会出事的是「声明长度 > 实际发送」。
 * 4. **可用性判不出来就按不可用处理**（找不到任务/句柄 → 等待 → 503），绝不当整文件可用；
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
  private val progressiveMinBytes: Long = DEFAULT_PROGRESSIVE_MIN_BYTES,
  maxConcurrency: Int = 8,
  private val queueCapacity: Int = DEFAULT_QUEUE_CAPACITY,
  private val readTimeoutMs: Int = READ_TIMEOUT_MS,
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

  /**
   * 有界线程池（S4，2026-09-18）。
   *
   * 旧实现是 `Executors.newFixedThreadPool`，其内部队列**无界**：8 个阻塞等待的
   * 请求就能占满线程，后续请求只是无限排队 —— 服务端「活着但不可用」，且
   * 没有任何错误信号（客户端只能干等到自己的读超时）。这与桌面
   * `_respond_503` 的「超限立即 503，绝不排队堆积」语义相反。
   *
   * 现在改成有界队列 + `AbortPolicy`：拒绝时由 accept 循环回 **503 +
   * `Retry-After`**（可重试），绝不静默断连。队列给 16 —— 播放器正常只会
   * 并发 1~3 个请求，留出足够吸收突发。
   */
  private val pool =
    ThreadPoolExecutor(
      maxOf(1, maxConcurrency),
      maxOf(1, maxConcurrency),
      0L,
      TimeUnit.MILLISECONDS,
      ArrayBlockingQueue(maxOf(1, queueCapacity)),
      ThreadPoolExecutor.AbortPolicy(),
    )
  private val running = AtomicBoolean(false)
  private var acceptThread: Thread? = null

  // ---- 写空闲看门狗（审计 2026-09-29 P2）--------------------------------
  // Java Socket 没有写超时 API（soTimeout 只管读）：对端停止读取（播放器
  // 暂停后半开 / 异常客户端）会让 write 永久阻塞在 TCP 发送缓冲满，固定
  // 线程池被逐步占满——S5 的读超时只护请求头阶段。唯一可靠的「打断阻塞
  // write」手段是从别处 close() 该 socket（write 抛 SocketException）。
  // 看门狗每 5s 扫描活跃写连接，超过 [WRITE_IDLE_TIMEOUT_MS] 没有成功写出
  // 任何字节即 close。正常播放（边播边缓冲）持续有写出，不会误伤；慢速
  // 网络下 60s 一个字节都写不出去本就等于链路死了。
  private val activeWrites = ConcurrentHashMap<Socket, AtomicLong>()
  private val writeWatchdog = ScheduledThreadPoolExecutor(1) { r ->
    Thread(r, "stream-write-watchdog").apply { isDaemon = true }
  }

  private fun sweepWrites() {
    val now = System.nanoTime()
    for ((s, last) in activeWrites) {
      val idleMs = (now - last.get()) / 1_000_000
      if (idleMs > WRITE_IDLE_TIMEOUT_MS) {
        log("写空闲超时（${idleMs}ms 无写出），断开连接防线程池占满")
        runCatching { s.close() }   // 阻塞中的 write 抛 SocketException → handle catch 收场
        activeWrites.remove(s)
      }
    }
  }

  /** write 成功后喂狗的流包装：所有响应写出（含 writeStatus / body 循环）统一计时。 */
  private class FeedDogOutputStream(
    private val inner: OutputStream,
    private val lastWriteNs: AtomicLong,
  ) : OutputStream() {
    override fun write(b: Int) {
      inner.write(b)
      lastWriteNs.set(System.nanoTime())
    }

    override fun write(b: ByteArray, off: Int, len: Int) {
      inner.write(b, off, len)
      lastWriteNs.set(System.nanoTime())
    }

    override fun flush() = inner.flush()
    override fun close() = inner.close()
  }

  val port: Int
    get() = server.localPort

  fun start() {
    if (!running.compareAndSet(false, true)) return
    writeWatchdog.scheduleWithFixedDelay(
      this::sweepWrites,
      WRITE_IDLE_TIMEOUT_MS,
      5_000L,
      TimeUnit.MILLISECONDS,
    )
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
          } catch (e: RejectedExecutionException) {
            // 过载：**明确回 503 + Retry-After**（与桌面 `_respond_503` 同语义）。
            // 绝不静默断连 —— 裸关连接会被 ExoPlayer 当成「流异常结束」，
            // 比一个可重试的状态码难排查得多（且它不会重试）。
            rejectOverloaded(socket)
          } catch (e: Throwable) {
            runCatching { socket.close() }
          }
        }
      }
  }

  /** 过载兜底应答：在 accept 线程里直接写一个最小的 503（不读请求体）。S8：X-Why=overloaded。 */
  private fun rejectOverloaded(socket: Socket) {
    runCatching {
      socket.use { s ->
        val out = BufferedOutputStream(s.getOutputStream(), 1024)
        writeStatus(
          out, 503, "Buffering",
          listOf("Retry-After" to "1", "X-Why" to "overloaded"),
        )
        out.flush()
      }
    }
  }

  override fun close() {
    running.set(false)
    runCatching { server.close() }
    pool.shutdownNow()
    writeWatchdog.shutdownNow()
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
        // S5（2026-09-18）：读请求阶段的 socket 超时 —— 防「连上却不发请求」的
        // 僵尸连接**永久占住线程**。固定线程池下 N 条这种连接即可冻结整个服务，
        // 且并发上限根本约束不到它们（限额是在请求处理里才抢的）。
        // 取值对齐桌面 `_StreamHandler.timeout = 15s`；测试可经构造参数调小。
        s.soTimeout = maxOf(1, readTimeoutMs)
        val input = s.getInputStream().buffered()
        val requestLine = readLine(input) ?: return
        val headers = readHeaders(input)
        // 请求读完 → **取消读超时**：写 body 期间播放器可能长时间暂停不读，
        // 带超时的 write 到点会把传输截断（与桌面 `_send_file` 同款处理）。
        // 但写阶段完全无超时的代价是：对端停止读取时 write 永久阻塞在 TCP
        // 发送缓冲满，固定线程池被逐步占满（soTimeout 管不了写）。改为
        // 「soTimeout=0 + 写空闲看门狗」：60s 无写出由 sweepWrites close 连接，
        // 阻塞中的 write 抛 SocketException 走下方 catch 收场。
        s.soTimeout = 0
        val lastWriteNs = AtomicLong(System.nanoTime())
        activeWrites[s] = lastWriteNs
        val out = FeedDogOutputStream(BufferedOutputStream(s.getOutputStream(), 64 * 1024), lastWriteNs)
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
      // 播放器中断请求属正常行为（连接被对端关闭/超时），静默即可。
      // 写空闲看门狗 close 连接引发的 SocketException 也走这里收场。
      log("连接异常（忽略）：${e.javaClass.simpleName}: ${e.message}")
    } finally {
      activeWrites.remove(socket)
    }
  }

  private fun serveFile(out: OutputStream, file: File, rangeHeader: String?, sendBody: Boolean) {
    val logical = file.length()
    when (val spec = parseRangeHeader(rangeHeader, logical)) {
      is RangeSpec.Unsatisfiable ->
        writeStatus(
          out, 416, "Range Not Satisfiable",
          listOf("Content-Range" to unsatisfiedRangeHeader(logical), "X-Why" to "range"),
        )
      is RangeSpec.None -> serveOpening(out, file, logical, sendBody)
      is RangeSpec.Bounded -> {
        val start = spec.start
        if (start >= logical) {
          writeStatus(
            out, 416, "Range Not Satisfiable",
            listOf("Content-Range" to unsatisfiedRangeHeader(logical), "X-Why" to "range"),
          )
          return
        }
        // 开区间（`bytes=a-`）必须自己定一个有界终点：无上界的请求永远等不到「全落盘」。
        var end = if (spec.openEnded) minOf(logical - 1, start + openEndedCapBytes - 1) else spec.endInclusive
        // 渐进服务：拿到「从 start 起真正就绪到哪」，而不是死等整段落盘。
        val ready = waitForReady(file, start, end + 1, logical)
        if (ready <= start) {
          writeNotReady(out, readyBytes = ready)
          return
        }
        if (ready >= end + 1) {
          // 目标区间已就绪：开区间可以顺带多发一些（`ready` 是以 logical 为界
          // 的连续前缀，这是既有语义，别退化）；闭区间必须停在请求上界。
          if (spec.openEnded) end = maxOf(end, minOf(logical - 1, ready - 1))
        } else {
          // 就绪不足：只回就绪部分 —— **合法短区间**，Content-Length 说实话。
          end = ready - 1
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
      writeNotReady(out)
      return
    }
    // 等「开播前缀目标」就绪；但返回值是以 logical 为界的**实际连续前缀**，
    // 因此可以直接拿它决定 200 / 206 —— 不需要再查一次可用性
    // （S6 后 pieceMapOf 挂增量视图：锚点推进摊销 O(1)，不再物化位图）。
    val want = minOf(logical, openingMinBytes)
    val ready = waitForReady(file, 0, want, logical)
    if (ready <= 0) {
      writeNotReady(out)
      return
    }
    if (ready >= logical) {
      writeFull(out, file, logical, sendBody)
    } else {
      writeRange(out, file, 0, ready - 1, logical, sendBody)
    }
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
   * 等待 `[start, endExcl)`，返回**从 start 起连续就绪的末端**（开区间）。
   *
   * 返回 `start` = 一个字节都没就绪（调用方回 503 让客户端稍后重试）。
   * 等待期间每 [demandRenewMs] 重新点播一次（幂等续期），每 [pollMs] 重查。
   *
   * 与旧版 `Boolean` 返回值的三点区别（对齐桌面 `_wait_ready`）：
   * 1. **渐进返回**：就绪 `>= [progressiveMinBytes]` 立即返回，不等整段 ——
   *    这是「seek 不再必然失败」的关键（旧版死等 1MB/整段，30s 才 504，
   *    而 ExoPlayer 默认读超时约 8s，先于服务端放弃）；
   * 2. **停滞提前返回**：就绪前缀连续 [STALL_POLLS] 轮不增长即交出
   *    （名义 10 × 50ms = 0.5s），不再空耗整个 [waitTimeoutMs]；
   * 3. 返回值让调用方可以**按就绪量回合法短区间**（Content-Length 说实话），
   *    而不是「要么整段、要么什么都不给」。
   */
  private fun waitForReady(file: File, start: Long, waitTargetExcl: Long, logical: Long): Long {
    val deadline = System.currentTimeMillis() + maxOf(0L, waitTimeoutMs)
    var nextDemand = 0L
    var lastReady = start
    var stall = 0
    while (true) {
      val pm = pieceMapOf(file)
      if (pm != null) {
        // S6：三个判定全走 `*_of` 派发——PieceMap 挂有增量视图时锚点推进
        // （摊销 O(1)、零位图物化）；无视图/不可判定逐字退回纯函数（语义冻结）。
        if (rangeAvailableOf(pm, start, waitTargetExcl)) {
          // 目标区间已就绪 —— 仍以 logical 为上界取「实际连续前缀」：
          // 开区间请求（`bytes=a-`）据此可以一次多发一些（少几轮往返），
          // 闭区间由调用方按请求上界夹回，不会越界。
          return readyUntilOf(pm, start, logical)
        }
        val ready = readyUntilOf(pm, start, logical)
        if (ready - start >= maxOf(1L, progressiveMinBytes)) return ready
        if (ready > start) {
          // 停滞检测：前缀不再增长（点播区间补完或补不动了）→ 提前把已就绪部分交出，
          // 避免空等整个超时周期。
          stall = if (ready == lastReady) stall + 1 else 0
          if (stall >= STALL_POLLS) return ready
          lastReady = ready
        }
      }
      val now = System.currentTimeMillis()
      if (now >= deadline) return lastReady
      if (now >= nextDemand) {
        try {
          pieceSource?.onDemand(file.path, start, waitTargetExcl)
        } catch (e: Throwable) {
          log("demand_cb 异常：${file.path} $start-$waitTargetExcl：${e.message}")
        }
        nextDemand = now + maxOf(1L, demandRenewMs)
      }
      try {
        Thread.sleep(maxOf(1L, pollMs))
      } catch (e: InterruptedException) {
        Thread.currentThread().interrupt()
        return lastReady
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

  /**
   * 数据未就绪（或等待超时）→ **503 + `Retry-After`**，与桌面 `_respond_503` 同语义。
   *
   * 为什么不是 504（2026-09-18 改）：本服务**没有上游网关**，不存在「网关超时」；
   * 语义上这里是「本端暂时还不能供数据，请稍后重试」。503 明确可重试，而 504 会把
   * 排查方向带偏到「网关」上—— 桌面侧对同一条件早就在用 503，这里只是对齐。
   *
   * S8 诊断头（2026-09-27，对齐桌面）：`X-Why` = no-data（数据未就绪，当前唯一取值），
   * `X-Ready-Bytes` = 从请求起点起连续就绪的字节数（对齐桌面 `_last_ready` 绝对口径）。
   * 自定义头播放器按 HTTP 规范忽略。
   */
  private fun writeNotReady(out: OutputStream, why: String = "no-data", readyBytes: Long = 0L) {
    writeStatus(
      out, 503, "Buffering",
      listOf(
        "Retry-After" to "1",
        "X-Why" to why,
        "X-Ready-Bytes" to readyBytes.toString(),
      ),
    )
  }

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

    val rawRel = decoded.trimStart('/')
    val safeRel = safeRelPath(rawRel)

    // S7（2026-09-18）：**逐根解析**，不再只用第一个根。
    //
    // 为什么必须改：`download_dir` 在缓存目录之外时（多根场景），旧实现只会去
    // 第一个根下拼路径。后果有两种 —— 要么 403（文件其实在第二个根下），
    // 要么**更糟**：第一个根下恰好存在同名文件 → `withinAnyRoot` 与 `isFile`
    // 全部通过，于是把**另一个文件**当成请求的资源发出去（内容错了但校验全过）。
    //
    // 命中顺序：优先返回「该根下真实存在」的那个；都不存在时回落**第一个根**
    // 的候选路径 —— 让调用方的 `isFile` 检查维持原有 404 语义（而不是把
    // 「文件不存在」误报成 403 越界）。
    var fallback: String? = null
    for (root in rootList) {
      val rawCanon =
        canonicalOrNull(File(root, rawRel.replace('/', File.separatorChar))) ?: continue
      if (!withinAnyRoot(rawCanon)) continue

      val safeCanon =
        canonicalOrNull(File(root, safeRel.replace('/', File.separatorChar))) ?: continue
      if (!withinAnyRoot(safeCanon)) continue
      if (safeCanon.isFile) return safeCanon.path
      if (fallback == null) fallback = safeCanon.path
    }
    return fallback
  }

  private fun canonicalOrNull(f: File): File? = try { f.canonicalFile } catch (e: IOException) { null }

  private fun withinAnyRoot(path: File): Boolean = rootList.any { root -> isWithin(root, path) }

  companion object {
    const val TOKEN_PARAM: String = "t"
    val ALLOWED_HOSTS: Set<String> = setOf("127.0.0.1", "localhost", "::1")
    /**
     * 单次请求的等待上限。**必须小于消费端的读超时**（否则客户端先放弃，
     * 服务端的等待与报错都白费）—— 2026-09-18 由 30s 收到 20s：
     * - 桌面同项即为 20s（`stream_server.py` 的 `wait_timeout`），两端对齐；
     * - 安卓播放器读超时设为 25s（`MagnetScreen.STREAM_READ_TIMEOUT_MS`）。
     *
     * 仍保留 20s 这么长的原因：**开播/seek 首块**在慢链路上确实可能要十几秒
     * （本机实测冷启动 DHT 爬升期），砍太短会把「慢」误判成「错」。
     * 真正让「等不出来」快速暴露的是**停滞提前返回**（约 0.5s），不是砍这个上限。
     */
    const val DEFAULT_WAIT_TIMEOUT_MS: Long = 20_000L
    /** 轮询间隔：与桌面 `sleep(0.05)` 一致。旧值 200ms 会让停滞判定退化成 1s+。 */
    const val DEFAULT_POLL_MS: Long = 50L
    const val DEFAULT_DEMAND_RENEW_MS: Long = 2_000L
    const val DEFAULT_OPEN_ENDED_CAP_BYTES: Long = 1L * 1024 * 1024
    const val DEFAULT_OPENING_MIN_BYTES: Long = 1L * 1024 * 1024
    /**
     * 渐进服务的就绪阈值（对齐桌面 `PROGRESSIVE_MIN=256KB`）：
     * 从请求起点起就绪这么多字节就立即应答，不再等整段落盘。
     */
    const val DEFAULT_PROGRESSIVE_MIN_BYTES: Long = 256L * 1024
    /**
     * 过载排队容量（S4）。播放器正常并发 1~3 个请求；16 用于吸收突发，
     * 超出即 503 让客户端重试，而不是无界排队。
     */
    const val DEFAULT_QUEUE_CAPACITY: Int = 16
    /**
     * 读请求阶段的 socket 超时（S5，2026-09-18，对齐桌面 `_StreamHandler.timeout = 15s`）。
     *
     * 防的是「连上却不发请求」的僵尸连接**永久占住线程** —— 固定线程池下
     * N 条这种连接即可冻结整个服务，且并发上限约束不到它们。
     *
     * **只作用于读请求阶段**：读完请求头立刻置 0（不设限），否则播放器
     * 暂停/缓冲时长时间不读，带超时的 write 会把传输截断。
     * 取 15s 与桌面同值：既要回收空闲连接，又不能太激进（播放器暂停后
     * 处于空闲 keep-alive 状态，过早断开会迫使它重连）。
     */
    const val READ_TIMEOUT_MS: Int = 15_000
    /**
     * 写空闲看门狗阈值（审计 2026-09-29）：超过这么久**一个字节都没写出**
     * 即判定对端死亡，close 连接放回线程。取 60s：慢速链路要放得过播放器
     * 短暂停顿（暂停期间 keep-alive 无写出也会被断，播放器会重连——与
     * 桌面空闲断连同代价），又不能让僵尸连接占线程太久。
     */
    const val WRITE_IDLE_TIMEOUT_MS: Long = 60_000L
    /**
     * 停滞判定轮数：就绪前缀连续这么多轮不再增长即提前交出（不等满超时）。
     * 名义 10 × [DEFAULT_POLL_MS] = **0.5s**（与桌面 `STALL_POLLS` 同值同义）。
     */
    const val STALL_POLLS: Int = 10
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
