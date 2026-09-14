package com.bitseed.magnetviewer.ui.magnet

import android.content.Context
import android.util.Log
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.bitseed.magnetviewer.core.PREVIEW_CACHE_HOLD
import com.bitseed.magnetviewer.core.model.ParseResult
import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.core.model.humanSize
import com.bitseed.magnetviewer.core.states.STATE_DOWNLOADING
import com.bitseed.magnetviewer.core.states.STATE_FAILED
import com.bitseed.magnetviewer.core.states.STATE_PAUSED
import com.bitseed.magnetviewer.data.AppSettings
import com.bitseed.magnetviewer.data.MagnetStorage
import com.bitseed.magnetviewer.data.ResumeStore
import com.bitseed.magnetviewer.data.SettingsStore
import com.bitseed.magnetviewer.data.TaskRecord
import com.bitseed.magnetviewer.data.applyDeletion
import com.bitseed.magnetviewer.data.loadTasks
import com.bitseed.magnetviewer.data.planDeletion
import com.bitseed.magnetviewer.data.previewDirFor
import com.bitseed.magnetviewer.data.removeTask
import com.bitseed.magnetviewer.data.saveTasksQuietly
import com.bitseed.magnetviewer.data.taskFromResult
import com.bitseed.magnetviewer.data.upsertTask
import com.bitseed.magnetviewer.engine.BtEngine
import com.bitseed.magnetviewer.engine.RestoreReport
import com.bitseed.magnetviewer.service.DownloadService
import com.bitseed.magnetviewer.stream.StreamServer
import java.io.File
import java.util.Locale
import kotlin.math.roundToInt
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
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
  /** 磁盘清单里的任务数（`.tasks.json` 读到的原始条数）。 */
  val persistedTaskCount: Int = 0,
  /** 启动恢复的一句话（日志与 UI 共用；恢复路径要看得清）。 */
  val restoreNote: String = "",
  /** 当前展示的任务在磁盘清单里的状态（PAUSED / DOWNLOADING / FAILED …）。 */
  val taskState: String = "",
  /** 最近一次落盘的结果文案（证明清单真的写到磁盘了）。 */
  val persistNote: String = "",
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
          currentInfoHash = infoHash
          log("UI 收到元数据就绪：name=${result.name} 文件数=${result.files.size} infohash=$infoHash")
          _state.update { st ->
            st.copy(
              busy = false,
              errorText = null,
              statusText = "元数据就绪（未下载本体）",
              torrentName = result.name,
              infoHash = infoHash,
              totalSizeText = humanSize(result.totalSize),
              persistNote = st.persistNote,
              files =
                result.viewFiles.map { f ->
                  MagnetFileItem(f.index, f.name, f.path, humanSize(f.size), f.isVideo)
                },
            )
          }
          // 元数据就绪 = 清单落盘点之一（重启后靠它直接上屏，不必再等 DHT）
          persistCurrentTask("元数据就绪", STATE_PAUSED)
          requestResume("元数据就绪")
        }

        override fun onMetadataError(message: String) {
          watchdog?.cancel()
          log("UI 收到元数据错误：$message")
          _state.update { it.copy(busy = false, statusText = "", errorText = message) }
          markState(STATE_FAILED, "元数据阶段失败", error = message)
        }

        override fun onStatus(message: String) {
          log("UI 状态：$message")
          _state.update { it.copy(statusText = message) }
        }

        override fun onTaskStateChanged(infoHash: String, state: String) {
          log("UI 收到任务状态变化：${infoHash.take(12)}… → $state")
          val cur = tasks[infoHash] ?: return // 记录还没建立（等 onMetadataReady 落盘）
          tasks = tasks + (infoHash to cur.copy(state = state))
          if (infoHash == currentInfoHash) _state.update { it.copy(taskState = state) }
          persistTasks("状态变化 $state")
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

  // ---- 持久化状态（任务清单 + 缓存根 + 当前任务标识）----
  /** 内存清单（`.tasks.json` 的镜像；每次变更后异步落盘）。 */
  private var tasks: Map<String, TaskRecord> = emptyMap()

  /** 缓存根 `<filesDir>/magnet/cache`（首次 IO 时惰性解析）。 */
  @Volatile private var cacheRoot: File? = null

  /** 当前展示的任务 info_hash（清单展示/落盘都以它为准）。 */
  @Volatile private var currentInfoHash: String = ""

  /** 当前任务的来源（磁力链原文），落盘时写进 `source`。 */
  @Volatile private var currentSource: String = ""

  /** 落盘请求队列（**合并**：只保留最新快照，见 [persistTasks]）。 */
  private val persistRequests = Channel<Pair<Map<String, TaskRecord>, String>>(Channel.CONFLATED)
  private var persistWorker: Job? = null

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
    startPersistWorker()
    // 启动恢复：读磁盘清单 → 立刻上屏 → 引擎按 fastresume 重建（见 restoreFromDisk）
    restoreFromDisk()
  }

  // ---------------------------------------------------------------- 持久化（清单 + fastresume）

  /** 缓存根（惰性解析；不可用时返回 null 并记日志）。 */
  private fun cacheRootOrNull(): File? {
    cacheRoot?.let { return it }
    val r = runCatching { MagnetStorage.cacheRoot(appContext) }.getOrNull()
    if (r == null) log("缓存根不可用：持久化被跳过") else cacheRoot = r
    return r
  }

  /**
   * 任务清单落盘（**异步 + 只告警**：写入失败绝不能阻断主流程，桌面 `persist_tasks` 同款）。
   *
   * 请求进**合并队列**（conflated）：同一时刻最多排队一次，永远只写「最新快照」——
   * 既不会让旧快照覆盖新快照，也不会两次写在同一份数据上打架。
   * [reason] 写进日志，让「什么时候写的、写了几个」可回溯。
   */
  private fun persistTasks(reason: String) {
    persistRequests.trySend(tasks to reason)
  }

  /** 落盘协程：串行消费合并队列（单写者，写入顺序 = 快照产生顺序）。 */
  private fun startPersistWorker() {
    persistWorker?.cancel()
    persistWorker =
      viewModelScope.launch {
        for ((snapshot, reason) in persistRequests) {
          try {
            writeSnapshot(snapshot, reason)
          } catch (e: CancellationException) {
            throw e
          } catch (e: Throwable) {
            log("落盘协程异常（$reason，已忽略）：" + (e.message ?: e.javaClass.simpleName))
          }
        }
      }
  }

  private suspend fun writeSnapshot(snapshot: Map<String, TaskRecord>, reason: String) {
    val f =
      withContext(Dispatchers.IO) {
        val root = cacheRootOrNull() ?: return@withContext null
        saveTasksQuietly(root, snapshot) { w -> log("持久化告警（$reason）：$w") }
      }
    if (f == null) {
      log("任务清单落盘失败（$reason，已忽略，不影响任务）")
      _state.update { it.copy(persistNote = "清单落盘失败（$reason，已忽略）") }
      return
    }
    val bytes = withContext(Dispatchers.IO) { f.length() }
    log("任务清单已落盘（$reason）：${snapshot.size} 个任务 → ${f.path}（$bytes 字节）")
    _state.update {
      it.copy(persistNote = "清单已落盘（$reason）：${snapshot.size} 个任务 / $bytes 字节", persistedTaskCount = snapshot.size)
    }
  }

  /**
   * 用引擎里当前的解析结果构造/更新任务记录并落盘（**元数据就绪 / 预览开始停止** 都走它）。
   *
   * 失败只记日志：记录构造不出来 = 少写一行 JSON，绝不影响正在跑的下载/预览。
   */
  private fun persistCurrentTask(reason: String, state: String, error: String = "") {
    val result = engine.result()
    if (result == null) {
      log("清单落盘跳过（$reason）：当前没有解析结果")
      return
    }
    val ih = result.infoHash.ifEmpty { engine.infoHash() }
    if (ih.isEmpty()) {
      log("清单落盘跳过（$reason）：info_hash 为空")
      return
    }
    val dir = engine.previewRoot()
    val fresh =
      try {
        taskFromResult(
          result = result,
          source = currentSource.ifEmpty { "magnet:?xt=urn:btih:$ih" },
          savePath = dir?.path ?: "",
          state = state,
          download = true,
          error = error,
          createdAt = nowSeconds(),
        )
      } catch (e: Throwable) {
        log("清单记录构造失败（$reason）：" + (e.message ?: e.javaClass.simpleName))
        return
      }
    val prev = tasks[ih]
    val merged =
      if (prev == null) {
        fresh
      } else {
        // 保留首次看到的创建时间与优先级/重试计数（进度类字段本就不落盘）
        fresh.copy(
          createdAt = prev.createdAt ?: fresh.createdAt,
          finishedAt = prev.finishedAt ?: fresh.finishedAt,
          priority = prev.priority,
          retries = prev.retries,
        )
      }
    tasks = upsertTask(tasks, merged)
    currentInfoHash = ih
    _state.update { it.copy(taskState = merged.state, persistedTaskCount = tasks.size) }
    persistTasks(reason)
  }

  /** 只改当前任务的状态并落盘（预览开始/暂停/继续/停止、元数据失败）。 */
  private fun markState(state: String, reason: String, error: String = "") {
    val ih = currentInfoHash
    val cur = tasks[ih]
    if (cur == null) {
      log("状态更新跳过（$reason）：清单里没有 ${ih.take(12)}…")
      return
    }
    tasks = tasks + (ih to cur.copy(state = state, error = error))
    _state.update { it.copy(taskState = state) }
    log("任务状态更新（$reason）：${ih.take(12)}… → $state")
    persistTasks(reason)
  }

  /**
   * **启动恢复**（本任务的核心）：读 `.tasks.json` → 清单**立刻上屏**（不等 DHT）→
   * 再让引擎按 `.resume/<ih>.fastresume` 把任务加回会话，并恢复下载/暂停状态。
   *
   * 关键点：**清单展示与引擎恢复是两条独立路径**——recovery 失败（会话起不来、resume 损坏、
   * 来源不可用）也照样把清单摆在屏幕上，只是状态栏说明原因。
   */
  private fun restoreFromDisk() {
    viewModelScope.launch {
      val root = cacheRootOrNull()
      if (root == null) {
        _state.update { it.copy(restoreNote = "启动恢复跳过：缓存根不可用") }
        return@launch
      }
      val loaded = withContext(Dispatchers.IO) { loadTasks(root) { w -> log("清单读取告警：" + w) } }
      tasks = loaded
      val present = withContext(Dispatchers.IO) { ResumeStore.presentKeys(root, loaded.keys) }
      log("启动：读 .tasks.json → ${loaded.size} 个任务（${root.path}）；fastresume 命中 ${present.size}/${loaded.size}")
      if (loaded.isEmpty()) {
        _state.update { it.copy(persistedTaskCount = 0, restoreNote = "启动：磁盘上没有任务清单（首次启动）") }
        return@launch
      }
      val first = loaded.values.first()
      showRecordOnScreen(first)
      _state.update {
        it.copy(
          persistedTaskCount = loaded.size,
          restoreNote = "启动：读到 ${loaded.size} 个任务，fastresume 命中 ${present.size}（清单来自磁盘）",
        )
      }
      // 引擎侧重建（native）：失败只记日志，清单已经在屏上
      val report =
        withContext(Dispatchers.IO) {
          runCatching { engine.restoreTasks(root, loaded.values.toList()) }
            .getOrElse { e ->
              log("引擎恢复异常（清单仍在屏上）：" + (e.message ?: e.javaClass.simpleName))
              null
            }
        }
      if (report == null) {
        _state.update { it.copy(restoreNote = it.restoreNote + " · 引擎恢复未完成（详见日志）") }
        return@launch
      }
      log("启动恢复结果：" + report.summary())
      // 用真实状态校正内存清单（失败任务带错误文本）
      var next = tasks
      for (t in report.tasks) {
        val cur = next[t.infoHash] ?: continue
        next = next + (t.infoHash to cur.copy(state = t.state, error = t.error.ifEmpty { cur.error }))
      }
      tasks = next
      val shown = report.tasks.firstOrNull { it.infoHash == currentInfoHash } ?: report.tasks.firstOrNull()
      // 引擎里若已有更权威的清单（resume 带 info-dict 时立刻就有），用它刷新界面
      val engResult = engine.result()
      val engIh = engine.infoHash()
      if (engResult != null && engIh == currentInfoHash) {
        currentFiles = engResult.files
        _state.update { st ->
          st.copy(
            errorText = null,
            torrentName = engResult.name,
            totalSizeText = humanSize(engResult.totalSize),
            files = engResult.viewFiles.map { f -> MagnetFileItem(f.index, f.name, f.path, humanSize(f.size), f.isVideo) },
          )
        }
      }
      _state.update {
        it.copy(
          busy = false,
          restoreNote = compactRestoreNote(report),
          taskState = shown?.state ?: it.taskState,
          persistedTaskCount = tasks.size,
        )
      }
      if (shown != null && shown.error.isNotEmpty()) _state.update { it.copy(errorText = shown.error) }
      persistTasks("启动恢复")
    }
  }

  /** 恢复总账的短版（UI 一行放得下；完整版 `RestoreReport.summary()` 进日志）。 */
  private fun compactRestoreNote(report: RestoreReport): String =
    "启动恢复：${report.restored}/${report.total} 个任务（resume 命中 ${report.resumeHits}，" +
      "暂停 ${report.paused}，续传 ${report.downloading}）"

  /** 把磁盘记录直接摆到界面上（**不依赖 DHT/会话**：重启后立刻能看到清单）。 */
  private fun showRecordOnScreen(record: TaskRecord) {
    val viewFiles = record.viewFiles
    currentFiles = record.files
    currentInfoHash = record.infoHash
    if (record.source.isNotEmpty()) currentSource = record.source
    _state.update { st ->
      st.copy(
        busy = false,
        errorText = record.error.ifEmpty { null },
        statusText = "清单来自磁盘（重启后无需重新粘贴磁力链）",
        torrentName = record.name,
        infoHash = record.infoHash,
        totalSizeText = humanSize(record.totalSize),
        taskState = record.state,
        files = viewFiles.map { f -> MagnetFileItem(f.index, f.name, f.path, humanSize(f.size), f.isVideo) },
      )
    }
    log(
      "启动恢复：清单已上屏（磁盘）name=${record.name} 文件数=${viewFiles.size} " +
        "总大小=${record.totalSize} state=${record.state} ih=${record.infoHash.take(12)}…"
    )
  }

  /**
   * App 进入后台/被停止（Activity `onStop`）：清单与 fastresume 各落一次盘（尽力而为）。
   * 被 `am force-stop` 直接杀掉时来不及跑——所以**每个状态变化点都会落盘**，不指望这一下。
   */
  fun onAppStop() {
    log("App onStop：落盘任务清单（${tasks.size} 个任务）+ 请求 fastresume")
    persistTasks("App onStop")
    requestResume("App onStop")
  }

  /**
   * 请求引擎写 fastresume（异步、失败只告警）。
   *
   * 走方法调用而不是在 listener 里直接摸 `engine`：`engine` 的初始化闭包引用它自己会触发
   * Kotlin 的「初始化递归」（同文件顶部那条注释警告的同一类问题）。
   */
  private fun requestResume(reason: String) {
    viewModelScope.launch {
      withContext(Dispatchers.IO) { runCatching { engine.requestResumeData(reason) } }
    }
  }

  /**
   * 移除当前任务：**默认只摘清单记录 + 删 fastresume，绝不删用户数据目录**（不变量 C）。
   *
   * 删除动作由纯函数 [planDeletion] 决定：数据目录只有调用方**显式**传
   * `deleteUserData = true` 且目录确实在应用私有目录内才会被删。
   *
   * @return 无（结果经 [MagnetUiState.restoreNote] / 日志上报）
   */
  fun removeCurrentTask(deleteUserData: Boolean = false) {
    val ih = currentInfoHash
    val record = tasks[ih]
    if (record == null) {
      _state.update { it.copy(errorText = "没有可移除的任务（清单为空）") }
      return
    }
    viewModelScope.launch {
      val root = cacheRootOrNull()
      val dataDir = root?.let { previewDirFor(it, ih) }
      val appRoot = root?.parentFile ?: root
      val plan = if (dataDir != null && appRoot != null) planDeletion(ih, dataDir, appRoot, deleteUserData) else null
      if (plan == null || !plan.valid) {
        val msg = plan?.error?.ifEmpty { "删除计划不可用" } ?: "删除计划不可用"
        log("移除任务中止：$msg")
        _state.update { it.copy(errorText = msg) }
        return@launch
      }
      val applied = withContext(Dispatchers.IO) { applyDeletion(plan, root, dataDir) }
      val resumeDeleted = applied.resumeDeleted
      val dataDeleted = applied.userDataDeleted
      tasks = removeTask(tasks, ih)
      log(
        "移除任务：${ih.take(12)}… 清单已摘除（resume 删除=$resumeDeleted）" +
          if (plan.removeUserDataDir) "；用户数据目录**按显式要求**删除=$dataDeleted（${plan.userDataDir}）"
          else "；用户数据目录**保留**（${plan.userDataDir}）"
      )
      currentInfoHash = ""
      currentFiles = emptyList()
      _state.update {
        it.copy(
          torrentName = "",
          infoHash = "",
          totalSizeText = "",
          files = emptyList(),
          taskState = "",
          previewIndex = -1,
          previewTitle = "",
          previewNote = "",
          bufferPercent = 0f,
          bufferText = "",
          streamUrl = "",
          persistedTaskCount = tasks.size,
          restoreNote =
            "已移除任务记录（resume 删除=$resumeDeleted）；用户数据目录" +
              if (plan.removeUserDataDir) "已按显式要求删除" else "保留：" + plan.userDataDir,
        )
      }
      persistTasks("移除任务")
    }
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
    currentSource = uri
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
      // 预览开始 = 状态变化点（清单落盘 + fastresume 由引擎侧请求）
      persistCurrentTask("预览开始", STATE_DOWNLOADING)
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
      if (ok) markState(STATE_PAUSED, "预览暂停")
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
      if (ok) markState(STATE_DOWNLOADING, "预览继续")
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
    // 停止预览 = 状态变化点：hold 档冻结（PAUSED），convert 档仍在后台缓存（DOWNLOADING）
    markState(if (mode == PREVIEW_CACHE_HOLD) STATE_PAUSED else STATE_DOWNLOADING, "停止预览")
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
    persistWorker?.cancel()
    persistRequests.close()
    server?.let { runCatching { it.close() } }
    server = null
    val e = engine
    // SessionManager.stop() 阻塞在 native 销毁：绝不能放主线程（NOTES-libtorrent4j §6.1）
    Thread({ runCatching { e.shutdown() } }, "bt-engine-shutdown").also { it.isDaemon = true }.start()
    super.onCleared()
  }

  // ---------------------------------------------------------------- 时间

  /** 当前时间（秒，浮点；桌面 `time.time()` 同形，写进 `.tasks.json` 的 created_at）。 */
  private fun nowSeconds(): Double = System.currentTimeMillis() / 1000.0

  private companion object {
    const val PREVIEW_GATE_TIMEOUT_MS = 30_000L
    const val TAG = "MagnetViewer"
  }
}
