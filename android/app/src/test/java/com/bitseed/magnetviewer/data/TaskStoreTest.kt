package com.bitseed.magnetviewer.data

import com.bitseed.magnetviewer.core.model.ParseResult
import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.core.states.STATE_COMPLETED
import com.bitseed.magnetviewer.core.states.STATE_PAUSED
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 任务清单持久化单测（**JVM，不碰 libtorrent4j / android**）：
 *
 * 1. 原子写：临时文件 + rename，成功后不留 `.tmp`；覆盖写不产生半截文件；
 * 2. 损坏容错：缺失 / 非 JSON / 截断 / 顶层结构错 / 版本不符 / 单条非法 —— 全部不抛异常，
 *    整表坏 → 空表 + 告警；单条坏 → 只丢那条；
 * 3. 字段序列化往返：TaskRecord ↔ JSON 全字段一致（桌面同名键）；
 * 4. 写失败只告警（`saveTasksQuietly` 返回 null，warn 被调用，**不抛**）；
 * 5. 路径安全：`isWithin` 不被同前缀兄弟目录绕过、`..` 逃逸判 false，
 *    `safeTaskSavePath` 把逃逸路径拉回默认目录。
 */
class TaskStoreTest {

  private fun tempDir(tag: String): File {
    val d = File(System.getProperty("java.io.tmpdir"), "mv-taskstore-$tag-${System.nanoTime()}")
    d.mkdirs()
    return d
  }

  private fun sampleRecord(ih: String, state: String = STATE_PAUSED): TaskRecord {
    val files =
      listOf(
        TorrentFile(0, "fixture/fixture.mp4", 80832L, 0L, 0, 4),
        TorrentFile(1, "fixture/fixture-tail.mp4", 80832L, 80832L, 5, 9),
        TorrentFile(2, "fixture/.pad/102400", 20480L, 161664L, 10, 10),
      )
    return TaskRecord(
      infoHash = ih,
      source = "magnet:?xt=urn:btih:$ih&dn=fixture&x.pe=10.0.2.2:16881",
      name = "fixture",
      totalSize = 161664L,
      files = files,
      selected = listOf("fixture/fixture.mp4", "fixture/fixture-tail.mp4"),
      state = state,
      priority = 3,
      savePath = "/data/user/0/com.bitseed.magnetviewer/files/magnet/cache/.preview/$ih",
      error = "",
      retries = 1,
      createdAt = 1757856000.125,
      finishedAt = null,
      seed = false,
      download = true,
    )
  }

  // ------------------------------------------------------------ 1. 原子写

  @Test
  fun atomicWrite_createsFile_andLeavesNoTmpResidue() {
    val dir = tempDir("atomic")
    val tasks = mapOf(SAMPLE_IH to sampleRecord(SAMPLE_IH))
    val f = saveTasks(dir, tasks)
    assertEquals(File(dir, TASK_FILE_NAME).path, f.path)
    assertTrue(f.isFile)
    assertTrue("内容必须非空", f.length() > 0)
    val leftovers = dir.listFiles()?.filter { it.name.endsWith(".tmp") } ?: emptyList()
    assertTrue("成功后不得留 .tmp 残留：$leftovers", leftovers.isEmpty())
    assertEquals(mapOf(SAMPLE_IH to sampleRecord(SAMPLE_IH)), loadTasks(dir))
    dir.deleteRecursively()
  }

  @Test
  fun atomicWrite_overwriteReplacesContent_andNeverHalfWrites() {
    val dir = tempDir("overwrite")
    saveTasks(dir, mapOf(SAMPLE_IH to sampleRecord(SAMPLE_IH)))
    val first = taskFilePath(dir).readText(Charsets.UTF_8)
    val second = sampleRecord(SAMPLE_IH).copy(name = "第二个名字", state = STATE_COMPLETED, priority = 9)
    saveTasks(dir, mapOf(SAMPLE_IH to second))
    val text = taskFilePath(dir).readText(Charsets.UTF_8)
    assertTrue("新内容必须生效", text.contains("第二个名字"))
    assertFalse("旧内容必须被整体替换", text.contains("\"name\": \"fixture\""))
    assertTrue("覆盖前后都必须是可解析 JSON", parseJv(text) != null)
    assertTrue(first != text)
    assertTrue("覆盖后同样不留 .tmp", (dir.listFiles()?.none { it.name.endsWith(".tmp") } ?: false))
    // 半截写模拟：磁盘上留一个 .tmp（崩溃现场）不应影响读取
    File(dir, TASK_FILE_NAME + ".tmp").writeText("{ 半截", Charsets.UTF_8)
    assertEquals(mapOf(SAMPLE_IH to second), loadTasks(dir))
    dir.deleteRecursively()
  }

  @Test
  fun concurrentWrites_neverCollideAndLeaveNoResidue() {
    // 实测踩到过：两次并发落盘共用同一个 `<name>.tmp` → rename 失败（只告警不阻断，
    // 但白写一次）。加固后：唯一临时名 + 同路径串行化 → 并发下每次写都成功且结果完整。
    val dir = tempDir("concurrent")
    val errors = java.util.Collections.synchronizedList(ArrayList<Throwable>())
    val threads =
      (1..8).map { n ->
        Thread {
          try {
            for (i in 1..15) {
              saveTasks(dir, mapOf(SAMPLE_IH to sampleRecord(SAMPLE_IH).copy(name = "writer-$n-$i")))
            }
          } catch (t: Throwable) {
            errors.add(t)
          }
        }
      }
    threads.forEach { it.start() }
    threads.forEach { it.join() }
    assertTrue("并发落盘不得失败：${errors.map { it.message }}", errors.isEmpty())
    val text = taskFilePath(dir).readText(Charsets.UTF_8)
    assertTrue("落盘结果必须是完整 JSON（不是半截）", parseJv(text) != null)
    assertTrue("最后一版内容必须完整（有结尾大括号）", text.trimEnd().endsWith("}"))
    assertEquals(1, loadTasks(dir).size)
    val leftovers = dir.listFiles()?.filter { it.name.endsWith(".tmp") } ?: emptyList()
    assertTrue("不得留 .tmp 残留：$leftovers", leftovers.isEmpty())
    dir.deleteRecursively()
  }

  @Test
  fun savedFile_hasDesktopShapeAndFieldNames() {
    val dir = tempDir("shape")
    saveTasks(dir, mapOf(SAMPLE_IH to sampleRecord(SAMPLE_IH)))
    val text = taskFilePath(dir).readText(Charsets.UTF_8)
    // 顶层：version + tasks（与桌面 core/taskstore.py 一致）
    assertTrue(text.startsWith("{"))
    assertTrue(text.contains("\"version\": 1"))
    assertTrue(text.contains("\"tasks\""))
    // 键名逐个对拍（桌面 taskstore.normalize_task 的键集 + 安卓侧显式加的 download）
    for (key in DESKTOP_KEYS + listOf("download")) {
      assertTrue("缺键 $key：$text", text.contains("\"$key\""))
    }
    // 键排序 + 2 空格缩进（桌面 sort_keys=True, indent=2）
    assertTrue("必须是缩进 JSON", text.contains("\n  \"tasks\""))
    assertTrue("键必须排序（tasks 在 version 前）", text.indexOf("\"tasks\"") < text.indexOf("\"version\""))
    // UTF-8 直出（ensure_ascii=False）
    assertTrue(text.toByteArray(Charsets.UTF_8).size > 100)
    dir.deleteRecursively()
  }

  @Test
  fun encode_isDeterministic_regardlessOfInsertionOrder() {
    val a = linkedMapOf(SAMPLE_IH to sampleRecord(SAMPLE_IH), IH2 to sampleRecord(IH2, STATE_COMPLETED))
    val b = linkedMapOf(IH2 to sampleRecord(IH2, STATE_COMPLETED), SAMPLE_IH to sampleRecord(SAMPLE_IH))
    assertEquals(encodeTasks(a), encodeTasks(b))
  }

  // ------------------------------------------------------------ 2. 损坏容错

  @Test
  fun missingFile_yieldsEmptyTable_withoutWarning() {
    val dir = tempDir("missing")
    val warns = ArrayList<String>()
    assertEquals(emptyMap<String, TaskRecord>(), loadTasks(dir) { warns.add(it) })
    assertTrue("文件根本不存在时不该告警", warns.isEmpty())
    dir.deleteRecursively()
  }

  @Test
  fun corruptedFiles_allDegradeToEmptyTable_withWarning_neverThrow() {
    val cases =
      mapOf(
        "非 JSON 文本" to "这不是 JSON",
        "截断的 JSON" to "{\"version\": 1, \"tasks\": {\"abc\": {",
        "顶层是数组" to "[1,2,3]",
        "顶层是标量" to "\"just a string\"",
        "版本不符" to "{\"version\": 99, \"tasks\": {\"$SAMPLE_IH\": {}}}",
        "缺 tasks" to "{\"version\": 1}",
        "tasks 类型错" to "{\"version\": 1, \"tasks\": []}",
        "空文件" to "",
      )
    for ((label, text) in cases) {
      val dir = tempDir("corrupt")
      taskFilePath(dir).writeText(text, Charsets.UTF_8)
      val warns = ArrayList<String>()
      val loaded = loadTasks(dir) { warns.add(it) }
      assertEquals("[$label] 必须降级为空表", emptyMap<String, TaskRecord>(), loaded)
      assertTrue("[$label] 必须有告警（不能静默）", warns.isNotEmpty())
      dir.deleteRecursively()
    }
  }

  @Test
  fun singleBadRecord_isDroppedOthersSurvive() {
    val dir = tempDir("badtask")
    val good = SAMPLE_IH
    val text =
      """
      {
        "version": 1,
        "tasks": {
          "$good": ${encodeTask(sampleRecord(good)).encode()},
          "not-a-hash": {"info_hash": "not-a-hash", "name": "坏键"},
          "$IH2": {"name": "没有 info_hash"},
          "$IH3": {"info_hash": "$IH2", "name": "键与记录不一致"}
        }
      }
      """.trimIndent()
    taskFilePath(dir).writeText(text, Charsets.UTF_8)
    val warns = ArrayList<String>()
    val loaded = loadTasks(dir) { warns.add(it) }
    assertEquals("只保留合法那一条", setOf(good), loaded.keys)
    assertEquals(3, warns.size)
    dir.deleteRecursively()
  }

  @Test
  fun fieldTypesAreCoerced_notTrusted() {
    val dir = tempDir("coerce")
    val text =
      """
      {
        "version": 1,
        "tasks": {
          "$SAMPLE_IH": {
            "info_hash": "$SAMPLE_IH",
            "name": 12345,
            "total_size": "80832",
            "state": null,
            "priority": "7",
            "files": [{"index": "0", "path": "a/b.mp4", "size": "10"}, {"path": ""}, "not-an-object"],
            "selected": ["a/b.mp4", 42],
            "created_at": null,
            "seed": "yes",
            "download": 1,
            "未知键": "应被丢弃"
          }
        }
      }
      """.trimIndent()
    taskFilePath(dir).writeText(text, Charsets.UTF_8)
    val t = loadTasks(dir)[SAMPLE_IH]
    assertNotNull(t)
    assertEquals("12345", t!!.name)
    assertEquals(80832L, t.totalSize)
    assertEquals("unknown", t.state)
    assertEquals(7, t.priority)
    assertEquals("坏文件条目只丢自己：空 path 与非对象都丢", 1, t.files.size)
    assertEquals(10L, t.files[0].size)
    assertEquals(listOf("a/b.mp4"), t.selected)
    assertNull(t.createdAt)
    assertTrue("seed='yes' 视为 true", t.seed)
    assertTrue("download=1 视为 true", t.download)
    dir.deleteRecursively()
  }

  // ------------------------------------------------------------ 3. 字段往返

  @Test
  fun roundTrip_preservesEveryField() {
    val dir = tempDir("roundtrip")
    val rec =
      sampleRecord(SAMPLE_IH).copy(
        state = STATE_COMPLETED,
        error = "网络中断",
        finishedAt = 1757856999.5,
        retries = 4,
        priority = -1,
        seed = true,
        download = false,
        selected = emptyList(),
      )
    saveTasks(dir, mapOf(SAMPLE_IH to rec))
    val back = loadTasks(dir)[SAMPLE_IH]
    assertEquals(rec, back)
    assertEquals(rec.files, back!!.files)
    // null 字段（created_at / finished_at）同样要能往返
    val recNullTimes = sampleRecord(IH2).copy(createdAt = null, finishedAt = null)
    saveTasks(dir, mapOf(SAMPLE_IH to rec, IH2 to recNullTimes))
    val back2 = loadTasks(dir)
    assertEquals(rec, back2[SAMPLE_IH])
    assertNull(back2[IH2]?.createdAt)
    assertNull(back2[IH2]?.finishedAt)
    dir.deleteRecursively()
  }

  @Test
  fun taskFromResult_derivesViewFilesAndRejectsBadHash() {
    val ih = SAMPLE_IH
    val result =
      ParseResult(
        infoHash = ih,
        name = "fixture",
        totalSize = 161664L,
        pieceSize = 16384,
        numPieces = 11,
        files = sampleRecord(ih).files,
        source = "magnet",
      )
    val t = taskFromResult(result, source = "magnet:?xt=urn:btih:$ih", savePath = "/tmp/x", state = STATE_PAUSED)
    assertEquals(ih, t.infoHash)
    assertEquals(listOf("fixture/fixture.mp4", "fixture/fixture-tail.mp4"), t.selected)
    assertEquals("默认下载型任务", true, t.download)
    // 非法 info_hash 必须直接拒（内存数据不该被静默接受）
    var threw = false
    try {
      taskFromResult(result.copy(infoHash = "../evil"), "magnet:x", "/tmp/x")
    } catch (_: IllegalArgumentException) {
      threw = true
    }
    assertTrue("非法 info_hash 必须抛 IllegalArgumentException", threw)
  }

  // ------------------------------------------------------------ 4. 写失败只告警

  @Test
  fun saveFailure_warnsAndDoesNotThrow() {
    val notADir = File(tempDir("fail"), "cache-is-a-file")
    notADir.writeText("我占住了这个路径", Charsets.UTF_8) // 让 mkdirs 失败
    val warns = ArrayList<String>()
    val out = saveTasksQuietly(notADir, mapOf(SAMPLE_IH to sampleRecord(SAMPLE_IH))) { warns.add(it) }
    assertNull("失败必须返回 null 而不是抛异常", out)
    assertEquals(1, warns.size)
    assertTrue("告警文本要说明是落盘失败：$warns", warns[0].contains("落盘失败"))
    // 但 saveTasks（底层）本身是要抛的：调用方才知道失败
    var threw = false
    try {
      saveTasks(notADir, mapOf(SAMPLE_IH to sampleRecord(SAMPLE_IH)))
    } catch (_: Throwable) {
      threw = true
    }
    assertTrue("saveTasks 失败必须抛给调用方", threw)
    notADir.deleteRecursively()
  }

  @Test
  fun loadUnreadablePath_degradesToEmpty() {
    // cacheDir 本身是个文件 → 清单路径不可用：必须空表、不抛、不崩溃
    val dir = tempDir("unreadable")
    val f = File(dir, "cache")
    f.writeText("x", Charsets.UTF_8)
    assertEquals(emptyMap<String, TaskRecord>(), loadTasks(f))
    // 也不允许把它当目录建进去（saveTasks 会失败，但只由调用方决定是否告警）
    assertNull(saveTasksQuietly(f, mapOf(SAMPLE_IH to sampleRecord(SAMPLE_IH))) { })
    dir.deleteRecursively()
  }

  // ------------------------------------------------------------ 5. 路径安全

  @Test
  fun isWithin_blocksSiblingPrefixAndTraversal() {
    val root = tempDir("root")
    val sibling = File(root.parentFile, root.name + "_evil")
    sibling.mkdirs()
    assertTrue(isWithin(root, File(root, "cache/.preview/x")))
    assertTrue("根自身算在内", isWithin(root, root))
    assertFalse("同前缀兄弟目录必须判 false", isWithin(root, File(sibling, "x")))
    assertFalse(".. 逃逸必须判 false", isWithin(root, File(root, "../outside")))
    assertFalse(isWithin(File(root, "cache"), File(root, "cacheT_evil/x")))
    sibling.deleteRecursively()
    root.deleteRecursively()
  }

  @Test
  fun safeTaskSavePath_fallsBackToDefaultOnEscape() {
    val cache = File(tempDir("savepath"), "cache")
    cache.mkdirs()
    val default = previewDirFor(cache, SAMPLE_IH)
    assertEquals("默认目录", default, safeTaskSavePath(cache, default, ""))
    val good = File(cache, ".preview/$SAMPLE_IH")
    assertEquals(good, safeTaskSavePath(cache, default, good.path))
    assertEquals("逃出缓存根 → 回落默认", default, safeTaskSavePath(cache, default, "/data/local/tmp/hack"))
    assertEquals("穿越路径 → 回落默认", default, safeTaskSavePath(cache, default, File(cache, ".preview/../../escape").path))
    assertEquals("同前缀兄弟目录 → 回落默认", default, safeTaskSavePath(cache, default, cache.path + "T_evil"))
    cache.deleteRecursively()
  }

  @Test
  fun previewDirFor_sanitizesInfoHash() {
    val cache = tempDir("previewdir")
    assertEquals(File(cache, ".preview/$SAMPLE_IH"), previewDirFor(cache, SAMPLE_IH))
    assertEquals("大小写与空格归一", File(cache, ".preview/$SAMPLE_IH"), previewDirFor(cache, "  ${SAMPLE_IH.uppercase()} "))
    assertFalse("非法 ih 不得拼出路径段", previewDirFor(cache, "../../etc").path.contains("etc"))
    assertEquals(File(cache, ".preview/tmp"), previewDirFor(cache, "../evil"))
    cache.deleteRecursively()
  }

  @Test
  fun normalizeInfoHash_valueDomain() {
    assertEquals(SAMPLE_IH, normalizeInfoHash(SAMPLE_IH))
    assertEquals(SAMPLE_IH, normalizeInfoHash("  ${SAMPLE_IH.uppercase()}  "))
    assertNull(normalizeInfoHash(null))
    assertNull(normalizeInfoHash(""))
    assertNull(normalizeInfoHash("xyz"))
    assertNull(normalizeInfoHash(SAMPLE_IH.dropLast(1)))
    assertNull(normalizeInfoHash(SAMPLE_IH + "0"))
    assertNull("非 hex 字符必须拒", normalizeInfoHash("z" + SAMPLE_IH.drop(1)))
    assertNull(normalizeInfoHash("../../etc/passwd"))
    assertEquals(IH2_64, normalizeInfoHash(IH2_64))
    assertTrue(isResumeKey(SAMPLE_IH))
    assertTrue(isResumeKey(IH2_64))
    assertFalse(isResumeKey("tmp-1"))
  }

  // ------------------------------------------------------------ 变更操作

  @Test
  fun upsertAndRemove_arePureAndKeyed() {
    val a = mapOf(SAMPLE_IH to sampleRecord(SAMPLE_IH))
    val b = upsertTask(a, sampleRecord(IH2, STATE_COMPLETED))
    assertEquals(setOf(SAMPLE_IH, IH2), b.keys)
    assertEquals("原表不被改动", setOf(SAMPLE_IH), a.keys)
    val c = upsertTask(b, sampleRecord(SAMPLE_IH).copy(name = "改了"))
    assertEquals("同 hash 覆盖", "改了", c[SAMPLE_IH]?.name)
    assertEquals("删除后只剩另一个（原表不动）", setOf(SAMPLE_IH), removeTask(c, IH2).keys)
    assertEquals("删除后原表仍是两条", setOf(SAMPLE_IH, IH2), c.keys)
    assertEquals(c, removeTask(c, "非法"))
    assertEquals(c, removeTask(c, IH3))
    var threw = false
    try {
      upsertTask(a, sampleRecord(SAMPLE_IH).copy(infoHash = "bad"))
    } catch (_: IllegalArgumentException) {
      threw = true
    }
    assertTrue(threw)
  }

  // ------------------------------------------------------------ C45 写锁分片（内存有界）

  @Test
  fun writeLocks_areShardedAndBounded() {
    // C45（对齐桌面 core/taskstore.py:165-189）：锁必须固定 64 分片，**不随路径数增长**
    // （旧实现按路径建 ConcurrentHashMap，`.meta` 写也走它 → 锁对象随「看过的种子数」无界膨胀）
    assertEquals(64, WRITE_LOCK_SHARDS)
    // 同路径恒得同一把（互斥语义不变）
    val p = "/data/cache/.tasks.json"
    assertTrue(writeLockFor(p) === writeLockFor(p))
    assertTrue(writeLockFor(p) === writeLockFor(p.uppercase()))
    // 大量不同路径只落在 ≤64 个锁对象上（有界性）
    val distinct = HashSet<Any>()
    for (i in 0 until 5000) distinct.add(writeLockFor("/meta/$i.torrent"))
    assertTrue("锁对象必须 ≤ 分片数（实得 ${distinct.size}）", distinct.size <= WRITE_LOCK_SHARDS)
  }

  // ------------------------------------------------------------ 4.3 共享数据模型（schema_version）

  @Test
  fun savedFile_carriesSharedSchemaVersion() {
    val dir = tempDir("schemaver")
    saveTasks(dir, mapOf(SAMPLE_IH to sampleRecord(SAMPLE_IH)))
    val text = taskFilePath(dir).readText(Charsets.UTF_8)
    assertTrue("顶层必须写 schema_version（shared/task-schema.json 契约版本）",
      text.contains("\"schema_version\": $SHARED_SCHEMA_VERSION"))
    assertEquals("带 schema_version 的文件必须正常读回", mapOf(SAMPLE_IH to sampleRecord(SAMPLE_IH)), loadTasks(dir))
    dir.deleteRecursively()
  }

  @Test
  fun legacyFile_withoutSchemaVersion_isTolerated() {
    // 旧文件（缺 schema_version = 版本 0）：静默兼容，绝不因缺字段拒读（对齐桌面 load_tasks）
    val dir = tempDir("legacy")
    taskFilePath(dir).writeText(
      "{\"version\": 1, \"tasks\": {\"$SAMPLE_IH\": ${encodeTask(sampleRecord(SAMPLE_IH)).encode()}}}",
      Charsets.UTF_8,
    )
    val warns = ArrayList<String>()
    val loaded = loadTasks(dir) { warns.add(it) }
    assertEquals("缺 schema_version 的旧文件必须照常读回", setOf(SAMPLE_IH), loaded.keys)
    dir.deleteRecursively()
  }

  @Test
  fun schemaVersionMismatch_isTolerated_butStructVersionStillRejects() {
    // schema_version 取值不同（未来契约）→ 容忍；结构版本 version 不符 → 拒读（语义不变）
    val dir = tempDir("svtol")
    taskFilePath(dir).writeText(
      "{\"version\": 1, \"schema_version\": 99, \"tasks\": {\"$SAMPLE_IH\": ${encodeTask(sampleRecord(SAMPLE_IH)).encode()}}}",
      Charsets.UTF_8,
    )
    assertEquals("schema_version 超前也必须容忍读回", setOf(SAMPLE_IH), loadTasks(dir).keys)
    taskFilePath(dir).writeText(
      "{\"version\": 2, \"schema_version\": 1, \"tasks\": {}}",
      Charsets.UTF_8,
    )
    assertEquals("结构版本不符仍一律拒读", emptyMap<String, TaskRecord>(), loadTasks(dir))
    dir.deleteRecursively()
  }

  private companion object {
    const val SAMPLE_IH = "444b8b5c9a69b388a5013d85a42caef060f03225"
    const val IH2 = "0123456789abcdef0123456789abcdef01234567"
    const val IH3 = "fedcba9876543210fedcba9876543210fedcba98"
    const val IH2_64 = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"

    /** 桌面 `core/taskstore.py: normalize_task` 的键集（安卓侧同名字段）。 */
    val DESKTOP_KEYS =
      listOf(
        "info_hash",
        "source",
        "name",
        "total_size",
        "files",
        "selected",
        "state",
        "priority",
        "save_path",
        "error",
        "retries",
        "created_at",
        "finished_at",
        "seed",
      )
  }
}
