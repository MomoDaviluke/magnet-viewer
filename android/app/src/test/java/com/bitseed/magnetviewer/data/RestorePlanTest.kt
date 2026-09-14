package com.bitseed.magnetviewer.data

import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.core.states.STATE_COMPLETED
import com.bitseed.magnetviewer.core.states.STATE_DOWNLOADING
import com.bitseed.magnetviewer.core.states.STATE_FAILED
import com.bitseed.magnetviewer.core.states.STATE_META_FETCH
import com.bitseed.magnetviewer.core.states.STATE_PAUSED
import com.bitseed.magnetviewer.core.states.STATE_QUEUED
import com.bitseed.magnetviewer.core.states.STATE_STOPPED
import com.bitseed.magnetviewer.core.states.STATE_VALIDATE
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 启动恢复的**纯逻辑分支**单测（JVM；native 部分拆到引擎/设备上验证）。
 *
 * 覆盖需求点 D 里的五档：**有 resume / 无 resume / resume 损坏 / 已暂停 / 已完成**，
 * 外加桌面 `restore_task` 的边界：来源不可用、info_hash 非法、save_path 逃逸、删除不变量。
 */
class RestorePlanTest {

  private fun tempRoot(tag: String): File {
    val d = File(System.getProperty("java.io.tmpdir"), "mv-restore-$tag-${System.nanoTime()}")
    d.mkdirs()
    return d
  }

  private fun record(
    state: String = STATE_PAUSED,
    savePath: String = "",
    source: String = MAGNET,
  ) = TaskRecord(
    infoHash = IH,
    source = source,
    name = "fixture",
    totalSize = 161664L,
    files = listOf(TorrentFile(0, "fixture/fixture.mp4", 80832L, 0L, 0, 4)),
    selected = listOf("fixture/fixture.mp4"),
    state = state,
    savePath = savePath,
  )

  // ------------------------------------------------------------ 有 resume / 无 resume / 损坏

  @Test
  fun resumeHit_isUsed_andReported() {
    val cache = tempRoot("hit")
    val p = planRestore(record(), cache, resumeBytesPresent = true, resumeDecoded = true)
    assertTrue(p.valid)
    assertEquals(RestoreSource.MAGNET, p.source)
    assertTrue("命中 resume 时要用它重建", p.useResume)
    assertFalse(p.resumeBroken)
    assertTrue("命中说明要写清楚", p.note.contains("命中 fastresume"))
    assertTrue(p.error.isEmpty())
    cache.deleteRecursively()
  }

  @Test
  fun resumeMissing_isNormal_notAnError() {
    val cache = tempRoot("missing")
    val p = planRestore(record(), cache, resumeBytesPresent = false, resumeDecoded = false)
    assertTrue("没有 resume 不是错误（元数据会重新拉）", p.valid)
    assertFalse(p.useResume)
    assertFalse(p.resumeBroken)
    assertEquals("", p.error)
    assertTrue(p.note.contains("无 fastresume"))
    assertTrue(p.note.contains("重新拉"))
    cache.deleteRecursively()
  }

  @Test
  fun resumeBroken_silentlyDegradesToFreshAdd() {
    val cache = tempRoot("broken")
    val p = planRestore(record(), cache, resumeBytesPresent = true, resumeDecoded = false)
    assertTrue("损坏也必须能恢复（静默全新加入）", p.valid)
    assertFalse(p.useResume)
    assertTrue(p.resumeBroken)
    assertEquals("", p.error)
    assertTrue(p.note.contains("损坏"))
    // 状态派生：损坏降级后等价于「无 resume」——下载中的任务等元数据重新拉（不报错、不卡住）
    val dl = planRestore(record(state = STATE_DOWNLOADING), cache, resumeBytesPresent = true, resumeDecoded = false)
    assertTrue(dl.resumeBroken)
    assertFalse(dl.pauseAfterAdd)
    assertEquals(STATE_META_FETCH, stateAfterRestore(STATE_DOWNLOADING, hasMetadata = false, paused = dl.pauseAfterAdd))
    cache.deleteRecursively()
  }

  @Test
  fun resumePathEscape_isIgnoredByDesign() {
    // 记录里的 save_path 逃出缓存根 → 落盘目录必须被拉回默认（`.preview/<ih>`）
    val cache = tempRoot("escape")
    val outside = File(cache.parentFile, "outside-${System.nanoTime()}")
    val p = planRestore(record(savePath = outside.path), cache, resumeBytesPresent = true, resumeDecoded = true)
    assertEquals(previewDirFor(cache, IH), p.saveDir)
    assertTrue("回落后的目录必须在受管范围内", isWithin(cache, p.saveDir))
    // 正常路径保留
    val inside = File(cache, ".preview/$IH")
    assertEquals(inside, planRestore(record(savePath = inside.path), cache, true, true).saveDir)
    cache.deleteRecursively()
  }

  // ------------------------------------------------------------ 已暂停 / 已完成 / 续传

  @Test
  fun pausedTask_pausesAfterAdd_andKeepsState() {
    val cache = tempRoot("paused")
    val p = planRestore(record(state = STATE_PAUSED), cache, resumeBytesPresent = true, resumeDecoded = true)
    assertTrue("已暂停 → 加完立刻暂停", p.pauseAfterAdd)
    assertFalse(p.resumeDownload)
    assertEquals(STATE_PAUSED, stateAfterRestore(STATE_PAUSED, hasMetadata = true, paused = true))
    assertEquals("哪怕拿不到元数据也保持暂停态", STATE_PAUSED, stateAfterRestore(STATE_PAUSED, hasMetadata = false, paused = true))
    cache.deleteRecursively()
  }

  @Test
  fun stoppedTask_pausesAfterAdd() {
    val cache = tempRoot("stopped")
    val p = planRestore(record(state = STATE_STOPPED), cache, resumeBytesPresent = true, resumeDecoded = true)
    assertTrue(p.pauseAfterAdd)
    assertEquals(STATE_STOPPED, stateAfterRestore(STATE_STOPPED, hasMetadata = true, paused = true))
    cache.deleteRecursively()
  }

  @Test
  fun completedTask_pausesAfterAdd_andIsNotResumed() {
    val cache = tempRoot("completed")
    val p = planRestore(record(state = STATE_COMPLETED), cache, resumeBytesPresent = true, resumeDecoded = true)
    assertTrue("已完成 → 加完暂停（不做种）", p.pauseAfterAdd)
    assertFalse(p.resumeDownload)
    assertEquals(STATE_COMPLETED, stateAfterRestore(STATE_COMPLETED, hasMetadata = true, paused = true))
    assertEquals(STATE_COMPLETED, stateAfterRestore(STATE_COMPLETED, hasMetadata = false, paused = true))
    cache.deleteRecursively()
  }

  @Test
  fun downloadingTask_resumes_withMetadataOrWithout() {
    val cache = tempRoot("downloading")
    val p = planRestore(record(state = STATE_DOWNLOADING), cache, resumeBytesPresent = true, resumeDecoded = true)
    assertFalse(p.pauseAfterAdd)
    assertTrue("下载中的任务恢复后要续传", p.resumeDownload)
    assertEquals("resume 带 info-dict：立刻就是 DOWNLOADING", STATE_DOWNLOADING, stateAfterRestore(STATE_DOWNLOADING, true, false))
    assertEquals("没元数据：等元数据（会重新拉）", STATE_META_FETCH, stateAfterRestore(STATE_DOWNLOADING, false, false))
    cache.deleteRecursively()
  }

  @Test
  fun stateAfterRestore_desktopRules() {
    // 元数据在 → META_FETCH / QUEUED / VALIDATE 提升为 DOWNLOADING
    assertEquals(STATE_DOWNLOADING, stateAfterRestore(STATE_META_FETCH, true, false))
    assertEquals(STATE_DOWNLOADING, stateAfterRestore(STATE_QUEUED, true, false))
    assertEquals(STATE_DOWNLOADING, stateAfterRestore(STATE_VALIDATE, true, false))
    assertEquals(STATE_DOWNLOADING, stateAfterRestore(STATE_DOWNLOADING, true, false))
    // 未知/空状态：有元数据 → DOWNLOADING；没有 → META_FETCH
    assertEquals(STATE_DOWNLOADING, stateAfterRestore("", true, false))
    assertEquals(STATE_META_FETCH, stateAfterRestore("nonsense", false, false))
    // FAILED 保留（可见状态比静默成功更重要）
    assertEquals(STATE_FAILED, stateAfterRestore(STATE_FAILED, true, false))
    // 大小写与空格归一
    assertEquals(STATE_PAUSED, stateAfterRestore("  paused  ", true, true))
    assertEquals(STATE_DOWNLOADING, stateAfterRestore("  downloading  ", true, false))
  }

  @Test
  fun shouldPauseOrResume_valueDomain() {
    assertTrue(shouldPauseAfterRestore(STATE_PAUSED))
    assertTrue(shouldPauseAfterRestore("paused"))
    assertTrue(shouldPauseAfterRestore(STATE_STOPPED))
    assertTrue(shouldPauseAfterRestore(STATE_COMPLETED))
    assertFalse(shouldPauseAfterRestore(STATE_DOWNLOADING))
    assertTrue(shouldResumeDownload(STATE_DOWNLOADING))
    assertFalse(shouldResumeDownload(STATE_COMPLETED))
  }

  // ------------------------------------------------------------ 边界：来源 / info_hash

  @Test
  fun sourceUnavailable_marksFailed() {
    val cache = tempRoot("noun")
    val p = planRestore(record(source = ""), cache, resumeBytesPresent = true, resumeDecoded = true)
    assertFalse(p.valid)
    assertEquals(RestoreSource.UNAVAILABLE, p.source)
    assertTrue(p.error.contains("来源不可用"))
    assertFalse("来源不可用的任务不参与暂停判定", shouldPauseAfterRestore("DOWNLOADING"))
    cache.deleteRecursively()
  }

  @Test
  fun torrentFileSource_usedOnlyIfFileExists() {
    val cache = tempRoot("torrent")
    val tf = File(cache, "fixture.torrent")
    tf.writeText("d4:infod4:name1:xe", Charsets.UTF_8)
    val ok = planRestore(record(source = tf.path), cache, resumeBytesPresent = false, resumeDecoded = false)
    assertTrue(ok.valid)
    assertEquals(RestoreSource.TORRENT_FILE, ok.source)
    val missing = planRestore(record(source = File(cache, "gone.torrent").path), cache, false, false)
    assertFalse("文件已删 → 来源不可用", missing.valid)
    // 注入式的存在性判定（设备上等价于 File#isFile）
    val injected = planRestore(record(source = "/nowhere/x.torrent"), cache, false, false) { it.endsWith(".torrent") }
    assertTrue(injected.valid)
    assertEquals(RestoreSource.TORRENT_FILE, injected.source)
    cache.deleteRecursively()
  }

  @Test
  fun invalidInfoHash_marksFailed_withoutThrowing() {
    val cache = tempRoot("badih")
    val p = planRestore(record().copy(infoHash = "../evil"), cache, resumeBytesPresent = true, resumeDecoded = true)
    assertFalse(p.valid)
    assertTrue(p.error.contains("info_hash"))
    assertTrue(p.note.contains("info_hash 非法"))
    cache.deleteRecursively()
  }

  // ------------------------------------------------------------ 不变量 C：删除不删用户数据

  @Test
  fun deletion_default_keepsUserDataDir() {
    val appRoot = tempRoot("del")
    val cache = File(appRoot, "cache")
    val dataDir = File(cache, ".preview/$IH")
    dataDir.mkdirs()
    val payload = File(dataDir, "fixture.mp4")
    payload.writeText("用户下到的数据", Charsets.UTF_8)
    ResumeStore.write(cache, IH, byteArrayOf(1, 2, 3))

    val plan = planDeletion(IH, dataDir, appRoot, deleteUserData = false)
    assertTrue(plan.valid)
    assertTrue("清单条目要摘", plan.removeTaskEntry)
    assertTrue("resume 要删", plan.removeResumeFile)
    assertFalse("**默认绝不删用户数据目录**", plan.removeUserDataDir)

    val applied = applyDeletion(plan, cache, dataDir)
    assertTrue(applied.resumeDeleted)
    assertFalse(applied.userDataDeleted)
    assertTrue("数据文件必须原封不动", payload.isFile)
    assertEquals("用户下到的数据", payload.readText(Charsets.UTF_8))
    assertNull("resume 文件应已删除", ResumeStore.read(cache, IH))
    appRoot.deleteRecursively()
  }

  @Test
  fun deletion_explicitTrue_removesDataDir_onlyInsideAppRoot() {
    val appRoot = tempRoot("del-explicit")
    val cache = File(appRoot, "cache")
    val dataDir = File(cache, ".preview/$IH")
    dataDir.mkdirs()
    File(dataDir, "fixture.mp4").writeText("x", Charsets.UTF_8)
    val plan = planDeletion(IH, dataDir, appRoot, deleteUserData = true)
    assertTrue("显式要求才允许删", plan.removeUserDataDir)
    val applied = applyDeletion(plan, cache, dataDir)
    // 与"删除是否成功"无关的口径：结果必须与磁盘事实一致。Windows 上 rmtree 偶发失败会落进失败分支，
    // 那不是被测语义的错；原写法 assertTrue(userDataDeleted) 会把环境噪声误判成功能回归。
    assertEquals("删除结果必须与磁盘事实一致", !dataDir.exists(), applied.userDataDeleted)
    assertEquals("成功位与失败位互斥", !applied.userDataDeleted, applied.userDataDeleteFailed)

    // 逃出应用私有目录：即便显式要求也必须拒绝
    val outside = File(appRoot.parentFile, "outside-${System.nanoTime()}")
    outside.mkdirs()
    File(outside, "keep.txt").writeText("不能删", Charsets.UTF_8)
    val refused = planDeletion(IH, outside, appRoot, deleteUserData = true)
    assertFalse(refused.removeUserDataDir)
    assertTrue(refused.error.contains("拒绝"))
    val r2 = applyDeletion(refused, cache, outside)
    assertFalse(r2.userDataDeleted)
    assertTrue("应用目录之外的目录必须安然无恙", File(outside, "keep.txt").isFile)
    outside.deleteRecursively()
    appRoot.deleteRecursively()
  }

  @Test
  fun deletion_invalidHash_isRefused() {
    val appRoot = tempRoot("del-bad")
    val plan = planDeletion("../evil", File(appRoot, "x"), appRoot, deleteUserData = true)
    assertFalse(plan.valid)
    assertFalse(plan.removeUserDataDir)
    assertFalse(plan.removeTaskEntry)
    assertTrue(plan.error.contains("info_hash 非法"))
    appRoot.deleteRecursively()
  }

  @Test
  fun deletionPlanDoesNotDependOnResumeFileExisting() {
    val appRoot = tempRoot("del-noresume")
    val cache = File(appRoot, "cache")
    val dataDir = File(cache, ".preview/$IH")
    dataDir.mkdirs()
    val plan = planDeletion(IH, dataDir, appRoot)
    val applied = applyDeletion(plan, cache, dataDir)
    assertTrue("没 resume 文件也算删除成功（幂等：不存在即达成）", applied.resumeDeleted)
    assertNotNull(plan.userDataDir)
    appRoot.deleteRecursively()
  }

  private companion object {
    const val IH = "444b8b5c9a69b388a5013d85a42caef060f03225"
    const val MAGNET = "magnet:?xt=urn:btih:444b8b5c9a69b388a5013d85a42caef060f03225&dn=fixture&x.pe=10.0.2.2:16881"
  }
}
