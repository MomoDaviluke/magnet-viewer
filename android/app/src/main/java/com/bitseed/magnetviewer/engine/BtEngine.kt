package com.bitseed.magnetviewer.engine

import android.util.Log
import com.bitseed.magnetviewer.core.PREVIEW_CACHE_CONVERT
import com.bitseed.magnetviewer.core.PREVIEW_CACHE_HOLD
import com.bitseed.magnetviewer.core.model.ParseResult
import com.bitseed.magnetviewer.core.model.PieceMap
import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.core.model.safeRelPath
import com.bitseed.magnetviewer.core.normalizePreviewCacheMode
import com.bitseed.magnetviewer.core.states.BOOTSTRAP_TRACKERS
import com.bitseed.magnetviewer.core.states.METADATA_TIMEOUT_SECONDS
import com.bitseed.magnetviewer.core.states.STATE_COMPLETED
import com.bitseed.magnetviewer.core.states.STATE_DELETED
import com.bitseed.magnetviewer.core.states.STATE_DOWNLOADING
import com.bitseed.magnetviewer.core.states.STATE_FAILED
import com.bitseed.magnetviewer.core.states.STATE_META_FETCH
import com.bitseed.magnetviewer.core.states.STATE_PAUSED
import com.bitseed.magnetviewer.core.states.STATE_QUEUED
import com.bitseed.magnetviewer.core.states.STATE_READY
import com.bitseed.magnetviewer.core.states.STATE_VALIDATE
import com.bitseed.magnetviewer.data.RestorePlan
import com.bitseed.magnetviewer.data.RestoreSource
import com.bitseed.magnetviewer.data.RemoveOutcome
import com.bitseed.magnetviewer.data.ResumeStore
import com.bitseed.magnetviewer.data.TaskRecord
import com.bitseed.magnetviewer.data.applyRemoveTask
import com.bitseed.magnetviewer.data.clampTaskPriority
import com.bitseed.magnetviewer.data.downloadedBytes
import com.bitseed.magnetviewer.data.isResumeKey
import com.bitseed.magnetviewer.data.isValidTaskPriority
import com.bitseed.magnetviewer.data.isWithin
import com.bitseed.magnetviewer.data.ltPriority
import com.bitseed.magnetviewer.data.planPromote
import com.bitseed.magnetviewer.data.planRemoveTask
import com.bitseed.magnetviewer.data.planRestore
import com.bitseed.magnetviewer.data.previewDirFor
import com.bitseed.magnetviewer.data.priorityQueueOrder
import com.bitseed.magnetviewer.data.promoteDirFor
import com.bitseed.magnetviewer.data.stateAfterRestore
import com.bitseed.magnetviewer.stream.PieceSource
import java.io.File
import java.util.Collections
import java.util.concurrent.ConcurrentHashMap
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
import org.libtorrent4j.alerts.SaveResumeDataAlert
import org.libtorrent4j.alerts.SaveResumeDataFailedAlert
import org.libtorrent4j.alerts.StorageMovedAlert
import org.libtorrent4j.alerts.StorageMovedFailedAlert
import org.libtorrent4j.alerts.TorrentErrorAlert

/**
 * 启动恢复的单个任务结果（宿主据此把内存清单校正到真实状态）。
 */
data class RestoredTask(
  val infoHash: String,
  val name: String,
  val state: String,
  /** 是否命中 fastresume（续传数据可用）。 */
  val resumeUsed: Boolean = false,
  /** 磁盘上是否有 fastresume 文件。 */
  val resumePresent: Boolean = false,
  /** fastresume 存在但解不开（损坏，已静默全新加入）。 */
  val resumeBroken: Boolean = false,
  val files: Int = 0,
  val totalSize: Long = 0L,
  val error: String = "",
  val note: String = "",
)

/**
 * 一次启动恢复的总账（**恢复路径要能在日志里看清**：读了多少、命中多少、暂停/续传各几个）。
 */
data class RestoreReport(
  val total: Int = 0,
  val restored: Int = 0,
  val failed: Int = 0,
  val resumeHits: Int = 0,
  val resumeMissing: Int = 0,
  val resumeBroken: Int = 0,
  val paused: Int = 0,
  val downloading: Int = 0,
  val waitingMetadata: Int = 0,
  val tasks: List<RestoredTask> = emptyList(),
  val errorText: String = "",
) {
  /** 一句话总结（写日志 + 上 UI 都用它）。 */
  fun summary(): String =
    "恢复 $total 个任务：成功 $restored / 失败 $failed；resume 命中 $resumeHits，" +
      "缺失 $resumeMissing，损坏 $resumeBroken；暂停 $paused，续传 $downloading，待元数据 $waitingMetadata"
}

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
 * 一个任务的**快照**（引擎 → UI；全是普通类型，句柄不出本包）。
 *
 * 与桌面 `core/taskops.py: tasks()` 的字段对齐（`info_hash/name/state/total_size/progress/
 * down_rate/priority/save_path/error/selected_files/seed/download`），另加本 demo 需要的
 * [files]（清单条数）与 [hasMetadata]。
 */
data class TaskSnapshot(
  val infoHash: String,
  val name: String,
  val state: String,
  val totalSize: Long,
  /** **已下载字节**（`handle.fileProgress()` 求和 = 显示口径；与磁盘占用是两回事）。 */
  val downloadedBytes: Long = 0L,
  val rateKbps: Int = 0,
  val peers: Int = 0,
  val priority: Int = 0,
  /** 是否下载型任务（转正后/续传任务为 true；只解析看清单的为 false）。 */
  val download: Boolean = false,
  val savePath: String = "",
  val error: String = "",
  val files: Int = 0,
  val selected: List<String> = emptyList(),
  val hasMetadata: Boolean = false,
  val seed: Boolean = false,
  /** **选中文件字节**（百分比的分子/分母口径；见 [selectedBytes]）。0 = 没有选中文件。 */
  val selectedBytes: Long = 0L,
) {
  /**
   * 进度 0..1 = **已下载 / 选中字节**（不是 / 总大小）。
   *
   * 没有选中文件（仅解析任务 / 优先级全 0）时分母不成立 → 0.0，配合 [progressKnown]
   * 让 UI 不显示百分比 —— 修掉 QA C-02 的「已下载 0 B（100%）」。
   */
  val progress: Double
    get() = progressOf(downloadedBytes, selectedBytes) ?: 0.0

  /** 百分比是否可知（选中字节 > 0）。false 时 UI 应显示「未选择文件」而不是 0%/100%。 */
  val progressKnown: Boolean
    get() = selectedBytes > 0L
}

/**
 * 引擎里的一个任务（**所有 libtorrent4j 对象都关在本包内**）。
 *
 * 句柄纪律与焦点任务的 `currentHandle` 一致：只放 [reacquireHandle] / `addTorrent` 拿到的
 * **自有副本**，每次调用前先 `isValid()`。
 */
internal class EngineTask(
  val infoHash: String,
  /** 创建序（列表顺序 / 优先级稳定排序都用它）。 */
  val order: Int,
  var source: String = "",
  var saveDir: File? = null,
  var state: String = STATE_QUEUED,
  var priority: Int = 0,
  /** 是否下载型任务（转正 / 续传 / 预览开始过）。 */
  var download: Boolean = false,
  var selected: List<String> = emptyList(),
  var error: String = "",
) {
  @Volatile var handle: TorrentHandle? = null
  @Volatile var result: ParseResult? = null

  /** 元数据获取起表（毫秒；0 = 不在等待元数据）。每任务独立计时 → 多任务各自超时。 */
  @Volatile var metaStartedMs: Long = 0L

  val name: String get() = result?.name ?: infoHash
  val totalSize: Long get() = result?.totalSize ?: 0L
}

/**
 * 一个 info_hash 的会话持有者：磁力链解析（DHT 拿元数据，**不下载本体**）+
 * 单文件预览调度 + 流服务的分块可用性来源。
 *
 * 约束：**所有 libtorrent4j 调用都在本包内**，UI/Compose 只碰 [BtEngine] 的
 * 普通类型（String/Long/Boolean/ParseResult）。
 *
 * **句柄纪律（本文件的核心不变量）**：
 * 1. 任何 `TorrentHandle.xxx()` 之前先 `isValid()`——`status()` 等 swig 调用对失效句柄
 *    会原生 SIGSEGV，`try/catch` 接不住（见 [HandleAccess]）。句柄类的调用统一走
 *    [TorrentHandleOps]（它在每次调用前再守一道）。
 * 2. **绝不缓存告警里拿到的句柄**：`TorrentAlert.handle()` 返回的是 `swigCMemOwn=false`
 *    的**非持有视图**（指针指向告警对象内部的 `torrent_handle` 成员）；告警一旦被释放
 *    （GC/delete），这个句柄就是悬垂指针，下次 `status()` 直接崩。
 *    正确做法：在告警回调内读信息，然后按 info_hash 从会话**重新取一个自有副本**
 *    （`SessionManager.find` → `find_torrent`，swig 侧 `new torrent_handle(ptr, true)`）。
 * 3. 引擎把「我在干什么」发布到 [EngineHub]（前台服务据此驻留/更新通知/持唤醒锁），
 *    并实现 [EngineControl] 接收服务侧的通知动作（暂停/继续/停止/超时）。
 */
class BtEngine(private val listener: Listener) : EngineControl {

  /** 引擎 → 宿主（ViewModel）的事件回调；回调可能来自 libtorrent 告警线程。 */
  interface Listener {
    /**
     * 元数据就绪：清单已解析（「只解析」任务会按 upload_mode + 暂停收回，不下载本体）。
     *
     * [focused] = 这条任务是否是当前焦点任务（多任务：宿主据此决定要不要更新中间的清单区）。
     * 默认 true 保持旧调用点语义不变。
     */
    fun onMetadataReady(infoHash: String, result: ParseResult, focused: Boolean = true)

    /**
     * 元数据阶段失败（含 add 失败 / DHT 失败 / 超时）。
     *
     * [infoHash] = 出错的任务（多任务：不能把「某个任务的超时」记到当前焦点头上；
     * 空串 = 无归属，宿主按焦点处理）。
     */
    fun onMetadataError(message: String, infoHash: String = "")

    /** 一般性状态文本（中文，UI 直接显示）。 */
    fun onStatus(message: String)

    /**
     * 任务状态变化（**清单落盘的触发点之一**，桌面「状态切换即写 .tasks.json」的落点）。
     *
     * 默认空实现：只有关心持久化的宿主（ViewModel）需要覆写。
     */
    fun onTaskStateChanged(infoHash: String, state: String) {}
  }

  private val manager = SessionManager()
  private val scheduler = PreviewScheduler { log -> this.log("调度：" + log) }

  @Volatile private var started = false
  @Volatile private var currentHandle: TorrentHandle? = null
  @Volatile private var currentResult: ParseResult? = null
  @Volatile private var currentInfoHash: String = ""
  @Volatile private var saveDir: File? = null
  @Volatile private var previewFile: TorrentFile? = null

  /** 缓存根（`<filesDir>/magnet/cache`）：fastresume 落盘与恢复都从这里推路径。 */
  @Volatile private var cacheRoot: File? = null

  // ---- 任务注册表（多任务：桌面 `core/registry.py` 的最小对应物）----
  /**
   * 全任务注册表：键 = v1 info_hash（与 `.tasks.json` / `.resume/<ih>.fastresume` 同一套键）。
   *
   * 为什么要有它：单任务焦点（`currentHandle` 等）只够跑一次预览，而「解析新磁力链不覆盖
   * 已有任务 / 同 hash 复用 / 逐任务暂停继续移除」必须按 info_hash 各自记账。句柄纪律不变：
   * 每个任务只存**自有副本**，任何调用前先 `isValid()`。
   */
  private val taskLock = Any()
  private val taskMap = LinkedHashMap<String, EngineTask>()

  /** 哈希别名 → 主键（混合种子自报 v2 截断哈希时用它把告警路由回正确任务）。 */
  private val taskAliases = HashMap<String, String>()
  private var taskSeq = 0

  /**
   * 「恢复续传」的 info_hash 集合：这些任务的元数据到达后**不要**按解析语义暂停
   * （它们是恢复出来的下载任务，清单已从磁盘展示过，元数据一到就该继续下）。
   */
  private val continuationHashes: MutableSet<String> = Collections.newSetFromMap(ConcurrentHashMap())

  /**
   * 「先拉元数据、拿到后暂停回去」的 info_hash 集合：恢复出来的**暂停**任务若没带元数据
   * （fastresume 没写 info-dict），会被临时放开去拉元数据；元数据到达时按本集合暂停回原状态。
   */
  private val pauseAfterMetadataHashes: MutableSet<String> = Collections.newSetFromMap(ConcurrentHashMap())

  // ---- 热更新配置（applyProxy / applyRateLimit / applyMetadataTimeout / setPreviewCacheMode）----
  @Volatile private var lastProxy: ProxyConfig? = null
  @Volatile private var lastRateLimitKbps: Int = 0
  @Volatile private var metadataTimeoutSeconds: Int = METADATA_TIMEOUT_SECONDS

  /** 预览缓存模式：`convert`（默认，桌面 config 默认值）/ `hold`；只由 setPreviewCacheMode 改。 */
  @Volatile private var cacheMode: String = PREVIEW_CACHE_CONVERT

  // ---- 元数据阶段（解析）状态：超时判定用 ----
  @Volatile private var metaFetching = false
  @Volatile private var metaFetchStartMs = 0L
  @Volatile private var metaTimeoutFired = false

  init {
    // 前台服务通过 EngineHub 找到本引擎（进程级控制面，见 EngineActivity.kt）
    EngineHub.attach(this)
  }

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
          // fastresume：save_resume_data 是异步请求，落盘时机在告警里（桌面 core/resume.py 同款）
          AlertType.SAVE_RESUME_DATA.swig(),
          AlertType.SAVE_RESUME_DATA_FAILED.swig(),
          // 转正（moveStorage）的回执：搬运完成/失败都要能在日志里看见
          AlertType.STORAGE_MOVED.swig(),
          AlertType.STORAGE_MOVED_FAILED.swig(),
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
      reapplyHotSettings()
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
      reapplyHotSettings()
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
        reapplyHotSettings()
        true
      } catch (e2: Throwable) {
        log("会话启动失败（回退也失败）：" + (e2.message ?: e2.javaClass.simpleName))
        listener.onMetadataError("DHT 会话启动失败：" + (e2.message ?: e2.javaClass.simpleName))
        false
      }
    }
  }

  /** 会话（重）启动后，把之前热更新过的代理/限速重新下发一次（否则重建会话即丢配置）。 */
  private fun reapplyHotSettings() {
    try {
      if (lastRateLimitKbps > 0) applyRateLimitNow(lastRateLimitKbps)
      lastProxy?.let { applyProxyNow(it) }
    } catch (e: Throwable) {
      log("重放热更新设置异常：" + (e.message ?: e.javaClass.simpleName))
    }
  }

  /**
   * 退出前尽力让 fastresume 落盘：请求 + **有界等待**（默认 1.5s）文件真的出现。
   * 超时只告警，绝不阻断退出（下次启动按「无 resume」处理，元数据会重新拉）。
   */
  private fun drainResumeBeforeShutdown(timeoutMs: Long = 1500L) {
    val root = cacheRoot ?: return
    val ih = currentInfoHash.trim().lowercase()
    if (!isResumeKey(ih)) return
    val h = currentHandle ?: return
    if (!isHandleUsable(h)) return
    val target = ResumeStore.path(root, ih) ?: return
    if (target.isFile && target.length() > 0) return
    requestResumeData("会话关闭")
    val deadline = System.currentTimeMillis() + timeoutMs
    while (System.currentTimeMillis() < deadline) {
      if (target.isFile && target.length() > 0) {
        log("退出前 fastresume 已确认落盘：${target.path}")
        return
      }
      try {
        Thread.sleep(100L)
      } catch (_: InterruptedException) {
        return
      }
    }
    log("退出前 fastresume 未在 ${timeoutMs}ms 内落盘（只告警，不阻断退出）")
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
    // 退出前尽力把 fastresume 落盘（有界等待；超时只告警——桌面 drain_resume_alerts 同款语义）
    try {
      drainResumeBeforeShutdown()
    } catch (e: Throwable) {
      log("退出前 fastresume 收尾异常（已忽略）：" + (e.message ?: e.javaClass.simpleName))
    }
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
      metaFetching = false
      synchronized(taskLock) {
        taskMap.clear()
        taskAliases.clear()
      }
      // 控权面交还：只有仍归属本实例时才摘除（避免把重建后的新引擎摘掉）
      if (EngineHub.control === this) {
        EngineHub.attach(null)
        EngineHub.reset()
      }
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
    this.cacheRoot = cacheRoot
    val atp =
      try {
        AddTorrentParams.parseMagnetUri(uri)
      } catch (e: Throwable) {
        log("磁力链接解析失败：" + (e.message ?: e.javaClass.simpleName))
        listener.onMetadataError("磁力链接无效：" + (e.message ?: e.javaClass.simpleName))
        return null
      }
    val magnetHash = infoHashOf(atp)
    // **同一 info_hash 重复解析：复用既有任务**（不重复建、不覆盖别人的任务；桌面 add_task 去重同款）
    if (magnetHash.isNotEmpty()) {
      val existing = taskOf(magnetHash)
      if (existing != null) {
        log("加种复用：info_hash=$magnetHash 已在任务列表（不重复建）")
        if (existing.source.isEmpty()) existing.source = uri
        focusTaskImpl(existing.infoHash)
        val dir = existing.saveDir ?: previewDir(cacheRoot, magnetHash)
        val done = existing.result
        if (done != null) listener.onMetadataReady(existing.infoHash, done, true)
        listener.onStatus("该磁力链已在任务列表（复用既有任务，不重复建）")
        publishActivity()
        return dir
      }
    }
    val ih = magnetHash
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
      // 入表：新任务是「只解析」任务（download=false），是否下载由「转正」或「预览」决定
      val task =
        registerTask(
          infoHash = ih.ifEmpty { tempTaskKey() },
          handle = h,
          saveDir = dir,
          source = uri,
          download = false,
          priority = 0,
          state = STATE_META_FETCH,
        )
      registerHandleAliases(task, h)
      focusTaskImpl(task.infoHash)
      // 超时判定起表：解析阶段从这一刻开始计时（applyMetadataTimeout 的落点）
      metaFetching = true
      metaTimeoutFired = false
      metaFetchStartMs = System.currentTimeMillis()
      // 重复解析同一磁力链：libtorrent 返回既有句柄，可能不会再发 metadata_received
      val already = try {
        if (h.isValid()) h.torrentFile() else null
      } catch (e: Throwable) {
        log("既有元数据查询异常：" + (e.message ?: e.javaClass.simpleName))
        null
      }
      if (already != null) onMetadataReceived(h) else listener.onStatus("正在通过 DHT 获取元数据…")
      publishActivity()
      dir
    } catch (e: Throwable) {
      log("加种异常：" + (e.message ?: e.javaClass.simpleName))
      metaFetching = false
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
        val alert = a as? MetadataFailedAlert
        val err = alert?.getError()
        val msg = "元数据获取失败：" + (err?.getMessage() ?: "未知原因")
        log(msg)
        metaFetching = false
        // 多任务：失败要记在**出错的那个任务**头上（用告警句柄反查主键）
        listener.onMetadataError(msg, taskKeyOfAlertHandle(alert?.handle()))
        publishActivity()
      }
      AlertType.ADD_TORRENT -> {
        val err = (a as? AddTorrentAlert)?.error()
        if (err != null && err.isError) {
          log("加种告警失败：" + err.getMessage())
          metaFetching = false
          listener.onMetadataError("加入会话失败：" + err.getMessage())
          publishActivity()
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
      AlertType.SAVE_RESUME_DATA -> {
        val alert = a as? SaveResumeDataAlert
        if (alert == null) log("fastresume 告警类型不符：${a.javaClass.simpleName}") else writeResumeFromAlert(alert)
      }
      AlertType.SAVE_RESUME_DATA_FAILED -> {
        val err = (a as? SaveResumeDataFailedAlert)?.error()
        log("fastresume 请求失败告警：" + (err?.getMessage() ?: "未知原因"))
      }
      AlertType.STORAGE_MOVED -> {
        // 转正搬运完成：复核句柄的 save_path 是否已指向新目录（日志是验收证据的一部分）
        val p = (a as? StorageMovedAlert)?.storagePath() ?: "?"
        log("转正搬运完成（storage_moved）：目录=$p")
        listener.onStatus("转正搬运完成：$p")
        publishActivity()
      }
      AlertType.STORAGE_MOVED_FAILED -> {
        val err = (a as? StorageMovedFailedAlert)?.error()
        val msg = "转正搬运失败（storage_moved_failed）：" + (err?.getMessage() ?: "未知原因")
        log(msg)
        listener.onStatus(msg)
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
      metaFetching = false
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
      metaFetching = false
      listener.onMetadataError("元数据处理失败：torrent_file 为空")
      return
    }
    val alertHash = try {
      alertHandle.infoHash()?.toString()?.lowercase() ?: ""
    } catch (e: Throwable) {
      log("info_hash 读取异常：" + (e.message ?: e.javaClass.simpleName))
      ""
    }
    // 任务主键必须是与磁力链 btih 一致的 **v1 哈希**：libtorrent 2.1 的
    // `torrent_handle.info_hash()`（乃至 `torrent_info.info_hash()`）对混合种子返回
    // **v2 截断哈希**（靶场 README「四个坑」§1 与 NOTES §0 都实测过）。旧写法只看
    // 「长度 40」就采信，会把任务键写成 v2 哈希 → `.tasks.json` 的键与
    // `.resume/<ih>.fastresume` 的文件名（按 v1 落盘）对不上，重启恢复永远 miss resume。
    // 优先级：磁力链 v1 btih（会话主键） → 元数据自报的 v1 → 句柄自报哈希（兜底）。
    val tiV1 =
      try {
        val ihs = ti.infoHashes()
        if (ihs.hasV1()) ihs.getV1()?.toString()?.trim()?.lowercase().orEmpty() else ""
      } catch (e: Throwable) {
        log("元数据 v1 哈希读取异常：" + (e.message ?: e.javaClass.simpleName))
        ""
      }
    val magnetHash = currentInfoHash.trim().lowercase()
    // **多任务路由**：告警只按哈希说话（不再假设「当前焦点就是它」）。
    // 候选顺序：元数据自报的 v1 → 句柄自报哈希（混合种子这里是 v2 截断哈希）→ 焦点任务
    val routeCandidates = listOf(tiV1, alertHash, magnetHash).filter { it.isNotEmpty() }.distinct()
    val ih = resolveTaskKey(routeCandidates) ?: routeCandidates.firstOrNull { isResumeKey(it) } ?: magnetHash
    log(
      "元数据到位：name=${ti.name()} 文件数=${ti.numFiles()} 分块数=${ti.numPieces()} " +
        "块长=${ti.pieceLength()} info_hash=$ih（候选=${routeCandidates.joinToString(",") { it.take(8) }}）"
    )
    val result =
      try {
        resultFromTorrentInfo(ti, ih)
      } catch (e: Throwable) {
        log("元数据处理异常：" + (e.message ?: e.javaClass.simpleName))
        metaFetching = false
        listener.onMetadataError("元数据处理失败：" + (e.message ?: e.javaClass.simpleName), ih)
        return
      }
    // 关键一步：丢掉告警内视图，换成会话里的自有句柄（否则后续轮询 = 悬垂指针 → SIGSEGV）
    // 候选顺序：本任务主键（磁力链 v1 btih）→ 句柄自报哈希
    val h = reacquireHandle(listOf(ih, magnetHash, alertHash))
    if (h == null || !h.isValid()) {
      log("元数据已就绪但自有句柄获取失败：不写入 currentHandle，报错给 UI")
      metaFetching = false
      listener.onMetadataError("元数据已到位，但会话句柄获取失败（种子可能已被移除）", ih)
      return
    }
    // 入表 / 更新既有记录（多任务：对象是「这条哈希的任务」，不是「当前焦点」）
    val task = taskForUpdate(ih, h, result, previewDirFor(cacheRoot ?: File("."), ih))
    registerHandleAliases(task, h)
    // 恢复续传任务：清单早已从磁盘展示过，元数据一到就该继续下（不按「解析」语义暂停）
    val pauseAfterMeta =
      pauseAfterMetadataHashes.remove(ih) || pauseAfterMetadataHashes.remove(magnetHash)
    val keepDownloading =
      !pauseAfterMeta && (continuationHashes.remove(ih) || continuationHashes.remove(magnetHash))
    val wantDownload = keepDownloading || task.download
    try {
      if (wantDownload) {
        h.unsetFlags(TorrentFlags.AUTO_MANAGED)
        h.resume()
        log("元数据到位（下载型任务）：保持下载，不暂停、不清优先级")
      } else {
        val n = ti.numFiles()
        if (n > 0) h.prioritizeFiles(Priority.array(Priority.IGNORE, n))
        h.pause()
        h.unsetFlags(TorrentFlags.AUTO_MANAGED)
        h.setFlags(TorrentFlags.UPLOAD_MODE)
        if (pauseAfterMeta) log("元数据到位（恢复的暂停任务）：已按原状态暂停回去")
      }
    } catch (e: Throwable) {
      log("暂停句柄失败（不影响清单）：" + (e.message ?: e.javaClass.simpleName))
      listener.onStatus("暂停句柄失败（不影响清单）：" + (e.message ?: e.javaClass.simpleName))
    }
    val newState = when {
      wantDownload -> STATE_DOWNLOADING
      task.download -> STATE_PAUSED
      else -> STATE_PAUSED
    }
    synchronized(taskLock) {
      task.state = newState
      task.error = ""
      task.metaStartedMs = 0L
    }
    if (task.infoHash == currentInfoHash) {
      applyFocusAliases(task)
      metaFetching = false
      if (saveDir == null) log("警告：落盘目录未知（saveDir=null），预览流可能不可用")
    } else {
      log("元数据到位（非焦点任务 ${task.infoHash.take(12)}…）：只更新该任务，不动焦点")
    }
    log("元数据就绪：清单 " + result.files.size + " 个（非 pad " + result.viewFiles.size + " 个），总大小 " + result.totalSize + " 字节")
    result.viewFiles.take(MAX_LOGGED_FILES).forEach { f ->
      log("清单条目：index=${f.index} name=${f.name} size=${f.size} 分块 ${f.startPiece}..${f.endPiece}")
    }
    listener.onTaskStateChanged(ih, newState)
    listener.onMetadataReady(ih, result, task.infoHash == currentInfoHash)
    publishActivity()
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

  // ---------------------------------------------------------------- 持久化：fastresume + 启动恢复

  /** 缓存根（恢复/落盘用）；宿主在加种或恢复时设置。 */
  fun setCacheRoot(dir: File) {
    cacheRoot = dir
  }

  /**
   * 请求写 fastresume（**异步**：真正落盘在 `save_resume_data_alert` 里，桌面
   * `core/persist.py: request_resume` 同款）。失败只记日志——持久化不阻断主流程。
   *
   * [reason] 只用于日志（`预览开始 / 预览暂停 / 停止预览 / App onStop` …）。
   */
  fun requestResumeData(reason: String = "") {
    val h = currentHandle ?: return
    val ih = currentInfoHash.trim().lowercase()
    if (!isResumeKey(ih)) {
      log("fastresume 请求跳过：info_hash 非法（'$ih'）")
      return
    }
    try {
      if (!isHandleUsable(h)) {
        log("fastresume 请求跳过：句柄失效")
        return
      }
      h.setFlags(TorrentFlags.NEED_SAVE_RESUME)
      // **必须带 SAVE_INFO_DICT**：只传 FLUSH_DISK_CACHE 时 libtorrent **不写 info-dict**
      // （宿主机 python-libtorrent 实测：read_resume_data 出来的 ti 为空）。
      // 桌面版能兜住这种情况是因为它给恢复参数补了 `atp.url = 磁力链`；而 libtorrent4j 的
      // Java 封装**没有 add_torrent_params.url 访问器**（只有 url_seeds），安卓侧补不了 →
      // 不带 info-dict 的 fastresume 恢复出来就是「有 info_hash 但没元数据的暂停种子」：
      // 不会连 peer → 永远拉不到清单 → 预览直接失败（块长 0）。故此处显式带上。
      h.saveResumeData(TorrentHandle.SAVE_INFO_DICT.or_(TorrentHandle.FLUSH_DISK_CACHE))
      log("已请求 fastresume（${reason.ifEmpty { "-" }}）：${ih.take(12)}…（含 info-dict）")
    } catch (e: Throwable) {
      log("请求 fastresume 异常（$reason）：" + (e.message ?: e.javaClass.simpleName))
    }
  }

  /**
   * `save_resume_data_alert` → `.resume/<ih>.fastresume`（桌面
   * `core/persist.py: write_resume_from_alert` 同款：坏告警/坏 ih/写失败都只告警）。
   */
  private fun writeResumeFromAlert(alert: SaveResumeDataAlert) {
    val root = cacheRoot
    if (root == null) {
      log("fastresume 落盘跳过：缓存根未知（cacheRoot=null）")
      return
    }
    val params =
      try {
        alert.params()
      } catch (e: Throwable) {
        log("fastresume 告警取参数异常：" + (e.message ?: e.javaClass.simpleName))
        return
      }
    val ih =
      infoHashOfParams(params).ifEmpty { currentInfoHash.trim().lowercase() }
    if (!isResumeKey(ih)) {
      log("fastresume 落盘跳过：拿不到合法 info_hash（'$ih'）")
      return
    }
    val bytes =
      try {
        AddTorrentParams.writeResumeDataBuf(params)
      } catch (e: Throwable) {
        log("fastresume 编码异常：" + (e.message ?: e.javaClass.simpleName))
        return
      }
    if (bytes == null || bytes.isEmpty()) {
      log("fastresume 内容为空，跳过落盘：${ih.take(12)}…")
      return
    }
    val f = ResumeStore.writeQuietly(root, ih, bytes) { w -> log(w) }
    if (f != null) log("fastresume 已落盘：${f.path}（${bytes.size} 字节）")
  }

  /** `AddTorrentParams` 里的 v1 info_hash（没有则取 best）；拿不到返回 ""。 */
  private fun infoHashOfParams(atp: AddTorrentParams): String =
    try {
      val ihs = atp.getInfoHashes()
      val sha = if (ihs.hasV1()) ihs.getV1() else ihs.getBest()
      sha?.toString()?.trim()?.lowercase().orEmpty()
    } catch (e: Throwable) {
      log("参数 info_hash 读取异常：" + (e.message ?: e.javaClass.simpleName))
      ""
    }

  /**
   * fastresume 字节 → `AddTorrentParams`（**native**）。
   *
   * 注意 API 选择（本仓库 2.1.0-39 实测）：libtorrent4j 的 Java 封装里**没有**
   * `AddTorrentParams.parseResumeData(...)`；对应能力是 swig 侧的
   * `org.libtorrent4j.swig.libtorrent.read_resume_data_ex(byte_vector, error_code)`
   * （javap 核对过：`read_resume_data` / `read_resume_data_ex` / `write_resume_data_buf_ex`
   * 三个静态方法在 jar 里都在）。损坏/解不开一律返回 null → 上层静默按全新加入（元数据会重新拉）。
   */
  private fun decodeResumeData(bytes: ByteArray): AddTorrentParams? =
    try {
      val vec = org.libtorrent4j.swig.byte_vector(bytes)
      val ec = org.libtorrent4j.swig.error_code()
      val swigParams = org.libtorrent4j.swig.libtorrent.read_resume_data_ex(vec, ec)
      if (swigParams == null || ec.failed()) {
        log("fastresume 解码失败（已按全新加入处理）：" + ec.message())
        null
      } else {
        AddTorrentParams(swigParams)
      }
    } catch (e: Throwable) {
      log("fastresume 解码异常（已按全新加入处理）：" + (e.message ?: e.javaClass.simpleName))
      null
    }

  /**
   * 按恢复计划拼 `AddTorrentParams`。
   *
   * resume 参数优先（含 info-hash / 分块位图 / 文件优先级），但**磁力链里的
   * tracker 与 `x.pe` peer 提示要保住**（`AddTorrentParams` 没有 url 访问器，
   * 所以用「磁力链参数 + resume 参数」合并的方式补回来）。
   */
  private fun buildRestoreParams(
    plan: RestorePlan,
    resume: AddTorrentParams?,
    dir: File,
  ): AddTorrentParams? {
    if (plan.source == RestoreSource.TORRENT_FILE) {
      val ti =
        try {
          TorrentInfo(File(plan.sourceText))
        } catch (e: Throwable) {
          log("恢复读种子文件失败：" + (e.message ?: e.javaClass.simpleName))
          null
        } ?: return null
      val atp = resume ?: AddTorrentParams()
      atp.setTorrentInfo(ti)
      atp.setSavePath(dir.path)
      return atp
    }
    val magnet =
      try {
        AddTorrentParams.parseMagnetUri(plan.sourceText)
      } catch (e: Throwable) {
        log("恢复解析磁力链失败：" + (e.message ?: e.javaClass.simpleName))
        null
      } ?: return null
    val atp = resume ?: magnet
    atp.setSavePath(dir.path)
    val trackers = atp.getTrackers()
    if (trackers.isNullOrEmpty()) {
      val fromMagnet = magnet.getTrackers()
      atp.setTrackers(if (fromMagnet.isNullOrEmpty()) BOOTSTRAP_TRACKERS else fromMagnet)
    }
    if (resume != null) {
      val peers = magnet.peers()
      if (!peers.isNullOrEmpty()) atp.peers(peers)
    }
    return atp
  }

  /**
   * **启动恢复**：按磁盘清单把任务重新加回会话（对齐桌面
   * `core/persist.py: TaskPersistence.restore_task` 的九组场景：resume 命中 / 缺失 / 损坏 /
   * 已暂停 / 已完成 / 来源不可用 / 目录非法 / info_hash 非法 / 加种失败）。
   *
   * 纯判断在 [planRestore]（data 层，JVM 单测覆盖），本方法只负责 native 部分
   * （解码 resume、addTorrent、pause/resume）与日志。**任何单个任务失败都不影响其余任务**，
   * 也绝不抛异常打断启动。
   *
   * 清单的**展示**不依赖本方法（宿主先读 `.tasks.json` 直接上屏）；这里返回的总账用于
   * 把内存态校正到真实状态（含失败任务的 FAILED/错误文本）。
   */
  @Synchronized
  fun restoreTasks(cacheDir: File, tasks: List<TaskRecord>): RestoreReport {
    setCacheRoot(cacheDir)
    // 恢复是启动期的一次性动作：先清注册表（避免重复恢复把同一任务记两份）
    synchronized(taskLock) {
      taskMap.clear()
      taskAliases.clear()
    }
    if (tasks.isEmpty()) {
      log("启动恢复：磁盘上没有任务")
      return RestoreReport()
    }
    log("启动恢复：读到 ${tasks.size} 个任务，开始重建（cacheDir=${cacheDir.path}）")
    if (!ensureStarted()) {
      val msg = "会话不可用，无法恢复任务"
      log("启动恢复失败：$msg")
      return RestoreReport(total = tasks.size, failed = tasks.size, errorText = msg)
    }
    var restored = 0
    var failed = 0
    var resumeHits = 0
    var resumeMissing = 0
    var resumeBroken = 0
    var pausedCount = 0
    var downloadingCount = 0
    var waitingMeta = 0
    val out = ArrayList<RestoredTask>(tasks.size)
    for (t in tasks) {
      val bytes = ResumeStore.read(cacheDir, t.infoHash)
      val decoded = if (bytes != null) decodeResumeData(bytes) else null
      if (bytes == null) resumeMissing++ else if (decoded == null) resumeBroken++ else resumeHits++
      val plan = planRestore(t, cacheDir, resumeBytesPresent = bytes != null, resumeDecoded = decoded != null)
      log("恢复任务：${t.name}（${t.infoHash.take(12)}…）state=${t.state} ${plan.note}")
      if (!plan.valid) {
        failed++
        log("恢复失败：${t.infoHash.take(12)}… ${plan.error}")
        listener.onTaskStateChanged(plan.infoHash, STATE_FAILED)
        out.add(
          RestoredTask(
            infoHash = plan.infoHash,
            name = t.name,
            state = STATE_FAILED,
            resumePresent = bytes != null,
            resumeBroken = plan.resumeBroken,
            error = plan.error,
            note = plan.note,
          )
        )
        continue
      }
      val dir = plan.saveDir
      if (!dir.exists() && !dir.mkdirs()) {
        failed++
        val msg = "重启恢复失败：无法创建落盘目录 ${dir.path}"
        log(msg)
        listener.onTaskStateChanged(plan.infoHash, STATE_FAILED)
        out.add(RestoredTask(plan.infoHash, t.name, STATE_FAILED, error = msg, note = plan.note))
        continue
      }
      val atp = buildRestoreParams(plan, decoded, dir)
      if (atp == null) {
        failed++
        val msg = "重启恢复失败：来源不可用/参数构造失败"
        log(msg)
        listener.onTaskStateChanged(plan.infoHash, STATE_FAILED)
        out.add(RestoredTask(plan.infoHash, t.name, STATE_FAILED, error = msg, note = plan.note))
        continue
      }
      if (plan.pauseAfterAdd) {
        // 加种即暂停：不留「加完先下一会儿再暂停」的窗口
        atp.setFlags(atp.getFlags().or_(TorrentFlags.PAUSED))
      }
      val added =
        try {
          val ec = ErrorCode(org.libtorrent4j.swig.error_code())
          val h = SessionHandle(manager.swig()).addTorrent(atp, ec)
          if (ec.isError) {
            log("恢复加种失败：${ec.getMessage()}")
            null
          } else {
            h
          }
        } catch (e: Throwable) {
          log("恢复加种异常：" + (e.message ?: e.javaClass.simpleName))
          null
        }
      if (added == null || !added.isValid()) {
        failed++
        val msg = "重启恢复失败：加入会话失败（句柄无效）"
        log(msg)
        listener.onTaskStateChanged(plan.infoHash, STATE_FAILED)
        out.add(RestoredTask(plan.infoHash, t.name, STATE_FAILED, error = msg, note = plan.note))
        continue
      }
      // 优先取「会话里的自有副本」（addTorrent 返回的句柄同样按自有处理，取不到再回退）
      val h = reacquireHandle(listOf(plan.infoHash)) ?: added
      val ti =
        try {
          if (h.isValid()) h.torrentFile() else null
        } catch (e: Throwable) {
          log("恢复读元数据异常：" + (e.message ?: e.javaClass.simpleName))
          null
        }
      val hasMeta = ti != null
      val stateFromDisk = stateAfterRestore(t.state, hasMeta, plan.pauseAfterAdd)
      // 「只解析」任务（download=false）：清单在，但不下载本体 —— 恢复成 upload_mode + 暂停
      val state = if (t.download) stateFromDisk else STATE_PAUSED
      var result: ParseResult? = null
      if (ti != null) {
        result =
          try {
            resultFromTorrentInfo(ti, plan.infoHash)
          } catch (e: Throwable) {
            log("恢复清单派生失败：" + (e.message ?: e.javaClass.simpleName))
            null
          }
      }
      try {
        if (!t.download) {
          if (hasMeta) {
            val n = ti?.numFiles() ?: 0
            if (n > 0) h.prioritizeFiles(Priority.array(Priority.IGNORE, n))
            h.pause()
            h.unsetFlags(TorrentFlags.AUTO_MANAGED)
            h.setFlags(TorrentFlags.UPLOAD_MODE)
            pausedCount++
            log("恢复：只解析任务（download=false）→ upload_mode + 暂停（不下载本体）")
          } else {
            // 元数据不在：先放开去拉，拿到后由 onMetadataReceived 按 upload_mode/暂停收回
            h.resume()
            h.unsetFlags(TorrentFlags.AUTO_MANAGED)
            pauseAfterMetadataHashes.add(plan.infoHash)
            pausedCount++
            log("恢复：只解析任务且缺元数据 → 先放开拉元数据，元数据到位后再收成 upload_mode")
          }
        } else if (plan.pauseAfterAdd && hasMeta) {
          h.pause()
          h.unsetFlags(TorrentFlags.AUTO_MANAGED)
          pausedCount++
        } else if (plan.pauseAfterAdd) {
          // 暂停档但**元数据不在**（老 fastresume / 没带 info-dict）：暂停的种子不连 peer，
          // 元数据永远不会到 → 清单虽在屏上却点不开预览。先放它去拉元数据，
          // 拿到之后由 onMetadataReceived 再按原状态暂停回去（见 pauseAfterMetadataHashes）。
          h.resume()
          h.unsetFlags(TorrentFlags.AUTO_MANAGED)
          pauseAfterMetadataHashes.add(plan.infoHash)
          pausedCount++
          log("恢复：任务已暂停但缺元数据 → 先放开拉元数据，元数据到位后再暂停回原状态")
        } else {
          h.resume()
          h.unsetFlags(TorrentFlags.AUTO_MANAGED)
          downloadingCount++
        }
      } catch (e: Throwable) {
        log("恢复暂停/续传动作异常：" + (e.message ?: e.javaClass.simpleName))
      }
      if (t.download && !plan.pauseAfterAdd && !hasMeta) {
        // 元数据还没到（无 resume）：到达后按「续传」处理，不要再暂停
        continuationHashes.add(plan.infoHash)
        waitingMeta++
      }
      // 入表（多任务）：恢复出来的任务同样进注册表 —— UI 列表、逐任务操作、缓存配额保护名单都用它
      val et =
        registerTask(
          infoHash = plan.infoHash,
          handle = h,
          saveDir = dir,
          source = t.source,
          download = t.download,
          priority = t.priority,
          state = state,
        )
      synchronized(taskLock) {
        et.result = result
        et.download = t.download
        et.selected = t.selected
        et.error = ""
      }
      if (currentInfoHash.isEmpty()) {
        focusTaskImpl(plan.infoHash)
        if (!hasMeta) {
          metaFetching = true
          metaTimeoutFired = false
          metaFetchStartMs = System.currentTimeMillis()
        }
      }
      restored++
      val fileCount = result?.files?.size ?: t.files.size
      val total = result?.totalSize ?: t.totalSize
      log(
        "恢复成功：${plan.infoHash.take(12)}… state=$state 文件数=$fileCount 总大小=$total " +
          "（resume=${if (plan.useResume) "命中" else if (plan.resumeBroken) "损坏" else "缺失"}，" +
          "暂停=${plan.pauseAfterAdd}）"
      )
      listener.onTaskStateChanged(plan.infoHash, state)
      out.add(
        RestoredTask(
          infoHash = plan.infoHash,
          name = result?.name ?: t.name,
          state = state,
          resumeUsed = plan.useResume,
          resumePresent = bytes != null,
          resumeBroken = plan.resumeBroken,
          files = fileCount,
          totalSize = total,
          note = plan.note,
        )
      )
    }
    val report =
      RestoreReport(
        total = tasks.size,
        restored = restored,
        failed = failed,
        resumeHits = resumeHits,
        resumeMissing = resumeMissing,
        resumeBroken = resumeBroken,
        paused = pausedCount,
        downloading = downloadingCount,
        waitingMetadata = waitingMeta,
        tasks = out,
      )
    log("启动恢复完成：" + report.summary())
    listener.onStatus("已恢复 $restored/${tasks.size} 个任务（resume 命中 $resumeHits）")
    publishActivity()
    return report
  }

  // ---------------------------------------------------------------- 任务注册表（多任务）

  /** 临时任务键（磁力链里没有 btih 时；不落盘——持久化要求合法 info_hash）。 */
  private fun tempTaskKey(): String = "tmp-" + (++taskSeq)

  /** 登记任务（幂等：同键已存在则只补句柄/目录）。 */
  private fun registerTask(
    infoHash: String,
    handle: TorrentHandle?,
    saveDir: File?,
    source: String,
    download: Boolean,
    priority: Int,
    state: String,
  ): EngineTask =
    synchronized(taskLock) {
      val existing = taskMap[infoHash]
      if (existing != null) {
        if (handle != null) existing.handle = handle
        if (saveDir != null) existing.saveDir = saveDir
        if (source.isNotEmpty()) existing.source = source
        return@synchronized existing
      }
      val t =
        EngineTask(
          infoHash = infoHash,
          order = taskSeq++,
          source = source,
          saveDir = saveDir,
          state = state,
          priority = clampTaskPriority(priority),
          download = download,
        )
      t.handle = handle
      if (state == STATE_META_FETCH) t.metaStartedMs = System.currentTimeMillis()
      taskMap[infoHash] = t
      log("任务登记：${infoHash.take(12)}… state=$state download=$download 共 ${taskMap.size} 个任务")
      t
    }

  /** 按 info_hash 取任务（键归一化；找不到返回 null）。 */
  private fun taskOf(infoHash: String): EngineTask? {
    val key = infoHash.trim().lowercase()
    if (key.isEmpty()) return null
    return synchronized(taskLock) { taskMap[key] ?: taskAliases[key]?.let { taskMap[it] } }
  }

  /** 公开：某任务是否有清单（UI 取清单用）。 */
  fun taskResult(infoHash: String): ParseResult? = taskOf(infoHash)?.result

  /** 公开：某任务是否下载型（转正/预览过）。 */
  fun isDownloadTask(infoHash: String): Boolean = taskOf(infoHash)?.download == true

  /** 公开：某任务当前落盘目录（无记录返回 null）。 */
  fun taskSaveDir(infoHash: String): File? = taskOf(infoHash)?.saveDir

  /**
   * **缓存配额的保护名单**：当前活跃任务的落盘目录。
   *
   * 每调用一次取一次**当下**快照（配额侧在每次删除前再调一次，防陈旧快照误删）。
   */
  fun activeTaskDirs(): Set<String> =
    synchronized(taskLock) { taskMap.values.mapNotNull { it.saveDir?.path }.toSet() }

  /** 按候选哈希找已登记任务（含别名；都没命中返回 null）。 */
  private fun resolveTaskKey(candidates: List<String>): String? =
    synchronized(taskLock) {
      for (c in candidates) {
        val k = c.trim().lowercase()
        if (k.isEmpty()) continue
        if (taskMap.containsKey(k)) return@synchronized k
        val alias = taskAliases[k]
        if (alias != null && taskMap.containsKey(alias)) return@synchronized alias
      }
      null
    }

  /**
   * 告警里的句柄 → 已登记任务键（找不到返回 ""）。
   *
   * 只在告警回调内读 `info_hash`（非持有视图的只读访问是安全的），绝不把句柄放出去。
   */
  private fun taskKeyOfAlertHandle(h: TorrentHandle?): String {
    if (h == null) return ""
    return try {
      if (!h.isValid()) return ""
      val self = h.infoHash()?.toString()?.trim()?.lowercase().orEmpty()
      resolveTaskKey(listOf(self)) ?: ""
    } catch (e: Throwable) {
      log("告警任务反查异常：" + (e.message ?: e.javaClass.simpleName))
      ""
    }
  }

  /** 登记句柄自报哈希的别名（混合种子的 v2 截断哈希 / 非主键 v1）→ 告警能路由回正确任务。 */
  private fun registerHandleAliases(task: EngineTask, h: TorrentHandle) {
    try {
      if (!isHandleUsable(h)) return
      val self = h.infoHash()?.toString()?.trim()?.lowercase().orEmpty()
      if (self.isNotEmpty() && self != task.infoHash) {
        synchronized(taskLock) { taskAliases[self] = task.infoHash }
        log("登记哈希别名：${self.take(12)}… → ${task.infoHash.take(12)}…")
      }
      val ti =
        try {
          h.torrentFile()
        } catch (e: Throwable) {
          null
        }
      val ihs = ti?.infoHashes() ?: return
      if (ihs.hasV1()) {
        val v1 = ihs.getV1()?.toString()?.trim()?.lowercase().orEmpty()
        if (v1.isNotEmpty() && v1 != task.infoHash) synchronized(taskLock) { taskAliases[v1] = task.infoHash }
      }
    } catch (e: Throwable) {
      log("登记哈希别名异常：" + (e.message ?: e.javaClass.simpleName))
    }
  }

  /**
   * 元数据到达时的任务归属：优先命中已登记任务；未登记则**补建**（临时键 → 真实哈希的升级路径）。
   */
  private fun taskForUpdate(
    infoHash: String,
    handle: TorrentHandle,
    result: ParseResult,
    fallbackDir: File,
  ): EngineTask =
    synchronized(taskLock) {
      val existing = taskMap[infoHash]
      if (existing != null) {
        existing.handle = handle
        existing.result = result
        if (existing.saveDir == null) existing.saveDir = fallbackDir
        return@synchronized existing
      }
      val dir = cacheRoot?.let { previewDirFor(it, infoHash) } ?: fallbackDir
      val t =
        EngineTask(
          infoHash = infoHash,
          order = taskSeq++,
          saveDir = dir,
          state = STATE_META_FETCH,
          priority = 0,
          download = false,
        )
      t.handle = handle
      t.result = result
      // 临时键（磁力链无 btih）升级为真实哈希：把旧条目摘掉，避免同一任务两份记录
      val stale = currentInfoHash.trim().lowercase()
      if (stale.isNotEmpty() && stale != infoHash && !isResumeKey(stale) && taskMap.containsKey(stale)) {
        taskMap.remove(stale)
        log("任务键升级：$stale → ${infoHash.take(12)}…（临时键已摘除）")
      }
      taskMap[infoHash] = t
      log("任务补建（元数据到达时未在注册表）：${infoHash.take(12)}… → ${dir.path}")
      t
    }

  /** 把焦点别名（currentHandle/currentResult/saveDir/currentInfoHash）指向某任务。 */
  private fun applyFocusAliases(t: EngineTask) {
    currentInfoHash = t.infoHash
    currentHandle = t.handle
    currentResult = t.result
    saveDir = t.saveDir
  }

  /** 公开：把某任务设为「当前」（UI 点行 → 展示它的清单/预览它）。 */
  fun focusTask(infoHash: String): Boolean = focusTaskImpl(infoHash)

  private fun focusTaskImpl(infoHash: String): Boolean {
    val t = taskOf(infoHash) ?: return false
    if (t.infoHash != currentInfoHash) {
      // 切换焦点 = 停掉当前预览（同一时刻只播一个）
      try {
        if (scheduler.active) scheduler.stop()
      } catch (e: Throwable) {
        log("焦点切换停预览异常：" + (e.message ?: e.javaClass.simpleName))
      }
      previewFile = null
    }
    applyFocusAliases(t)
    log("焦点任务：${t.infoHash.take(12)}…（state=${t.state} download=${t.download}）")
    return true
  }

  /** 全任务快照（键序 = 创建序；UI 任务列表直接用；句柄访问全部先过 isValid）。 */
  fun taskSnapshots(): List<TaskSnapshot> {
    val list: List<EngineTask> = synchronized(taskLock) { taskMap.values.sortedBy { it.order } }
    val running = isRunning()
    val out = ArrayList<TaskSnapshot>(list.size)
    for (t in list) {
      var downloaded = 0L
      var rate = 0
      var peers = 0
      var finished = false
      val h = t.handle
      if (h != null && running && isHandleUsable(h)) {
        try {
          val st = h.status()
          rate = st.downloadRate() / 1024
          peers = st.numPeers()
          finished = st.isFinished() || st.isSeeding()
        } catch (e: Throwable) {
          log("任务状态读取异常（${t.infoHash.take(12)}…）：" + (e.message ?: e.javaClass.simpleName))
        }
        try {
          // **已下载字节**（显示口径）= fileProgress 求和；与磁盘占用（配额口径）不可互换
          downloaded = downloadedBytes(h.fileProgress())
        } catch (e: Throwable) {
          log("任务已下载字节读取异常（${t.infoHash.take(12)}…）：" + (e.message ?: e.javaClass.simpleName))
        }
      }
      // 百分比的**分母**（选中文件字节；J-01/C-02）：仅解析任务没有任何选中文件 → 0，
      // 于是 progress 不可知、也不会被判完成（见 isTaskCompleted 的注释）
      val selBytes = selectedBytes(t.result, t.selected, t.download)
      val state =
        if (isTaskCompleted(finished, t.download, selBytes) && t.state == STATE_DOWNLOADING) STATE_COMPLETED
        else t.state
      if (state != t.state && state == STATE_COMPLETED) {
        synchronized(taskLock) {
          t.state = STATE_COMPLETED
          t.error = ""
        }
        log("任务完成：${t.infoHash.take(12)}…（下载 $downloaded 字节 / 选中 $selBytes 字节）")
        listener.onTaskStateChanged(t.infoHash, STATE_COMPLETED)
      }
      out.add(
        TaskSnapshot(
          infoHash = t.infoHash,
          name = t.name,
          state = state,
          totalSize = t.totalSize,
          downloadedBytes = downloaded,
          rateKbps = rate,
          peers = peers,
          priority = t.priority,
          download = t.download,
          savePath = t.saveDir?.path ?: "",
          error = t.error,
          files = t.result?.files?.size ?: 0,
          selected = t.selected,
          hasMetadata = t.result != null,
          seed = finished,
          selectedBytes = selBytes,
        )
      )
    }
    return out
  }

  // ---------------------------------------------------------------- 任务操作（对齐桌面 taskops）

  /**
   * 暂停任务（桌面 `pause_task`：`pause` + 撤 `auto_managed`）。
   *
   * 若该任务正在预览，同时把预览调度器冻结（缓冲读数不再增长，「继续」可原地恢复）。
   */
  fun pauseTask(infoHash: String): Boolean {
    val t = taskOf(infoHash) ?: return false
    val h = t.handle
    if (h == null || !isHandleUsable(h)) {
      log("暂停任务中止：句柄不可用（${t.infoHash.take(12)}…）")
      return false
    }
    try {
      h.pause()
      h.unsetFlags(TorrentFlags.AUTO_MANAGED)
    } catch (e: Throwable) {
      log("暂停任务异常（${t.infoHash.take(12)}…）：" + (e.message ?: e.javaClass.simpleName))
    }
    synchronized(taskLock) {
      t.state = STATE_PAUSED
      t.error = ""
    }
    if (t.infoHash == currentInfoHash) {
      try {
        if (scheduler.active && !scheduler.paused) scheduler.pause()
      } catch (e: Throwable) {
        log("暂停任务时冻结预览异常：" + (e.message ?: e.javaClass.simpleName))
      }
    }
    requestResumeDataFor(t, "暂停任务")
    listener.onTaskStateChanged(t.infoHash, STATE_PAUSED)
    publishActivity()
    return true
  }

  /** 继续任务（桌面 `resume_task`：清 error、按有无元数据定状态、重新激活下载）。 */
  fun resumeTask(infoHash: String): Boolean {
    val t = taskOf(infoHash) ?: return false
    val h = t.handle
    if (h == null || !isHandleUsable(h)) {
      log("继续任务中止：句柄不可用（${t.infoHash.take(12)}…）")
      return false
    }
    val hasMeta = t.result != null
    // **J-01**：仅解析任务（没有选中任何文件、文件优先级全 0）点「继续」不得进入
    // DOWNLOADING —— 那会让下一拍快照直接把它判成 COMPLETED（libtorrent 对 want 0 的
    // 种子 isFinished() 恒真），用户看到「已下载 0 B（100%）」。
    val nextState = if (!hasMeta) STATE_META_FETCH else stateAfterResume(t.download, t.selected)
    synchronized(taskLock) {
      t.error = ""
      t.state = nextState
      t.metaStartedMs = if (hasMeta) 0L else System.currentTimeMillis()
    }
    if (!hasMeta) {
      try {
        h.resume()
        h.unsetFlags(TorrentFlags.AUTO_MANAGED)
      } catch (e: Throwable) {
        log("继续任务（等元数据）异常：" + (e.message ?: e.javaClass.simpleName))
      }
    } else if (t.download) {
      activateTask(t)
    } else {
      // 「只解析」任务点继续 = 重新放开去拉元数据/保持只读（不下载本体）
      try {
        h.resume()
      } catch (e: Throwable) {
        log("继续只解析任务异常：" + (e.message ?: e.javaClass.simpleName))
      }
      log("继续任务：仅解析任务（未选择要下载的文件）→ 保持 ${STATE_PAUSED}，不进入下载态")
      listener.onStatus(PARSE_ONLY_NO_SELECTION_NOTE)
    }
    if (t.infoHash == currentInfoHash) {
      try {
        if (scheduler.active && scheduler.paused) {
          val ok = scheduler.resume(TorrentHandleOps(h))
          if (ok) previewFile = scheduler.previewFile
          log("继续任务：预览调度器已恢复=$ok")
        }
      } catch (e: Throwable) {
        log("继续任务重放预览异常：" + (e.message ?: e.javaClass.simpleName))
      }
    }
    requestResumeDataFor(t, "继续任务")
    listener.onTaskStateChanged(t.infoHash, nextState)
    publishActivity()
    return true
  }

  /** 设置任务优先级（0..3；值域外**拒绝**，不改变现状——桌面 `set_priority` 同款）。 */
  fun setTaskPriority(infoHash: String, priority: Int): Boolean {
    val t = taskOf(infoHash) ?: return false
    if (!isValidTaskPriority(priority)) {
      log("优先级设置忽略：$priority（值域 0..3）")
      return false
    }
    synchronized(taskLock) { t.priority = priority }
    applyQueueOrder()
    val h = t.handle
    if (h != null && isHandleUsable(h)) {
      try {
        if (t.priority > 0) h.setFlags(TorrentFlags.AUTO_MANAGED)
      } catch (e: Throwable) {
        log("优先级联动 auto_managed 异常：" + (e.message ?: e.javaClass.simpleName))
      }
    }
    log("任务优先级：${t.infoHash.take(12)}… = $priority（libtorrent 权重 ${ltPriority(priority)}）")
    listener.onTaskStateChanged(t.infoHash, t.state)
    return true
  }

  /** 把优先级下发给会话：高优先 → 队列靠前（配合 `active_downloads` 限制并发）。 */
  private fun applyQueueOrder() {
    val entries: List<Pair<String, Int>>
    val byKey: Map<String, EngineTask>
    synchronized(taskLock) {
      entries = taskMap.values.map { it.infoHash to it.priority }
      byKey = LinkedHashMap(taskMap)
    }
    if (entries.isEmpty()) return
    val order = priorityQueueOrder(entries)
    order.forEachIndexed { pos, key ->
      val t = byKey[key] ?: return@forEachIndexed
      val h = t.handle ?: return@forEachIndexed
      if (!isHandleUsable(h)) return@forEachIndexed
      try {
        // libtorrent 2.1 用「队列位置」表达优先级（torrent_priority 已无对应 API，见 data/TaskOps.kt 头注释）
        h.swig().queue_position_set_ex(pos)
      } catch (e: Throwable) {
        log("设置队列位置异常（${key.take(12)}… p$pos）：" + (e.message ?: e.javaClass.simpleName))
      }
    }
    log("优先级已下发（高优先在前）：" + order.joinToString(",") { it.take(8) + "@" + (byKey[it]?.priority ?: 0) })
  }

  /**
   * **转正**：把任务目录 `.preview/<ih>` 转成 `<cache>/downloads/<ih>` 并继续下载该文件
   * （桌面 D10「沿用已下载分块、零额外下载」+ 本任务书的落盘要求）。
   *
   * 步骤：停预览（若在预览）→ `moveStorage`（异步，落盘由 libtorrent 搬）→ 解 upload_mode →
   * 按选中文件置优先级 → auto_managed → resume。搬运完成有 `storage_moved_alert` 回执。
   */
  fun promoteTask(infoHash: String, selectedFiles: List<String>? = null): Boolean {
    val t = taskOf(infoHash) ?: return false
    val root = cacheRoot
    if (root == null) {
      log("转正中止：缓存根未知")
      return false
    }
    val plan = planPromote(t.infoHash, root, t.saveDir?.path ?: "")
    if (!plan.valid) {
      log("转正中止：${plan.error}")
      listener.onStatus(plan.error)
      return false
    }
    val h = t.handle
    if (h == null || !isHandleUsable(h)) {
      log("转正中止：句柄不可用（${t.infoHash.take(12)}…）")
      listener.onStatus("转正失败：句柄不可用（任务可能已被移除）")
      return false
    }
    val toDir = plan.toDir!!
    val selected =
      (selectedFiles ?: t.selected).ifEmpty { t.result?.viewFiles?.map { it.path } ?: emptyList() }
    if (!toDir.exists() && !toDir.mkdirs()) {
      log("转正中止：无法创建下载目录 ${toDir.path}")
      listener.onStatus("转正失败：无法创建目录 ${toDir.path}")
      return false
    }
    // 正在预览它：先停预览（搬运期间不对同一批文件读写）
    if (t.infoHash == currentInfoHash) {
      try {
        scheduler.stop()
      } catch (e: Throwable) {
        log("转正停预览异常：" + (e.message ?: e.javaClass.simpleName))
      }
      previewFile = null
    }
    try {
      if (!plan.alreadyPromoted) {
        h.pause()
        h.moveStorage(toDir.path)
        log("转正：moveStorage 已下发 ${plan.fromDir?.path} → ${toDir.path}（异步搬运，等 storage_moved 告警）")
      } else {
        log("转正：任务已在 downloads/（幂等，不重复搬运）")
      }
    } catch (e: Throwable) {
      log("转正失败（moveStorage 异常）：" + (e.message ?: e.javaClass.simpleName))
      listener.onStatus("转正失败：" + (e.message ?: e.javaClass.simpleName))
      return false
    }
    synchronized(taskLock) {
      t.download = true
      t.saveDir = toDir
      t.selected = selected
      t.state = STATE_DOWNLOADING
      t.error = ""
    }
    // 继续下载「该文件」（selected 为空 = 全部非 pad 文件）
    activateTask(t, selected)
    requestResumeDataFor(t, "转正")
    listener.onTaskStateChanged(t.infoHash, STATE_DOWNLOADING)
    listener.onStatus("已转正：数据目录 → ${toDir.path}（继续下载 ${selected.size} 个文件）")
    publishActivity()
    return true
  }

  /** 让任务真正开始下载（桌面 `activate_download`：解 upload_mode → 按 selected 置文件优先级 → auto_managed → resume）。 */
  private fun activateTask(t: EngineTask, selected: List<String>? = null) {
    val h = t.handle
    if (h == null || !isHandleUsable(h)) {
      log("激活任务中止：句柄不可用（${t.infoHash.take(12)}…）")
      return
    }
    try {
      h.unsetFlags(TorrentFlags.UPLOAD_MODE)
      h.setFlags(TorrentFlags.AUTO_MANAGED)
      val sel = (selected ?: t.selected).toSet()
      val ti = h.torrentFile()
      if (ti != null) {
        val n = ti.numFiles()
        if (n > 0) {
          val prio = Priority.array(Priority.IGNORE, n)
          val pathByIdx = HashMap<Int, String>()
          t.result?.files?.forEach { f -> pathByIdx[f.index] = f.path }
          for (i in 0 until n) {
            val p = pathByIdx[i]
            // selected 为空 = 全选（桌面 `not selected or f.path in selected` 的同一判式）
            if (sel.isEmpty() || (p != null && p in sel)) prio[i] = Priority.DEFAULT
          }
          h.prioritizeFiles(prio)
        }
      }
      if (t.priority > 0) applyQueueOrder()
      h.resume()
      log("任务已激活：${t.infoHash.take(12)}… 选中 ${sel.size} 个文件（selected 空 = 全选）")
    } catch (e: Throwable) {
      log("激活任务异常（${t.infoHash.take(12)}…）：" + (e.message ?: e.javaClass.simpleName))
    }
  }

  /**
   * **移除任务**（桌面 `remove_task`）：断句柄（可选连带删文件）→ 摘注册表 →
   * `deleteData=true` 时删除落盘目录。
   *
   * **删数据必须过守卫**（`data/planRemoveTask`）：目录要在受管根内**且**目录名 == info_hash；
   * 守卫拒绝时数据一律保留——绝不删到用户其它数据。
   */
  fun removeTask(infoHash: String, deleteData: Boolean = false): Boolean {
    val t = taskOf(infoHash) ?: return false
    val root = cacheRoot
    val plan = planRemoveTask(t.infoHash, t.saveDir?.path ?: "", root ?: File("."), deleteData)
    if (plan.rejected) {
      log("移除任务：守卫拒绝删数据 → ${plan.error}")
      listener.onStatus("已移除任务记录，但**拒绝删除**数据目录：" + plan.error)
    }
    if (t.infoHash == currentInfoHash) {
      try {
        if (scheduler.active) scheduler.stop()
      } catch (e: Throwable) {
        log("移除任务停预览异常：" + (e.message ?: e.javaClass.simpleName))
      }
      previewFile = null
    }
    val h = t.handle
    if (h != null && isRunning() && isHandleUsable(h)) {
      try {
        val ses = SessionHandle(manager.swig())
        if (plan.deleteData) {
          ses.removeTorrent(h, SessionHandle.DELETE_FILES)
        } else {
          ses.removeTorrent(h)
        }
      } catch (e: Throwable) {
        log("移除任务断句柄异常（已忽略）：" + (e.message ?: e.javaClass.simpleName))
      }
    }
    val outcome = if (root != null) applyRemoveTask(plan, root) else RemoveOutcome(note = plan.note)
    synchronized(taskLock) {
      taskMap.remove(t.infoHash)
      taskAliases.entries.removeAll { it.value == t.infoHash }
    }
    if (currentInfoHash == t.infoHash) {
      currentInfoHash = ""
      currentHandle = null
      currentResult = null
      saveDir = null
      previewFile = null
      metaFetching = false
    } else {
      applyFocusAliasesToCurrentOrClear()
    }
    log(
      "移除任务：${t.infoHash.take(12)}…（删数据=${plan.deleteData} 结果=${outcome.dataDeleted}，" +
        "守卫拒绝=${plan.rejected}，resume 删除=${outcome.resumeDeleted}）${plan.note}"
    )
    listener.onTaskStateChanged(t.infoHash, STATE_DELETED)
    listener.onStatus(
      "已移除任务（${if (plan.deleteData) "数据已删除" else if (plan.rejected) "数据保留：守卫拒绝删除" else "数据保留"}）"
    )
    publishActivity()
    return true
  }

  /** 焦点任务被摘除后，把别名指向剩余的第一个任务（或清空）。 */
  private fun applyFocusAliasesToCurrentOrClear() {
    val next = synchronized(taskLock) { taskMap.values.minByOrNull { it.order } }
    if (next == null) {
      currentInfoHash = ""
      currentHandle = null
      currentResult = null
      saveDir = null
    } else if (taskOf(currentInfoHash) == null) {
      applyFocusAliases(next)
      log("焦点任务已摘除：焦点顺延到 ${next.infoHash.take(12)}…")
    }
  }

  /** 按任务请求 fastresume（多任务：不再假设「当前焦点就是它」）。 */
  private fun requestResumeDataFor(t: EngineTask, reason: String) {
    val h = t.handle ?: return
    if (!isResumeKey(t.infoHash)) return
    if (h == null || !isHandleUsable(h)) return
    try {
      h.setFlags(TorrentFlags.NEED_SAVE_RESUME)
      h.saveResumeData(TorrentHandle.SAVE_INFO_DICT.or_(TorrentHandle.FLUSH_DISK_CACHE))
      log("已请求 fastresume（${reason.ifEmpty { "-" }}）：${t.infoHash.take(12)}…（含 info-dict）")
    } catch (e: Throwable) {
      log("请求 fastresume 异常（$reason）：" + (e.message ?: e.javaClass.simpleName))
    }
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
      scheduler.begin(TorrentHandleOps(h), file)
      markPreviewTarget(file)
      listener.onStatus("预览开始：${file.name}（只下这一个文件的分块）")
      true
    } catch (e: Throwable) {
      log("预览启动失败：" + (e.message ?: e.javaClass.simpleName))
      listener.onStatus("预览启动失败：" + (e.message ?: e.javaClass.simpleName))
      false
    }
  }

  /**
   * **切换预览目标**（画廊翻页 / 点开下一张图时用；A 项的入口）。
   *
   * 与 [beginPreview] 的区别只有一条：换目标时**先清旧预约再重建窗口**
   * （[PreviewScheduler.switchTarget]），同目标幂等。图片与视频共用这一条路径——
   * `beginPreview` 本来就与文件类型无关（调度器只按分块窗口干活），所以图片走
   * 「单文件优先 + 按需补拉」的现成路径，不需要第二套下载逻辑。
   *
   * @return false = 没有可用句柄/切换失败（调用方保留旧目标，不当作已切换）
   */
  fun switchPreviewTarget(file: TorrentFile): Boolean {
    val h = currentHandle ?: return false
    if (!isHandleUsable(h)) {
      log("切换预览目标中止：句柄失效")
      currentHandle = null
      listener.onMetadataError(HANDLE_INVALID_TEXT)
      return false
    }
    return try {
      val ok = scheduler.switchTarget(TorrentHandleOps(h), file)
      if (!ok) {
        log("切换预览目标失败：调度器拒绝（句柄不可用）")
        listener.onStatus("切换预览目标失败：句柄不可用")
        return false
      }
      markPreviewTarget(file)
      listener.onStatus("预览目标已切换：${file.name}（只下这一个文件的分块）")
      true
    } catch (e: Throwable) {
      log("切换预览目标失败：" + (e.message ?: e.javaClass.simpleName))
      listener.onStatus("切换预览目标失败：" + (e.message ?: e.javaClass.simpleName))
      false
    }
  }

  /**
   * 预览目标的固定善后：记录当前文件 → 任务转下载型并记住选中文件（转正/重启续传都靠它）
   * → 请求 fastresume → 发布活动态。[beginPreview] / [switchPreviewTarget] 共用。
   */
  private fun markPreviewTarget(file: TorrentFile) {
    previewFile = file
    val t = taskOf(currentInfoHash)
    if (t != null) {
      synchronized(taskLock) {
        t.download = true
        t.selected = listOf(file.path)
        t.state = STATE_DOWNLOADING
        t.error = ""
      }
      requestResumeDataFor(t, "预览开始")
    } else {
      requestResumeData("预览开始")
    }
    publishActivity()
  }

  /** 当前预览目标在清单里的文件下标（无预览任务 → -1）。 */
  fun previewFileIndex(): Int = previewFile?.index ?: -1

  /**
   * **当前预览文件的下载读数**（A 项要的「该文件的下载进度」）。
   *
   * 口径 = 桌面 `status["file_progress"][f.index]`：只管**这一个文件**已下多少字节；
   * 无预览任务 / 无句柄 / 会话已停 一律返回 null（UI 据此显示「未开始」而不是 0%）。
   */
  fun previewFileReading(): FileDownloadReading? {
    val f = previewFile ?: return null
    val h = currentHandle ?: return null
    if (!isRunning() || !isHandleUsable(h)) return null
    return try {
      val fp = h.fileProgress()
      val got = if (f.index in fp.indices) fp[f.index] else 0L
      FileDownloadReading(f.index, f.name, f.size, got)
    } catch (e: Throwable) {
      log("预览文件读数异常：" + (e.message ?: e.javaClass.simpleName))
      null
    }
  }

  /** **当前预览文件是否下载完成**（A 项要的判定；桌面 `progress[idx] >= f.size`）。 */
  fun previewFileComplete(): Boolean = previewFileReading()?.complete == true

  /**
   * 当前任务各文件的下载读数（键 = 文件下标；无句柄/无清单 → 空表）。
   *
   * 画廊网格按它给每张图算进度（未下载的显示占位 + 进度），不必为每张图单独问句柄。
   */
  fun fileReadings(): List<FileDownloadReading> {
    val res = currentResult ?: return emptyList()
    val h = currentHandle ?: return emptyList()
    if (!isRunning() || !isHandleUsable(h)) return emptyList()
    return try {
      val fp = h.fileProgress()
      res.files.map { f ->
        FileDownloadReading(f.index, f.name, f.size, if (f.index in fp.indices) fp[f.index] else 0L)
      }
    } catch (e: Throwable) {
      log("文件读数批量读取异常：" + (e.message ?: e.javaClass.simpleName))
      emptyList()
    }
  }

  /**
   * 暂停预览（**hold 档语义**）：清 deadline + 文件优先级全 0 + 撤 AUTO_MANAGED + pause。
   *
   * 逐条对齐桌面 `core/scheduler.py:stop(release_only=False)` 的句柄动作；
   * 与桌面全量 stop 的唯一区别是**保留句柄/文件与进度查询**——暂停后缓冲读数应当
   * 冻结（而不是掉回 0：数据其实一点没少），[resumePreview] 才能原地继续。
   *
   * @return false = 当前没有预览任务
   */
  fun pausePreview(): Boolean {
    if (!scheduler.active) {
      log("暂停预览：当前没有预览任务")
      return false
    }
    scheduler.pause()
    // 暂停点：fastresume 必须落盘（下一次启动靠它续传）
    requestResumeData("预览暂停")
    listener.onStatus("预览已暂停（缓冲不再增长；「继续」可恢复）")
    publishActivity()
    return true
  }

  /**
   * 继续预览：把 [pausePreview]（或 convert 档的释放）撤掉——重放优先级/auto_managed/resume
   * 并重建「尾部窗口 + 顺序窗口」。句柄失效时先按 info_hash 从会话重取自有副本。
   *
   * @return true = 已重新拉起（下载继续推进）
   */
  fun resumePreview(): Boolean {
    val f = previewFile
    if (f == null) {
      log("继续预览：没有可恢复的预览任务")
      return false
    }
    var h = currentHandle
    if (h == null || !isHandleUsable(h)) {
      log("继续预览：句柄不可用，尝试按 info_hash 重取自有副本")
      h = reacquireHandle(listOf(currentInfoHash))
      if (h == null) {
        listener.onMetadataError(HANDLE_INVALID_TEXT)
        return false
      }
      currentHandle = h
    }
    val ok =
      try {
        scheduler.resume(TorrentHandleOps(h))
      } catch (e: Throwable) {
        log("继续预览失败：" + (e.message ?: e.javaClass.simpleName))
        false
      }
    if (ok) requestResumeData("预览继续")
    listener.onStatus(if (ok) "预览已恢复" else "继续预览失败：调度器不可用")
    publishActivity()
    return ok
  }

  /**
   * 停止预览。**语义由 [previewCacheMode] 决定**（逐条对齐桌面
   * `core/scheduler.py:stop(release_only=…)`）：
   *
   * - `hold`：等同 [pausePreview] —— 冻结（清 deadline + 优先级全 0 + 撤 auto_managed + pause）；
   * - `convert`（默认）：只清 deadline + 还原锚点，**不 pause、不清优先级、不撤 auto_managed** ——
   *   `begin()` 置下的 file-priority 继续生效（引擎本就在全文件落盘），是否转正由调用方决定。
   */
  fun stopPreview() {
    if (cacheMode == PREVIEW_CACHE_HOLD) {
      log("停止预览（hold 档）：冻结暂停")
      pausePreview()
      return
    }
    log("停止预览（convert 档）：只清 deadline / 还原锚点，不 pause、不清优先级")
    try {
      scheduler.stop(releaseOnly = true)
    } catch (e: Throwable) {
      log("停止预览异常：" + (e.message ?: e.javaClass.simpleName))
    }
    requestResumeData("停止预览")
    publishActivity()
  }

  fun previewTick() {
    if (scheduler.active) scheduler.tick()
  }

  fun tailEntryReady(): Boolean = scheduler.tailEntryReady()

  fun bufferProgress(): Double = scheduler.bufferProgress()

  fun bufferedBytes(): Long = scheduler.contiguousProgress()

  fun previewFileSize(): Long = previewFile?.size ?: 0L

  /**
   * 跳转（字节偏移，文件内相对）。
   *
   * 真正的动作在 [PreviewScheduler.seekToByte]：**必须先 `clearPieceDeadlines()`
   * 再重建窗口**——残留的旧位置 ASAP 预约会继续以最高优先级抢带宽，把已缓存区域
   * 拖卡（桌面 `scheduler.seek_to_byte` 的原注释）。暂停/释放状态下忽略跳转。
   */
  fun previewSeek(byteOffset: Long) {
    if (!scheduler.active) {
      log("跳转忽略：当前没有预览任务")
      return
    }
    scheduler.seekToByte(byteOffset)
    publishActivity()
  }

  // ---------------------------------------------------------------- 热更新（代理 / 限速 / 超时 / 缓存模式）

  /**
   * 热更新代理（无需重建会话）。映射走 [proxySettingsFor]（桌面 `core/config.py:lt_proxy_settings`
   * 的移植）：未启用代理或主机为空 → `proxy_type=none`，**且** peer/tracker 两条连接开关
   * **一起**回落 false（只重置 proxy_type 会让 tracker 继续走旧代理——历史缺陷）。
   */
  fun applyProxy(cfg: ProxyConfig) {
    lastProxy = cfg
    val s = proxySettingsFor(cfg)
    log(
      "应用代理：type=${s.proxyType} host=${s.hostname ?: "-"} port=${s.port ?: "-"} " +
        "peer=${s.peerConnections} tracker=${s.trackerConnections} 会话运行中=${isRunning()}"
    )
    if (!isRunning()) {
      listener.onStatus("代理设置已记录，将在会话启动时生效")
      return
    }
    applyProxyNow(cfg)
  }

  private fun applyProxyNow(cfg: ProxyConfig) {
    try {
      val sp = SettingsPack()
      applyProxySettings(sp, proxySettingsFor(cfg))
      manager.applySettings(sp)
      val s = proxySettingsFor(cfg)
      val text =
        if (s.proxyType == LtProxyType.NONE) "直连（已回退代理开关）"
        else "${s.hostname}:${s.port}（peer=${s.peerConnections}）"
      listener.onStatus("代理已更新：$text")
    } catch (e: Throwable) {
      log("应用代理失败：" + (e.message ?: e.javaClass.simpleName))
      listener.onStatus("代理设置应用失败：" + (e.message ?: e.javaClass.simpleName))
    }
  }

  /**
   * 热更新下载限速（KB/s，0 = 不限）。libtorrent 单位是**字节/秒**，配置层用 KB/s
   * （桌面 `session.apply_rate_limit` 同款换算：`kbps * 1024`，负数按 0 处理）。
   */
  fun applyRateLimit(kbps: Int) {
    val v = maxOf(0, kbps)
    lastRateLimitKbps = v
    log("应用下载限速：$v KB/s（0 = 不限）会话运行中=${isRunning()}")
    if (!isRunning()) {
      listener.onStatus("限速设置已记录，将在会话启动时生效")
      return
    }
    applyRateLimitNow(v)
  }

  private fun applyRateLimitNow(kbps: Int) {
    try {
      val sp = SettingsPack()
      sp.downloadRateLimit(maxOf(0, kbps) * 1024) // libtorrent 单位：字节/秒
      manager.applySettings(sp)
      listener.onStatus(if (kbps <= 0) "已取消下载限速" else "下载限速 $kbps KB/s")
    } catch (e: Throwable) {
      log("应用限速失败：" + (e.message ?: e.javaClass.simpleName))
      listener.onStatus("限速设置应用失败：" + (e.message ?: e.javaClass.simpleName))
    }
  }

  /**
   * 热更新元数据获取超时（秒）。桌面 `session.start(metadata_timeout=…)` 的 Android 落点：
   * 超时判定为「资源无做种」。实际计时由本引擎在 [tick] 心跳里做
   * （<=0 视为无效输入，忽略，与桌面 `if metadata_timeout:` 的真值判断一致）。
   */
  fun applyMetadataTimeout(seconds: Int) {
    if (seconds <= 0) {
      log("元数据超时设置忽略：$seconds（<=0）")
      return
    }
    metadataTimeoutSeconds = seconds
    log("元数据超时已设为 ${seconds}s")
    listener.onStatus("元数据超时：${seconds}s")
  }

  fun metadataTimeoutSeconds(): Int = metadataTimeoutSeconds

  /**
   * 热更新预览缓存模式（值域 `convert` / `hold`，见 [normalizePreviewCacheMode]）。
   * 值域之外**忽略**本次设置并记日志（不改变现状）。
   */
  fun setPreviewCacheMode(mode: String) {
    val v = normalizePreviewCacheMode(mode)
    if (v == null) {
      log("预览缓存模式设置忽略：$mode（值域 convert/hold）")
      return
    }
    cacheMode = v
    log("预览缓存模式已设为 $v")
    listener.onStatus("预览缓存模式：$v")
  }

  fun previewCacheMode(): String = cacheMode

  // ---------------------------------------------------------------- 活动态（前台服务用）

  /** 一次受守卫的 status 读取（句柄不可用/会话停止 → null）。 */
  private fun statusReadingOrNull(): StatusReading? {
    val h = currentHandle ?: return null
    if (!isRunning() || !isHandleUsable(h)) return null
    return when (val r = readHandleStatus(TorrentHandleProbe(h))) {
      is HandleReadResult.Ok -> r.reading
      else -> null
    }
  }

  /** 当前阶段（[EngineActivity] 的 phase，见 EngineActivity.kt 的取值语义）。 */
  private fun currentPhase(): String {
    val s = scheduler
    return when {
      metaFetching -> PHASE_META
      s.active && s.released -> {
        // convert 档释放：文件按优先级继续缓存；下完（progress >= 1）即视为无任务
        val p = statusReadingOrNull()?.progress
        if (p != null && p < 1f) PHASE_DOWNLOAD else PHASE_IDLE
      }
      s.active && s.paused -> PHASE_PAUSED
      s.active -> PHASE_PREVIEW
      else -> PHASE_IDLE
    }
  }

  /**
   * 发布活动态（前台服务据此驻留/更新通知/持唤醒锁）。
   *
   * [note] 用于把一次性提示带进通知（如「前台服务超时」）。
   */
  private fun publishActivity(note: String = "") {
    try {
      val phase = currentPhase()
      val reading = statusReadingOrNull()
      val result = currentResult
      val f = previewFile
      val title =
        when {
          f != null -> f.name
          result != null -> result.name
          else -> currentInfoHash
        }
      val percent =
        if (phase == PHASE_PREVIEW || phase == PHASE_PAUSED || phase == PHASE_DOWNLOAD) {
          (scheduler.bufferProgress() * 100.0).toInt().coerceIn(0, 100)
        } else {
          0
        }
      EngineHub.publish(
        EngineActivity(
          phase = phase,
          title = title,
          percent = percent,
          speedKbps = reading?.downloadRateKbps ?: 0,
          peers = reading?.peers ?: 0,
          note = note,
        )
      )
    } catch (e: Throwable) {
      log("活动态发布异常：" + (e.message ?: e.javaClass.simpleName))
    }
  }

  // ---------------------------------------------------------------- EngineControl（前台服务动作）

  /**
   * 心跳（前台服务每秒调用一次，UI 轮询里也会间接走到）：
   * 1. 推进预览窗口；2. 元数据超时判定；3. 刷新活动态快照。
   */
  override fun tick() {
    try {
      if (scheduler.active) scheduler.tick()
    } catch (e: Throwable) {
      log("心跳 tick 异常：" + (e.message ?: e.javaClass.simpleName))
    }
    checkMetadataTimeout()
    publishActivity()
  }

  /**
   * 解析阶段超时判定（[applyMetadataTimeout] 的落点）：超时判定为「资源无做种」。
   *
   * **每任务独立计时**（多任务：一个任务没做种不该把别人的钟一起打掉）。焦点任务的超时
   * 额外走 [Listener.onMetadataError]（UI 的 busy 态据此收尾）。
   */
  private fun checkMetadataTimeout() {
    val limit = metadataTimeoutSeconds.toLong() * 1000L
    if (limit <= 0) return
    val now = System.currentTimeMillis()
    val timedOut = ArrayList<EngineTask>(2)
    synchronized(taskLock) {
      for (t in taskMap.values) {
        if (t.state != STATE_META_FETCH) continue
        val start = t.metaStartedMs
        if (start <= 0L) continue
        if (now - start < limit) continue
        t.metaStartedMs = 0L
        t.state = STATE_FAILED
        t.error = "资源无做种（${metadataTimeoutSeconds}s 内未获取到元数据）"
        timedOut.add(t)
      }
    }
    for (t in timedOut) {
      val msg = t.error
      log("元数据超时（${t.infoHash.take(12)}…）：$msg")
      listener.onTaskStateChanged(t.infoHash, STATE_FAILED)
      listener.onMetadataError(msg, t.infoHash)
    }
  }

  override fun onPauseRequest() {
    log("前台服务动作：暂停")
    if (!pausePreview()) listener.onStatus("当前没有可暂停的预览任务")
  }

  override fun onResumeRequest() {
    log("前台服务动作：继续")
    if (!resumePreview()) listener.onStatus("继续失败：没有可恢复的预览任务或句柄已失效")
  }

  override fun onStopRequest() {
    log("前台服务动作：停止")
    stopPreview()
    listener.onStatus("已停止当前任务（模式：$cacheMode）")
  }

  /**
   * 前台服务超时（Android 15+ 对 `dataSync` 有 6h/24h 上限，见 DownloadService.onTimeout）：
   * **优雅暂停任务**并把原因交给 UI/通知显示；服务随后自行退场。
   */
  override fun onTimeoutPause() {
    log("前台服务超时：优雅暂停任务")
    val ok = pausePreview()
    listener.onStatus(
      if (ok) "前台服务到达系统时限，下载已暂停（可在应用内继续）" else "前台服务到达系统时限，任务已转入暂停"
    )
    publishActivity(note = FGS_TIMEOUT_TEXT)
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
      return StatusReading(st.numPeers(), st.numSeeds(), st.downloadRate() / 1024, st.progress())
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
    const val FGS_TIMEOUT_TEXT = "前台服务已到系统时限，下载已暂停"
  }
}
