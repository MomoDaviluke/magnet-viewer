package com.bitseed.magnetviewer.ui.magnet

import android.content.Context
import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.bitseed.magnetviewer.core.model.ParseResult
import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.core.model.humanSize
import com.bitseed.magnetviewer.core.states.METADATA_TIMEOUT_SECONDS
import com.bitseed.magnetviewer.data.MagnetStorage
import com.bitseed.magnetviewer.engine.BtEngine
import com.bitseed.magnetviewer.stream.StreamServer
import java.util.Locale
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
)

/**
 * 主界面状态机：磁力链解析（DHT 拿元数据）→ 文件清单 → 点视频边下边播。
 *
 * 现场约束：`SessionManager.stop()` 会阻塞在 native 销毁，必须走 IO 线程；
 * 频繁/阻塞的 libtorrent 调用一律 `Dispatchers.IO`，主线程只碰 StateFlow。
 */
class MagnetViewModel(private val appContext: Context) : ViewModel() {

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

  private val _state = MutableStateFlow(MagnetUiState())
  val state: StateFlow<MagnetUiState> = _state.asStateFlow()

  private var server: StreamServer? = null
  private var currentFiles: List<TorrentFile> = emptyList()
  private var watchdog: Job? = null
  private var pollJob: Job? = null

  init {
    startPolling()
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
    stopPreviewInternal()
    currentFiles = emptyList()
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
      )
    }
    viewModelScope.launch {
      val cacheRoot = withContext(Dispatchers.IO) { MagnetStorage.cacheRoot(appContext) }
      val dir = withContext(Dispatchers.IO) { engine.addMagnet(uri, cacheRoot) }
      if (dir == null) return@launch // 失败原因已由 listener.onMetadataError 上报
      watchdog?.cancel()
      watchdog =
        viewModelScope.launch {
          delay(METADATA_TIMEOUT_SECONDS * 1000L)
          if (_state.value.busy) {
            _state.update {
              it.copy(busy = false, statusText = "", errorText = "资源无做种（${METADATA_TIMEOUT_SECONDS}s 内未获取到元数据）")
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
        )
      }
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

  fun stopPreview() {
    stopPreviewInternal()
    _state.update {
      it.copy(previewIndex = -1, previewTitle = "", previewNote = "", bufferPercent = 0f, bufferText = "", streamUrl = "")
    }
  }

  private fun stopPreviewInternal() {
    viewModelScope.launch(Dispatchers.IO) { engine.stopPreview() }
    server?.let { runCatching { it.close() } }
    server = null
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
                    "缓冲 %.1f%%（%s / %s）",
                    percent * 100.0,
                    humanSize(buffered),
                    humanSize(size),
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
              cur.copy(statusText = statusText, bufferPercent = percent.toFloat(), bufferText = previewText)
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
