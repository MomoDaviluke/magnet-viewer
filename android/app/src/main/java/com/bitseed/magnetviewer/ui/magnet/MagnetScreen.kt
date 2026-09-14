package com.bitseed.magnetviewer.ui.magnet

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
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.media3.common.MediaItem
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.ui.PlayerView
import com.bitseed.magnetviewer.theme.Pal

/**
 * 主界面：磁力链输入 → 文件清单（过滤 .pad）→ 点视频边下边播（本地流 + Media3）。
 */
@Composable
fun MagnetScreen(modifier: Modifier = Modifier) {
  val appContext = LocalContext.current.applicationContext
  val viewModel: MagnetViewModel = viewModel { MagnetViewModel(appContext) }
  val state by viewModel.state.collectAsStateWithLifecycle()
  MagnetContent(
    state = state,
    onInput = viewModel::onInputChange,
    onResolve = viewModel::resolve,
    onPreview = viewModel::preview,
    onStopPreview = viewModel::stopPreview,
    modifier = modifier,
  )
}

@Composable
internal fun MagnetContent(
  state: MagnetUiState,
  onInput: (String) -> Unit,
  onResolve: () -> Unit,
  onPreview: (Int) -> Unit,
  onStopPreview: () -> Unit,
  modifier: Modifier = Modifier,
) {
  Column(modifier = modifier.fillMaxSize().background(Pal.bg).padding(16.dp)) {
    Text("磁力链预览器", fontSize = 18.sp, fontWeight = FontWeight.SemiBold, color = Pal.text)
    Spacer(Modifier.height(4.dp))
    Text("DHT 取元数据 · 不下载本体 · 点视频边下边播", fontSize = 13.sp, color = Pal.textMuted)
    Spacer(Modifier.height(12.dp))

    Row(verticalAlignment = Alignment.CenterVertically) {
      OutlinedTextField(
        value = state.input,
        onValueChange = onInput,
        modifier = Modifier.weight(1f),
        singleLine = true,
        enabled = !state.busy,
        textStyle = TextStyle(fontSize = 14.sp, color = Pal.text),
        placeholder = { Text("粘贴磁力链接 magnet:?xt=urn:btih:…", fontSize = 13.sp, color = Pal.textDim) },
      )
      Spacer(Modifier.width(8.dp))
      Button(onClick = onResolve, enabled = !state.busy, shape = RoundedCornerShape(8.dp)) {
        Text(if (state.busy) "解析中…" else "解析", fontSize = 14.sp)
      }
    }

    if (state.statusText.isNotEmpty()) {
      Spacer(Modifier.height(8.dp))
      Text(state.statusText, fontSize = 13.sp, color = Pal.textMuted, maxLines = 2, overflow = TextOverflow.Ellipsis)
    }
    state.errorText?.let {
      Spacer(Modifier.height(8.dp))
      Text(it, fontSize = 13.sp, color = Pal.danger)
    }

    if (state.torrentName.isNotEmpty()) {
      Spacer(Modifier.height(12.dp))
      PanelCard {
        Text(state.torrentName, fontSize = 16.sp, fontWeight = FontWeight.SemiBold, color = Pal.text, maxLines = 2, overflow = TextOverflow.Ellipsis)
        Spacer(Modifier.height(4.dp))
        Text(
          "总大小 ${state.totalSizeText} · ${state.files.size} 个文件${if (state.infoHash.isNotEmpty()) " · " + state.infoHash.take(16) + "…" else ""}",
          fontSize = 13.sp,
          color = Pal.textMuted,
        )
      }
    }

    if (state.previewIndex >= 0) {
      Spacer(Modifier.height(12.dp))
      PreviewPane(
        streamUrl = state.streamUrl,
        title = state.previewTitle,
        note = state.previewNote,
        bufferText = state.bufferText,
        bufferPercent = state.bufferPercent,
        onStop = onStopPreview,
      )
    }

    Spacer(Modifier.height(12.dp))
    LazyColumn(modifier = Modifier.weight(1f)) {
      items(state.files, key = { it.index }) { item ->
        FileRow(item = item, selected = item.index == state.previewIndex, onClick = { onPreview(item.index) })
        Spacer(Modifier.height(6.dp))
      }
    }
  }
}

@Composable
private fun FileRow(item: MagnetFileItem, selected: Boolean, onClick: () -> Unit) {
  Row(
    modifier =
      Modifier.fillMaxWidth()
        .clip(RoundedCornerShape(8.dp))
        .background(if (selected) Pal.bgSelected else Pal.panel)
        .border(1.dp, Pal.border, RoundedCornerShape(8.dp))
        .clickable(enabled = item.isVideo, onClick = onClick)
        .padding(horizontal = 12.dp, vertical = 10.dp),
    verticalAlignment = Alignment.CenterVertically,
  ) {
    Column(modifier = Modifier.weight(1f)) {
      Text(item.name, fontSize = 14.sp, color = Pal.text, maxLines = 1, overflow = TextOverflow.Ellipsis)
      Spacer(Modifier.height(2.dp))
      Text(
        if (item.path != item.name) "${item.sizeText} · ${item.path}" else item.sizeText,
        fontSize = 13.sp,
        color = Pal.textMuted,
        maxLines = 1,
        overflow = TextOverflow.Ellipsis,
      )
    }
    Spacer(Modifier.width(8.dp))
    if (item.isVideo) {
      Text(
        if (selected) "预览中" else "预览",
        fontSize = 13.sp,
        color = if (selected) Pal.ok else Pal.accent,
        fontWeight = FontWeight.Medium,
      )
    } else {
      Text("非视频", fontSize = 13.sp, color = Pal.textDim)
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
  onStop: () -> Unit,
) {
  PanelCard {
    Text(title, fontSize = 14.sp, fontWeight = FontWeight.Medium, color = Pal.text, maxLines = 1, overflow = TextOverflow.Ellipsis)
    Spacer(Modifier.height(8.dp))
    Box(
      modifier = Modifier.fillMaxWidth().height(200.dp).clip(RoundedCornerShape(8.dp)).background(Color.Black),
      contentAlignment = Alignment.Center,
    ) {
      if (streamUrl.isNotEmpty()) {
        VideoPlayer(url = streamUrl)
      } else {
        Text(note.ifEmpty { "正在准备索引块…" }, fontSize = 13.sp, color = Color(0xFFD0D4DA))
      }
    }
    Spacer(Modifier.height(8.dp))
    if (bufferPercent > 0f || bufferText.isNotEmpty()) {
      ThinProgress(percent = bufferPercent)
      Spacer(Modifier.height(6.dp))
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
      Text(
        if (bufferText.isNotEmpty()) bufferText else note,
        modifier = Modifier.weight(1f),
        fontSize = 13.sp,
        color = Pal.textMuted,
        maxLines = 2,
        overflow = TextOverflow.Ellipsis,
      )
      TextButton(onClick = onStop) { Text("停止预览", fontSize = 13.sp, color = Pal.accent) }
    }
  }
}

/** Media3/ExoPlayer 播放本地流（http://127.0.0.1:port/…?t=token）。 */
@Composable
private fun VideoPlayer(url: String) {
  val context = LocalContext.current
  val player = remember { ExoPlayer.Builder(context).build() }
  DisposableEffect(player) { onDispose { player.release() } }
  LaunchedEffect(url) {
    player.setMediaItem(MediaItem.fromUri(url))
    player.prepare()
    player.playWhenReady = true
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
  Box(
    modifier = Modifier.fillMaxWidth().height(6.dp).clip(RoundedCornerShape(3.dp)).background(Pal.border)
  ) {
    Box(
      modifier =
        Modifier.fillMaxHeight()
          .fillMaxWidth(percent.coerceIn(0f, 1f))
          .background(Pal.accent)
    )
  }
}

@Composable
private fun PanelCard(modifier: Modifier = Modifier, content: @Composable ColumnScope.() -> Unit) {
  Column(
    modifier =
      modifier
        .fillMaxWidth()
        .clip(RoundedCornerShape(10.dp))
        .background(Pal.panel)
        .border(1.dp, Pal.border, RoundedCornerShape(10.dp))
        .padding(12.dp),
    content = content,
  )
}
