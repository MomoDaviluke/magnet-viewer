package com.bitseed.magnetviewer.data

import java.io.File
import java.io.IOException
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 任务操作单测（**JVM**；纯逻辑 + 虚拟树/临时目录，绝不实例化 libtorrent4j 原生对象）。
 *
 * 覆盖（逐条对应任务书 D 的后两项与 B 的语义）：
 * - 优先级：值域（0..3）与桌面 `lt_priority` 映射表、队列排序（高优先在前、同优先保序）；
 * - **转正路径计算**：`.preview/<ih>` → `downloads/<ih>`（含幂等与非法 hash）；
 * - **移除的路径守卫**：受管根包含性 + 目录名 == info_hash，越界一律拒绝（且**不调删除**）；
 * - 删除执行的 IO 语义：默认保留数据、显式删才删、守卫拒绝绝不删、失败（拒删/异常/假成功）
 *   如实报告且不计为已删。
 *
 * **为什么这些断言是确定性的**（旧写法在此处偶发翻红）：
 * `TaskOps.applyRemoveTask` 与文件系统的接触面（`deleteDir` / `dirExists`）全部可注入，
 * 默认即真实实现，生产调用点一个参数都不用传。多数用例走 [VirtualTaskTree]——**内存虚拟
 * 目录树**：目录内容、删除结局（真删 / 拒删 / 抛异常 / 假成功）、存在性全由替身回答，
 * 一个真实目录都不删。断言随之只落在移除的**语义**上（双守卫、保留/删除两条路、失败不入账），
 * 不再被「Windows 上文件被占用或防病毒让 `deleteRecursively()` 偶发失败」这种环境噪声
 * 左右——旧写法把「尝试删」当「删成功」（`assertTrue(del.dataDeleted)`），一次 rmtree
 * 失败就随机翻红（同型：`removeDirForOtherTaskDoesNotTouchNeighbour` 里的
 * `assertFalse(mine.exists())`，它的真命题是「别人的目录不受影响」）。
 *
 * 真实临时目录只用于两类用例：
 * - 守卫/计划类（[planRemoveTask_rejectsOutsideManagedRoots] 等）：本来就不发生删除，
 *   真实文件是「用户数据安然无恙」的硬证据；
 * - 端到端 1 个（[realTempDir_deleteDataPath_dataDeletedMatchesReality]）：断言口径刻意
 *   **与「删除是否成功」无关**——只断言「布尔与目录真实状态一致」（如实报告）+「目标目录
 *   确实被尝试过删除」。
 */
class TaskOpsTest {

  private val IH = "444b8b5c9a69b388a5013d85a42caef060f03225"
  private val IH2 = "d14e4e7f2e1979052b966008ea2900a9f60eda06"

  private fun tempRoot(tag: String): File =
    File(System.getProperty("java.io.tmpdir") ?: ".", "mv-taskops-$tag-${System.nanoTime()}").also { it.mkdirs() }

  // ------------------------------------------------------------ 接缝替身

  /**
   * **记录器**：删除仍走真实实现（万一守卫失守，真实文件会被真删——用例里「用户数据还在」
   * 的断言因此依然有效），但每次调用都被记下。用于「一次删除调用都不能发生」这类断言：
   * 记录与否**与删除是否成功无关**，所以完全确定。
   */
  private class RecordingFs {
    val attempts = ArrayList<String>()
    val deleteDir: (File) -> Boolean = { f ->
      attempts.add(f.path)
      f.deleteRecursively()
    }
    val dirExists: (File) -> Boolean = { it.exists() }
  }

  /**
   * 虚拟删除的可编程结局（与 `CacheQuotaTest.DeleteOutcome` 同款四种真实世界结局）。
   *
   * 刻意包含「返回 true 但目录还在」的**假成功**——`deleteRecursively()` 会吞掉删除失败
   * （文件被占用/防病毒），所以「已删」只能以「目录是否还在」为准，不能看实现返回值。
   */
  private enum class DeleteOutcome {
    /** 真删：目录从虚拟树移除，删除实现返回 true。 */
    DELETED,

    /** 拒删：目录留在虚拟树，删除实现返回 false（等价于 rmtree 报失败）。 */
    REFUSED,

    /** 抛异常：目录留在虚拟树，删除实现抛 [IOException]（等价于文件被占用）。 */
    THREW,

    /** 假成功：删除实现返回 true，但目录仍在虚拟树里。 */
    LIED,
  }

  /**
   * 内存虚拟任务目录树：`applyRemoveTask` 的两个文件系统接缝（`deleteDir` / `dirExists`）
   * 全部由它回答，删除结局由 [onDelete] 编程——**不做任何真实删除**。
   *
   * [labelRoot] 只作路径标签（`isWithin` 走 canonical 判定，需要父路径真实存在，故只
   * `mkdirs` 这一个空目录），树里的任务目录/文件刻意**不**落盘。
   */
  private class VirtualTaskTree(tag: String) {
    val labelRoot: File =
      File(System.getProperty("java.io.tmpdir") ?: ".", "mv-taskops-virtual-$tag-${System.nanoTime()}")
        .also { it.mkdirs() }

    /** 受管缓存根（空目录）：`planRemoveTask` 的包含性判定用它。 */
    val cache: File = File(labelRoot, "cache").also { it.mkdirs() }

    private class Dir(val path: String, val files: LinkedHashMap<String, Int>)

    private val live = LinkedHashMap<String, Dir>() // 路径 -> 仍在的目录（含文件模型）

    /** 删除结局（默认总是真删）；入参是目标路径，便于「只让某个目录删不掉」。 */
    var onDelete: (String) -> DeleteOutcome = { DeleteOutcome.DELETED }

    /** 收到删除调用的路径（按调用顺序）——用来断言「动没动手、对谁动手」。 */
    val attempts = ArrayList<String>()

    /** 虚拟树里真正被移走的路径。 */
    val removed = ArrayList<String>()

    /** 加一个虚拟任务目录（`rel` 是相对 cache 根的路径，例如 `downloads/<ih>`）。 */
    fun dir(rel: String, vararg files: Pair<String, Int>): File {
      val f = File(cache, rel)
      val m = LinkedHashMap<String, Int>()
      for ((name, bytes) in files) m[name] = bytes
      live[f.path] = Dir(f.path, m)
      return f
    }

    /** 删除替身：记录调用，再按 [onDelete] 落定虚拟树状态并返回实现返回值。 */
    val deleteDir: (File) -> Boolean = { f ->
      attempts.add(f.path)
      when (onDelete(f.path)) {
        DeleteOutcome.DELETED -> {
          if (live.remove(f.path) != null) removed.add(f.path)
          true
        }
        DeleteOutcome.REFUSED -> false
        DeleteOutcome.THREW -> throw IOException("被占用（虚拟）：${f.path}")
        DeleteOutcome.LIED -> true
      }
    }

    /** 存在性替身：目录是否仍在虚拟树里（[DeleteOutcome.LIED] 时仍在）。 */
    val dirExists: (File) -> Boolean = { f -> live.values.any { it.path == f.path } }

    fun stillThere(path: String): Boolean = live.values.any { it.path == path }

    /** 目录内的文件名清单（目录不在树里返回空）——「文件仍在」的断言口径。 */
    fun filesOf(path: String): List<String> =
      live.values.firstOrNull { it.path == path }?.files?.keys?.toList() ?: emptyList()

    fun cleanup() {
      labelRoot.deleteRecursively()
    }
  }

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
    val payload = File(dir, "payload.mp4").also { it.writeBytes(ByteArray(16)) }
    val fs = RecordingFs()

    val plan = planRemoveTask(IH, dir.path, cache)
    val out = applyRemoveTask(plan, cache, fs.deleteDir, fs.dirExists)

    assertTrue(plan.valid)
    assertTrue("记录要摘", plan.removeEntry)
    assertTrue("resume 要删", plan.removeResume)
    assertFalse("**默认绝不删数据**", plan.deleteData)
    assertFalse(plan.rejected)
    assertFalse("保留数据档：结果不得说删了", out.dataDeleted)
    assertFalse("保留数据档：也不是「删失败」（压根没动手）", out.dataDeleteFailed)
    assertTrue("保留数据档：一次删除调用都不能发生", fs.attempts.isEmpty())
    assertTrue("保留数据档：数据目录必须还在", dir.isDirectory)
    assertTrue("保留数据档：目录内文件必须还在", payload.isFile)
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
    val fs = RecordingFs()

    val plan = planRemoveTask(IH, outside.path, cache, deleteData = true)
    val outcome = applyRemoveTask(plan, cache, fs.deleteDir, fs.dirExists)

    assertTrue("守卫一：目录名虽 == info_hash，但不在受管根内 → 拒绝", plan.rejected)
    assertFalse(plan.deleteData)
    assertTrue(plan.error.contains("拒绝"))
    assertFalse(outcome.dataDeleted)
    assertFalse("守卫拒绝不是「删失败」：根本没动手", outcome.dataDeleteFailed)
    assertTrue("守卫拒绝后一次删除调用都不能发生", fs.attempts.isEmpty())
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
    val fs = RecordingFs()

    val plan = planRemoveTask(IH, shared.path, cache, deleteData = true)
    val outcome = applyRemoveTask(plan, cache, fs.deleteDir, fs.dirExists)

    assertTrue("守卫二：受管根内但目录名 != info_hash → 拒绝", plan.rejected)
    assertFalse(plan.deleteData)
    assertTrue(plan.error.contains("目录名与 info_hash 不符"))
    assertFalse(outcome.dataDeleted)
    assertFalse("守卫拒绝不是「删失败」：根本没动手", outcome.dataDeleteFailed)
    assertTrue("守卫拒绝后一次删除调用都不能发生", fs.attempts.isEmpty())
    assertTrue("共享目录里的数据必须原样", f.isFile)
    root.deleteRecursively()
  }

  @Test
  fun planRemoveTask_rejectsTraversalPaths() {
    val root = tempRoot("remove-traversal")
    val cache = File(root, "cache").also { it.mkdirs() }
    val evil = File(root, "evil/$IH").also { it.mkdirs() }
    File(evil, "keep.txt").writeText("x", Charsets.UTF_8)
    val fs = RecordingFs()

    // cache/../evil/<ih> 归一后落在受管根之外 → 拒绝（basename 仍等于 ih，靠包含性判定拦下）
    val plan = planRemoveTask(IH, File(cache, "../evil/$IH").path, cache, deleteData = true)
    val outcome = applyRemoveTask(plan, cache, fs.deleteDir, fs.dirExists)

    assertTrue(plan.rejected)
    assertFalse(outcome.dataDeleted)
    assertTrue("越界路径：一次删除调用都不能发生", fs.attempts.isEmpty())
    assertTrue(File(evil, "keep.txt").isFile)
    root.deleteRecursively()
  }

  @Test
  fun planRemoveTask_invalidHashOrMissingPath() {
    val root = tempRoot("remove-bad")
    val cache = File(root, "cache").also { it.mkdirs() }
    val fs = RecordingFs()

    val bad = planRemoveTask("../evil", File(cache, "x").path, cache, deleteData = true)
    assertFalse(bad.valid)
    assertFalse(bad.removeEntry)
    assertTrue(bad.error.contains("info_hash 非法"))
    val badOut = applyRemoveTask(bad, cache, fs.deleteDir, fs.dirExists)
    assertFalse(badOut.dataDeleted)
    assertFalse(badOut.dataDeleteFailed)
    assertTrue("非法计划：一次删除调用都不能发生", fs.attempts.isEmpty())

    val noPath = planRemoveTask(IH, "", cache, deleteData = true)
    assertTrue(noPath.valid)
    assertFalse("没有可核验的目录：拒绝删除但不拒绝摘记录", noPath.deleteData)
    assertTrue(noPath.rejected)
    assertTrue(noPath.removeEntry)
    val noPathOut = applyRemoveTask(noPath, cache, fs.deleteDir, fs.dirExists)
    assertFalse(noPathOut.dataDeleted)
    assertFalse("没目录可删 ≠ 删失败", noPathOut.dataDeleteFailed)
    assertTrue("没有可核验目录：一次删除调用都不能发生", fs.attempts.isEmpty())
    root.deleteRecursively()
  }

  // ------------------------------------------------------------ 删除执行（虚拟树：确定性）

  @Test
  fun applyRemoveTask_deletesOnlyWhatThePlanAllows() {
    val v = VirtualTaskTree("apply")
    val dir = v.dir("downloads/$IH", "payload.mp4" to 16)

    // 保留数据：目录与内容原样，且**一次删除调用都不能发生**
    val keep = applyRemoveTask(planRemoveTask(IH, dir.path, v.cache, deleteData = false), v.cache, v.deleteDir, v.dirExists)
    assertTrue(keep.resumeDeleted)
    assertFalse(keep.dataDeleted)
    assertFalse(keep.dataDeleteFailed)
    assertTrue("保留数据档：一次删除调用都不能发生", v.attempts.isEmpty())
    assertTrue("保留数据档：数据目录必须还在", v.stillThere(dir.path))
    assertEquals("保留数据档：目录内文件必须还在", listOf("payload.mp4"), v.filesOf(dir.path))

    // 删数据：目标目录被尝试删除，**成功**（虚拟树真删）才计入 dataDeleted
    val del = applyRemoveTask(planRemoveTask(IH, dir.path, v.cache, deleteData = true), v.cache, v.deleteDir, v.dirExists)
    assertTrue(del.resumeDeleted)
    assertTrue("删数据档：成功必须计入 dataDeleted", del.dataDeleted)
    assertFalse(del.dataDeleteFailed)
    assertEquals("删数据档：必须对目标目录动过手（且只对它）", listOf(dir.path), v.attempts)
    assertFalse("删数据档：目录确实从树里没了", v.stillThere(dir.path))
    assertEquals("删数据档：确实被移走", listOf(dir.path), v.removed)
    v.cleanup()
  }

  @Test
  fun applyRemoveTask_refusedDelete_isReportedAsFailure_notCountedAsDeleted() {
    val v = VirtualTaskTree("refused")
    val dir = v.dir("downloads/$IH", "payload.mp4" to 16)
    v.onDelete = { DeleteOutcome.REFUSED } // 等价于 rmtree 报失败（没删掉）

    val out = applyRemoveTask(planRemoveTask(IH, dir.path, v.cache, deleteData = true), v.cache, v.deleteDir, v.dirExists)

    assertFalse("拒删绝不能计入「已删」", out.dataDeleted)
    assertTrue("拒删必须如实报告失败", out.dataDeleteFailed)
    assertTrue("失败要留痕（说明数据仍在）", out.note.contains("未成功"))
    assertTrue("resume 的删除不受数据目录失败影响", out.resumeDeleted)
    assertEquals("必须真的尝试过删除", listOf(dir.path), v.attempts)
    assertTrue("数据目录必须还在", v.stillThere(dir.path))
    assertEquals("目录内文件一条都不能少", listOf("payload.mp4"), v.filesOf(dir.path))
    assertTrue("没删掉就不算移走", v.removed.isEmpty())
    v.cleanup()
  }

  @Test
  fun applyRemoveTask_throwingDelete_isReportedAsFailure_notCountedAsDeleted() {
    val v = VirtualTaskTree("threw")
    val dir = v.dir("downloads/$IH", "payload.mp4" to 16)
    v.onDelete = { DeleteOutcome.THREW } // 等价于文件被占用：rmtree 抛异常

    val out = applyRemoveTask(planRemoveTask(IH, dir.path, v.cache, deleteData = true), v.cache, v.deleteDir, v.dirExists)

    assertFalse("抛异常同样不得计入「已删」", out.dataDeleted)
    assertTrue("抛异常必须被吞掉并如实报告失败", out.dataDeleteFailed)
    assertTrue(out.note.contains("未成功"))
    assertTrue("异常不得影响 resume 的删除", out.resumeDeleted)
    assertEquals("必须真的尝试过删除", listOf(dir.path), v.attempts)
    assertTrue("数据目录必须还在", v.stillThere(dir.path))
    v.cleanup()
  }

  @Test
  fun applyRemoveTask_fakeSuccessDirStays_isReportedAsFailure_notCountedAsDeleted() {
    val v = VirtualTaskTree("lied")
    val dir = v.dir("downloads/$IH", "payload.mp4" to 16)
    v.onDelete = { DeleteOutcome.LIED } // 嘴上说删成功（返回 true），目录却还在

    val out = applyRemoveTask(planRemoveTask(IH, dir.path, v.cache, deleteData = true), v.cache, v.deleteDir, v.dirExists)

    assertFalse("假成功绝不能计入「已删」：口径是「目录是否还在」", out.dataDeleted)
    assertTrue("假成功必须如实报告失败", out.dataDeleteFailed)
    assertTrue(out.note.contains("未成功"))
    assertEquals("必须真的尝试过删除", listOf(dir.path), v.attempts)
    assertTrue("数据目录必须还在（含文件）", v.stillThere(dir.path))
    assertEquals(listOf("payload.mp4"), v.filesOf(dir.path))
    v.cleanup()
  }

  @Test
  fun applyRemoveTask_guardRejections_neverCallDelete() {
    val v = VirtualTaskTree("guard")
    val insideWrongName = v.dir("downloads/other-task", "theirs.bin" to 8)
    val outside = File(v.labelRoot, "user-data/$IH") // 名字对，位置不在受管根内

    val outsideOut = applyRemoveTask(planRemoveTask(IH, outside.path, v.cache, deleteData = true), v.cache, v.deleteDir, v.dirExists)
    val wrongNameOut =
      applyRemoveTask(planRemoveTask(IH, insideWrongName.path, v.cache, deleteData = true), v.cache, v.deleteDir, v.dirExists)

    assertTrue("守卫一（不在受管根内）必须拒绝", outsideOut.rejected)
    assertTrue("守卫二（目录名 != info_hash）必须拒绝", wrongNameOut.rejected)
    assertTrue("双守卫拒绝后一次删除调用都不能发生", v.attempts.isEmpty())
    assertFalse(outsideOut.dataDeleted || wrongNameOut.dataDeleted)
    assertFalse("守卫拒绝不算「删失败」（没动手）", outsideOut.dataDeleteFailed || wrongNameOut.dataDeleteFailed)
    assertTrue("共享目录里的非本任务数据必须原样", v.stillThere(insideWrongName.path))
    assertEquals(listOf("theirs.bin"), v.filesOf(insideWrongName.path))
    v.cleanup()
  }

  @Test
  fun removeDirForOtherTaskDoesNotTouchNeighbour() {
    val v = VirtualTaskTree("neighbour")
    val mine = v.dir("downloads/$IH", "mine.bin" to 16)
    val theirs = v.dir("downloads/$IH2", "theirs.bin" to 32)

    val out = applyRemoveTask(planRemoveTask(IH, mine.path, v.cache, deleteData = true), v.cache, v.deleteDir, v.dirExists)

    assertTrue(out.dataDeleted)
    assertEquals("只对目标目录动过手", listOf(mine.path), v.attempts)
    assertFalse("别人的任务目录不得收到删除调用", v.attempts.contains(theirs.path))
    assertTrue("别人的任务目录不受影响", v.stillThere(theirs.path))
    assertEquals("别人的文件也原样", listOf("theirs.bin"), v.filesOf(theirs.path))
    assertFalse("自己的目录确实没了", v.stillThere(mine.path))
    v.cleanup()
  }

  // ------------------------------------------------------------ 真实临时目录（端到端，口径与删除是否成功无关）

  /**
   * 真实文件系统上的删数据端到端：断言刻意**与「删除是否成功」无关**——只断言
   * 「目标目录确实被尝试过删除」+「[RemoveOutcome] 如实反映目录的真实状态」
   * （成功 ⇒ 真没了；没删掉 ⇒ 真还在），所以 rmtree 偶发失败也不会让用例翻红。
   */
  @Test
  fun realTempDir_deleteDataPath_dataDeletedMatchesReality() {
    val root = tempRoot("remove-real")
    val cache = File(root, "cache").also { it.mkdirs() }
    val dir = File(cache, "downloads/$IH").also { it.mkdirs() }
    File(dir, "payload.mp4").writeBytes(ByteArray(16))
    ResumeStore.write(cache, IH, byteArrayOf(1, 2, 3))
    val fs = RecordingFs()

    val out = applyRemoveTask(planRemoveTask(IH, dir.path, cache, deleteData = true), cache, fs.deleteDir, fs.dirExists)

    assertTrue("resume 真删（单文件删除不受 rmtree 噪声影响）", out.resumeDeleted)
    assertNull(ResumeStore.read(cache, IH))
    assertEquals("目标目录必须被尝试删除（且只对它）", listOf(dir.path), fs.attempts)
    assertEquals("如实报告：dataDeleted 必须等于「目录真的没了」", !dir.exists(), out.dataDeleted)
    assertEquals("只有尝试过没删掉才算失败：与 dataDeleted 互补", !out.dataDeleted, out.dataDeleteFailed)
    if (out.dataDeleted) {
      assertFalse("说删了就必须真没了", dir.exists())
    } else {
      assertTrue("没删掉就必须还在（数据不丢，留待人工处理）", dir.exists())
    }
    root.deleteRecursively()
  }
}
