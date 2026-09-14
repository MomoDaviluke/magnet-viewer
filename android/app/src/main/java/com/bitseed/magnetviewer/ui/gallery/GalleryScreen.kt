package com.bitseed.magnetviewer.ui.gallery

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.bitseed.magnetviewer.engine.ImageDownloadState
import com.bitseed.magnetviewer.engine.imageStateText
import com.bitseed.magnetviewer.theme.LocalPal
import java.io.File
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

/**
 * 画廊界面状态（VM → UI；纯数据）。
 *
 * [position] = 大图区当前索引（**-1 = 停在缩略图网格**，与桌面「列表 + 大图同屏」的
 * 语义等价：这里把「网格」与「大图」做成两态，点缩略图进大图，返回网格退回）。
 */
data class GalleryUiState(
  val open: Boolean = false,
  val items: List<GalleryItem> = emptyList(),
  val position: Int = -1,
  /** 大图缩放比例（1.0 = 适应窗口；夹取见 [clampScale]）。 */
  val scale: Float = 1f,
  /** 当前预览目标（引擎侧正在按需下载的那张图）在清单里的文件下标；-1 = 无。 */
  val targetIndex: Int = -1,
  val note: String = "",
)

/** 画廊里的一张图（展示态：把「清单条目 + 下载读数 + 本地文件事实」摊平）。 */
data class GalleryItem(
  val index: Int,
  val name: String,
  val path: String,
  val size: Long,
  val sizeText: String,
  /** 本地绝对路径（映射自任务落盘目录；空 = 目录未知，不能读盘）。 */
  val diskPath: String,
  /**
   * 「**内容**已完整落盘」的确认（`deriveImageState` 的第一判据）。
   *
   * ⚠️ 不可由文件长度得出：libtorrent 预分配稀疏文件，长度从第一刻就是全长。
   * VM 里的算法 = 引擎读数达标（`file_progress >= size`）**且**磁盘上文件存在。
   */
  val onDiskComplete: Boolean,
  val downloadedBytes: Long,
  val state: ImageDownloadState,
  /** 是否当前预览目标（引擎正在为它做「单文件优先 + 按需补拉」）。 */
  val isTarget: Boolean,
) {
  /** 缩略图/大图能否直接解码本地文件。 */
  val renderable: Boolean
    get() = state == ImageDownloadState.READY && diskPath.isNotEmpty()
}

/**
 * 图片画廊（对齐桌面 `ui/gallery.py`）：
 * 缩略图网格（未下载的显示占位 + 进度）→ 点开大图（缩放 / 上一张 / 下一张）。
 *
 * **解码方式的选择：直接用 `BitmapFactory` 解码本地文件，不引入 Coil。**
 * 理由（二选一并说明）：
 * 1. 图片就在**本地磁盘**（libtorrent 落盘路径已知），没有网络/URL 场景，Coil 的
 *    磁盘缓存、请求管线、`AsyncImage` 这些能力全都用不上；
 * 2. 本仓库对「不新增依赖」敏感（依赖下载慢、包体已经在 60MB+），而 `BitmapFactory` +
 *    `inSampleSize` 正是 Coil 在本地文件这条路上的底层实现；
 * 3. 抽样解码需要一个可断言的规则（缩略图按目标像素取 2 的幂次采样），用 20 行代码
 *    就能写清，Coil 反而要额外的 `ImageRequest` 配置；
 * 4. 只在 [GalleryItem.renderable]（本地文件已完整）时才解码——避免对稀疏/半截文件
 *    调用解码器。
 */
@Composable
fun GalleryScreen(
  state: GalleryUiState,
  onClose: () -> Unit,
  onOpenImage: (Int) -> Unit,
  onStep: (Int) -> Unit,
  onZoom: (Boolean) -> Unit,
  onResetZoom: () -> Unit,
  onBackToGrid: () -> Unit,
  modifier: Modifier = Modifier,
) {
  val pal = LocalPal.current
  val items = state.items
  // **-1 = 停在缩略图网格**（见 viewerPosition 的注释：clampIndex 会把 -1 夹成 0）
  val position = viewerPosition(state.position, items.size)
  val cur = if (position >= 0) items[position] else null
  Column(
    modifier = modifier.fillMaxSize().background(pal.bg).padding(12.dp),
    verticalArrangement = Arrangement.spacedBy(8.dp),
  ) {
    // 头：标题 + 返回
    Row(verticalAlignment = Alignment.CenterVertically) {
      Column(modifier = Modifier.weight(1f)) {
        Text("图片画廊", fontSize = 18.sp, fontWeight = FontWeight.SemiBold, color = pal.text)
        Spacer(Modifier.height(2.dp))
        Text(
          "共 ${items.size} 张图片 · " +
            if (position < 0) "点缩略图看大图（翻到未下载的图片会自动按需下载）"
            else "大图查看（可缩放 / 上一张 / 下一张）",
          fontSize = 12.sp,
          color = pal.textMuted,
          maxLines = 2,
          overflow = TextOverflow.Ellipsis,
        )
      }
      TextButton(onClick = onClose) { Text("返回文件清单", fontSize = 13.sp, color = pal.accent) }
    }
    if (state.note.isNotEmpty()) {
      Text(state.note, fontSize = 12.sp, color = pal.textMuted, maxLines = 3, overflow = TextOverflow.Ellipsis)
    }
    if (items.isEmpty()) {
      Text("该资源中没有图片文件", fontSize = 14.sp, color = pal.textDim)
      return@Column
    }
    if (cur == null) {
      ThumbGrid(items = items, onOpenImage = onOpenImage, modifier = Modifier.weight(1f))
    } else {
      BigViewer(
        item = cur,
        position = position,
        count = items.size,
        scale = state.scale,
        targetIndex = state.targetIndex,
        onStep = onStep,
        onZoom = onZoom,
        onResetZoom = onResetZoom,
        onBackToGrid = onBackToGrid,
        modifier = Modifier.weight(1f),
      )
    }
  }
}

/** 缩略图网格：已下载 → 图片本体（抽样解码）；未下载 → 占位 + 进度。 */
@Composable
private fun ThumbGrid(items: List<GalleryItem>, onOpenImage: (Int) -> Unit, modifier: Modifier = Modifier) {
  val pal = LocalPal.current
  LazyVerticalGrid(
    columns = GridCells.Fixed(3),
    modifier = modifier.fillMaxWidth(),
    horizontalArrangement = Arrangement.spacedBy(8.dp),
    verticalArrangement = Arrangement.spacedBy(8.dp),
  ) {
    items(items, key = { it.index }) { item ->
      val pos = items.indexOfFirst { it.index == item.index }
      ThumbCell(item = item, onClick = { onOpenImage(pos) })
    }
  }
}

@Composable
private fun ThumbCell(item: GalleryItem, onClick: () -> Unit) {
  val pal = LocalPal.current
  val bmp = rememberSampledBitmap(item.diskPath, item.renderable, THUMB_PX)
  Column(
    modifier =
      Modifier.fillMaxWidth()
        .clip(RoundedCornerShape(8.dp))
        .background(pal.panel)
        .border(1.dp, if (item.isTarget) pal.accent else pal.border, RoundedCornerShape(8.dp))
        .clickable(onClick = onClick)
        .padding(6.dp)
  ) {
    Box(
      modifier = Modifier.fillMaxWidth().aspectRatio(1.33f).clip(RoundedCornerShape(6.dp)).background(pal.bgInput),
      contentAlignment = Alignment.Center,
    ) {
      if (bmp != null) {
        Image(
          bitmap = bmp,
          contentDescription = "缩略图 ${item.name}",
          modifier = Modifier.fillMaxSize(),
          contentScale = ContentScale.Crop,
        )
      } else {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
          Text("图片占位", fontSize = 11.sp, color = pal.textDim)
          Spacer(Modifier.height(4.dp))
          Text(
            imageStateText(item.state, item.downloadedBytes.toDouble() / maxOf(1L, item.size)),
            fontSize = 11.sp,
            color = if (item.state == ImageDownloadState.READY) pal.ok else pal.warn,
          )
          Spacer(Modifier.height(4.dp))
          ThinBar(fraction = item.downloadedBytes.toDouble() / maxOf(1L, item.size), width = 72.dp)
        }
      }
    }
    Spacer(Modifier.height(4.dp))
    Text(
      item.name,
      fontSize = 12.sp,
      fontWeight = FontWeight.Medium,
      color = pal.text,
      maxLines = 1,
      overflow = TextOverflow.Ellipsis,
    )
    Text(
      item.sizeText + " · " +
        imageStateText(item.state, item.downloadedBytes.toDouble() / maxOf(1L, item.size)) +
        (if (item.isTarget) " · 预览目标" else ""),
      fontSize = 11.sp,
      color = if (item.state == ImageDownloadState.READY) pal.ok else pal.textMuted,
      maxLines = 1,
      overflow = TextOverflow.Ellipsis,
    )
  }
}

/** 大图区：缩放（按钮步进）+ 上一张/下一张（首末张不越界）+ 未下载时显示进度。 */
@Composable
private fun BigViewer(
  item: GalleryItem,
  position: Int,
  count: Int,
  scale: Float,
  targetIndex: Int,
  onStep: (Int) -> Unit,
  onZoom: (Boolean) -> Unit,
  onResetZoom: () -> Unit,
  onBackToGrid: () -> Unit,
  modifier: Modifier = Modifier,
) {
  val pal = LocalPal.current
  val bmp = rememberSampledBitmap(item.diskPath, item.renderable, VIEWER_PX)
  Column(modifier = modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
    Box(
      modifier =
        Modifier.fillMaxWidth()
          .height(300.dp)
          .clip(RoundedCornerShape(8.dp))
          .background(pal.bgInput)
          .border(1.dp, pal.border, RoundedCornerShape(8.dp)),
      contentAlignment = Alignment.Center,
    ) {
      if (bmp != null) {
        Image(
          bitmap = bmp,
          contentDescription = "大图 ${item.name}",
          modifier = Modifier.fillMaxSize().graphicsLayer(scaleX = scale, scaleY = scale),
          contentScale = ContentScale.Fit,
        )
      } else {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
          val pct = (item.downloadedBytes.toDouble() / maxOf(1L, item.size) * 100.0).toInt().coerceIn(0, 99)
          Text(
            if (item.state == ImageDownloadState.READY) "正在解码本地图片…"
            else "下载中 $pct%…（完成后自动显示）",
            fontSize = 13.sp,
            color = pal.warn,
          )
          Spacer(Modifier.height(6.dp))
          ThinBar(fraction = item.downloadedBytes.toDouble() / maxOf(1L, item.size), width = 200.dp)
        }
      }
    }
    Text(
      galleryInfoText(position, count, item.name, item.sizeText),
      fontSize = 13.sp,
      color = pal.text,
      maxLines = 1,
      overflow = TextOverflow.Ellipsis,
    )
    Text(
      "缩放 ${zoomPercent(scale)}%（范围 ${(MIN_SCALE * 100).toInt()}% ~ ${(MAX_SCALE * 100).toInt()}%）" +
        (if (item.isTarget) " · 当前预览目标（按需下载中）" else ""),
      fontSize = 12.sp,
      color = pal.textMuted,
    )
    Row(verticalAlignment = Alignment.CenterVertically) {
      TextButton(onClick = { onStep(-1) }) { Text("← 上一张", fontSize = 13.sp, color = pal.accent) }
      TextButton(onClick = { onStep(1) }) { Text("下一张 →", fontSize = 13.sp, color = pal.accent) }
      Spacer(Modifier.width(4.dp))
      TextButton(onClick = { onZoom(false) }) { Text("缩小", fontSize = 13.sp, color = pal.accent) }
      TextButton(onClick = onResetZoom) { Text("重置", fontSize = 13.sp, color = pal.accent) }
      TextButton(onClick = { onZoom(true) }) { Text("放大", fontSize = 13.sp, color = pal.accent) }
    }
    Row(verticalAlignment = Alignment.CenterVertically) {
      TextButton(onClick = onBackToGrid) { Text("返回网格", fontSize = 13.sp, color = pal.accent) }
      Text(
        if (position == 0) "已是第一张（不会越界）" else if (position == count - 1) "已是最后一张（不会越界）" else "",
        fontSize = 12.sp,
        color = pal.textDim,
        modifier = Modifier.weight(1f),
      )
      Text(
        if (targetIndex >= 0) "预览目标 #$targetIndex" else "无预览目标",
        fontSize = 12.sp,
        color = pal.textDim,
      )
    }
  }
}

/** 细进度条（占位格 / 大图区的下载进度）。 */
@Composable
private fun ThinBar(fraction: Double, width: androidx.compose.ui.unit.Dp) {
  val pal = LocalPal.current
  val f = fraction.coerceIn(0.0, 1.0).toFloat()
  Box(modifier = Modifier.width(width).height(6.dp).clip(RoundedCornerShape(3.dp)).background(pal.border)) {
    Box(modifier = Modifier.fillMaxHeight().fillMaxWidth(f).background(pal.accent))
  }
}

/** 目标解码像素：缩略图小、大图大（抽样比例据此算）。 */
private const val THUMB_PX = 240
private const val VIEWER_PX = 1080

/**
 * 抽样解码本地图片（`BitmapFactory` + `inSampleSize`），**失败会重试**。
 *
 * 为什么必须重试：下载完成的那一刻 `file_progress` 已达标（`READY`），但 libtorrent 对
 * 该文件所在的最后一批分块**可能还在 flush**——此时读盘解码会拿到不完整的内容并返回
 * null。属性（`ready`）不变 → `LaunchedEffect` 不会再触发 → 占位文案永远不消失
 * （实测踩过：小图 `READY` 后 4 秒仍是「正在解码本地图片…」，而 5 秒后手动触发就成功）。
 * 所以这里在「应当能显示」但解码失败时**按间隔重试**（上限 [DECODE_RETRY_MAX] 次），
 * 一旦成功后不再重试。
 */
@Composable
private fun rememberSampledBitmap(path: String, ready: Boolean, targetPx: Int): ImageBitmap? {
  var bmp by remember { mutableStateOf<ImageBitmap?>(null) }
  LaunchedEffect(path, ready, targetPx) {
    bmp = null
    if (!ready || path.isEmpty()) return@LaunchedEffect
    var tries = 0
    while (bmp == null && tries < DECODE_RETRY_MAX) {
      tries++
      val got = withContext(Dispatchers.IO) { decodeSampled(path, targetPx) }
      if (got != null) {
        bmp = got.asImageBitmap()
      } else {
        logcat("画廊解码：第 $tries 次失败，稍后重试（文件可能还在 flush）")
        delay(DECODE_RETRY_DELAY_MS)
      }
    }
  }
  return bmp
}

/** 解码重试上限与间隔（覆盖「分块刚落盘但还没 flush 完」的窗口）。 */
private const val DECODE_RETRY_MAX = 12
private const val DECODE_RETRY_DELAY_MS = 700L

/** 纯解码（可单测规则之外的最小实现；异常一律吞掉返回 null，但会打日志）。 */
private fun decodeSampled(path: String, targetPx: Int): Bitmap? =
  try {
    val f = File(path)
    if (!f.isFile || f.length() <= 0L) {
      logcat("画廊解码：文件不存在/为空 path=$path")
      null
    } else {
      val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
      BitmapFactory.decodeFile(path, bounds)
      if (bounds.outWidth <= 0 || bounds.outHeight <= 0) {
        logcat("画廊解码：读不出图片尺寸（$path，${f.length()} 字节）")
        null
      } else {
        var sample = 1
        while (bounds.outWidth / (sample * 2) >= targetPx && bounds.outHeight / (sample * 2) >= targetPx) {
          sample *= 2
        }
        val bmp = BitmapFactory.decodeFile(path, BitmapFactory.Options().apply { inSampleSize = sample })
        logcat(
          "画廊解码：path=$path 原图 ${bounds.outWidth}x${bounds.outHeight} 采样 1/$sample " +
            (if (bmp == null) "→ 失败（返回 null）" else "→ 成功 ${bmp.width}x${bmp.height}")
        )
        bmp
      }
    }
  } catch (e: Throwable) {
    logcat("画廊解码异常：path=$path ${e.javaClass.simpleName}: ${e.message}")
    null
  }

/** 解码路径的日志（tag=MagnetViewer；android.util.Log 在单测里未 mock 也不能崩）。 */
private fun logcat(message: String) {
  try {
    android.util.Log.i("MagnetViewer", message)
  } catch (e: Throwable) {
    // 忽略
  }
}
