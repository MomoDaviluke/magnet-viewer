package com.bitseed.magnetviewer.ui.magnet

import android.Manifest
import android.app.Activity
import android.content.pm.ActivityInfo
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.os.Build
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import androidx.media3.ui.PlayerView
import com.bitseed.magnetviewer.core.PREVIEW_CACHE_HOLD
import com.bitseed.magnetviewer.theme.LocalPal
import com.bitseed.magnetviewer.theme.LocalShapes
import com.bitseed.magnetviewer.theme.MagnetViewerTheme
import com.bitseed.magnetviewer.ui.components.DeleteConfirmSheet
import com.bitseed.magnetviewer.ui.components.StallBanner
import com.bitseed.magnetviewer.ui.components.TaskCard
import com.bitseed.magnetviewer.ui.components.TaskCardLogic
import com.bitseed.magnetviewer.ui.gallery.GalleryScreen
import com.bitseed.magnetviewer.ui.settings.SettingsScreen
import androidx.compose.material3.MaterialTheme
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.res.stringResource
import com.bitseed.magnetviewer.R
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** 拖动条的可访问名（uiautomator 也能按它拿到坐标）。 */
private const val SEEK_SLIDER_DESC = "预览跳转条"

/**
 * 空态「示例磁力链」的演示输入（审计 2026-09-29：EmptyState 原先没接线，按钮点击无反应）。
 * 取 Ubuntu 24.04.4 官方发布种子（releases.ubuntu.com 的 .torrent 直接算 info-hash），
 * 公网长期有官方/社区做种，解析成功率高。
 */
private const val SAMPLE_MAGNET =
  "magnet:?xt=urn:btih:01c137287d6f0ed05a56742dae794f632c79ff3d" +
    "&dn=ubuntu-24.04.4-desktop-amd64.iso"

/**
 * 主界面：磁力链输入 → 文件清单（过滤 .pad）→ 点视频边下边播（本地流 + Media3）。
 *
 * 顺带负责两件系统级接线：
 * 1. Android 13+ 首次进入请求 `POST_NOTIFICATIONS`（**拒绝也能跑**，只是前台服务没有通知）；
 * 2. 按设置里的 `ui_theme` 决定浅色 / 深色 / 跟随系统（保存即换肤）。
 */
@Composable
fun MagnetScreen(modifier: Modifier = Modifier) {
  val appContext = LocalContext.current.applicationContext
  val viewModel: MagnetViewModel = viewModel { MagnetViewModel(appContext) }
  val state by viewModel.state.collectAsStateWithLifecycle()

  // Android 13+：通知权限（前台服务通知需要它；拒绝后一切照旧，只是没通知）
  val notifLauncher =
    rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
      viewModel.onNotificationPermissionResult(granted)
    }
  LaunchedEffect(Unit) {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
      val granted =
        ContextCompat.checkSelfPermission(appContext, Manifest.permission.POST_NOTIFICATIONS) ==
          PackageManager.PERMISSION_GRANTED
      if (!granted) notifLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
    }
  }

  // App 进后台 / Activity 被停止（含关屏、被系统回收前）→ 清单与 fastresume 落盘（尽力而为）
  val lifecycleOwner = LocalLifecycleOwner.current
  DisposableEffect(lifecycleOwner) {
    val observer =
      LifecycleEventObserver { _, event ->
        if (event == Lifecycle.Event.ON_STOP) viewModel.onAppStop()
      }
    lifecycleOwner.lifecycle.addObserver(observer)
    onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
  }

  // A-01：系统返回键在子屏逐级退（画廊/设置 → 主屏；播放中先停预览）。内部自定义目标（见 Navigation.kt 注释）。
  // 顺序即优先级：画廊 → 设置 → 播放中（停预览）→ 交给系统（退回桌面）。
  // 注意**播放中这条**：主屏按返回默认会把 Activity 退到桌面，而预览（分片调度 + 本地流）照旧跑，
  // 从用户视角就是「明明按了返回，视频/下载还在动」——按 A-01 的约定，先停预览、留在主屏。
  BackHandler(enabled = state.gallery.open) { viewModel.closeGallery() }
  BackHandler(enabled = !state.gallery.open && state.settingsOpen) { viewModel.closeSettings() }
  BackHandler(enabled = !state.gallery.open && !state.settingsOpen && state.previewIndex >= 0) {
    viewModel.stopPreview()
  }

  MagnetViewerTheme(themeMode = state.settings.uiTheme) {
    if (state.gallery.open) {
      // 图片画廊：图片文件按需下载 → 缩略图网格 → 点开大图（缩放 / 翻页）
      GalleryScreen(
        state = state.gallery,
        onClose = viewModel::closeGallery,
        onOpenImage = viewModel::galleryOpenImage,
        onStep = viewModel::galleryStep,
        onZoom = viewModel::galleryZoom,
        onResetZoom = viewModel::galleryResetZoom,
        onBackToGrid = viewModel::galleryBackToGrid,
        modifier = modifier,
      )
    } else if (state.settingsOpen) {
      SettingsScreen(
        initial = state.settings,
        note = state.settingsNote,
        cacheUsageText = state.cacheUsageText,
        cacheNote = state.cacheNote,
        onClearCache = viewModel::clearCacheNow,
        onSave = viewModel::saveSettings,
        onBack = viewModel::closeSettings,
        modifier = modifier,
      )
    } else {
      MagnetContent(
        state = state,
        onInput = viewModel::onInputChange,
        onResolve = viewModel::resolve,
        onPreview = viewModel::preview,
        onOpenGallery = viewModel::openGallery,
        onPausePreview = viewModel::pausePreview,
        onResumePreview = viewModel::resumePreview,
        onStopPreview = viewModel::stopPreview,
        onSeek = viewModel::previewSeek,
        onOpenSettings = viewModel::openSettings,
        onFocusTask = viewModel::focusTask,
        onToggleExpand = viewModel::toggleTaskExpand,
        onPauseTask = viewModel::pauseTask,
        onResumeTask = viewModel::resumeTask,
        onPromoteTask = viewModel::promoteTask,
        onRemoveTask = viewModel::removeTask,
        onSetPriority = viewModel::setTaskPriority,
        onRetryPreview = viewModel::retryPreview,
        modifier = modifier,
      )
    }
  }
}

/** 主屏（内部目标之一；其他子屏 = Gallery / Settings，BackHandler 逐级退）。 */
@Composable
internal fun MagnetContent(
  state: MagnetUiState,
  onInput: (String) -> Unit,
  onResolve: () -> Unit,
  onPreview: (Int) -> Unit,
  onPausePreview: () -> Unit,
  onResumePreview: () -> Unit,
  onStopPreview: () -> Unit,
  onSeek: (Float) -> Unit,
  onOpenSettings: () -> Unit,
  onOpenGallery: (Int) -> Unit = {},
  onFocusTask: (String) -> Unit = {},
  onToggleExpand: (String) -> Unit = {},
  onPauseTask: (String) -> Unit = {},
  onResumeTask: (String) -> Unit = {},
  onPromoteTask: (String) -> Unit = {},
  onRemoveTask: (String, Boolean) -> Unit = { _, _ -> },
  onSetPriority: (String, Int) -> Unit = { _, _ -> },
  onRetryPreview: () -> Unit = {},
  modifier: Modifier = Modifier,
) {
  val pal = LocalPal.current
  val shapes = LocalShapes.current
  val landscape = LocalConfiguration.current.orientation == Configuration.ORIENTATION_LANDSCAPE
  var pendingDelete by remember { mutableStateOf<Pair<String, String>?>(null) } // (infoHash, name)
  val listState = rememberLazyListState()

  // ================================================================ Wave B 横竖屏/全屏
  // 播放器的**实例提升到本作用域**：横屏全屏覆盖层与竖屏内嵌卡片共享同一个 ExoPlayer。
  // 若在两个分支各自 remember，切换分支时旧实例随 DisposableEffect 释放 → 正在播的流
  // 中断（商用播放器横竖屏切换绝不重新加载，这是底线）。
  val ctx = LocalContext.current
  val activity = ctx as? Activity
  val exoPlayer = remember {
    val http = DefaultHttpDataSource.Factory()
      .setConnectTimeoutMs(STREAM_CONNECT_TIMEOUT_MS)
      .setReadTimeoutMs(STREAM_READ_TIMEOUT_MS)
    ExoPlayer.Builder(ctx)
      .setMediaSourceFactory(DefaultMediaSourceFactory(http))
      .build()
  }
  DisposableEffect(exoPlayer) { onDispose { exoPlayer.release() } }

  // 媒体装载与 seek（player 实例共享，url/seek 由状态驱动；与旧 VideoPlayer 语义一致）。
  LaunchedEffect(state.streamUrl) {
    if (state.streamUrl.isNotEmpty()) {
      exoPlayer.setMediaItem(MediaItem.fromUri(state.streamUrl))
      exoPlayer.prepare()
      exoPlayer.playWhenReady = true
    }
  }
  // 预览结束（previewIndex 复位）→ 停掉播放器并清队列：共享实例不再随组合销毁，
  // 不主动停的话它会一直缓冲已死流的残余数据。
  LaunchedEffect(state.previewIndex) {
    if (state.previewIndex < 0) {
      exoPlayer.stop()
      exoPlayer.clearMediaItems()
    }
  }
  LaunchedEffect(state.seekSeq) {
    if (state.seekSeq > 0) {
      val duration = exoPlayer.duration
      if (duration > 0 && duration != C.TIME_UNSET) {
        exoPlayer.seekTo((duration * state.seekFraction).toLong())
      }
    }
  }

  // 重力感应自动横竖屏：播放中把方向交给**全传感器**（含倒置），转手机即触发
  // landscape 变化 → 覆盖层/内嵌自动切换；停止播放恢复系统默认（不锁向）。
  // 注意 configChanges 已在 Manifest 声明，旋转不重建 Activity，player 与播放进度都在。
  LaunchedEffect(state.previewIndex) {
    activity?.requestedOrientation =
      if (state.previewIndex >= 0) ActivityInfo.SCREEN_ORIENTATION_FULL_SENSOR
      else ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
  }
  // 退出播放时把方向复位（本作用域销毁 = 预览结束，交还系统）。
  DisposableEffect(Unit) {
    onDispose { activity?.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED }
  }

  // 全屏（横屏覆盖层）时沉浸：隐藏状态栏 + 导航栏，让视频铺满整块屏；退出即恢复。
  DisposableEffect(landscape && state.previewIndex >= 0) {
    val window = activity?.window ?: return@DisposableEffect onDispose { }
    val controller = WindowCompat.getInsetsController(window, window.decorView)
    val immersive = landscape && state.previewIndex >= 0
    if (immersive) {
      controller.systemBarsBehavior =
        WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
      controller.hide(WindowInsetsCompat.Type.systemBars())
    } else {
      controller.show(WindowInsetsCompat.Type.systemBars())
    }
    onDispose { controller.show(WindowInsetsCompat.Type.systemBars()) }
  }

  // 横屏全屏态下返回键 = 先退全屏（转竖屏），而非直接退出预览/页面（商用播放器惯例）。
  val uiScope = rememberCoroutineScope()
  // rememberUpdatedState：exitFullscreen 的协程在 delay 之后才读 previewIndex，
  // 直接捕获组合期的 state 快照会拿到旧值（Compose 经典坑）；取"最新值"引用才正确。
  val currentPreviewIndex by rememberUpdatedState(state.previewIndex)
  // 退出全屏的统一实现：强制竖屏 → 短暂延迟后把方向交还 FULL_SENSOR。
  // 直接交还（不延迟）会让"横持手机点退出"失效（传感器仍横向，立刻又转回横屏）；
  // 永久锁 PORTRAIT 又会杀死播放期间的重力感应（需求要求自动横竖屏），故取折中。
  val exitFullscreen: () -> Unit = {
    val act = activity
    act?.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_PORTRAIT
    uiScope.launch {
      delay(600)
      if (currentPreviewIndex >= 0) act?.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_FULL_SENSOR
    }
  }
  BackHandler(enabled = landscape && state.previewIndex >= 0) { exitFullscreen() }

  // D-02（设备实测 2026-09-15）：播放卡是页面的第 6 块内容，竖屏下它的控件行（暂停/停止预览）
  // 会落在首屏之外（实测 y≈2309-2358，屏高 2400），**必须先滚动才能操作**。
  // 光收视频高度不够（只上移 84px，仍被手势条压住），所以这里补「打开预览即把播放卡滚进视野」：
  // 播放卡之后只有文件行，故它的索引 = 总 item 数 − 1 − 文件行数（各类 `item(key=…)` 各占 1 项）。
  LaunchedEffect(state.previewIndex) {
    if (state.previewIndex < 0) return@LaunchedEffect
    withFrameNanos { } // 等一帧，让 layoutInfo 拿到本次组成的 item 总数
    val total = listState.layoutInfo.totalItemsCount
    if (total > 0) {
      val previewItem = (total - 1 - state.files.size).coerceAtLeast(0)
      listState.animateScrollToItem(previewItem)
    }
  }

  // B-03：重复解析去重提示 → Snackbar（并聚焦到既有任务）
  val snackbarHostState = remember { SnackbarHostState() }
  LaunchedEffect(state.statusText) {
    if (state.statusText.contains("复用既有任务") || state.statusText.contains("已在任务列表")) {
      snackbarHostState.showSnackbar(state.statusText)
    }
  }

  Box(modifier = modifier.fillMaxSize()) {
    LazyColumn(
      // D-02（设备实测 2026-09-15）：targetSdk 36 默认 edge-to-edge，而这里只写了 16dp padding
      // → 列表底部**伸到导航条/手势条下面**，播放卡的「暂停 / 停止预览」正好落在那一带
      // （实测 y≈2309-2358，屏高 2400），点不到、必须滚动。加 [navigationBarsPadding] 把可用区
      // 收回到系统栏之上，内容才能**完整**滚进视野（QA 建议的「底部留白」）。
      state = listState,
      modifier =
        Modifier.fillMaxSize()
          .background(if (landscape) pal.bg else pal.surfaceSunken)
          .navigationBarsPadding()
          .padding(18.dp),
      verticalArrangement = androidx.compose.foundation.layout.Arrangement.spacedBy(8.dp),
    ) {
      // ---- 头部（L-01 横屏收起副标题，状态区改两行）----
      item(key = "header") {
        Row(verticalAlignment = Alignment.CenterVertically) {
          Column(modifier = Modifier.weight(1f)) {
            Text(stringResource(R.string.app_title), style = MaterialTheme.typography.titleMedium, color = pal.text)
            if (!landscape) {
              Spacer(Modifier.height(2.dp))
              Text(
                "DHT 取元数据 · 不下载本体 · 缓存模式 ${state.cacheMode}",
                style = MaterialTheme.typography.labelMedium,
                color = pal.textMuted,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
              )
            }
          }
          TextButton(
          modifier = Modifier.heightIn(min = 48.dp),
          onClick = onOpenSettings) { Text(stringResource(R.string.settings), style = MaterialTheme.typography.bodySmall, color = pal.accent) }
        }
      }
      // ---- 输入行（B-02 错误就地：输入框下方）----
      item(key = "input") {
        Row(verticalAlignment = Alignment.CenterVertically) {
          OutlinedTextField(
            value = state.input,
            onValueChange = onInput,
            modifier = Modifier.weight(1f),
            singleLine = true,
            enabled = !state.busy,
            // 审计 2026-09-29（§4.5）：磁力链是 URI——键盘给 Uri 布局、回车键
            // 直接触发解析；错误态标红输入框（原先只有框下红字，框本身无反馈）。
            isError = state.errorText != null,
            textStyle = MaterialTheme.typography.bodyLarge.copy(color = pal.text),
            placeholder = { Text(stringResource(R.string.input_placeholder), style = MaterialTheme.typography.bodySmall, color = pal.textDim) },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Search),
            keyboardActions = KeyboardActions(onSearch = { onResolve() }),
          )
          Spacer(Modifier.width(8.dp))
          Button(onClick = onResolve, enabled = !state.busy, shape = shapes.shapeButton) {
            Text(if (state.busy) stringResource(R.string.resolving) else stringResource(R.string.resolve), style = MaterialTheme.typography.bodyLarge)
          }
        }
        // B-02：解析错误就地（输入框下方紧贴）
        state.errorText?.let {
          Spacer(Modifier.height(4.dp))
          Text(it, style = MaterialTheme.typography.labelMedium, color = pal.danger, maxLines = 2, overflow = TextOverflow.Ellipsis)
        }
      }
      // ---- 状态主行（15sp；工程类一句人话收「详情」）----
      // A-02：信息型启动提示只在空态展示（有任务上屏即让位）；告警型无条件常驻。
      val startupNote =
        if (shouldShowStartupNote(state.startupInfoOnly, state.taskRows.size)) state.restoreNote else ""
      if (state.statusText.isNotEmpty() || startupNote.isNotEmpty() || state.persistNote.isNotEmpty()) {
        item(key = "status") {
          Column(modifier = Modifier.fillMaxWidth()) {
            if (state.statusText.isNotEmpty()) {
              Text(state.statusText, style = MaterialTheme.typography.bodyLarge, color = pal.text, maxLines = 2, overflow = TextOverflow.Ellipsis)
            }
            // 工程信息只有一行 12sp dim（不堆）
            val engNote = startupNote.ifEmpty { state.persistNote }
            if (engNote.isNotEmpty()) {
              Text(
                engNote,
                style = MaterialTheme.typography.labelMedium,
                color = pal.textDim,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
              )
            }
          }
        }
      }
      // ---- 停滞提示（StallBanner；C-01）----
      if (state.stallNote != null) {
        item(key = "stall") { StallBanner(note = state.stallNote) }
      }
      // ---- 预览失败重试（D-01；previewFailed → retryPreview）----
      if (state.previewFailed) {
        item(key = "preview-failed") {
          Column(modifier = Modifier.fillMaxWidth().clip(shapes.shapeCard).background(pal.dangerSoft).padding(12.dp)) {
            Text(state.previewNote.ifEmpty { "预览失败" }, style = MaterialTheme.typography.bodySmall, color = pal.danger)
            Spacer(Modifier.height(8.dp))
            Button(onClick = onRetryPreview, shape = shapes.shapeButton) { Text(stringResource(R.string.retry), style = MaterialTheme.typography.bodySmall) }
          }
        }
      }
      // ---- 任务列表（一任务一张卡）----
      if (state.taskRows.isNotEmpty()) {
        items(state.taskRows, key = { it.infoHash }) { row ->
          TaskCard(
            row = row,
            onFocus = onFocusTask,
            onPause = onPauseTask,
            onResume = onResumeTask,
            onPromote = onPromoteTask,
            onRemove = { pendingDelete = it to row.name },
            onPriority = onSetPriority,
            onToggleDetail = onToggleExpand,
            detailOpen = row.expanded,
          )
        }
      } else if (!state.busy) {
        item(key = "empty") {
          // 审计 2026-09-29（P1）：原先是 EmptyState()——onSample 落默认空 lambda，
          // 「示例磁力链」按钮点击无任何反应。现在填入示例并直接触发解析。
          EmptyState(onSample = { onInput(SAMPLE_MAGNET); onResolve() })
        }
      }
      // ---- 焦点任务信息 / 播放卡 / 文件行 ----
      if (state.torrentName.isNotEmpty()) {
        item(key = "focus") {
          PanelCard(shapes = shapes) {
            Row(verticalAlignment = Alignment.CenterVertically) {
              Text(
                state.torrentName,
                modifier = Modifier.weight(1f),
                style = MaterialTheme.typography.titleMedium,
                color = pal.text,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
              )
              if (state.taskState.isNotEmpty()) {
                Text(state.taskState, style = MaterialTheme.typography.labelMedium, color = pal.textMuted)
              }
            }
            Spacer(Modifier.height(4.dp))
            Text(
              "总大小 ${state.totalSizeText} · ${state.files.size} 个文件",
              style = MaterialTheme.typography.bodySmall,
              color = pal.textMuted,
            )
          }
        }
      }
      if (state.previewIndex >= 0 && !landscape) {
        item(key = "preview") {
          // D-02：播放卡固定结构（状态文/控件同一行内排，不挤出）。
          // 横屏时本卡不渲染（由全屏覆盖层接管，见 Box 末尾）；竖屏/横屏切换
          // 共享同一个 exoPlayer 实例，播放不中断。
          PreviewPane(
            player = exoPlayer,
            streamUrl = state.streamUrl,
            title = state.previewTitle,
            note = state.previewNote,
            bufferText = state.bufferText,
            bufferPercent = state.bufferPercent,
            paused = state.previewPaused,
            cacheMode = state.cacheMode,
            seekFraction = state.seekFraction,
            seekSeq = state.seekSeq,
            seekNote = state.seekNote,
            onSeek = onSeek,
            onPause = onPausePreview,
            onResume = onResumePreview,
            onStop = onStopPreview,
            // Wave B：竖屏手动全屏按钮 → 强制横屏（覆盖层随 landscape 出现）。
            // FULL_SENSOR 下竖持不会自动转横，所以手动按钮用显式 LANDSCAPE 指令；
            // 之后重力感应仍由 LaunchedEffect(state.previewIndex) 的 FULL_SENSOR 接管。
            onEnterFullscreen = {
              activity?.requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_LANDSCAPE
            },
          )
        }
      }
      if (state.files.isNotEmpty()) {
        items(state.files, key = { it.index }) { file ->
          FileRow(
            item = file,
            selected = file.index == state.previewIndex,
            paused = state.previewPaused,
            onClick = { if (file.isImage) onOpenGallery(file.index) else onPreview(file.index) },
            onPause = onPausePreview,
            onResume = onResumePreview,
            onStop = onStopPreview,
          )
        }
      }
    }
    // ---- Wave B：横屏全屏覆盖层（播放中且横屏时接管视频，铺满整块屏）----
    // 与竖屏内嵌 PreviewPane 共享同一个 exoPlayer 实例，切换方向播放不中断。
    if (landscape && state.previewIndex >= 0) {
      FullscreenPlayer(
        player = exoPlayer,
        streamUrl = state.streamUrl,
        title = state.previewTitle,
        note = state.previewNote,
        bufferText = state.bufferText,
        paused = state.previewPaused,
        onExit = exitFullscreen,
        onPause = onPausePreview,
        onResume = onResumePreview,
        onStop = onStopPreview,
      )
    }
    // ---- Snackbar 宿主（B-03：重复解析提示；底部悬浮）----
    SnackbarHost(
      hostState = snackbarHostState,
      modifier = Modifier.align(Alignment.BottomCenter).padding(18.dp),
    )
  }
  // ---- 删除确认底部 Sheet（G-01）----
  pendingDelete?.let { (hash, name) ->
    DeleteConfirmSheet(
      taskName = name,
      onConfirm = { deleteData ->
        if (deleteData != null) onRemoveTask(hash, deleteData)
        pendingDelete = null
        deleteData
      },
      onDismiss = { pendingDelete = null },
    )
  }
}

/** 文件行（不变；视频行 → 预览；图片行 → 画廊）。 */
@Composable
private fun FileRow(
  item: MagnetFileItem,
  selected: Boolean,
  paused: Boolean,
  onClick: () -> Unit,
  onPause: () -> Unit,
  onResume: () -> Unit,
  onStop: () -> Unit,
) {
  val pal = LocalPal.current
  val clickable = item.isVideo || item.isImage
  Row(
    modifier =
      Modifier.fillMaxWidth()
        .clip(RoundedCornerShape4)
        .background(if (selected) pal.bgSelected else pal.panel)
        .border(1.dp, pal.border, RoundedCornerShape4)
        .clickable(enabled = clickable, onClick = onClick)
        .padding(horizontal = 12.dp, vertical = 12.dp),
    verticalAlignment = Alignment.CenterVertically,
  ) {
    Column(modifier = Modifier.weight(1f)) {
      Text(item.name, style = MaterialTheme.typography.bodyLarge, color = pal.text, maxLines = 1, overflow = TextOverflow.Ellipsis)
      Spacer(Modifier.height(2.dp))
      Text(
        if (item.path != item.name) "${item.sizeText} · ${item.path}" else item.sizeText,
        style = MaterialTheme.typography.bodySmall,
        color = pal.textMuted,
        maxLines = 1,
        overflow = TextOverflow.Ellipsis,
      )
    }
    Spacer(Modifier.width(4.dp))
    when {
      item.isImage -> Text(stringResource(R.string.action_image_gallery), style = MaterialTheme.typography.bodySmall, color = pal.accent, fontWeight = FontWeight.Medium)
      !item.isVideo -> Text(stringResource(R.string.action_not_video), style = MaterialTheme.typography.bodySmall, color = pal.textDim)
      !selected -> Text(stringResource(R.string.action_preview), style = MaterialTheme.typography.bodySmall, color = pal.accent, fontWeight = FontWeight.Medium)
      else -> {
        Text(
          if (paused) "暂停中" else "预览中",
          style = MaterialTheme.typography.labelMedium,
          color = if (paused) pal.warn else pal.ok,
          fontWeight = FontWeight.Medium,
        )
        TextButton(
          modifier = Modifier.heightIn(min = 48.dp),
          onClick = if (paused) onResume else onPause) {
          Text(if (paused) "继续" else "暂停", style = MaterialTheme.typography.bodySmall, color = pal.accent)
        }
        TextButton(
          modifier = Modifier.heightIn(min = 48.dp),
          onClick = onStop) { Text(stringResource(R.string.stop_preview), style = MaterialTheme.typography.bodySmall, color = pal.danger) }
      }
    }
  }
}

/** 文件行/PanelCard 共用的小圆角（4 = 行内元素）。 */
private val RoundedCornerShape4 = androidx.compose.foundation.shape.RoundedCornerShape(8.dp)

@Composable
private fun PreviewPane(
  player: ExoPlayer,
  streamUrl: String,
  title: String,
  note: String,
  bufferText: String,
  bufferPercent: Float,
  paused: Boolean,
  cacheMode: String,
  seekFraction: Float,
  seekSeq: Int,
  seekNote: String,
  onSeek: (Float) -> Unit,
  onPause: () -> Unit,
  onResume: () -> Unit,
  onStop: () -> Unit,
  onEnterFullscreen: () -> Unit,
) {
  val pal = LocalPal.current
  val shapes = LocalShapes.current
  PanelCard(shapes = shapes) {
    Row(verticalAlignment = Alignment.CenterVertically) {
      Text(
        title,
        modifier = Modifier.weight(1f),
        style = MaterialTheme.typography.labelLarge,
        color = pal.text,
        maxLines = 1,
        overflow = TextOverflow.Ellipsis,
      )
      // Wave B：竖屏手动全屏按钮（点击进入横屏全屏，与重力感应殊途同归）。
      TextButton(
        modifier = Modifier.heightIn(min = 44.dp),
        onClick = onEnterFullscreen,
      ) { Text("全屏", style = MaterialTheme.typography.bodySmall, color = pal.accent) }
    }
    Spacer(Modifier.height(8.dp))
    Box(
      // 视频区保持 200dp（D-02 不改视觉尺寸：可达性由「打开预览自动滚到播放卡」解决，
      // 见本文件 MagnetScreen 里的 LaunchedEffect(state.previewIndex)）。
      modifier = Modifier.fillMaxWidth().height(200.dp).clip(shapes.shapeCard).background(Color.Black),
      contentAlignment = Alignment.Center,
    ) {
      // 共享 player 实例（MagnetContent 作用域 remember）；横竖屏切换不重建、不中断。
      StreamPlayerSurface(player = player)
      // 占位条件用 **streamUrl**（Compose 快照状态，变化即重组）；
      // player.currentMediaItem 不是快照状态，读它不会触发重组（会停在旧值）。
      if (streamUrl.isEmpty()) {
        Text(note.ifEmpty { "正在准备索引块…" }, style = MaterialTheme.typography.bodySmall, color = pal.videoPlaceholder)
      }
    }
    Spacer(Modifier.height(8.dp))
    if (bufferPercent > 0f || bufferText.isNotEmpty()) {
      ThinProgress(percent = bufferPercent)
      Spacer(Modifier.height(8.dp))
    }
    SeekBar(fraction = seekFraction, onSeek = onSeek)
    if (seekNote.isNotEmpty()) {
      Text(seekNote, style = MaterialTheme.typography.labelMedium, color = pal.textMuted, maxLines = 2, overflow = TextOverflow.Ellipsis)
      Spacer(Modifier.height(4.dp))
    }
    // D-02：状态文与控件同一排（字数短、禁止溢出卡）
    Row(verticalAlignment = Alignment.CenterVertically) {
      Text(
        if (bufferText.isNotEmpty()) bufferText else note,
        modifier = Modifier.weight(1f),
        style = MaterialTheme.typography.bodySmall,
        color = pal.textMuted,
        maxLines = 2,
        overflow = TextOverflow.Ellipsis,
      )
      TextButton(
          modifier = Modifier.heightIn(min = 48.dp),
          onClick = if (paused) onResume else onPause) {
        Text(if (paused) "继续" else "暂停", style = MaterialTheme.typography.bodySmall, color = pal.accent)
      }
      TextButton(
          modifier = Modifier.heightIn(min = 48.dp),
          onClick = onStop) { Text(stringResource(R.string.stop_preview), style = MaterialTheme.typography.bodySmall, color = pal.accent) }
    }
    Spacer(Modifier.height(4.dp))
    Text(
      if (cacheMode == PREVIEW_CACHE_HOLD) "停止语义：hold（冻结暂停，缓冲保留）"
      else "停止语义：convert（转正继续缓存，不暂停）",
      style = MaterialTheme.typography.labelMedium,
      color = pal.textDim,
    )
  }
}

/**
 * Wave B：横屏全屏播放覆盖层。
 *
 * 与竖屏内嵌 [PreviewPane] 共享同一个 [player] 实例（在 MagnetContent 作用域 remember），
 * 因此旋转触发本覆盖层出现时，视频**继续播放、进度不丢**——这正是 Manifest 加
 * `configChanges`（旋转不重建 Activity）与「实例提升」两处设计共同保证的底线。
 *
 * 控制条走 Media3 `PlayerView.useController`（系统级播放/暂停/seek/时长，商用播放器同款），
 * 叠加自绘的「退出全屏 / 停止预览」按钮（左上/右上，半透明胶囊，不遮挡画面主体）。
 */
@Composable
private fun FullscreenPlayer(
  player: ExoPlayer,
  streamUrl: String,
  title: String,
  note: String,
  bufferText: String,
  paused: Boolean,
  onExit: () -> Unit,
  onPause: () -> Unit,
  onResume: () -> Unit,
  onStop: () -> Unit,
) {
  val pal = LocalPal.current
  Box(
    modifier = Modifier
      .fillMaxSize()
      .background(Color.Black),
  ) {
    // 视频主体：PlayerView 自带控制条（横屏下 useController 体验最接近系统播放器）。
    StreamPlayerSurface(player = player)
    if (streamUrl.isEmpty()) {
      // 索引块尚未就绪：覆盖层中央给占位文案（与竖屏内嵌同语义）。
      Text(
        note.ifEmpty { "正在准备索引块…" },
        modifier = Modifier.align(Alignment.Center),
        style = MaterialTheme.typography.bodyMedium,
        color = pal.videoPlaceholder,
      )
    }
    // ---- 顶部控制条：标题 + 退出/停止（半透明底，浮于画面之上）----
    // statusBarsPadding：edge-to-edge 下避开状态栏；全屏沉浸态系统栏已隐藏，padding 自然为 0。
    Row(
      modifier = Modifier
        .align(Alignment.TopStart)
        .fillMaxWidth()
        .statusBarsPadding()
        .background(androidx.compose.ui.graphics.Color(0x66000000))
        .padding(horizontal = 12.dp, vertical = 8.dp),
      verticalAlignment = Alignment.CenterVertically,
    ) {
      Text(
        title,
        modifier = Modifier.weight(1f),
        style = MaterialTheme.typography.bodyMedium,
        color = Color.White,
        maxLines = 1,
        overflow = TextOverflow.Ellipsis,
      )
      // 缓冲态短提示（有则显，无则不占位）。
      if (bufferText.isNotEmpty()) {
        Text(bufferText, style = MaterialTheme.typography.labelMedium, color = Color(0xCCFFFFFF))
        Spacer(Modifier.width(12.dp))
      }
      // 暂停/继续（与竖屏同一组语义：冻结预览调度，不动 player 本身）。
      TextButton(onClick = if (paused) onResume else onPause) {
        Text(if (paused) "继续" else "暂停", style = MaterialTheme.typography.bodySmall, color = Color.White)
      }
      TextButton(onClick = onStop) {
        Text("退出播放", style = MaterialTheme.typography.bodySmall, color = Color(0xFFFF8A75))
      }
      TextButton(onClick = onExit) {
        Text("退出全屏", style = MaterialTheme.typography.bodySmall, color = Color.White)
      }
    }
  }
}

/**
 * 共享的 ExoPlayer 渲染面（PlayerView + useController）。
 *
 * 单独抽出来是因为竖屏内嵌与横屏全屏两处要渲染**同一个** player 实例——
 * AndroidView 的 factory 只在首次组合执行，更新靠 update 块重绑 player 引用。
 */
@Composable
private fun StreamPlayerSurface(player: ExoPlayer) {
  AndroidView(
    modifier = Modifier.fillMaxSize(),
    factory = { ctx ->
      PlayerView(ctx).apply {
        this.player = player
        useController = true
        setShutterBackgroundColor(android.graphics.Color.TRANSPARENT)
      }
    },
    update = { view -> view.player = player },
  )
}

/** 跳转条（0..1 = 文件比例）。**只在拖动结束时**回调 [onSeek]（D-03 与提示共用同一取整 [TaskCardLogic.percentOf]）。 */
@Composable
private fun SeekBar(fraction: Float, onSeek: (Float) -> Unit) {
  val pal = LocalPal.current
  var local by remember { mutableFloatStateOf(fraction) }
  LaunchedEffect(fraction) { local = fraction }
  Column(modifier = Modifier.fillMaxWidth()) {
    Slider(
      value = local,
      onValueChange = { local = it },
      onValueChangeFinished = { onSeek(local) },
      valueRange = 0f..1f,
      modifier = Modifier.fillMaxWidth().semantics { contentDescription = SEEK_SLIDER_DESC },
    )
    Row(verticalAlignment = Alignment.CenterVertically) {
      Text("跳转 ${TaskCardLogic.percentOf(local)}%", style = MaterialTheme.typography.labelMedium, color = pal.textMuted, modifier = Modifier.weight(1f))
      Text(stringResource(R.string.seek_finish_note), style = MaterialTheme.typography.labelMedium, color = pal.textDim)
    }
  }
}

/**
 * Media3/ExoPlayer 播放本地流（http://127.0.0.1:port/…?t=token）。
 *
 * **Data source 超时必须显式设定（2026-09-18 修）**：本地流服务在区间未就绪时
 * 会「点播 + 等待」（上限见 `StreamServer.DEFAULT_WAIT_TIMEOUT_MS`），而
 * `DefaultHttpDataSource` 的默认读超时约 8s —— 比服务端等待上限**短**。
 * 于是只要等待超过 8s，播放器先判失败、服务端才回响应：表现为「seek 后必然
 * 播放失败」，且报错是播放器侧的，与服务端日志对不上，极难排查。
 * 这里把读超时设到服务端等待上限**之上**，并让连接超时保持较短
 * （回环连接不需要等）。改超时必须同步检查这条单调链：
 * `停滞(~0.5s) < progressiveMin 观测 < 服务端等待(20s) < 读超时(25s)`。
 * 该链由 `MagnetStatusLogicTest.streamTimeoutBudget_isMonotonic` 看守。
 */
internal const val STREAM_CONNECT_TIMEOUT_MS = 5_000
internal const val STREAM_READ_TIMEOUT_MS = 25_000

/** 自绘细进度条（缓冲比例）。 */
@Composable
private fun ThinProgress(percent: Float) {
  val pal = LocalPal.current
  Box(
    modifier = Modifier.fillMaxWidth().height(8.dp).clip(androidx.compose.foundation.shape.CircleShape).background(pal.border)
  ) {
    Box(
      modifier =
        Modifier.fillMaxHeight()
          .fillMaxWidth(percent.coerceIn(0f, 1f))
          .background(pal.accent)
    )
  }
}

@Composable
private fun PanelCard(shapes: com.bitseed.magnetviewer.theme.UiShapes, modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
  val pal = LocalPal.current
  Column(
    modifier =
      modifier
        .fillMaxWidth()
        .clip(shapes.shapeCard)
        .background(pal.panel)
        .padding(18.dp),
    content = content,
  )
}
