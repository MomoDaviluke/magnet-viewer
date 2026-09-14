package com.bitseed.magnetviewer.ui.magnet

import android.content.Context
import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.bitseed.magnetviewer.core.PREVIEW_CACHE_HOLD
import com.bitseed.magnetviewer.core.model.ParseResult
import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.core.model.humanSize
import com.bitseed.magnetviewer.data.AppSettings
import com.bitseed.magnetviewer.data.MagnetStorage
import com.bitseed.magnetviewer.data.SettingsStore
import com.bitseed.magnetviewer.engine.BtEngine
import com.bitseed.magnetviewer.service.DownloadService
import com.bitseed.magnetviewer.stream.StreamServer
import java.util.Locale
import kotlin.math.roundToInt
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** 界面用文件行（已过滤 .pad）。 */
data class MagnetFileItem(
  val index: Int,
  val name: String,
  val path: String,
  val sizeText: String,
  val isVideo: Boolean,
)

data class MagnetUiState(
  val input: String = "",
  val busy: Boolean = false,
  val statusText: String = "",
  val errorText: String? = null,
  val torrentName: String = "",
  val infoHash: String = "",
  val totalSizeText: String = "",
  val files: List<MagnetFileItem> = emptyList(),
  val previewIndex: Int = -1,
  val previewTitle: String = "",
  val previewNote: String = "",
  val bufferPercent: Float = 0f,
  val bufferText: String = "",
  val streamUrl: String = "",
  /** 预览是否处于「暂停（冻结）」态（hold 语义；缓冲读数应当不动）。 */
  val previewPaused: Boolean = false,
  /** 当前生效的预览缓存模式：`convert` / `hold`（停止预览的结果文案据此区分）。 */
  val cacheMode: String = "convert",
  /** 拖动条位置（文件比例 0..1，拖动结束时才有值）。 */
  val seekFraction: Float = 0f,
  /** 最近一次 seek 的目标字节位置（传给 `previewSeek(byteOffset)`）。 */
  val seekByte: Long = 0L,
  /** seek 请求序号（每次 +1，播放器据此触发一次真实 seek）。 */
  val seekSeq: Int = 0,
  val seekNote: String = "",
  /** 设置页是否打开。 */
  val settingsOpen: Boolean = false,
  /** 已保存（落盘）的设置。 */
  val settings: AppSettings = AppSettings(),
  val settingsNote: String = "",
  /** 通知权限（API 33+ 运行时权限）：null = 未询问过。 */
  val notificationsGranted: Boolean? = null,
)

/**
 * 主界面状态机：磁力链解析（DHT 拿元数据）→ 文件清单 → 点视频边下边播。
 *
 * 现场约束：`SessionManager.stop()` 会阻塞在 native 销毁，必须走 IO 线程；
 * 频繁/阻塞的 libtorrent 调用一律 `Dispatchers.IO`，主线程只碰 StateFlow。
 *
 * 本类同时是**引擎能力的接线点**（UI → 引擎）：
 * - 解析/预览开始 → [DownloadService.start]（前台服务驻留，灭屏继续跑）；
 * - 停预览 → [DownloadService.stopIfIdle]（有任务时服务自己会继续驻留）；
 * - 设置流 → `applyProxy / applyRateLimit / applyMetadataTimeout / setPreviewCacheMode`；
 * - 文件行 → `pausePreview / resumePreview / stopPreview`；
 * - 拖动条 → `previewSeek(byteOffset)`（字节偏移 = 比例 × `previewFileSize()`）。
 */
class MagnetViewModel(private val appContext: Context, settingsStore: SettingsStore? = null) : ViewModel() {

  private val store: SettingsStore? =
    settingsStore ?: runCatching { SettingsStore.instance(appContext) }.getOrElse { e -> log("设置存储不可用：" + e.message); null }

  private val engine =
    BtEngine(
      object : BtEngine.Listener {
        override fun onMetadataReady(infoHash: String, result: ParseResult) {
          watchdog?.cancel()
          currentFiles = result.files
          log("UI 收到元数据就绪：name=${result.name} 文件数=${result.files.size} infohash=$infoHash")
          _state.update { st ->
            st.copy(
              busy = false,
              errorText = null,
              statusText = "元数据就绪（未下载本体）",
              torrentName = result.name,
              infoHash = infoHash,
              totalSizeText = humanSize(result.totalSize),
              files =
                result.viewFiles.map { f ->
                  MagnetFileItem(f.index, f.name, f.path, humanSize(f.size), f.isVideo)
                },
            )
          }
        }

        override fun onMetadataError(message: String) {
          watchdog?.cancel()
          log("UI 收到元数据错误：$message")
          _state.update { it.copy(busy = false, statusText = "", errorText = message) }
        }

        override fun onStatus(message: String) {
          log("UI 状态：$message")
          _state.update { it.copy(statusText = message) }
        }
      }
    )

  private fun log(message: String) {
    try {
      Log.i(TAG, message)
    } catch (e: Throwable) {
      // android.util.Log 未 mock 时也不能崩
    }
  }

  // 注意：这里不要引用 engine（engine 的构造回调闭包又引用 _state → 初始化递归，Kotlin 无法推断类型）。
  // MagnetUiState 的 cacheMode 默认值就是引擎默认档（convert）；真实值由设置流/轮询回填。
  private val _state = MutableStateFlow(MagnetUiState())
  val state: StateFlow<MagnetUiState> = _state.asStateFlow()

  private var server: StreamServer? = null
  private var currentFiles: List<TorrentFile> = emptyList()
  private var watchdog: Job? = null
  private var pollJob: Job? = null
  private var settingsJob: Job? = null

  /**
   * 前台服务的启停闸门（**实证过的坑**：`startForegroundService` 与 `stopService` 打架时，
   * 服务会在 `startForeground()` 之前被拆掉，系统抛
   * `ForegroundServiceDidNotStartInTimeException` 直接把进程干掉）。
   *
   * 规则：任何「起服务」都会把 [serviceGeneration] 加一，让在途的「无任务就退场」回调失效；
   * 两边的判断与调用都在同一个锁里，杜绝「先 stop 再 start」的交叉。
   */
  private val serviceGate = Any()
  private var serviceGeneration = 0

  init {
    startPolling()
    observeSettings()
  }

  // ---------------------------------------------------------------- 设置（读 → 落盘 → 热更新）

  /**
   * 订阅设置流：**任何一次保存（含其他入口）都会重新下发到引擎**——这就是「保存即生效」。
   *
   * 下发顺序固定（代理 → 限速 → 超时 → 缓存模式），且都在 IO 线程（`applySettings` 走 swig）。
   */
  private fun observeSettings() {
    val s = store ?: run {
      log("设置存储缺失：跳过设置订阅（UI 用默认值）")
      return
    }
    settingsJob?.cancel()
    settingsJob =
      viewModelScope.launch {
        try {
          s.settings.collect { v ->
            _state.update { it.copy(settings = v) }
            applySettings(v)
            log("设置已下发引擎：代理=${v.proxyType}/${v.proxyHost}:${v.proxyPort} 限速=${v.downloadRateLimit}KB/s 超时=${v.metadataTimeout}s 缓存模式=${v.previewCacheMode} 主题=${v.uiTheme}")
          }
        } catch (e: CancellationException) {
          throw e
        } catch (e: Throwable) {
          log("设置流异常：" + (e.message ?: e.javaClass.simpleName))
        }
      }
  }

  /** 设置 → 引擎热更新接口（BT 引擎侧已有能力，这里只接线）。 */
  private suspend fun applySettings(s: AppSettings) {
    val v = s.sanitized()
    withContext(Dispatchers.IO) {
      runCatching { engine.applyProxy(v.toProxyConfig()) }
      runCatching { engine.applyRateLimit(v.downloadRateLimit) }
      runCatching { engine.applyMetadataTimeout(v.metadataTimeout) }
      runCatching { engine.setPreviewCacheMode(v.previewCacheMode) }
    }
    _state.update { it.copy(cacheMode = engine.previewCacheMode()) }
  }

  /** 设置页「保存并生效」：落盘 + 下发 + 更新提示。 */
  fun saveSettings(s: AppSettings) {
    val v = s.sanitized()
    viewModelScope.launch {
      try {
        store?.save(v)
        applySettings(v)
        _state.update {
          it.copy(
            settings = v,
            settingsNote =
              "已保存并生效：主题=${v.uiTheme} 限速=${v.downloadRateLimit}KB/s 超时=${v.metadataTimeout}s 缓存模式=${v.previewCacheMode} 代理=${v.proxyType}",
          )
        }
      } catch (e: Throwable) {
        log("保存设置失败：" + (e.message ?: e.javaClass.simpleName))
        _state.update { it.copy(settingsNote = "保存失败：" + (e.message ?: e.javaClass.simpleName)) }
      }
    }
  }

  fun openSettings() {
    _state.update { it.copy(settingsOpen = true, settingsNote = "") }
  }

  fun closeSettings() {
    _state.update { it.copy(settingsOpen = false, settingsNote = "") }
  }

  /** Android 13+ 通知权限结果（被拒也不影响下载/预览，只是没有通知）。 */
  fun onNotificationPermissionResult(granted: Boolean) {
    log("通知权限结果：granted=$granted")
    _state.update {
      it.copy(
        notificationsGranted = granted,
        statusText = if (granted) it.statusText else "通知权限被拒：后台任务照常运行，只是没有通知",
      )
    }
  }

  // ---------------------------------------------------------------- 输入 / 解析

  fun onInputChange(value: String) {
    _state.update { it.copy(input = value) }
  }

  fun resolve() {
    val uri = _state.value.input.trim()
    if (!uri.startsWith("magnet:?")) {
      _state.update { it.copy(errorText = "请输入合法磁力链接（以 magnet:? 开头）") }
      return
    }
    log("点击解析：uri 长度=${uri.length}")
    stopPreviewInternal(wantStopService = false)
    currentFiles = emptyList()
    // 解析即开工：先把前台服务拉起来（应用在前台，允许 startForegroundService），
    // 灭屏/退后台后 DHT 取元数据与后续下载才有驻留保障。
    startServiceForTask()
    val timeoutSeconds = _state.value.settings.metadataTimeout
    _state.update {
      it.copy(
        busy = true,
        errorText = null,
        statusText = "正在通过 DHT 获取元数据（不下载本体）…",
        torrentName = "",
        infoHash = "",
        totalSizeText = "",
        files = emptyList(),
        previewIndex = -1,
        previewTitle = "",
        previewNote = "",
        bufferPercent = 0f,
        bufferText = "",
        streamUrl = "",
        previewPaused = false,
        seekFraction = 0f,
        seekByte = 0L,
        seekNote = "",
      )
    }
    viewModelScope.launch {
      val cacheRoot = withContext(Dispatchers.IO) { MagnetStorage.cacheRoot(appContext) }
      val dir = withContext(Dispatchers.IO) { engine.addMagnet(uri, cacheRoot) }
      if (dir == null) return@launch // 失败原因已由 listener.onMetadataError 上报
      watchdog?.cancel()
      watchdog =
        viewModelScope.launch {
          delay(timeoutSeconds * 1000L)
          if (_state.value.busy) {
            _state.update {
              it.copy(busy = false, statusText = "", errorText = "资源无做种（${timeoutSeconds}s 内未获取到元数据）")
            }
          }
        }
    }
  }

  // ---------------------------------------------------------------- 预览

  /** 点视频文件：启动预览调度 → 本地流服务 → 交给 Media3 播放。 */
  fun preview(fileIndex: Int) {
    val file = currentFiles.firstOrNull { it.index == fileIndex } ?: return
    if (!file.isVideo) {
      _state.update { it.copy(errorText = "本 demo 只支持视频预览：" + file.name) }
      return
    }
    viewModelScope.launch {
      _state.update {
        it.copy(
          previewIndex = file.index,
          previewTitle = file.name,
          previewNote = "正在预约索引块与头部数据…",
          bufferPercent = 0f,
          bufferText = "",
          streamUrl = "",
          errorText = null,
          previewPaused = false,
          seekFraction = 0f,
          seekByte = 0L,
          seekNote = "",
        )
      }
      startServiceForTask()
      val ok = withContext(Dispatchers.IO) { engine.beginPreview(file) }
      if (!ok) {
        _state.update { it.copy(previewIndex = -1, previewNote = "", errorText = "预览启动失败（句柄不可用）") }
        return@launch
      }
      val root = withContext(Dispatchers.IO) { engine.previewRoot() }
      if (root == null) {
        _state.update { it.copy(previewIndex = -1, errorText = "预览目录不可用") }
        return@launch
      }
      val srv =
        server
          ?: withContext(Dispatchers.IO) {
            StreamServer(listOf(root), engine.pieceSource()).also { it.start() }
          }
      server = srv
      val url = srv.urlFor(file.path)
      // 开播门控：文件头有连续数据 + 尾部入口块就绪（MP4 的 moov 在最末尾）
      val ready =
        withContext(Dispatchers.IO) {
          val deadline = System.currentTimeMillis() + PREVIEW_GATE_TIMEOUT_MS
          while (System.currentTimeMillis() < deadline) {
            engine.previewTick()
            if (engine.bufferedBytes() > 0 && engine.tailEntryReady()) return@withContext true
            delay(300)
          }
          false
        }
      if (!ready) {
        _state.update { it.copy(previewNote = "索引块/头部数据等待超时（可能无做种）") }
        return@launch
      }
      _state.update { it.copy(previewNote = "索引块就绪，开始播放", streamUrl = url) }
    }
  }

  /** 暂停预览（冻结：缓冲读数不动；「继续」原地恢复）。 */
  fun pausePreview() {
    if (_state.value.previewIndex < 0) {
      _state.update { it.copy(errorText = "当前没有预览任务可暂停") }
      return
    }
    viewModelScope.launch {
      val ok = withContext(Dispatchers.IO) { engine.pausePreview() }
      val mode = engine.previewCacheMode()
      _state.update {
        it.copy(
          previewPaused = ok,
          cacheMode = mode,
          statusText =
            if (ok) "预览已暂停（hold 语义：deadline 已清 / 优先级 0 / pause；缓冲冻结）"
            else "暂停失败：没有可暂停的预览任务",
        )
      }
    }
  }

  /** 继续预览（重放优先级/auto_managed/resume + 重建窗口）。 */
  fun resumePreview() {
    viewModelScope.launch {
      val ok = withContext(Dispatchers.IO) { engine.resumePreview() }
      _state.update {
        it.copy(
          previewPaused = if (ok) false else it.previewPaused,
          statusText = if (ok) "预览已恢复（缓冲继续增长）" else "继续失败：没有可恢复的预览任务或句柄已失效",
        )
      }
    }
  }

  /**
   * 停止预览（**语义由设置里的 previewCacheMode 决定**，引擎侧现读现判）：
   * - `hold`：冻结暂停（缓冲读数保留）；
   * - `convert`：只清预约/还原锚点，缓存按文件优先级继续（不暂停）。
   *
   * 停完调 [DownloadService.stopIfIdle]：仍有任务（convert 后台缓存 / 暂停等待继续）时服务继续驻留。
   */
  fun stopPreview() {
    val mode = engine.previewCacheMode()
    stopPreviewInternal(wantStopService = true)
    _state.update {
      it.copy(
        previewIndex = -1,
        previewTitle = "",
        previewNote = "",
        bufferPercent = 0f,
        bufferText = "",
        streamUrl = "",
        previewPaused = false,
        seekFraction = 0f,
        seekNote = "",
        cacheMode = mode,
        statusText =
          if (mode == PREVIEW_CACHE_HOLD) "已停止预览（hold 档：已冻结暂停，缓冲读数保留）"
          else "已停止预览（convert 档：只清预约，缓存按文件优先级继续）",
      )
    }
  }

  /**
   * 停掉引擎里的预览任务并关掉本地流服务。
   *
   * [wantStopService] = true 时才在结束时调 [DownloadService.stopIfIdle]：
   * - **解析/预览开始前**的清理必须传 false —— 紧接着就要起前台服务，
   *   否则 `stopService` 会抢在 `startForeground()` 之前把服务拆掉（系统直接杀进程）；
   * - 用户点「停止预览」时传 true（有任务时服务自己继续驻留）。
   */
  private fun stopPreviewInternal(wantStopService: Boolean) {
    val srv = server
    server = null
    val gen = synchronized(serviceGate) { ++serviceGeneration }
    viewModelScope.launch {
      withContext(Dispatchers.IO) { engine.stopPreview() }
      srv?.let { runCatching { it.close() } }
      if (wantStopService) {
        synchronized(serviceGate) {
          // 期间若又起了新任务（解析/预览），这次「退场」作废
          if (gen == serviceGeneration) DownloadService.stopIfIdle(appContext)
        }
      }
    }
  }

  /** 起前台服务（并让在途的「退场」回调失效，见 [serviceGate]）。 */
  private fun startServiceForTask() {
    synchronized(serviceGate) {
      serviceGeneration++
      DownloadService.start(appContext)
    }
  }

  // ---------------------------------------------------------------- 跳转（seek 拖动条）

  /**
   * 拖动结束：比例 → **字节偏移**（`byteOffset = 比例 × previewFileSize()`，分母与
   * [MagnetUiState.bufferPercent] 的 `bufferedBytes()/previewFileSize()` 同一个文件大小），
   * 交给引擎 `previewSeek(byteOffset)`（引擎内部先清旧 deadline 再重建窗口）。
   */
  fun previewSeek(fraction: Float) {
    val f = fraction.coerceIn(0f, 1f)
    viewModelScope.launch {
      val size = withContext(Dispatchers.IO) { engine.previewFileSize() }
      if (size <= 0L) {
        _state.update { it.copy(seekNote = "文件大小未知（尚未开始预览），忽略本次跳转") }
        return@launch
      }
      val target = (size.toDouble() * f).toLong().coerceIn(0L, maxOf(0L, size - 1L))
      val paused = _state.value.previewPaused
      withContext(Dispatchers.IO) { engine.previewSeek(target) }
      val pct = (f * 100f).roundToInt()
      log("seek：比例=$f → 字节 $target / $size（暂停中=$paused）")
      _state.update {
        it.copy(
          seekFraction = f,
          seekByte = target,
          seekSeq = it.seekSeq + 1,
          seekNote =
            if (paused) "已请求 $pct%（第 $target 字节）；预览暂停中，引擎已忽略，继续后可再拖"
            else "已跳转 $pct%（第 $target 字节），正在重排分块窗口",
        )
      }
    }
  }

  // ---------------------------------------------------------------- 轮询（缓冲进度 / peer 状态）

  /**
   * 每秒轮询一次（缓冲进度 / peer 状态）。
   *
   * 两条硬约束：
   * 1. 句柄失效（`isValid=false`）或会话已停 → **立即停止轮询**并显示错误，绝不每秒重试
   *    （失效句柄上的 native `status()` 会 SIGSEGV，`try/catch` 拦不住）。
   * 2. 整个循环体包在 try/catch 里：任何 Java 异常都只记日志 + 显示，不让轮询协程静默死掉。
   */
  private fun startPolling() {
    pollJob?.cancel()
    pollJob =
      viewModelScope.launch {
        while (isActive) {
          try {
            val previewing = _state.value.previewIndex >= 0
            if (previewing) engine.previewTick()
            val status = withContext(Dispatchers.IO) { engine.pollStatus() }
            val percent = withContext(Dispatchers.IO) { engine.bufferProgress() }
            val buffered = withContext(Dispatchers.IO) { engine.bufferedBytes() }
            val size = withContext(Dispatchers.IO) { engine.previewFileSize() }
            val line = status.line
            _state.update { cur ->
              val previewText =
                if (cur.previewIndex >= 0) {
                  String.format(
                    Locale.ROOT,
                    "缓冲 %.1f%%（%s / %s）%s",
                    percent * 100.0,
                    humanSize(buffered),
                    humanSize(size),
                    if (cur.previewPaused) " · 已暂停（hold）" else "",
                  )
                } else {
                  cur.bufferText
                }
              val statusText =
                when {
                  cur.previewIndex >= 0 -> "预览中 · $line"
                  cur.busy -> "正在通过 DHT 获取元数据（不下载本体）· $line"
                  else -> if (line.isEmpty()) cur.statusText else line
                }
              cur.copy(
                statusText = statusText,
                bufferPercent = percent.toFloat(),
                bufferText = previewText,
                cacheMode = engine.previewCacheMode(),
              )
            }
            if (status.handleInvalid || status.sessionStopped) {
              val err = status.error ?: "引擎句柄已失效，已停止轮询"
              log("轮询停止：$err")
              _state.update { it.copy(busy = false, statusText = "", errorText = err) }
              return@launch // 停止轮询：不再对失效句柄做任何调用
            }
            status.error?.let { err ->
              log("轮询报错（继续轮询）：$err")
              _state.update { it.copy(errorText = err) }
            }
          } catch (e: CancellationException) {
            throw e
          } catch (e: Throwable) {
            log("轮询异常：" + (e.message ?: e.javaClass.simpleName))
            _state.update { it.copy(errorText = "轮询异常：" + (e.message ?: e.javaClass.simpleName)) }
          }
          delay(1000L)
        }
      }
  }

  override fun onCleared() {
    pollJob?.cancel()
    watchdog?.cancel()
    settingsJob?.cancel()
    server?.let { runCatching { it.close() } }
    server = null
    val e = engine
    // SessionManager.stop() 阻塞在 native 销毁：绝不能放主线程（NOTES-libtorrent4j §6.1）
    Thread({ runCatching { e.shutdown() } }, "bt-engine-shutdown").also { it.isDaemon = true }.start()
    super.onCleared()
  }

  private companion object {
    const val PREVIEW_GATE_TIMEOUT_MS = 30_000L
    const val TAG = "MagnetViewer"
  }
}
