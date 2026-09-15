package com.bitseed.magnetviewer.ui.magnet

import android.Manifest
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
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.exoplayer.ExoPlayer
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

/** 拖动条的可访问名（uiautomator 也能按它拿到坐标）。 */
private const val SEEK_SLIDER_DESC = "预览跳转条"

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

  // B-03：重复解析去重提示 → Snackbar（并聚焦到既有任务）
  val snackbarHostState = remember { SnackbarHostState() }
  LaunchedEffect(state.statusText) {
    if (state.statusText.contains("复用既有任务") || state.statusText.contains("已在任务列表")) {
      snackbarHostState.showSnackbar(state.statusText)
    }
  }

  Box(modifier = modifier.fillMaxSize()) {
    LazyColumn(
      modifier = Modifier.fillMaxSize().background(if (landscape) pal.bg else pal.surfaceSunken).padding(16.dp),
      verticalArrangement = androidx.compose.foundation.layout.Arrangement.spacedBy(8.dp),
    ) {
      // ---- 头部（L-01 横屏收起副标题，状态区改两行）----
      item(key = "header") {
        Row(verticalAlignment = Alignment.CenterVertically) {
          Column(modifier = Modifier.weight(1f)) {
            Text("磁力链预览器", fontSize = 16.sp, fontWeight = FontWeight.SemiBold, color = pal.text)
            if (!landscape) {
              Spacer(Modifier.height(2.dp))
              Text(
                "DHT 取元数据 · 不下载本体 · 缓存模式 ${state.cacheMode}",
                fontSize = 12.sp,
                color = pal.textMuted,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
              )
            }
          }
          TextButton(onClick = onOpenSettings) { Text("设置", fontSize = 13.sp, color = pal.accent) }
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
            textStyle = TextStyle(fontSize = 14.sp, color = pal.text),
            placeholder = { Text("粘贴磁力链接 magnet:?xt=urn:btih:…", fontSize = 13.sp, color = pal.textDim) },
          )
          Spacer(Modifier.width(8.dp))
          Button(onClick = onResolve, enabled = !state.busy, shape = shapes.shapeButton) {
            Text(if (state.busy) "解析中…" else "解析", fontSize = 14.sp)
          }
        }
        // B-02：解析错误就地（输入框下方紧贴）
        state.errorText?.let {
          Spacer(Modifier.height(4.dp))
          Text(it, fontSize = 12.sp, color = pal.danger, maxLines = 2, overflow = TextOverflow.Ellipsis)
        }
      }
      // ---- 状态主行（15sp；工程类一句人话收「详情」）----
      if (state.statusText.isNotEmpty() || state.restoreNote.isNotEmpty() || state.persistNote.isNotEmpty()) {
        item(key = "status") {
          Column(modifier = Modifier.fillMaxWidth()) {
            if (state.statusText.isNotEmpty()) {
              Text(state.statusText, fontSize = 15.sp, color = pal.text, maxLines = 2, overflow = TextOverflow.Ellipsis)
            }
            // 工程信息只有一行 12sp dim（不堆）
            if (state.restoreNote.isNotEmpty() || state.persistNote.isNotEmpty()) {
              Text(
                state.restoreNote.ifEmpty { state.persistNote },
                fontSize = 12.sp,
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
            Text(state.previewNote.ifEmpty { "预览失败" }, fontSize = 13.sp, color = pal.danger)
            Spacer(Modifier.height(6.dp))
            Button(onClick = onRetryPreview, shape = shapes.shapeButton) { Text("重试", fontSize = 13.sp) }
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
        item(key = "empty") { EmptyState() }
      }
      // ---- 焦点任务信息 / 播放卡 / 文件行 ----
      if (state.torrentName.isNotEmpty()) {
        item(key = "focus") {
          PanelCard(shapes = shapes) {
            Row(verticalAlignment = Alignment.CenterVertically) {
              Text(
                state.torrentName,
                modifier = Modifier.weight(1f),
                fontSize = 16.sp,
                fontWeight = FontWeight.SemiBold,
                color = pal.text,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
              )
              if (state.taskState.isNotEmpty()) {
                Text(state.taskState, fontSize = 12.sp, color = pal.textMuted)
              }
            }
            Spacer(Modifier.height(4.dp))
            Text(
              "总大小 ${state.totalSizeText} · ${state.files.size} 个文件",
              fontSize = 13.sp,
              color = pal.textMuted,
            )
          }
        }
      }
      if (state.previewIndex >= 0) {
        item(key = "preview") {
          // D-02：播放卡固定结构（状态文/控件同一行内排，不挤出）
          PreviewPane(
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
    // ---- Snackbar 宿主（B-03：重复解析提示；底部悬浮）----
    SnackbarHost(
      hostState = snackbarHostState,
      modifier = Modifier.align(Alignment.BottomCenter).padding(16.dp),
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
        .padding(horizontal = 12.dp, vertical = 10.dp),
    verticalAlignment = Alignment.CenterVertically,
  ) {
    Column(modifier = Modifier.weight(1f)) {
      Text(item.name, fontSize = 15.sp, color = pal.text, maxLines = 1, overflow = TextOverflow.Ellipsis)
      Spacer(Modifier.height(2.dp))
      Text(
        if (item.path != item.name) "${item.sizeText} · ${item.path}" else item.sizeText,
        fontSize = 13.sp,
        color = pal.textMuted,
        maxLines = 1,
        overflow = TextOverflow.Ellipsis,
      )
    }
    Spacer(Modifier.width(4.dp))
    when {
      item.isImage -> Text("图片 · 画廊", fontSize = 13.sp, color = pal.accent, fontWeight = FontWeight.Medium)
      !item.isVideo -> Text("非视频", fontSize = 13.sp, color = pal.textDim)
      !selected -> Text("预览", fontSize = 13.sp, color = pal.accent, fontWeight = FontWeight.Medium)
      else -> {
        Text(
          if (paused) "暂停中" else "预览中",
          fontSize = 12.sp,
          color = if (paused) pal.warn else pal.ok,
          fontWeight = FontWeight.Medium,
        )
        TextButton(onClick = if (paused) onResume else onPause) {
          Text(if (paused) "继续" else "暂停", fontSize = 13.sp, color = pal.accent)
        }
        TextButton(onClick = onStop) { Text("停止预览", fontSize = 13.sp, color = pal.danger) }
      }
    }
  }
}

/** 文件行/PanelCard 共用的小圆角（4 = 行内元素）。 */
private val RoundedCornerShape4 = androidx.compose.foundation.shape.RoundedCornerShape(8.dp)

@Composable
private fun PreviewPane(
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
) {
  val pal = LocalPal.current
  val shapes = LocalShapes.current
  PanelCard(shapes = shapes) {
    Text(title, fontSize = 15.sp, fontWeight = FontWeight.Medium, color = pal.text, maxLines = 1, overflow = TextOverflow.Ellipsis)
    Spacer(Modifier.height(8.dp))
    Box(
      modifier = Modifier.fillMaxWidth().height(200.dp).clip(shapes.shapeCard).background(Color.Black),
      contentAlignment = Alignment.Center,
    ) {
      if (streamUrl.isNotEmpty()) {
        VideoPlayer(url = streamUrl, seekFraction = seekFraction, seekSeq = seekSeq)
      } else {
        Text(note.ifEmpty { "正在准备索引块…" }, fontSize = 13.sp, color = Color(0xFFD0D4DA))
      }
    }
    Spacer(Modifier.height(8.dp))
    if (bufferPercent > 0f || bufferText.isNotEmpty()) {
      ThinProgress(percent = bufferPercent)
      Spacer(Modifier.height(6.dp))
    }
    SeekBar(fraction = seekFraction, onSeek = onSeek)
    if (seekNote.isNotEmpty()) {
      Text(seekNote, fontSize = 12.sp, color = pal.textMuted, maxLines = 2, overflow = TextOverflow.Ellipsis)
      Spacer(Modifier.height(4.dp))
    }
    // D-02：状态文与控件同一排（字数短、禁止溢出卡）
    Row(verticalAlignment = Alignment.CenterVertically) {
      Text(
        if (bufferText.isNotEmpty()) bufferText else note,
        modifier = Modifier.weight(1f),
        fontSize = 13.sp,
        color = pal.textMuted,
        maxLines = 2,
        overflow = TextOverflow.Ellipsis,
      )
      TextButton(onClick = if (paused) onResume else onPause) {
        Text(if (paused) "继续" else "暂停", fontSize = 13.sp, color = pal.accent)
      }
      TextButton(onClick = onStop) { Text("停止预览", fontSize = 13.sp, color = pal.accent) }
    }
    Spacer(Modifier.height(4.dp))
    Text(
      if (cacheMode == PREVIEW_CACHE_HOLD) "停止语义：hold（冻结暂停，缓冲保留）"
      else "停止语义：convert（转正继续缓存，不暂停）",
      fontSize = 12.sp,
      color = pal.textDim,
    )
  }
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
      Text("跳转 ${TaskCardLogic.percentOf(local)}%", fontSize = 12.sp, color = pal.textMuted, modifier = Modifier.weight(1f))
      Text("拖动结束生效（按字节位置）", fontSize = 12.sp, color = pal.textDim)
    }
  }
}

/** Media3/ExoPlayer 播放本地流（http://127.0.0.1:port/…?t=token）。 */
@Composable
private fun VideoPlayer(url: String, seekFraction: Float, seekSeq: Int) {
  val context = LocalContext.current
  val player = remember { ExoPlayer.Builder(context).build() }
  DisposableEffect(player) { onDispose { player.release() } }
  LaunchedEffect(url) {
    player.setMediaItem(MediaItem.fromUri(url))
    player.prepare()
    player.playWhenReady = true
  }
  LaunchedEffect(seekSeq) {
    if (seekSeq > 0) {
      val duration = player.duration
      if (duration > 0 && duration != C.TIME_UNSET) player.seekTo((duration * seekFraction).toLong())
    }
  }
  AndroidView(
    modifier = Modifier.fillMaxSize(),
    factory = { ctx ->
      PlayerView(ctx).apply {
        this.player = player
        useController = true
      }
    },
  )
}

/** 自绘细进度条（缓冲比例）。 */
@Composable
private fun ThinProgress(percent: Float) {
  val pal = LocalPal.current
  Box(
    modifier = Modifier.fillMaxWidth().height(6.dp).clip(androidx.compose.foundation.shape.CircleShape).background(pal.border)
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
        .padding(16.dp),
    content = content,
  )
}
