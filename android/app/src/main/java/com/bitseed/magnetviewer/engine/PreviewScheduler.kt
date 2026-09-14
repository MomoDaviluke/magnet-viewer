package com.bitseed.magnetviewer.engine

import android.util.Log
import com.bitseed.magnetviewer.core.model.PieceMap
import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.core.model.contiguousBytes
import com.bitseed.magnetviewer.core.sched.DEADLINE_STEP_MS
import com.bitseed.magnetviewer.core.sched.ascendingPlan
import com.bitseed.magnetviewer.core.sched.rangePlan
import com.bitseed.magnetviewer.core.sched.tailEntryPieces
import com.bitseed.magnetviewer.core.sched.tailPieceWindow
import com.bitseed.magnetviewer.core.sched.tailPlan
import com.bitseed.magnetviewer.core.sched.windowPieces
import org.libtorrent4j.PieceIndexBitfield
import org.libtorrent4j.Priority
import org.libtorrent4j.TorrentFlags
import org.libtorrent4j.TorrentHandle
import org.libtorrent4j.TorrentStatus

/**
 * 一次 `TorrentHandle.status(QUERY_PIECES)` 的位图快照（P2-1 口径：1 次绑定调用 +
 * Kotlin 侧 O(1) 索引，而不是逐块 `havePiece()`）。
 *
 * 位图不可用（`pieces()` 返回 0 指针 / 空位图）时退化为逐块 `havePiece()`；
 * 两者都判不出来一律返回 false（不可判定按**不可用**处理，绝不误判可读）。
 */
internal class TorrentPieceSnapshot(
  private val status: TorrentStatus?,
  private val handle: TorrentHandle?,
) {
  private val bits: PieceIndexBitfield? =
    try {
      status?.pieces()
    } catch (e: Throwable) {
      null
    }

  private val bitCount: Int =
    try {
      bits?.size() ?: 0
    } catch (e: Throwable) {
      0
    }

  val hasBitfield: Boolean
    get() = bits != null && bitCount > 0

  fun have(piece: Int): Boolean {
    if (piece < 0) return false
    val b = bits
    if (hasBitfield && b != null) {
      return try {
        if (piece >= bitCount) false else b.getBit(piece)
      } catch (e: Throwable) {
        false
      }
    }
    val h = handle ?: return false
    return try {
      // 位图不可用时才逐块问句柄：同样必须先判 isValid（失效句柄 = 原生崩溃）
      if (!h.isValid()) false else h.havePiece(piece)
    } catch (e: Throwable) {
      false
    }
  }

  /** 供流服务使用的「已下载分块」视图（status 被闭包持有 → 位图内存存活）。 */
  fun toPieceMap(file: TorrentFile, pieceLength: Int): PieceMap =
    PieceMap(pieceLength, file.offset, file.startPiece, file.endPiece, file.size) { p -> have(p) }
}

/**
 * 预览调度器：桌面 `core/scheduler.py:PreviewScheduler` 的 Kotlin 移植。
 *
 * 调度参数全部走 `core.sched` 的纯函数（窗口按字节、尾部窗口收敛、deadline 递增），
 * 本类只负责「把计划真正下发给句柄」。所有 libtorrent4j 调用都在 try/catch 内，
 * 失败只记不抛（与桌面 `log_warning` 语义一致：调度失败不该打断播放）。
 */
class PreviewScheduler(private val log: (String) -> Unit = {}) {

  private var handle: TorrentHandle? = null
  private var file: TorrentFile? = null
  private var pieceLength: Int = 0
  private var scheduledTo: Int = -1
  private var playFrom: Int = -1
  private var headN: Int = 0
  private var tailPieces: List<Int> = emptyList()
  private var entryPieces: Set<Int> = emptySet()

  /**
   * **句柄有效性守卫**：失效句柄上的任何 swig 调用（status/prioritizeFiles/pause/…）
   * 都可能原生 SIGSEGV，`try/catch` 接不住。返回 null 表示「本调用必须放弃」。
   */
  private fun usable(h: TorrentHandle?, what: String): TorrentHandle? {
    if (h == null) return null
    val ok =
      try {
        h.isValid()
      } catch (e: Throwable) {
        logcat("$what: 句柄有效性检查异常 " + e.javaClass.simpleName)
        false
      }
    if (!ok) {
      logcat("$what: 句柄失效（isValid=false），跳过本次句柄调用")
      return null
    }
    return h
  }

  /** 调度器日志：既回给宿主 UI，也打 logcat（tag=MagnetViewer）。 */
  private fun logcat(message: String) {
    try {
      Log.i(TAG, message)
    } catch (e: Throwable) {
      // android.util.Log 在 JVM 单测里未 mock：绝不能因此崩溃
    }
    try {
      log(message)
    } catch (e: Throwable) {
      // 宿主回调异常不影响调度
    }
  }

  val active: Boolean
    get() = handle != null

  val previewFile: TorrentFile?
    get() = file

  val previewPieceLength: Int
    get() = pieceLength

  // ---------------------------------------------------------------- 生命周期

  /**
   * 开始预览指定文件：
   * 只保留目标文件优先级（其余置 `Priority.IGNORE`），解除 upload_mode、
   * 打开 auto_managed 并 resume，然后预约尾部索引窗口 + 顺序窗口。
   *
   * 优先级取 `Priority.DEFAULT`（= 4）——与桌面 `scheduler.begin` 里 `prio[idx] = 4`
   * 逐位对齐（libtorrent4j 的 4 就是 `Priority.DEFAULT`）。
   */
  fun begin(h: TorrentHandle, target: TorrentFile) {
    val guarded = usable(h, "begin") ?: throw IllegalStateException("句柄无效（isValid=false）")
    val ti = guarded.torrentFile() ?: throw IllegalStateException("元数据尚未就绪")
    val pl = ti.pieceLength()
    if (pl <= 0) throw IllegalStateException("块长非法：$pl")
    val n = ti.numFiles()
    val prio = Priority.array(Priority.IGNORE, maxOf(1, n))
    if (target.index in 0 until prio.size) prio[target.index] = Priority.DEFAULT
    safe("begin.flags") { guarded.unsetFlags(TorrentFlags.UPLOAD_MODE) }
    safe("begin.auto_managed") { guarded.setFlags(TorrentFlags.AUTO_MANAGED) }
    safe("begin.resume") { guarded.resume() }
    safe("begin.prioritize") { guarded.prioritizeFiles(prio) }

    handle = guarded
    file = target
    pieceLength = pl
    scheduledTo = target.startPiece - 1
    playFrom = target.startPiece
    headN = windowPieces(pl)
    tailPieces = tailPieceWindow(target, pl)
    entryPieces = if (tailPieces.isNotEmpty()) tailEntryPieces(target, pl).toSet() else emptySet()
    logcat("begin: 文件 ${target.name}（${target.size} 字节）分块 ${target.startPiece}..${target.endPiece}，块长 $pl")
    requestTail()
    tick()
  }

  /** 取消预览：清空全部 deadline，并按桌面 hold 档把全部文件优先级置 0 + 暂停。 */
  fun stop() {
    val h = usable(handle, "stop")
    if (h != null) {
      safe("stop.clear_deadlines") { h.clearPieceDeadlines() }
      // 桌面 stop(release_only=False)：全部优先级置 0、撤 auto_managed、pause
      // —— 停止预览后不在后台继续拉这个文件的分块（再次预览时 begin() 会重新拉起）。
      safe("stop.prioritize") {
        val n = if (usable(h, "stop.torrent_file") != null) h.torrentFile()?.numFiles() ?: 0 else 0
        if (n > 0) h.prioritizeFiles(Priority.array(Priority.IGNORE, n))
      }
      safe("stop.auto_managed") { h.unsetFlags(TorrentFlags.AUTO_MANAGED) }
      safe("stop.pause") { h.pause() }
    }
    handle = null
    file = null
    pieceLength = 0
    scheduledTo = -1
    playFrom = -1
    headN = 0
    tailPieces = emptyList()
    entryPieces = emptySet()
  }

  // ---------------------------------------------------------------- 预约

  /** 尾部索引窗口预约（幂等）：入口块提前，其余尾块排在头窗之后。 */
  private fun requestTail() {
    val h = usable(handle, "request_tail") ?: return
    if (tailPieces.isEmpty()) return
    for (d in tailPlan(headN, tailPieces, entryPieces, DEADLINE_STEP_MS)) {
      safe("request_tail piece=${d.piece}") { h.setPieceDeadline(d.piece, d.deadlineMs) }
    }
  }

  /**
   * 按字节区间即时点播（文件内相对偏移，[endByteExcl] 为开区间）。
   * 单次上限 60 块，**不移动顺序锚点**（点播是临时插队）。
   */
  fun requestRange(startByte: Long, endByteExcl: Long) {
    val h = usable(handle, "request_range") ?: return
    val f = file ?: return
    if (pieceLength <= 0) return
    for (d in rangePlan(f, pieceLength, startByte, endByteExcl, DEADLINE_STEP_MS)) {
      safe("request_range piece=${d.piece}") { h.setPieceDeadline(d.piece, d.deadlineMs) }
    }
  }

  /**
   * 跳转：先 `clearPieceDeadlines()` 清掉旧位置的 ASAP 预约，再重建
   * 「尾部窗口 + 新位置窗口」（窗口块数走 `windowPieces`，deadline 从新位置起递增）。
   */
  fun seekToByte(byteOffset: Long) {
    val h = usable(handle, "seek") ?: return
    val f = file ?: return
    if (pieceLength <= 0) return
    val piece = (f.startPiece + (byteOffset / pieceLength).toInt()).coerceIn(f.startPiece, f.endPiece)
    safe("seek.clear") { h.clearPieceDeadlines() }
    playFrom = piece
    scheduledTo = piece - 1
    headN = windowPieces(pieceLength)
    requestTail()
    val target = minOf(f.endPiece, piece + headN - 1)
    for (d in ascendingPlan(piece, target, anchor = piece, stepMs = DEADLINE_STEP_MS)) {
      safe("seek.deadline piece=${d.piece}") { h.setPieceDeadline(d.piece, d.deadlineMs) }
    }
    scheduledTo = maxOf(scheduledTo, target - 1)
    tick()
  }

  /**
   * 周期调用：从**播放位置**起按下载进度向前滚动预约窗口；
   * 尾部窗口未齐时持续补拉（入口就绪不取消整尾窗）。
   */
  fun tick() {
    val h = usable(handle, "tick") ?: return
    val f = file ?: return
    if (pieceLength <= 0) return
    if (tailPieces.isNotEmpty() && !tailReady()) requestTail()
    val n = if (headN > 0) headN else windowPieces(pieceLength)
    val firstMissing = firstMissingFrom(playFrom, f)
    if (firstMissing <= scheduledTo - 16) return // 窗口仍充裕
    val start = maxOf(firstMissing, scheduledTo + 1)
    val target = minOf(f.endPiece, firstMissing + n - 1)
    for (p in start..target) {
      safe("tick.deadline piece=$p") { h.setPieceDeadline(p, (p - playFrom) * DEADLINE_STEP_MS) }
    }
    scheduledTo = maxOf(scheduledTo, target - 1)
  }

  // ---------------------------------------------------------------- 状态查询

  internal fun snapshot(): TorrentPieceSnapshot? {
    val h = usable(handle, "snapshot") ?: return null
    return snapshotOf(h)
  }

  /** 尾部索引窗口是否已全部落盘（无窗口时恒为 true）。 */
  fun tailReady(): Boolean {
    val f = file ?: return true
    if (tailPieces.isEmpty()) return true
    val snap = snapshot() ?: return false
    return tailPieces.all { p -> snap.have(p) }
  }

  /** 尾部**入口**是否就绪（开播门控判据：只等文件最后 min(2MB, size) 覆盖的块）。 */
  fun tailEntryReady(): Boolean {
    if (entryPieces.isEmpty()) return true
    val snap = snapshot() ?: return false
    return entryPieces.all { p -> snap.have(p) }
  }

  /** 从文件头开始的连续可读字节数（播放器真正能消费的量）。 */
  fun contiguousProgress(): Long {
    val f = file ?: return 0
    val snap = snapshot() ?: return 0
    return contiguousBytes(snap.toPieceMap(f, pieceLength))
  }

  /** 当前预览文件的缓冲比例 0~1（按连续可读前缀计）。 */
  fun bufferProgress(): Double {
    val f = file ?: return 0.0
    if (f.size <= 0) return 0.0
    return minOf(1.0, contiguousProgress().toDouble() / f.size.toDouble())
  }

  // ---------------------------------------------------------------- 内部

  private fun firstMissingFrom(from: Int, f: TorrentFile): Int {
    val snap = snapshot() ?: return maxOf(from, f.startPiece)
    var p = maxOf(from, f.startPiece)
    while (p <= f.endPiece && snap.have(p)) p++
    return minOf(p, f.endPiece)
  }

  private inline fun safe(what: String, body: () -> Unit) {
    try {
      body()
    } catch (e: Throwable) {
      logcat("$what: ${e.javaClass.simpleName}: ${e.message}")
    }
  }

  companion object {
    private const val TAG = "MagnetViewer"

    /**
     * `TorrentHandle.status(QUERY_PIECES)` 快照（要拿最新分块位图必须带这个 flag）。
     *
     * **先判 `isValid()`**：失效句柄上的 `status()` 会在 swig 层解引用空 shared_ptr →
     * SIGSEGV，`try/catch` 拦不住（本次 P0 的第二个调用点）。
     */
    internal fun snapshotOf(h: TorrentHandle): TorrentPieceSnapshot? =
      try {
        val ok =
          try {
            h.isValid()
          } catch (e: Throwable) {
            false
          }
        if (!ok) {
          try {
            Log.i(TAG, "snapshot: 句柄失效（isValid=false），跳过 status(QUERY_PIECES)")
          } catch (e: Throwable) {
            // ignore
          }
          null
        } else {
          TorrentPieceSnapshot(h.status(TorrentHandle.QUERY_PIECES), h)
        }
      } catch (e: Throwable) {
        try {
          Log.i(TAG, "snapshot 异常：${e.javaClass.simpleName}: ${e.message}")
        } catch (e2: Throwable) {
          // ignore
        }
        null
      }
  }
}
