package com.bitseed.magnetviewer.data

import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 任务操作单测（**JVM**；纯逻辑 + 临时目录，绝不实例化 libtorrent4j 原生对象）。
 *
 * 覆盖（逐条对应任务书 D 的后两项与 B 的语义）：
 * - 优先级：值域（0..3）与桌面 `lt_priority` 映射表、队列排序（高优先在前、同优先保序）；
 * - **转正路径计算**：`.preview/<ih>` → `downloads/<ih>`（含幂等与非法 hash）；
 * - **移除的路径守卫**：受管根包含性 + 目录名 == info_hash，越界一律拒绝；
 * - 删除执行的 IO 语义：默认保留数据、显式删才删、守卫拒绝绝不删。
 */
class TaskOpsTest {

  private val IH = "444b8b5c9a69b388a5013d85a42caef060f03225"
  private val IH2 = "d14e4e7f2e1979052b966008ea2900a9f60eda06"

  private fun tempRoot(tag: String): File =
    File(System.getProperty("java.io.tmpdir") ?: ".", "mv-taskops-$tag-${System.nanoTime()}").also { it.mkdirs() }

  // ------------------------------------------------------------ 优先级

  @Test
  fun priorityValueRange_matchesDesktop() {
    assertTrue(isValidTaskPriority(0))
    assertTrue(isValidTaskPriority(3))
    assertFalse("4 不是合法任务优先级（那是「不截断」的边界）", isValidTaskPriority(4))
    assertFalse(isValidTaskPriority(-1))
    // 桌面 {0:1, 1:50, 2:150, 3:255}；未知值回落 1
    assertEquals(1, ltPriority(0))
    assertEquals(50, ltPriority(1))
    assertEquals(150, ltPriority(2))
    assertEquals(255, ltPriority(3))
    assertEquals(1, ltPriority(9))
    assertEquals(1, ltPriority(-3))
  }

  @Test
  fun priorityQueueOrder_highFirst_tiesKeepInsertionOrder() {
    val order =
      priorityQueueOrder(
        listOf(
          "a" to 0,
          "b" to 3,
          "c" to 0,
          "d" to 2,
          "e" to 3,
        )
      )
    // 3 档在前（b 先于 e：同档按原序），再 2 档，再 0 档（a 先于 c）
    assertEquals(listOf("b", "e", "d", "a", "c"), order)
  }

  // ------------------------------------------------------------ 转正路径

  @Test
  fun planPromote_movesPreviewDirIntoDownloads() {
    val root = tempRoot("promote")
    val cache = File(root, "cache").also { it.mkdirs() }
    val preview = File(cache, ".preview/$IH").also { it.mkdirs() }
    File(preview, "fixture.mp4").writeBytes(ByteArray(1024))

    val plan = planPromote(IH, cache, preview.path)

    assertTrue(plan.valid)
    assertEquals(preview.path, plan.fromDir?.path)
    assertEquals(File(cache, "downloads/$IH").path, plan.toDir?.path)
    assertFalse("第一次转正不是幂等命中", plan.alreadyPromoted)
    assertTrue(plan.note.contains("继续下载"))
    root.deleteRecursively()
  }

  @Test
  fun planPromote_isIdempotentWhenAlreadyInDownloads() {
    val root = tempRoot("promote2")
    val cache = File(root, "cache").also { it.mkdirs() }
    val downloads = File(cache, "downloads/$IH").also { it.mkdirs() }

    val plan = planPromote(IH, cache, downloads.path)

    assertTrue(plan.valid)
    assertTrue("已在 downloads/ 就是幂等命中（不重复搬运）", plan.alreadyPromoted)
    assertEquals(downloads.path, plan.toDir?.path)
    root.deleteRecursively()
  }

  @Test
  fun planPromote_sanitizesBadHash() {
    val root = tempRoot("promote3")
    val cache = File(root, "cache").also { it.mkdirs() }

    val plan = planPromote("../../etc", cache, "")

    assertFalse(plan.valid)
    assertNull(plan.toDir)
    assertTrue(plan.error.contains("info_hash"))
    assertNull("非法 key 不得拼出路径", promoteDirFor(cache, "../evil"))
    root.deleteRecursively()
  }

  // ------------------------------------------------------------ 移除守卫

  @Test
  fun planRemoveTask_defaultKeepsData() {
    val root = tempRoot("remove-default")
    val cache = File(root, "cache").also { it.mkdirs() }
    val dir = File(cache, ".preview/$IH").also { it.mkdirs() }

    val plan = planRemoveTask(IH, dir.path, cache)

    assertTrue(plan.valid)
    assertTrue("记录要摘", plan.removeEntry)
    assertTrue("resume 要删", plan.removeResume)
    assertFalse("**默认绝不删数据**", plan.deleteData)
    assertFalse(plan.rejected)
    root.deleteRecursively()
  }

  @Test
  fun planRemoveTask_explicitDelete_insideManagedRootWithMatchingName() {
    val root = tempRoot("remove-ok")
    val cache = File(root, "cache").also { it.mkdirs() }
    for (dir in listOf(File(cache, ".preview/$IH"), File(cache, "downloads/$IH"))) {
      dir.mkdirs()
      val plan = planRemoveTask(IH, dir.path, cache, deleteData = true)
      assertTrue("受管根内 + 目录名 == info_hash → 允许删", plan.deleteData)
      assertFalse(plan.rejected)
    }
    root.deleteRecursively()
  }

  @Test
  fun planRemoveTask_rejectsOutsideManagedRoots() {
    val root = tempRoot("remove-outside")
    val cache = File(root, "cache").also { it.mkdirs() }
    val outside = File(root, "user-data/$IH").also { it.mkdirs() }
    val userFile = File(outside, "keep.txt").also { it.writeText("不能删", Charsets.UTF_8) }

    val plan = planRemoveTask(IH, outside.path, cache, deleteData = true)

    assertTrue(plan.rejected)
    assertFalse(plan.deleteData)
    assertTrue(plan.error.contains("拒绝"))
    val outcome = applyRemoveTask(plan, cache)
    assertFalse(outcome.dataDeleted)
    assertTrue("用户其它数据必须安然无恙", userFile.isFile)
    root.deleteRecursively()
  }

  @Test
  fun planRemoveTask_rejectsSiblingDirsWithDifferentName() {
    val root = tempRoot("remove-sibling")
    val cache = File(root, "cache").also { it.mkdirs() }
    // 受管根内但目录名不是 info_hash（共享目录里的非本任务数据）
    val shared = File(cache, "downloads/other-task").also { it.mkdirs() }
    val f = File(shared, "x.bin").also { it.writeBytes(ByteArray(8)) }

    val plan = planRemoveTask(IH, shared.path, cache, deleteData = true)

    assertTrue(plan.rejected)
    assertFalse(plan.deleteData)
    assertTrue(plan.error.contains("目录名与 info_hash 不符"))
    applyRemoveTask(plan, cache)
    assertTrue(f.isFile)
    root.deleteRecursively()
  }

  @Test
  fun planRemoveTask_rejectsTraversalPaths() {
    val root = tempRoot("remove-traversal")
    val cache = File(root, "cache").also { it.mkdirs() }
    val evil = File(root, "evil/$IH").also { it.mkdirs() }
    File(evil, "keep.txt").writeText("x", Charsets.UTF_8)

    // cache/../evil/<ih> 归一后落在受管根之外 → 拒绝（basename 仍等于 ih，靠包含性判定拦下）
    val plan = planRemoveTask(IH, File(cache, "../evil/$IH").path, cache, deleteData = true)

    assertTrue(plan.rejected)
    assertTrue(File(evil, "keep.txt").isFile)
    root.deleteRecursively()
  }

  @Test
  fun planRemoveTask_invalidHashOrMissingPath() {
    val root = tempRoot("remove-bad")
    val cache = File(root, "cache").also { it.mkdirs() }

    val bad = planRemoveTask("../evil", File(cache, "x").path, cache, deleteData = true)
    assertFalse(bad.valid)
    assertFalse(bad.removeEntry)
    assertTrue(bad.error.contains("info_hash 非法"))

    val noPath = planRemoveTask(IH, "", cache, deleteData = true)
    assertTrue(noPath.valid)
    assertFalse("没有可核验的目录：拒绝删除但不拒绝摘记录", noPath.deleteData)
    assertTrue(noPath.rejected)
    assertTrue(noPath.removeEntry)
    root.deleteRecursively()
  }

  @Test
  fun applyRemoveTask_deletesOnlyWhatThePlanAllows() {
    val root = tempRoot("remove-apply")
    val cache = File(root, "cache").also { it.mkdirs() }
    val dir = File(cache, "downloads/$IH").also { it.mkdirs() }
    File(dir, "payload.mp4").writeBytes(ByteArray(16))
    ResumeStore.write(cache, IH, byteArrayOf(1, 2, 3))

    // 保留数据：目录与内容原样，resume 仍删（清单没了，续传数据没意义）
    val keep = applyRemoveTask(planRemoveTask(IH, dir.path, cache, deleteData = false), cache)
    assertTrue(keep.resumeDeleted)
    assertFalse(keep.dataDeleted)
    assertTrue("保留数据档：目录必须还在", dir.isDirectory)

    // 删数据：目录连同内容一起没
    ResumeStore.write(cache, IH, byteArrayOf(1, 2, 3))
    val del = applyRemoveTask(planRemoveTask(IH, dir.path, cache, deleteData = true), cache)
    assertTrue(del.resumeDeleted)
    assertTrue(del.dataDeleted)
    assertFalse(dir.exists())
    assertNull(ResumeStore.read(cache, IH))
    root.deleteRecursively()
  }

  @Test
  fun removeDirForOtherTaskDoesNotTouchNeighbour() {
    val root = tempRoot("remove-neighbour")
    val cache = File(root, "cache").also { it.mkdirs() }
    val mine = File(cache, "downloads/$IH").also { it.mkdirs() }
    val theirs = File(cache, "downloads/$IH2").also { it.mkdirs() }
    File(theirs, "theirs.bin").writeBytes(ByteArray(32))

    applyRemoveTask(planRemoveTask(IH, mine.path, cache, deleteData = true), cache)

    assertFalse(mine.exists())
    assertNotNull(theirs.listFiles())
    assertTrue("别人的任务目录不受影响", File(theirs, "theirs.bin").isFile)
    root.deleteRecursively()
  }
}
