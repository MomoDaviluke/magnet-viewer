package com.bitseed.magnetviewer.engine

import com.bitseed.magnetviewer.core.model.PieceMap
import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.data.AppLog
import org.libtorrent4j.AnnounceEntry
import org.libtorrent4j.PieceIndexBitfield
import org.libtorrent4j.Priority
import org.libtorrent4j.TorrentFlags
import org.libtorrent4j.TorrentHandle
import org.libtorrent4j.TorrentStatus

/**
 * 分块可用性查询（窄接口，JVM 替身可用）。
 */
interface PieceAvailability {
  fun have(piece: Int): Boolean

  /** 供流服务使用的「已下载分块」视图（status 被闭包持有 → 位图内存存活）。 */
  fun toPieceMap(file: TorrentFile, pieceLength: Int): PieceMap =
    PieceMap(pieceLength, file.offset, file.startPiece, file.endPiece, file.size) { p -> have(p) }
}

/**
 * 调度器所需的**句柄能力**窄接口（[HandleProbe] 的同族思路）。
 *
 * 为什么必须切这一刀：`TorrentHandle` 是 swig 类型，`new TorrentHandle(...)` 依赖
 * 原生库（类静态块 loadLibrary），**JVM 单测里造不出来**。调度器的全部逻辑
 * （deadline 排序、暂停/继续/停止的调用序列、无效句柄守卫）都只依赖这个接口，
 * 于是单测可以用纯 Kotlin 替身**逐条断言调用顺序**，例如「hold 档必须有 pause()、
 * convert 档必须没有」。
 *
 * 真实实现是 [TorrentHandleOps]；每个方法进入前都会先查 `isValid()`（swig 原生
 * 调用对失效句柄会 SIGSEGV，`try/catch` 接不住）。
 */
interface PreviewHandleOps {
  fun isValid(): Boolean

  /** `torrent_file().pieceLength()`；未知返回 0。 */
  fun pieceLength(): Int

  /** `torrent_file().numFiles()`；未知返回 0。 */
  fun numFiles(): Int

  /** `unsetFlags(TorrentFlags.UPLOAD_MODE)`：离开「只解析不下载」模式。 */
  fun unsetUploadMode()

  /** `setFlags(TorrentFlags.AUTO_MANAGED)`：交回队列管理。 */
  fun setAutoManaged()

  /** `unsetFlags(TorrentFlags.AUTO_MANAGED)`：撤掉队列管理（否则它会自动 resume）。 */
  fun unsetAutoManaged()

  fun resume()

  fun pause()

  /** `prioritizeFiles(priorities)`（数组长度应等于文件数；越界由调用方夹取）。 */
  fun prioritizeFiles(priorities: Array<Priority>)

  fun setPieceDeadline(piece: Int, deadlineMs: Int)

  fun clearPieceDeadlines()

  /** `status(QUERY_PIECES)` 的快照；不可判定返回 null（调用方按不可用处理）。 */
  fun availability(): PieceAvailability?

  /**
   * S6：种子总块数（未知返回 0）。增量视图用它判定规模；0 时调用方退回旧路径。
   * 默认 0 —— 既有替身零破坏（同 [pieceProbe] 的约定）。
   */
  fun numPieces(): Int = 0

  /**
   * S6：增量可用性视图的探针（默认 **null = 视图不可用**，调用方退回
   * [availability] 旧路径——「缓存只许加速，不许致败」）。
   * 默认 null 让既有测试替身零破坏；真实现见 [TorrentHandleOps.pieceProbe]。
   */
  fun pieceProbe(): PieceView.PieceProbe? = null

  /**
   * `TorrentHandle.setDownloadLimit(bytes)`（**字节/秒**，0 = 不限）：逐任务限速。
   *
   * 为什么需要它（I-01 的实测结论）：libtorrent 的**会话级** `download_rate_limit`
   * 只管远端 peer，本机/局域网/回环对端走 `local_download_rate_limit`（默认 0 = 不限），
   * 而 libtorrent4j 2.1.0-39 的 `settings_pack.int_types` 里没有这个常量（javap 复核），
   * 所以「限速对已运行的任务生效」只能靠逐任务这一条路。
   */
  fun setDownloadLimit(bytes: Int)
}

/**
 * 一次 `TorrentHandle.status(QUERY_PIECES)` 的位图快照（P2-1 口径：1 次绑定调用 +
 * Kotlin 侧 O(1) 索引，而不是逐块 `havePiece()`）。
 *
 * 位图不可用（`pieces()` 返回 0 指针 / 空位图）时退化为逐块 `havePiece()`；
 * 两者都判不出来一律返回 false（不可判定按**不可用**处理，绝不误判可读）。
 *
 * S6：同时充当增量视图的一次性**基线**（[PieceView.Baseline]）——
 * 视图只在初始化 / 失效重建时物化一次，之后不再走本快照。
 */
internal class TorrentPieceSnapshot(
  private val status: TorrentStatus?,
  private val handle: TorrentHandle?,
) : PieceAvailability, PieceView.Baseline {
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

  /** S6 基线口径：位图块数（不可用 = 0，视图按「规模未知」保守处理）。 */
  override val size: Int
    get() = if (hasBitfield) bitCount else 0

  override fun have(piece: Int): Boolean {
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
}

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
      logcat("snapshot: 句柄失效（isValid=false），跳过 status(QUERY_PIECES)")
      null
    } else {
      TorrentPieceSnapshot(h.status(TorrentHandle.QUERY_PIECES), h)
    }
  } catch (e: Throwable) {
    logcat("snapshot 异常：${e.javaClass.simpleName}: ${e.message}")
    null
  }

/**
 * S6：[PieceView.PieceProbe] 的真实现——视图的三个探针动作全部带
 * `isValid()` 守卫（失效句柄上的 swig 调用是原生崩溃，try/catch 接不住）。
 *
 * - [have]：`havePiece` 单查（JNI ~100ns，与块数无关——这是视图省掉
 *   `status(QUERY_PIECES)` 全量位图拷贝的关键）；
 * - [scalars]：无 flag 的 `status()` 标量（totalDone + state），失效自检的
 *   O(1) 电平/边沿判据来源；checking 由 State ∈ {CHECKING_FILES,
 *   CHECKING_RESUME_DATA} 映射（javap 复核：libtorrent4j 2.1.0-39 的
 *   State 枚举**没有** ALLOCATING，与桌面 Python 侧的集合差一项）；
 * - [baseline]：[snapshotOf] 一次性位图快照（只在视图初始化/失效重建时物化）。
 *
 * [identity] 用宿主 [TorrentHandleOps] 实例：句柄换代时 ops 会重建，
 * 身份随之变化 → 视图按 matches 判定重建，防串台。
 */
private class HandlePieceProbe(
  private val ops: TorrentHandleOps,
  private val handle: TorrentHandle,
) : PieceView.PieceProbe {
  override val identity: Any get() = ops

  override fun have(piece: Int): Boolean =
    try {
      if (!handle.isValid()) false else handle.havePiece(piece)
    } catch (e: Throwable) {
      false
    }

  override fun scalars(): PieceView.Scalars? =
    try {
      if (!handle.isValid()) {
        null
      } else {
        handle.status()?.let { st ->
          val s = st.state()
          PieceView.Scalars(
            st.totalDone(),
            checking = s == TorrentStatus.State.CHECKING_FILES ||
              s == TorrentStatus.State.CHECKING_RESUME_DATA,
          )
        }
      }
    } catch (e: Throwable) {
      null
    }

  override fun baseline(): PieceView.Baseline? = snapshotOf(handle)
}

/**
 * [PreviewHandleOps] 的真实实现：薄薄一层委托 + **每次调用前的 `isValid()` 守卫**。
 *
 * 之所以在适配层再守一次（调度器入口也守）：句柄可能在一次调用序列中途失效
 * （种子被移除），而失效句柄上的 swig 调用是原生崩溃，`try/catch` 接不住。
 */
class TorrentHandleOps(private val handle: TorrentHandle) : PreviewHandleOps, TrackerTarget {

  override fun isValid(): Boolean =
    try {
      handle.isValid()
    } catch (e: Throwable) {
      false
    }

  override fun pieceLength(): Int = guardedValue("pieceLength", 0) { it.torrentFile()?.pieceLength() ?: 0 }

  override fun numFiles(): Int = guardedValue("numFiles", 0) { it.torrentFile()?.numFiles() ?: 0 }

  override fun unsetUploadMode() = guarded("unsetUploadMode") { it.unsetFlags(TorrentFlags.UPLOAD_MODE) }

  override fun setAutoManaged() = guarded("setAutoManaged") { it.setFlags(TorrentFlags.AUTO_MANAGED) }

  override fun unsetAutoManaged() = guarded("unsetAutoManaged") { it.unsetFlags(TorrentFlags.AUTO_MANAGED) }

  override fun resume() = guarded("resume") { it.resume() }

  override fun pause() = guarded("pause") { it.pause() }

  override fun prioritizeFiles(priorities: Array<Priority>) = guarded("prioritizeFiles") { it.prioritizeFiles(priorities) }

  override fun setPieceDeadline(piece: Int, deadlineMs: Int) =
    guarded("setPieceDeadline($piece)") { it.setPieceDeadline(piece, deadlineMs) }

  override fun clearPieceDeadlines() = guarded("clearPieceDeadlines") { it.clearPieceDeadlines() }

  override fun availability(): PieceAvailability? = snapshotOf(handle)

  override fun numPieces(): Int = guardedValue("numPieces", 0) { it.torrentFile()?.numPieces() ?: 0 }

  override fun pieceProbe(): PieceView.PieceProbe? =
    if (isValid()) HandlePieceProbe(this, handle) else null

  override fun setDownloadLimit(bytes: Int) = guarded("setDownloadLimit($bytes)") { it.setDownloadLimit(bytes) }

  // ---- TrackerTarget（诊断 D-2026-09-18）：给已在跑的任务追加兜底 tracker ----
  // 用 addTracker（追加）而不是 replaceTrackers（整体替换）——后者会把磁力链自带的
  // tr= 一并抹掉，那是净损失。详见 TrackerPush.kt 的口径说明。

  override fun trackerUrls(): List<String> =
    guardedValue("trackerUrls", emptyList()) { h -> h.trackers().map { it.url() } }

  override fun addTracker(url: String) =
    guarded("addTracker($url)") { it.addTracker(AnnounceEntry(url)) }

  private inline fun guarded(what: String, body: (TorrentHandle) -> Unit) {
    try {
      if (!handle.isValid()) {
        logcat("$what: 句柄失效（isValid=false），跳过本次句柄调用")
        return
      }
      body(handle)
    } catch (e: Throwable) {
      logcat("$what: ${e.javaClass.simpleName}: ${e.message}")
    }
  }

  private inline fun <T> guardedValue(what: String, fallback: T, body: (TorrentHandle) -> T): T =
    try {
      if (!handle.isValid()) {
        logcat("$what: 句柄失效（isValid=false），跳过本次句柄调用")
        fallback
      } else {
        body(handle)
      }
    } catch (e: Throwable) {
      logcat("$what: ${e.javaClass.simpleName}: ${e.message}")
      fallback
    }
}

private const val TAG = "MagnetViewer"

private fun logcat(message: String) = AppLog.log(message)
