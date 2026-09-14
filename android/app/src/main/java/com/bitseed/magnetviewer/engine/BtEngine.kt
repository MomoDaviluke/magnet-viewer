package com.bitseed.magnetviewer.engine

import android.util.Log
import com.bitseed.magnetviewer.core.model.ParseResult
import com.bitseed.magnetviewer.core.model.PieceMap
import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.core.model.safeRelPath
import com.bitseed.magnetviewer.core.states.BOOTSTRAP_TRACKERS
import com.bitseed.magnetviewer.stream.PieceSource
import java.io.File
import org.libtorrent4j.AddTorrentParams
import org.libtorrent4j.AlertListener
import org.libtorrent4j.ErrorCode
import org.libtorrent4j.Priority
import org.libtorrent4j.SessionHandle
import org.libtorrent4j.SessionManager
import org.libtorrent4j.SessionParams
import org.libtorrent4j.SettingsPack
import org.libtorrent4j.Sha1Hash
import org.libtorrent4j.TorrentFlags
import org.libtorrent4j.TorrentHandle
import org.libtorrent4j.TorrentInfo
import org.libtorrent4j.alerts.AddTorrentAlert
import org.libtorrent4j.alerts.Alert
import org.libtorrent4j.alerts.AlertType
import org.libtorrent4j.alerts.MetadataFailedAlert
import org.libtorrent4j.alerts.MetadataReceivedAlert
import org.libtorrent4j.alerts.TorrentErrorAlert

/**
 * 一次轮询的状态快照（宿主只碰普通类型）。句柄/会话失效时由宿主停止轮询并显示 [error]。
 */
data class PollStatus(
  val line: String = "",
  /** 句柄已失效（isValid=false）：必须停止轮询。 */
  val handleInvalid: Boolean = false,
  /** 会话已停止：必须停止轮询。 */
  val sessionStopped: Boolean = false,
  val error: String? = null,
)

/**
 * 一个 info_hash 的会话持有者：磁力链解析（DHT 拿元数据，**不下载本体**）+
 * 单文件预览调度 + 流服务的分块可用性来源。
 *
 * 约束：**所有 libtorrent4j 调用都在本包内**，UI/Compose 只碰 [BtEngine] 的
 * 普通类型（String/Long/Boolean/ParseResult）。
 *
 * **句柄纪律（本文件的核心不变量）**：
 * 1. 任何 `TorrentHandle.xxx()` 之前先 `isValid()`——`status()` 等 swig 调用对失效句柄
 *    会原生 SIGSEGV，`try/catch` 接不住（见 [HandleAccess]）。
 * 2. **绝不缓存告警里拿到的句柄**：`TorrentAlert.handle()` 返回的是 `swigCMemOwn=false`
 *    的**非持有视图**（指针指向告警对象内部的 `torrent_handle` 成员）；告警一旦被释放
 *    （GC/delete），这个句柄就是悬垂指针，下次 `status()` 直接崩。
 *    正确做法：在告警回调内读信息，然后按 info_hash 从会话**重新取一个自有副本**
 *    （`SessionManager.find` → `find_torrent`，swig 侧 `new torrent_handle(ptr, true)`）。
 */
class BtEngine(private val listener: Listener) {

  /** 引擎 → 宿主（ViewModel）的事件回调；回调可能来自 libtorrent 告警线程。 */
  interface Listener {
    /** 元数据就绪：清单已解析、句柄已暂停（不下载本体）。 */
    fun onMetadataReady(infoHash: String, result: ParseResult)

    /** 元数据阶段失败（含 add 失败 / DHT 失败）。 */
    fun onMetadataError(message: String)

    /** 一般性状态文本（中文，UI 直接显示）。 */
    fun onStatus(message: String)
  }

  private val manager = SessionManager()
  private val scheduler = PreviewScheduler { log -> this.log("调度：" + log) }

  @Volatile private var started = false
  @Volatile private var currentHandle: TorrentHandle? = null
  @Volatile private var currentResult: ParseResult? = null
  @Volatile private var currentInfoHash: String = ""
  @Volatile private var saveDir: File? = null
  @Volatile private var previewFile: TorrentFile? = null

  /** 统一日志（tag = MagnetViewer）：下一次出问题能靠 `logcat | grep MagnetViewer` 定位。 */
  private fun log(message: String) {
    try {
      Log.i(TAG, message)
    } catch (e: Throwable) {
      // 日志本身绝不能成为崩溃点（例如类被单测加载时 android.util.Log 未 mock）
    }
  }

  private val alertListener =
    object : AlertListener {
      override fun types(): IntArray =
        intArrayOf(
          AlertType.METADATA_RECEIVED.swig(),
          AlertType.METADATA_FAILED.swig(),
          AlertType.ADD_TORRENT.swig(),
          AlertType.TORRENT_ERROR.swig(),
          AlertType.ALERTS_DROPPED.swig(),
        )

      override fun alert(alert: Alert<*>?) {
        val a = alert ?: return
        try {
          onAlert(a)
        } catch (e: Throwable) {
          val msg = "告警处理异常：" + (e.message ?: e.javaClass.simpleName)
          log(msg)
          listener.onStatus(msg)
        }
      }
    }

  // ---------------------------------------------------------------- 会话生命周期

  /**
   * 启动 DHT 会话（幂等）。失败返回 false（错误已通过 [Listener.onMetadataError] 上报）。
   *
   * 会话是**引擎级单例**（`manager` 字段），全程只 start 一次、不在轮询期间 stop；
   * 只有 [shutdown] 会停它（调用方必须保证此后不再读句柄）。
   */
  @Synchronized
  fun ensureStarted(): Boolean {
    if (started && isRunning()) {
      log("会话已在运行，复用（监听 $LISTEN_INTERFACES）")
      return true
    }
    if (started && !isRunning()) {
      // 「一边轮询一边被停」的现场：会话已不在运行，先把所有句柄引用清掉再重建
      log("会话标记为已启动但实际未运行（可能被 stop 过）：清空句柄后重新启动")
      currentHandle = null
      currentResult = null
      started = false
    }
    log("会话启动中（监听 $LISTEN_INTERFACES，DHT/LSD 开启）…")
    return try {
      val settings = SettingsPack.defaultSettings()
      settings.setEnableDht(true)
      settings.setEnableLsd(true)
      // UPnP / NAT-PMP 没有专用 setter（NOTES-libtorrent4j §6.8）：本应用只「收」不做种
      settings.setBoolean(org.libtorrent4j.swig.settings_pack.bool_types.enable_upnp.swigValue(), false)
      settings.setBoolean(org.libtorrent4j.swig.settings_pack.bool_types.enable_natpmp.swigValue(), false)
      settings.listenInterfaces(LISTEN_INTERFACES)
      settings.connectionsLimit(300)
      settings.alertQueueSize(5000)
      settings.setMaxMetadataSize(MAX_METADATA_SIZE)
      settings.activeDownloads(3)
      manager.addListener(alertListener)
      manager.start(SessionParams(settings))
      started = true
      log("会话启动完成（监听 $LISTEN_INTERFACES，isRunning=" + isRunning() + "）")
      listener.onStatus("DHT 会话已启动（监听 $LISTEN_INTERFACES）")
      true
    } catch (e: Throwable) {
      log("会话启动失败（" + (e.message ?: e.javaClass.simpleName) + "）：回退随机端口重试")
      // 端口被占用等情况：回退随机端口再试一次（与桌面 session 回退同型）
      try {
        val settings = SettingsPack.defaultSettings()
        settings.setEnableDht(true)
        settings.setEnableLsd(true)
        settings.setBoolean(org.libtorrent4j.swig.settings_pack.bool_types.enable_upnp.swigValue(), false)
        settings.setBoolean(org.libtorrent4j.swig.settings_pack.bool_types.enable_natpmp.swigValue(), false)
        settings.listenInterfaces("0.0.0.0:0")
        settings.connectionsLimit(300)
        settings.setMaxMetadataSize(MAX_METADATA_SIZE)
        manager.addListener(alertListener)
        manager.start(SessionParams(settings))
        started = true
        log("会话启动完成（随机端口回退，isRunning=" + isRunning() + "）")
        listener.onStatus("DHT 会话已启动（随机端口回退）")
        true
      } catch (e2: Throwable) {
        log("会话启动失败（回退也失败）：" + (e2.message ?: e2.javaClass.simpleName))
        listener.onMetadataError("DHT 会话启动失败：" + (e2.message ?: e2.javaClass.simpleName))
        false
      }
    }
  }

  fun isRunning(): Boolean =
    try {
      manager.isRunning()
    } catch (e: Throwable) {
      log("会话状态查询异常：" + (e.message ?: e.javaClass.simpleName))
      false
    }

  /** 关闭会话（阻塞，务必在 IO 线程调用）。 */
  fun shutdown() {
    log("会话关闭中…")
    try {
      scheduler.stop()
    } catch (e: Throwable) {
      log("调度器停止异常：" + (e.message ?: e.javaClass.simpleName))
    }
    try {
      if (started) {
        try {
          manager.removeListener(alertListener)
        } catch (e: Throwable) {
          log("移除告警监听异常：" + (e.message ?: e.javaClass.simpleName))
        }
        manager.stop()
      }
    } catch (e: Throwable) {
      log("会话关闭异常：" + (e.message ?: e.javaClass.simpleName))
      listener.onStatus("会话关闭异常：" + (e.message ?: e.javaClass.simpleName))
    } finally {
      started = false
      currentHandle = null
      currentResult = null
      previewFile = null
      log("会话已关闭（句柄引用已清空）")
    }
  }

  // ---------------------------------------------------------------- 解析（只拿元数据）

  /**
   * 把磁力链接加入会话，仅通过 DHT/BEP-9 取元数据（upload_mode + DEFAULT_DONT_DOWNLOAD，
   * 且在元数据到达后把所有文件优先级置 0 并暂停——**不下载本体**）。
   *
   * @return 落盘目录（`<cacheRoot>/.preview/<infohash>`）；失败返回 null。
   */
  fun addMagnet(uri: String, cacheRoot: File): File? {
    if (!ensureStarted()) {
      log("加种中止：会话不可用")
      return null
    }
    val atp =
      try {
        AddTorrentParams.parseMagnetUri(uri)
      } catch (e: Throwable) {
        log("磁力链接解析失败：" + (e.message ?: e.javaClass.simpleName))
        listener.onMetadataError("磁力链接无效：" + (e.message ?: e.javaClass.simpleName))
        return null
      }
    val ih = infoHashOf(atp)
    val dir = previewDir(cacheRoot, ih)
    // 落盘目录必须在 addTorrent **之前**建好：目录不存在时加种会失败
    if (!dir.exists() && !dir.mkdirs()) {
      log("加种中止：无法创建落盘目录 " + dir.path)
      listener.onMetadataError("无法创建落盘目录：" + dir.path)
      return null
    }
    return try {
      atp.setSavePath(dir.path)
      if (atp.getTrackers().isNullOrEmpty()) atp.setTrackers(BOOTSTRAP_TRACKERS)
      // 只解析不下载：upload_mode + 「所有文件默认 dont_download」
      atp.setFlags(TorrentFlags.UPLOAD_MODE.or_(TorrentFlags.DEFAULT_DONT_DOWNLOAD))
      log("加种开始：infohash=${ih.ifEmpty { "?" }} 保存目录=${dir.path} peer提示数=${atp.peers()?.size ?: 0}")
      val ec = ErrorCode(org.libtorrent4j.swig.error_code())
      val h = SessionHandle(manager.swig()).addTorrent(atp, ec)
      log(
        "加种结果：isError=${ec.isError} message=${ec.getMessage()} " +
          "handle=${if (h == null) "null" else "isValid=" + h.isValid()}"
      )
      if (ec.isError) {
        listener.onMetadataError("加入 DHT 会话失败：" + ec.getMessage())
        return null
      }
      if (h == null || !h.isValid()) {
        log("加种自检失败：句柄无效，不写入 currentHandle（避免轮询到无效句柄）")
        listener.onMetadataError("加入 DHT 会话失败：句柄无效")
        return null
      }
      currentHandle = h
      currentResult = null
      currentInfoHash = ih
      saveDir = dir
      previewFile = null
      // 重复解析同一磁力链：libtorrent 返回既有句柄，可能不会再发 metadata_received
      val already = try {
        if (h.isValid()) h.torrentFile() else null
      } catch (e: Throwable) {
        log("既有元数据查询异常：" + (e.message ?: e.javaClass.simpleName))
        null
      }
      if (already != null) onMetadataReceived(h) else listener.onStatus("正在通过 DHT 获取元数据…")
      dir
    } catch (e: Throwable) {
      log("加种异常：" + (e.message ?: e.javaClass.simpleName))
      listener.onMetadataError("加入 DHT 会话失败：" + (e.message ?: e.javaClass.simpleName))
      null
    }
  }

  /** `parse_magnet_uri` 结果里的 info_hash（未 add 之前即可用；拿不到返回 ""）。 */
  private fun infoHashOf(atp: AddTorrentParams): String =
    try {
      val ih = atp.getInfoHashes()
      val sha = if (ih.hasV1()) ih.getV1() else ih.getBest()
      sha?.toString()?.trim()?.lowercase().orEmpty()
    } catch (e: Throwable) {
      log("info_hash 解析异常：" + (e.message ?: e.javaClass.simpleName))
      ""
    }

  private fun previewDir(cacheRoot: File, infoHash: String): File {
    val sub = if (infoHash.isEmpty()) ".preview/tmp" else ".preview/" + safeRelPath(infoHash)
    return File(cacheRoot, sub)
  }

  /**
   * 从会话**重新取一个自有句柄**（`find_torrent` → swig `new torrent_handle(ptr, true)`）。
   *
   * 这是本次 P0 的根因修复：告警里给的句柄是非持有的内部视图，缓存它 = 埋一个悬垂指针。
   *
   * [keys] 是候选 info_hash（按优先级）：先用磁力链里的 v1 btih（libtorrent 会话注册表的
   * 主键），再用句柄自报的哈希兜底（混合种子会自报 v2 截断哈希，见 NOTES-libtorrent4j §0）。
   */
  private fun reacquireHandle(keys: List<String>): TorrentHandle? {
    if (!isRunning()) {
      log("重新获取句柄中止：会话未运行")
      return null
    }
    val candidates = keys.map { it.trim().lowercase() }.filter { it.length == SHA1_HEX_LEN }.distinct()
    if (candidates.isEmpty()) {
      log("重新获取句柄中止：没有可用的 40 位 v1 info_hash（keys=$keys）")
      return null
    }
    for (k in candidates) {
      try {
        val h = manager.find(Sha1Hash.parseHex(k))
        if (h != null && h.isValid()) {
          log("重新获取句柄：成功（key=$k，共 ${candidates.size} 个候选）")
          return h
        }
        log("重新获取句柄：key=$k 未命中（会话里没有该种子）")
      } catch (e: Throwable) {
        log("重新获取句柄异常（key=$k）：" + (e.message ?: e.javaClass.simpleName))
      }
    }
    log("重新获取句柄失败：候选全部未命中（$candidates）")
    return null
  }

  // ---------------------------------------------------------------- 告警

  private fun onAlert(a: Alert<*>) {
    when (a.type()) {
      AlertType.METADATA_RECEIVED -> {
        val h = (a as? MetadataReceivedAlert)?.handle() ?: return
        onMetadataReceived(h)
      }
      AlertType.METADATA_FAILED -> {
        val err = (a as? MetadataFailedAlert)?.getError()
        val msg = "元数据获取失败：" + (err?.getMessage() ?: "未知原因")
        log(msg)
        listener.onMetadataError(msg)
      }
      AlertType.ADD_TORRENT -> {
        val err = (a as? AddTorrentAlert)?.error()
        if (err != null && err.isError) {
          log("加种告警失败：" + err.getMessage())
          listener.onMetadataError("加入会话失败：" + err.getMessage())
        } else {
          log("加种告警：成功")
        }
      }
      AlertType.TORRENT_ERROR -> {
        val err = (a as? TorrentErrorAlert)?.error()
        if (err != null && err.isError) {
          log("种子错误告警：" + err.getMessage())
          listener.onStatus("种子错误：" + err.getMessage())
        }
      }
      else -> {
        // 其余告警不参与本 demo 的状态机
      }
    }
  }

  /**
   * 元数据到达：解析清单 → 所有文件优先级置 0 + 暂停（不下载本体）→ 通知宿主。
   *
   * 注意 [alertHandle] 是**告警内部视图**（swigCMemOwn=false）：只在本次回调内使用，
   * 之后一律换成 [reacquireHandle] 拿到的自有句柄（否则下一个轮询就是 UAF）。
   */
  @Synchronized
  private fun onMetadataReceived(alertHandle: TorrentHandle) {
    val valid = try {
      alertHandle.isValid()
    } catch (e: Throwable) {
      log("元数据句柄有效性检查异常：" + (e.message ?: e.javaClass.simpleName))
      false
    }
    if (!valid) {
      log("元数据到位但告警句柄无效：放弃本次处理")
      listener.onMetadataError("元数据处理失败：告警句柄无效")
      return
    }
    val ti = try {
      alertHandle.torrentFile()
    } catch (e: Throwable) {
      log("torrent_file 读取异常：" + (e.message ?: e.javaClass.simpleName))
      null
    }
    if (ti == null) {
      log("元数据处理失败：torrent_file 为空")
      listener.onMetadataError("元数据处理失败：torrent_file 为空")
      return
    }
    val alertHash = try {
      alertHandle.infoHash()?.toString()?.lowercase() ?: ""
    } catch (e: Throwable) {
      log("info_hash 读取异常：" + (e.message ?: e.javaClass.simpleName))
      ""
    }
    val ih = if (alertHash.length == SHA1_HEX_LEN) alertHash else currentInfoHash
    log(
      "元数据到位：name=${ti.name()} 文件数=${ti.numFiles()} 分块数=${ti.numPieces()} " +
        "块长=${ti.pieceLength()} info_hash=$ih"
    )
    val result =
      try {
        resultFromTorrentInfo(ti, ih)
      } catch (e: Throwable) {
        log("元数据处理异常：" + (e.message ?: e.javaClass.simpleName))
        listener.onMetadataError("元数据处理失败：" + (e.message ?: e.javaClass.simpleName))
        return
      }
    // 关键一步：丢掉告警内视图，换成会话里的自有句柄（否则后续轮询 = 悬垂指针 → SIGSEGV）
    // 候选顺序：磁力链 v1 btih（主键）→ 句柄自报哈希（混合种子这里是 v2 截断哈希）
    val h = reacquireHandle(listOf(currentInfoHash, ih))
    if (h == null || !h.isValid()) {
      log("元数据已就绪但自有句柄获取失败：不写入 currentHandle，报错给 UI")
      listener.onMetadataError("元数据已到位，但会话句柄获取失败（种子可能已被移除）")
      return
    }
    try {
      val n = ti.numFiles()
      if (n > 0) h.prioritizeFiles(Priority.array(Priority.IGNORE, n))
      h.pause()
      h.unsetFlags(TorrentFlags.AUTO_MANAGED)
      h.setFlags(TorrentFlags.UPLOAD_MODE)
    } catch (e: Throwable) {
      log("暂停句柄失败（不影响清单）：" + (e.message ?: e.javaClass.simpleName))
      listener.onStatus("暂停句柄失败（不影响清单）：" + (e.message ?: e.javaClass.simpleName))
    }
    currentHandle = h
    currentResult = result
    currentInfoHash = ih
    if (saveDir == null) log("警告：落盘目录未知（saveDir=null），预览流可能不可用")
    log("元数据就绪：清单 " + result.files.size + " 个（非 pad " + result.viewFiles.size + " 个），总大小 " + result.totalSize + " 字节")
    result.viewFiles.take(MAX_LOGGED_FILES).forEach { f ->
      log("清单条目：index=${f.index} name=${f.name} size=${f.size} 分块 ${f.startPiece}..${f.endPiece}")
    }
    listener.onMetadataReady(ih, result)
  }

  /**
   * `TorrentInfo` → `ParseResult`（桌面 `core/resolver.py:result_from_torrent_info` 的移植）：
   * 单文件种子不套前缀、真多文件剥根目录段、safeRelPath 兜底防穿越、
   * totalSize 排除 .pad（BEP-47）。
   */
  private fun resultFromTorrentInfo(ti: TorrentInfo, infoHash: String): ParseResult {
    val fs = ti.files()
    val pl = ti.pieceLength()
    if (pl <= 0) throw IllegalStateException("块长非法：$pl")
    val root = ti.name().orEmpty()
    val n = fs.numFiles()
    val files = ArrayList<TorrentFile>(n)
    var offset = 0L
    val fp0 = if (n > 0) fs.filePath(0).replace('\\', '/') else ""
    // 「真多文件」判别：num_files>1 或单文件套在子目录里（file_path 含分隔符）
    val multi = n > 1 || fp0.contains('/')
    for (i in 0 until n) {
      val size = fs.fileSize(i)
      val fp = fs.filePath(i).replace('\\', '/')
      val inner = if (!multi) fp else if (fp.contains('/')) fp.substringAfter('/') else fp
      val path = if (multi) safeRelPath(root, inner) else safeRelPath(root)
      val start = (offset / pl).toInt()
      val end = if (size > 0) ((offset + size - 1) / pl).toInt() else start
      files.add(TorrentFile(i, path, size, offset, start, end))
      offset += size
    }
    val total = files.filter { !it.isPad }.sumOf { it.size }
    return ParseResult(
      infoHash = infoHash,
      name = root,
      totalSize = total,
      pieceSize = pl,
      numPieces = ti.numPieces(),
      files = files,
      trackers = emptyList(),
      comment = "",
      createdBy = "",
      source = "magnet",
    )
  }

  // ---------------------------------------------------------------- 预览

  /** 当前预览文件所在目录（流服务根目录）。 */
  fun previewRoot(): File? = saveDir

  /** 开始预览某个文件（只下这一个文件的分块；尾部 moov 优先 + 顺序 + 递增 deadline）。 */
  fun beginPreview(file: TorrentFile): Boolean {
    val h = currentHandle ?: return false
    if (!isHandleUsable(h)) {
      log("预览中止：句柄失效")
      currentHandle = null
      listener.onMetadataError(HANDLE_INVALID_TEXT)
      return false
    }
    return try {
      scheduler.begin(h, file)
      previewFile = file
      true
    } catch (e: Throwable) {
      log("预览启动失败：" + (e.message ?: e.javaClass.simpleName))
      listener.onStatus("预览启动失败：" + (e.message ?: e.javaClass.simpleName))
      false
    }
  }

  fun stopPreview() {
    scheduler.stop()
    previewFile = null
  }

  fun previewTick() {
    if (scheduler.active) scheduler.tick()
  }

  fun tailEntryReady(): Boolean = scheduler.tailEntryReady()

  fun bufferProgress(): Double = scheduler.bufferProgress()

  fun bufferedBytes(): Long = scheduler.contiguousProgress()

  fun previewFileSize(): Long = previewFile?.size ?: 0L

  fun previewSeek(byteOffset: Long) {
    if (scheduler.active) scheduler.seekToByte(byteOffset)
  }

  // ---------------------------------------------------------------- 流服务的可用性来源

  /** 流服务用的「分块可用性 + 按需点播」实现（找不到预览任务一律返回 null = 不可用）。 */
  fun pieceSource(): PieceSource =
    object : PieceSource {
      override fun pieceMap(diskPath: String): PieceMap? = pieceMapFor(diskPath)

      override fun onDemand(diskPath: String, start: Long, endExcl: Long) = demand(diskPath, start, endExcl)
    }

  /** 磁盘路径 → 预览文件的分块位图；路径不匹配/无预览任务时返回 null（不可用）。 */
  private fun pieceMapFor(diskPath: String): PieceMap? {
    val f = previewFile ?: return null
    val dir = saveDir ?: return null
    val pl = scheduler.previewPieceLength
    if (pl <= 0) return null
    if (!samePath(File(dir, f.path.replace('/', File.separatorChar)), File(diskPath))) return null
    val snap = scheduler.snapshot() ?: return null
    return snap.toPieceMap(f, pl)
  }

  private fun demand(diskPath: String, start: Long, endExcl: Long) {
    val f = previewFile ?: return
    val dir = saveDir ?: return
    if (!samePath(File(dir, f.path.replace('/', File.separatorChar)), File(diskPath))) return
    scheduler.requestRange(start, endExcl)
  }

  private fun samePath(a: File, b: File): Boolean =
    try {
      a.canonicalPath == b.canonicalPath
    } catch (e: Throwable) {
      a.absolutePath == b.absolutePath
    }

  // ---------------------------------------------------------------- 状态文本

  /** `isValid()` 的安全包装：**任何句柄调用前的第一道门**。 */
  private fun isHandleUsable(h: TorrentHandle): Boolean =
    try {
      h.isValid()
    } catch (e: Throwable) {
      log("句柄有效性检查异常：" + (e.message ?: e.javaClass.simpleName))
      false
    }

  /** 真实句柄 → [HandleProbe]（只在 isValid 通过后才会读 status）。 */
  private class TorrentHandleProbe(private val handle: TorrentHandle) : HandleProbe {
    override fun isValid(): Boolean = handle.isValid()

    override fun statusReading(): StatusReading {
      val st = handle.status()
      return StatusReading(st.numPeers(), st.numSeeds(), st.downloadRate() / 1024)
    }
  }

  /**
   * 轮询一次状态（**UI 友好的结果**，不抛异常、不崩溃）：
   * 会话已停 → [PollStatus.sessionStopped]；句柄失效 → [PollStatus.handleInvalid]；
   * 其余异常 → [PollStatus.error]（进程继续活着）。
   */
  fun pollStatus(): PollStatus {
    val h = currentHandle ?: return PollStatus(line = "无任务")
    if (!isRunning()) {
      log("轮询：会话未运行（会话被停止后所有句柄失效），停止读取并报错")
      currentHandle = null
      return PollStatus(sessionStopped = true, error = "DHT 会话已停止（句柄全部失效），请重新解析")
    }
    if (!isHandleUsable(h)) {
      log("轮询：句柄失效（isValid=false）→ 停止轮询并报错（不再调用 status()）")
      currentHandle = null
      return PollStatus(handleInvalid = true, error = HANDLE_INVALID_TEXT)
    }
    return when (val r = readHandleStatus(TorrentHandleProbe(h))) {
      HandleReadResult.NoHandle -> PollStatus(line = "无任务")
      is HandleReadResult.Ok ->
        PollStatus(line = "peer ${r.reading.peers} / seed ${r.reading.seeds} · ${r.reading.downloadRateKbps} KB/s")
      is HandleReadResult.Invalid -> {
        log("轮询：句柄失效（isValid=false）→ 停止轮询并报错")
        currentHandle = null
        PollStatus(handleInvalid = true, error = r.message)
      }
      is HandleReadResult.Failed -> {
        log("轮询：读取状态异常：" + r.message)
        PollStatus(error = "读取状态异常：" + r.message)
      }
    }
  }

  fun infoHash(): String = currentInfoHash

  fun result(): ParseResult? = currentResult

  private companion object {
    const val TAG = "MagnetViewer"
    const val LISTEN_INTERFACES = "0.0.0.0:6881"
    const val MAX_METADATA_SIZE = 8 * 1024 * 1024
    const val SHA1_HEX_LEN = 40
    const val MAX_LOGGED_FILES = 8
  }
}
