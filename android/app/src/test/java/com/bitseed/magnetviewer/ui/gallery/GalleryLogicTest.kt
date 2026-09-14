package com.bitseed.magnetviewer.ui.gallery

import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.engine.ImageDownloadState
import com.bitseed.magnetviewer.engine.deriveImageState
import com.bitseed.magnetviewer.engine.fileDownloadFraction
import com.bitseed.magnetviewer.engine.imageStateText
import com.bitseed.magnetviewer.engine.isFileComplete
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 画廊**纯逻辑**单测（JVM，无需设备/句柄/磁盘）：
 * 索引与翻页边界、缩放夹取、图片扩展名判定、未下载图片的下载状态派生。
 *
 * 这些规则与桌面 `ui/gallery.py` 一一对应：
 * - `_step`（首末张不越界）→ [stepIndex]/[clampIndex]；
 * - Ctrl+滚轮 `max(0.2, min(5.0, scale*factor))`（1.15 / 0.87）→ [applyZoom]/[clampScale]；
 * - `ParseResult.images`（IMAGE_EXTS + 过滤 .pad）→ [galleryEntries]/[isImageFileName]；
 * - `_poll_completed` 的 `progress[f.index] >= f.size` → [deriveImageState]。
 */
class GalleryLogicTest {

  private fun file(index: Int, path: String, size: Long = 4096L) =
    TorrentFile(index = index, path = path, size = size, offset = 0L, startPiece = index, endPiece = index)

  // ------------------------------------------------------------ 索引 / 翻页边界

  @Test
  fun stepIndex_staysInBounds_atBothEnds() {
    assertEquals("首张再往前必须停在原地", 0, stepIndex(0, -1, 3))
    assertEquals("末张再往后必须停在原地", 2, stepIndex(2, 1, 3))
    assertEquals(1, stepIndex(0, 1, 3))
    assertEquals(1, stepIndex(2, -1, 3))
    assertEquals("跨多张也不越界", 0, stepIndex(1, -5, 3))
    assertEquals(2, stepIndex(1, 5, 3))
  }

  @Test
  fun stepIndex_handlesEmptyAndOutOfRangeInput() {
    assertEquals("没有图片时索引恒为 -1", -1, stepIndex(0, 1, 0))
    assertEquals(-1, stepIndex(-1, 1, 0))
    assertEquals("起点越界（大）：先夹回末张，再往后翻仍停在末张", 2, stepIndex(9, 1, 3))
    assertEquals("起点越界（小）：夹回首张后往左翻不越界", 0, stepIndex(-9, -1, 3))
  }

  @Test
  fun clampIndex_clampsIntoRange_andReportsEmptyAsMinusOne() {
    assertEquals(0, clampIndex(0, 3))
    assertEquals(2, clampIndex(2, 3))
    assertEquals(2, clampIndex(99, 3))
    assertEquals(0, clampIndex(-99, 3))
    assertEquals("没有图片 → -1（UI 据此显示「该资源中没有图片文件」）", -1, clampIndex(0, 0))
  }

  @Test
  fun viewerPosition_keepsMinusOneAsGridMode() {
    assertEquals("负数 = 缩略图网格，绝不能被夹成 0（踩过的坑）", -1, viewerPosition(-1, 3))
    assertEquals(-1, viewerPosition(-9, 3))
    assertEquals(0, viewerPosition(0, 3))
    assertEquals(2, viewerPosition(9, 3))
    assertEquals("没有图片时不指向任何下标", -1, viewerPosition(0, 0))
  }

  // ------------------------------------------------------------ 缩放夹取

  @Test
  fun applyZoom_usesDesktopFactors_andClampsToBounds() {
    assertEquals(1.15f, applyZoom(1.0f, zoomIn = true), 0.0001f)
    assertEquals(0.87f, applyZoom(1.0f, zoomIn = false), 0.0001f)
    // 连续放大到上限：0.2~5.0（桌面 max(0.2, min(5.0, …))）
    var s = 1.0f
    repeat(40) { s = applyZoom(s, zoomIn = true) }
    assertEquals("放大到顶必须夹在 5.0", MAX_SCALE, s, 0.0001f)
    repeat(80) { s = applyZoom(s, zoomIn = false) }
    assertEquals("缩小到底必须夹在 0.2", MIN_SCALE, s, 0.0001f)
  }

  @Test
  fun clampScale_rejectsNanAndInfinity_asReset() {
    assertEquals(1.0f, clampScale(Float.NaN), 0.0001f)
    assertEquals(1.0f, clampScale(Float.POSITIVE_INFINITY), 0.0001f)
    assertEquals(1.0f, clampScale(Float.NEGATIVE_INFINITY), 0.0001f)
    assertEquals("小于下限夹到下限", MIN_SCALE, clampScale(0.0001f), 0.0001f)
    assertEquals("大于上限夹到上限", MAX_SCALE, clampScale(99f), 0.0001f)
    assertEquals(1.0f, clampScale(1.0f), 0.0001f)
  }

  @Test
  fun zoomPercent_formatsPercentForUi() {
    assertEquals(100, zoomPercent(1.0f))
    assertEquals(115, zoomPercent(1.15f))
    assertEquals(20, zoomPercent(0.01f))
    assertEquals(500, zoomPercent(9f))
  }

  // ------------------------------------------------------------ 图片扩展名判定

  @Test
  fun isImageFileName_matchesImageExtsCaseInsensitively() {
    for (n in listOf("a.jpg", "a.JPEG", "a.Png", "a.gif", "a.webp", "a.bmp", "a.svg")) {
      assertTrue("$n 应当判为图片", isImageFileName(n))
    }
    for (n in listOf("a.mp4", "a.mkv", "a.txt", "README", "a.", ".png", "a.jpg.bak", "a.png/evil.exe")) {
      assertFalse("$n 不应当判为图片", isImageFileName(n))
    }
    assertFalse("末位点不算扩展名（Python splitext 语义）", isImageFileName("a.jpg."))
  }

  @Test
  fun galleryEntries_keepsImageFilesInOrder_andDropsPadAndOthers() {
    val files =
      listOf(
        file(0, "payload_img/photo_big.png"),
        file(1, "payload_img/photo_mid.jpg"),
        file(2, "payload_img/photo_small.jpg"),
        file(3, "payload_img/clip.mp4"),
        file(4, "payload_img/.pad/3"),
        file(5, "payload_img/notes.txt"),
      )
    val entries = galleryEntries(files)
    assertEquals("只保留图片、按清单顺序", listOf(0, 1, 2), entries.map { it.index })
    assertEquals(listOf("photo_big.png", "photo_mid.jpg", "photo_small.jpg"), entries.map { it.name })
    assertTrue("图片条目必须带种子内相对路径（映射磁盘用）", entries.first().path.startsWith("payload_img/"))
  }

  // ------------------------------------------------------------ 未下载图片的下载状态派生（接缝）

  @Test
  fun deriveImageState_pendingDownloadingReady() {
    val size = 1000L
    assertEquals("读数为 0 → 未开始", ImageDownloadState.PENDING, deriveImageState(0L, size, onDiskComplete = false))
    assertEquals("有读数未达标 → 下载中", ImageDownloadState.DOWNLOADING, deriveImageState(1L, size, false))
    assertEquals(ImageDownloadState.DOWNLOADING, deriveImageState(999L, size, false))
    assertEquals("读数达标 → 已下载", ImageDownloadState.READY, deriveImageState(size, size, false))
    assertEquals("本地文件已完整 → 已下载（不看读数）", ImageDownloadState.READY, deriveImageState(0L, size, true))
  }

  @Test
  fun deriveImageState_treatsUnknownSizeAsNotDownloaded() {
    assertEquals("大小未知不当作已下载", ImageDownloadState.PENDING, deriveImageState(0L, 0L, false))
    assertEquals(ImageDownloadState.DOWNLOADING, deriveImageState(5L, 0L, false))
    assertFalse("size<=0 时 isFileComplete 恒 false", isFileComplete(5L, 0L))
  }

  @Test
  fun deriveImageState_sparseFileLengthIsNotProofOfCompletion() {
    // libtorrent 预分配稀疏文件：长度 = 全长但内容还是 0 → 读数 0 时必须判「未下载」
    // （实测踩过：用长度判，大图 0 字节时被标成「已下载完成」，解码拿到空内容反复失败）
    assertEquals(
      "文件长度不是完成证据（onDiskComplete 必须由「读数达标 + 文件存在」得出）",
      ImageDownloadState.PENDING,
      deriveImageState(0L, 3_965_830L, onDiskComplete = false),
    )
    assertEquals(
      "只下了一部分时是「下载中」而不是「已下载」",
      ImageDownloadState.DOWNLOADING,
      deriveImageState(1_057_827L, 3_965_830L, onDiskComplete = false),
    )
  }

  @Test
  fun fileDownloadFraction_clampsAndHandlesOverreport() {
    assertEquals(0.0, fileDownloadFraction(0L, 100L), 0.0001)
    assertEquals(0.5, fileDownloadFraction(50L, 100L), 0.0001)
    assertEquals("共享分块可能让读数略超文件大小 → 截到 1.0", 1.0, fileDownloadFraction(120L, 100L), 0.0001)
    assertEquals("大小未知 → 0", 0.0, fileDownloadFraction(10L, 0L), 0.0001)
    assertEquals("负数读数按 0", 0.0, fileDownloadFraction(-5L, 100L), 0.0001)
  }

  @Test
  fun imageStateText_isChineseAndNeverShows100BeforeReady() {
    assertEquals("已下载", imageStateText(ImageDownloadState.READY, 1.0))
    assertEquals("未下载 0%", imageStateText(ImageDownloadState.PENDING, 0.0))
    assertEquals("下载中 32%", imageStateText(ImageDownloadState.DOWNLOADING, 0.32))
    assertEquals("未完成时不显示 100%（99% 封顶）", "下载中 99%", imageStateText(ImageDownloadState.DOWNLOADING, 0.999))
  }

  // ------------------------------------------------------------ 大图信息行（桌面对齐）

  @Test
  fun galleryInfoText_matchesDesktopFormat() {
    assertEquals("1 / 3 · photo_big.png（3.8 MB）", galleryInfoText(0, 3, "photo_big.png", "3.8 MB"))
    assertEquals("3 / 3 · c.png（1.0 KB）", galleryInfoText(2, 3, "c.png", "1.0 KB"))
    assertEquals("越界下标也要给出合法的一行", "3 / 3 · c.png（1.0 KB）", galleryInfoText(99, 3, "c.png", "1.0 KB"))
    assertEquals("没有图片时的空态文案", "该资源中没有图片文件", galleryInfoText(0, 0, "", ""))
  }
}
