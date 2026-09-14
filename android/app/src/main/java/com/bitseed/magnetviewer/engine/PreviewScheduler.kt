package com.bitseed.magnetviewer.engine

import android.util.Log
import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.core.model.contiguousBytes
import com.bitseed.magnetviewer.core.sched.DEADLINE_STEP_MS
import com.bitseed.magnetviewer.core.sched.ascendingPlan
import com.bitseed.magnetviewer.core.sched.rangePlan
import com.bitseed.magnetviewer.core.sched.tailEntryPieces
import com.bitseed.magnetviewer.core.sched.tailPieceWindow
import com.bitseed.magnetviewer.core.sched.tailPlan
import com.bitseed.magnetviewer.core.sched.windowPieces
import org.libtorrent4j.Priority

/**
 * 预览调度器：桌面 `core/scheduler.py:PreviewScheduler` 的 Kotlin 移植。
 *
 * 调度参数全部走 `core.sched` 的纯函数（窗口按字节、尾部窗口收敛、deadline 递增），
 * 本类只负责「把计划真正下发给句柄」。句柄一律走 [PreviewHandleOps] 窄接口
 * （swig 的 `TorrentHandle` 造不进 JVM 单测；窄接口可以，于是暂停/继续/停止的
 * **调用序列**能被单测逐条断言）。所有句柄调用都在 try/catch 内，失败只记不抛
 * （与桌面 `log_warning` 语义一致：调度失败不该打断播放）。
 *
 * 三种生命周期操作与桌面 `stop(release_only=…)` 的对应：
 * - [pause]：**hold 档**——清 deadline + 文件优先级全 0 + 撤 auto_managed + pause；
 *   与桌面 `stop(release_only=False)` 的句柄动作逐条一致，区别只在于本移植保留
 *   句柄/文件引用与进度查询能力（否则暂停后 UI 的缓冲区读数会掉回 0，而实际
 *   已落盘数据一点没少）。[resume] 是它的逆操作。
 * - [stop]`(releaseOnly=false)`：hold 档的**完整**停止——执行 pause 的句柄动作后，
 *   再释放句柄/文件引用（调度器回到 inactive，与桌面一致）。
 * - [stop]`(releaseOnly=true)`：**convert 档**——只清 deadline + 还原锚点，
 *   不清优先级、不 pause、不撤 auto_managed；是否转正由宿主在锁外决策。
 *   本移植保留句柄/文件引用（[released] = true）以便继续供应分块可用性，
 *   但 tick/seek 不再自动补窗。
 */
class PreviewScheduler(private val log: (String) -> Unit = {}) {

  private var handle: PreviewHandleOps? = null
  private var file: TorrentFile? = null
  private var pieceLength: Int = 0
  private var scheduledTo: Int = -1
  private var playFrom: Int = -1
  private var headN: Int = 0
  private var tailPieces: List<Int> = emptyList()
  private var entryPieces: Set<Int> = emptySet()

  /** 预览已暂停（hold 档）：不再自动补窗，也不接受 seek；缓冲读数保持冻结。 */
  var paused: Boolean = false
    private set

  /**
   * 预览已**释放**（convert 档 stop）：只清 deadline/还原锚点，不 pause；
   * tick/seek 不再自动补窗（桌面此时由任务级路径接管），但分块可用性查询照旧。
   */
  var released: Boolean = false
    private set

  /**
   * **句柄有效性守卫**：失效句柄上的任何 swig 调用（status/prioritizeFiles/pause/…）
   * 都可能原生 SIGSEGV，`try/catch` 接不住。返回 null 表示「本调用必须放弃」。
   */
  private fun usable(h: PreviewHandleOps?, what: String): PreviewHandleOps? {
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
   * 开始（或恢复）预览指定文件：
   * 只保留目标文件优先级（其余置 `Priority.IGNORE`），解除 upload_mode、
   * 打开 auto_managed 并 resume，然后预约尾部索引窗口 + 顺序窗口。
   *
   * 优先级取 `Priority.DEFAULT`（= 4）——与桌面 `scheduler.begin` 里 `prio[idx] = 4`
   * 逐位对齐（libtorrent4j 的 4 就是 `Priority.DEFAULT`）。
   */
  fun begin(h: PreviewHandleOps, target: TorrentFile) {
    val guarded = usable(h, "begin") ?: throw IllegalStateException("句柄无效（isValid=false）")
    val pl = guarded.pieceLength()
    if (pl <= 0) throw IllegalStateException("块长非法：$pl")
    val n = guarded.numFiles()
    val prio = Priority.array(Priority.IGNORE, maxOf(1, n))
    if (target.index in 0 until prio.size) prio[target.index] = Priority.DEFAULT
    safe("begin.flags") { guarded.unsetUploadMode() }
    safe("begin.auto_managed") { guarded.setAutoManaged() }
    safe("begin.resume") { guarded.resume() }
    safe("begin.prioritize") { guarded.prioritizeFiles(prio) }

    handle = guarded
    file = target
    pieceLength = pl
    paused = false
    released = false
    scheduledTo = target.startPiece - 1
    playFrom = target.startPiece
    headN = windowPieces(pl)
    tailPieces = tailPieceWindow(target, pl)
    entryPieces = if (tailPieces.isNotEmpty()) tailEntryPieces(target, pl).toSet() else emptySet()
    logcat("begin: 文件 ${target.name}（${target.size} 字节）分块 ${target.startPiece}..${target.endPiece}，块长 $pl")
    requestTail()
    tick()
  }

  /**
   * **切换预览目标**（画廊翻页 / 点开另一张图时用）：清旧预约 → 重建窗口。
   *
   * 为什么要多这一步（桌面 `session.start_preview` 只是转发到 `scheduler.begin`）：换目标时
   * 旧文件那批 ASAP deadline（尾部索引块 + 顺序窗口）**仍然挂在句柄上**，会按最高优先级
   * 继续抢带宽，把新目标挤在后面——这正是桌面 `seek_to_byte` 里「必须先
   * `clear_piece_deadlines()` 再重建窗口」的原注释所描述的现象。目标一旦不同，先把本调度器
   * 名下所有预约清掉（旧的本地锚点/尾窗一并复位），再交给 [begin] 按新文件重建。
   *
   * **同一目标幂等**：与桌面 `main_window._on_gallery_file` 的
   * `if f is None or f == self._preview_file: return` 同款——同文件重复切是空操作，
   * 不重复下发优先级、不重建窗口。
   *
   * @return true = 目标已切换（或本来就是它）；false = 句柄不可用（什么都没做）
   */
  fun switchTarget(h: PreviewHandleOps, target: TorrentFile): Boolean {
    val cur = file
    if (cur != null && cur.index == target.index && cur.path == target.path) {
      logcat("switch: 同一目标（${target.name}），复用现有预约（不重建窗口）")
      return true
    }
    val guarded = usable(h, "switch.clear_old") ?: return false
    if (cur != null) {
      safe("switch.clear_old.deadlines") { guarded.clearPieceDeadlines() }
      logcat("switch: 已清旧目标 ${cur.name} 的全部 deadline（旧预约不再抢带宽）")
    }
    // 旧锚点/尾窗先复位（begin 会按新文件重建；不复位则残留旧文件的窗口判定）
    scheduledTo = -1
    playFrom = -1
    headN = 0
    tailPieces = emptyList()
    entryPieces = emptySet()
    paused = false
    released = false
    begin(guarded, target)
    logcat("switch: 预览目标已切到 ${target.name}（窗口已按新文件重建）")
    return true
  }

  /**
   * 暂停预览（**hold 档语义**，逐条对齐桌面 `stop(release_only=False)` 的句柄动作）：
   * 清全部 deadline → 文件优先级全 0 → 撤 `AUTO_MANAGED`（否则队列管理会自动 resume）
   * → `pause()`。
   *
   * 保留句柄/文件与进度查询：暂停后缓冲区读数应**冻结**而不是掉 0。
   */
  fun pause() {
    val h = usable(handle, "pause")
    if (h == null) {
      if (handle != null) logcat("pause: 句柄失效，只重置本地状态")
      resetLocal()
      return
    }
    safe("pause.clear_deadlines") { h.clearPieceDeadlines() }
    val n = usable(h, "pause.num_files")?.numFiles() ?: 0
    if (n > 0) safe("pause.prioritize") { h.prioritizeFiles(Priority.array(Priority.IGNORE, n)) }
    safe("pause.auto_managed") { h.unsetAutoManaged() }
    safe("pause.pause") { h.pause() }
    paused = true
    released = false
    logcat("pause: 已冻结（deadline 已清 / 优先级全 0 / 撤 auto_managed / pause）")
  }

  /**
   * 继续预览：把 [pause]（或 convert 档释放）撤掉——重放 `begin` 的句柄动作
   * （优先级、auto_managed、resume）并重建「尾部窗口 + 顺序窗口」。
   *
   * [fresh] 非空时替换当前句柄（宿主在句柄失效时重新 `find_torrent` 拿到的自有副本）。
   * 返回 true 表示已重新拉起。
   */
  fun resume(fresh: PreviewHandleOps? = null): Boolean {
    if (fresh != null) handle = fresh
    val h = usable(handle, "resume")
    val f = file
    if (h == null || f == null || pieceLength <= 0) {
      logcat("resume: 无可用预览任务（句柄/文件缺失），忽略")
      return false
    }
    val n = usable(h, "resume.num_files")?.numFiles() ?: 0
    val prio = Priority.array(Priority.IGNORE, maxOf(1, n))
    if (f.index in 0 until prio.size) prio[f.index] = Priority.DEFAULT
    safe("resume.flags") { h.unsetUploadMode() }
    safe("resume.auto_managed") { h.setAutoManaged() }
    safe("resume.resume") { h.resume() }
    safe("resume.prioritize") { h.prioritizeFiles(prio) }
    paused = false
    released = false
    scheduledTo = f.startPiece - 1
    playFrom = f.startPiece
    headN = windowPieces(pieceLength)
    tailPieces = tailPieceWindow(f, pieceLength)
    entryPieces = if (tailPieces.isNotEmpty()) tailEntryPieces(f, pieceLength).toSet() else emptySet()
    logcat("resume: 已恢复（优先级/auto_managed/resume + 重建窗口）")
    requestTail()
    tick()
    return true
  }

  /**
   * 停止预览（桌面 `stop(release_only=…)` 的移植）。
   *
   * - `releaseOnly = false`（hold 档／基线）：[pause] 的句柄动作之后再释放句柄/文件，
   *   调度器回到 inactive；
   * - `releaseOnly = true`（convert 档）：**只**清 deadline + 还原锚点——
   *   不清优先级、不 pause、不撤 auto_managed（[released] = true，tick/seek 停手，
   *   由调用方决定是否转正）。
   */
  fun stop(releaseOnly: Boolean = false) {
    val h = usable(handle, "stop")
    if (h != null) {
      safe("stop.clear_deadlines") { h.clearPieceDeadlines() }
      if (!releaseOnly) {
        val n = usable(h, "stop.num_files")?.numFiles() ?: 0
        if (n > 0) safe("stop.prioritize") { h.prioritizeFiles(Priority.array(Priority.IGNORE, n)) }
        safe("stop.auto_managed") { h.unsetAutoManaged() }
        safe("stop.pause") { h.pause() }
      }
      logcat(if (releaseOnly) "stop(release_only=true)：只清 deadline + 还原锚点" else "stop：冻结并释放")
    }
    // 锚点一律还原（桌面 stop 的两个分支都做）
    scheduledTo = -1
    playFrom = -1
    headN = 0
    paused = false
    if (releaseOnly) {
      // convert 档：保留句柄/文件（分块可用性查询、点播继续可用），只是不再自动补窗
      released = true
    } else {
      handle = null
      file = null
      pieceLength = 0
      tailPieces = emptyList()
      entryPieces = emptySet()
      released = false
    }
  }

  private fun resetLocal() {
    handle = null
    file = null
    pieceLength = 0
    scheduledTo = -1
    playFrom = -1
    headN = 0
    tailPieces = emptyList()
    entryPieces = emptySet()
    paused = false
    released = false
  }

  // ---------------------------------------------------------------- 预约

  /** 尾部索引窗口预约（幂等）：入口块提前，其余尾块排在头窗之后。 */
  private fun requestTail() {
    if (paused || released) return
    val h = usable(handle, "request_tail") ?: return
    if (tailPieces.isEmpty()) return
    for (d in tailPlan(headN, tailPieces, entryPieces, DEADLINE_STEP_MS)) {
      safe("request_tail piece=${d.piece}") { h.setPieceDeadline(d.piece, d.deadlineMs) }
    }
  }

  /**
   * 按字节区间即时点播（文件内相对偏移，[endByteExcl] 为开区间）。
   * 单次上限 60 块，**不移动顺序锚点**（点播是临时插队）。
   *
   * 暂停期间忽略（句柄已 pause，下 deadline 没有意义）；convert 档释放后仍生效
   * （文件正在全量缓存，点播只是加急）。
   */
  fun requestRange(startByte: Long, endByteExcl: Long) {
    if (paused) return
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
   *
   * 为什么必须先清（桌面 `scheduler.seek_to_byte` 的原注释）：拖到未缓存区再拖回
   * 已缓存区时，**残留的远端分块仍在以最高优先级并行下载**，与新播放位置争带宽 ——
   * 明明已缓存的区域反而卡顿。清完再重建，旧位置的预约才真正失效。
   * 尾部 moov 窗口同样被这次清理波及，所以重建顺序是「清 → 尾窗 → 新位置头窗」。
   */
  fun seekToByte(byteOffset: Long) {
    if (paused || released) {
      logcat("seek: 预览已暂停/已释放，忽略跳转（byteOffset=$byteOffset）")
      return
    }
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
    logcat("seek: 清旧预约后重建窗口 $piece..$target")
    tick()
  }

  /**
   * 周期调用：从**播放位置**起按下载进度向前滚动预约窗口；
   * 尾部窗口未齐时持续补拉（入口就绪不取消整尾窗）。
   */
  fun tick() {
    if (paused || released) return
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

  internal fun snapshot(): PieceAvailability? {
    val h = usable(handle, "snapshot") ?: return null
    return h.availability()
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
  }
}
