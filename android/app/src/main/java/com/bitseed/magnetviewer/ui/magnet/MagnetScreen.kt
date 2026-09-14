package com.bitseed.magnetviewer.ui.magnet

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
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
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
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
import com.bitseed.magnetviewer.theme.MagnetViewerTheme
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

  MagnetViewerTheme(themeMode = state.settings.uiTheme) {
    if (state.settingsOpen) {
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
        modifier = modifier,
      )
    }
  }
}

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
  onFocusTask: (String) -> Unit = {},
  onToggleExpand: (String) -> Unit = {},
  onPauseTask: (String) -> Unit = {},
  onResumeTask: (String) -> Unit = {},
  onPromoteTask: (String) -> Unit = {},
  onRemoveTask: (String, Boolean) -> Unit = { _, _ -> },
  onSetPriority: (String, Int) -> Unit = { _, _ -> },
  modifier: Modifier = Modifier,
) {
  val pal = LocalPal.current
  // 单列可滚动布局：任务列表变高后，文件清单/预览不能再被顶出屏幕（实测踩过——
  // 多任务列表把文件清单挤出可视区，预览点不到）。整屏一个 LazyColumn，文件行作为它的 items。
  LazyColumn(
    modifier = modifier.fillMaxSize().background(pal.bg).padding(16.dp),
    verticalArrangement = androidx.compose.foundation.layout.Arrangement.spacedBy(8.dp),
  ) {
    item(key = "header") {
      Row(verticalAlignment = Alignment.CenterVertically) {
        Column(modifier = Modifier.weight(1f)) {
          Text("磁力链预览器", fontSize = 18.sp, fontWeight = FontWeight.SemiBold, color = pal.text)
          Spacer(Modifier.height(4.dp))
          Text(
            "DHT 取元数据 · 不下载本体 · 点视频边下边播 · 缓存模式 ${state.cacheMode}",
            fontSize = 13.sp,
            color = pal.textMuted,
          )
        }
        TextButton(onClick = onOpenSettings) { Text("设置", fontSize = 14.sp, color = pal.accent) }
      }
    }
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
        Button(onClick = onResolve, enabled = !state.busy, shape = RoundedCornerShape(8.dp)) {
          Text(if (state.busy) "解析中…" else "解析", fontSize = 14.sp)
        }
      }
    }
    if (state.statusText.isNotEmpty() || state.errorText != null ||
      state.restoreNote.isNotEmpty() || state.persistNote.isNotEmpty()
    ) {
      item(key = "status") {
        Column(modifier = Modifier.fillMaxWidth()) {
          if (state.statusText.isNotEmpty()) {
            Text(state.statusText, fontSize = 13.sp, color = pal.textMuted, maxLines = 3, overflow = TextOverflow.Ellipsis)
          }
          state.errorText?.let { Text(it, fontSize = 13.sp, color = pal.danger) }
          if (state.restoreNote.isNotEmpty()) {
            Text(state.restoreNote, fontSize = 12.sp, color = pal.textMuted, maxLines = 4, overflow = TextOverflow.Ellipsis)
          }
          if (state.persistNote.isNotEmpty()) {
            Text(state.persistNote, fontSize = 12.sp, color = pal.textDim, maxLines = 3, overflow = TextOverflow.Ellipsis)
          }
        }
      }
    }
    // 多任务列表（每行：名称/状态/总大小/已下载/优先级）
    if (state.taskRows.isNotEmpty()) {
      item(key = "tasks") {
        TaskListPanel(
          rows = state.taskRows,
          onFocus = onFocusTask,
          onToggleExpand = onToggleExpand,
          onPause = onPauseTask,
          onResume = onResumeTask,
          onPromote = onPromoteTask,
          onRemove = onRemoveTask,
          onPriority = onSetPriority,
        )
      }
    }
    if (state.torrentName.isNotEmpty()) {
      item(key = "focus") {
        PanelCard {
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
            "总大小 ${state.totalSizeText} · ${state.files.size} 个文件${if (state.infoHash.isNotEmpty()) " · " + state.infoHash.take(16) + "…" else ""}",
            fontSize = 13.sp,
            color = pal.textMuted,
          )
          Spacer(Modifier.height(4.dp))
          Text(
            "清单来自磁盘清单文件 .tasks.json（重启/被杀后无需重新解析）· 操作见上方任务列表",
            fontSize = 12.sp,
            color = pal.textDim,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
          )
        }
      }
    }
    if (state.previewIndex >= 0) {
      item(key = "preview") {
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
    items(state.files, key = { it.index }) { item ->
      FileRow(
        item = item,
        selected = item.index == state.previewIndex,
        paused = state.previewPaused,
        onClick = { onPreview(item.index) },
        onPause = onPausePreview,
        onResume = onResumePreview,
        onStop = onStopPreview,
      )
    }
  }
}

/**
 * 任务列表（多任务）：一行一个 info_hash。
 *
 * 每行展示 **名称 / 状态 / 总大小 / 已下载 / 优先级**（与桌面 `taskops.tasks()` 快照同口径），
 * 行内可：切焦点（点行）· 展开文件清单 · 暂停/继续 · **转正** · 移除（保留数据 / 删数据）· 优先级 0..3。
 */
@Composable
private fun TaskListPanel(
  rows: List<TaskRow>,
  onFocus: (String) -> Unit,
  onToggleExpand: (String) -> Unit,
  onPause: (String) -> Unit,
  onResume: (String) -> Unit,
  onPromote: (String) -> Unit,
  onRemove: (String, Boolean) -> Unit,
  onPriority: (String, Int) -> Unit,
) {
  val pal = LocalPal.current
  PanelCard {
    Text("任务列表（${rows.size}）", fontSize = 14.sp, fontWeight = FontWeight.Medium, color = pal.text)
    Spacer(Modifier.height(2.dp))
    Text(
      "每行：名称 / 状态 / 总大小 / 已下载（fileProgress 求和）/ 优先级；点行切焦点（清单与预览跟随）",
      fontSize = 12.sp,
      color = pal.textMuted,
      maxLines = 2,
      overflow = TextOverflow.Ellipsis,
    )
    rows.forEach { row ->
      Spacer(Modifier.height(8.dp))
      TaskRowView(
        row = row,
        onFocus = onFocus,
        onToggleExpand = onToggleExpand,
        onPause = onPause,
        onResume = onResume,
        onPromote = onPromote,
        onRemove = onRemove,
        onPriority = onPriority,
      )
    }
  }
}

@Composable
private fun TaskRowView(
  row: TaskRow,
  onFocus: (String) -> Unit,
  onToggleExpand: (String) -> Unit,
  onPause: (String) -> Unit,
  onResume: (String) -> Unit,
  onPromote: (String) -> Unit,
  onRemove: (String, Boolean) -> Unit,
  onPriority: (String, Int) -> Unit,
) {
  val pal = LocalPal.current
  Column(
    modifier =
      Modifier.fillMaxWidth()
        .clip(RoundedCornerShape(8.dp))
        .background(if (row.focused) pal.bgSelected else pal.bgInput)
        .border(1.dp, if (row.focused) pal.accent else pal.border, RoundedCornerShape(8.dp))
        .clickable { onFocus(row.infoHash) }
        .padding(horizontal = 10.dp, vertical = 8.dp)
  ) {
    Row(verticalAlignment = Alignment.CenterVertically) {
      Text(
        (if (row.focused) "▶ " else "") + row.name,
        modifier = Modifier.weight(1f),
        fontSize = 14.sp,
        fontWeight = FontWeight.Medium,
        color = pal.text,
        maxLines = 1,
        overflow = TextOverflow.Ellipsis,
      )
      Text(row.state, fontSize = 12.sp, color = pal.textMuted)
    }
    Spacer(Modifier.height(2.dp))
    Text(
      "总大小 ${row.totalSizeText} · 已下载 ${row.downloadedText}（${row.progressPercent}%）" +
        (if (row.rateText.isNotEmpty()) " · ${row.rateText}" else "") +
        " · 优先级 P${row.priority}" +
        (if (row.isDownload) " · 下载任务" else " · 仅解析"),
      fontSize = 12.sp,
      color = pal.textMuted,
      maxLines = 2,
      overflow = TextOverflow.Ellipsis,
    )
    if (row.error.isNotEmpty()) {
      Text(row.error, fontSize = 12.sp, color = pal.danger, maxLines = 2, overflow = TextOverflow.Ellipsis)
    }
    // 优先级（0..3；影响下载队列与并发）
    Row(verticalAlignment = Alignment.CenterVertically) {
      Text("优先级", fontSize = 12.sp, color = pal.textDim)
      for (p in 0..3) {
        TextButton(onClick = { onPriority(row.infoHash, p) }) {
          Text(
            "P$p",
            fontSize = 12.sp,
            color = if (p == row.priority) pal.accent else pal.textMuted,
            fontWeight = if (p == row.priority) FontWeight.SemiBold else FontWeight.Normal,
          )
        }
      }
    }
    // 任务操作（逐条对齐桌面 taskops：转正 / 暂停继续 / 移除两档）
    // 「已转正」只按**落盘目录**判定（downloads/<ih> 才叫转正过）——预览任务同样是下载型，
    // 但它的数据还在 .preview/ 里（此时按钮该显示「转正」，点了才搬到 downloads/）。
    val promoted = row.savePath.contains("/downloads/")
    Row(verticalAlignment = Alignment.CenterVertically) {
      TextButton(onClick = { onPromote(row.infoHash) }) {
        Text(if (promoted) "已转正" else "转正", fontSize = 13.sp, color = pal.accent)
      }
      TextButton(onClick = { onPause(row.infoHash) }) { Text("暂停", fontSize = 13.sp, color = pal.accent) }
      TextButton(onClick = { onResume(row.infoHash) }) { Text("继续", fontSize = 13.sp, color = pal.accent) }
      TextButton(onClick = { onRemove(row.infoHash, false) }) {
        Text("移除(保留数据)", fontSize = 13.sp, color = pal.danger)
      }
      TextButton(onClick = { onRemove(row.infoHash, true) }) {
        Text("移除(删数据)", fontSize = 13.sp, color = pal.danger)
      }
      TextButton(onClick = { onToggleExpand(row.infoHash) }) {
        Text(if (row.expanded) "收起文件" else "展开文件", fontSize = 13.sp, color = pal.accent)
      }
    }
    if (row.expanded) {
      Text(
        "文件清单（${row.files.size} 个，已过滤 .pad）",
        fontSize = 12.sp,
        color = pal.textDim,
      )
      row.files.forEach { f ->
        Text(
          "${f.name} · ${f.sizeText}" + (if (f.isVideo) " · 视频" else ""),
          fontSize = 12.sp,
          color = pal.text,
          maxLines = 1,
          overflow = TextOverflow.Ellipsis,
        )
      }
    }
  }
}

/**
 * 文件行：视频行可点开预览；**当前预览行**额外挂「暂停 / 继续」与「停止预览」。
 * 选中态的文案区分两档缓存模式的停止语义（hold = 冻结；convert = 转正继续）。
 */
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
  Row(
    modifier =
      Modifier.fillMaxWidth()
        .clip(RoundedCornerShape(8.dp))
        .background(if (selected) pal.bgSelected else pal.panel)
        .border(1.dp, pal.border, RoundedCornerShape(8.dp))
        .clickable(enabled = item.isVideo, onClick = onClick)
        .padding(horizontal = 12.dp, vertical = 10.dp),
    verticalAlignment = Alignment.CenterVertically,
  ) {
    Column(modifier = Modifier.weight(1f)) {
      Text(item.name, fontSize = 14.sp, color = pal.text, maxLines = 1, overflow = TextOverflow.Ellipsis)
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
  PanelCard {
    Text(title, fontSize = 14.sp, fontWeight = FontWeight.Medium, color = pal.text, maxLines = 1, overflow = TextOverflow.Ellipsis)
    Spacer(Modifier.height(8.dp))
    Box(
      modifier = Modifier.fillMaxWidth().height(200.dp).clip(RoundedCornerShape(8.dp)).background(Color.Black),
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

/**
 * 跳转条（0..1 = 文件比例）。**只在拖动结束时**回调 [onSeek]
 * （避免拖动过程中每一帧都去重建分块窗口）。
 */
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
      Text("跳转 ${(local * 100).toInt()}%", fontSize = 12.sp, color = pal.textMuted, modifier = Modifier.weight(1f))
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
  // 拖动条落点 → 播放器跟着跳（时长已知时；未知就只靠引擎侧重排窗口）
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
    modifier = Modifier.fillMaxWidth().height(6.dp).clip(RoundedCornerShape(3.dp)).background(pal.border)
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
private fun PanelCard(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
  val pal = LocalPal.current
  Column(
    modifier =
      modifier
        .fillMaxWidth()
        .clip(RoundedCornerShape(10.dp))
        .background(pal.panel)
        .border(1.dp, pal.border, RoundedCornerShape(10.dp))
        .padding(12.dp),
    content = content,
  )
}
