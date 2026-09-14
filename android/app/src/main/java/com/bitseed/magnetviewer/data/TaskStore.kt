package com.bitseed.magnetviewer.data

import com.bitseed.magnetviewer.core.model.ParseResult
import com.bitseed.magnetviewer.core.model.TorrentFile
import com.bitseed.magnetviewer.core.states.STATE_DOWNLOADING
import com.bitseed.magnetviewer.core.states.STATE_PAUSED
import com.bitseed.magnetviewer.core.states.STATE_QUEUED
import com.bitseed.magnetviewer.core.states.STATE_READY
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/**
 * 任务清单持久化（**对齐桌面 `core/taskstore.py` 的语义与布局**）。
 *
 * 载体：`<cacheDir>/.tasks.json`，cacheDir = `<filesDir>/magnet/cache`（与预览缓存同根）。
 * 为什么不用 DataStore/SharedPreferences：清单是**结构化文档**（含文件表），要能被 `run-as cat`
 * 直接看、能原子替换、能容忍外部改坏——JSON 文件最合适（决策点 D6 与桌面同款）。
 *
 * 写：`临时文件 + fsync + rename`（[atomicWriteBytes]）——崩溃/断电不留下半截 JSON，
 * 旧文件保持完整可读。失败**向上抛 IOException**（是否只告警由调用方决定，
 * 落盘入口 [saveTasksQuietly] 已按桌面语义实现为「只告警」）。
 *
 * 读：任何损坏形态都返回空表（绝不抛、绝不阻断启动）：
 * 文件缺失 / 非 JSON / 截断 / 顶层结构错误 / 版本超前 → 空表；单条记录非法
 * （info_hash 缺失或格式不符、键与记录不一致）→ **只丢该条**，其余保留，逐条 `warn` 上报。
 *
 * 字段与桌面同名（`info_hash` / `source` / `name` / `total_size` / `files` / `selected` /
 * `state` / `priority` / `save_path` / `error` / `retries` / `created_at` / `finished_at` / `seed`），
 * 另加安卓侧显式需要的 `download`（是否下载：解析/预览任务为 false 时重启只恢复清单不续传）。
 * 速度/进度/ETA **不落盘**（重启后由 handle.status() 派生，与桌面一致）。
 */
const val TASK_FILE_NAME: String = ".tasks.json"

/** 清单结构版本（与桌面 `SCHEMA_VERSION` 同为 1；版本不符一律拒读，静默重建）。 */
const val TASK_SCHEMA_VERSION: Int = 1

/** fastresume 子目录（桌面 `core/resume.py: RESUME_SUBDIR` 同名同布局）。 */
const val RESUME_SUBDIR: String = ".resume"

/** 清单文件完整路径：`<cacheDir>/.tasks.json`。 */
fun taskFilePath(cacheDir: File): File = File(cacheDir, TASK_FILE_NAME)

/**
 * info_hash 归一化：小写 40（v1/SHA-1）或 64（v2/SHA-256）位 hex；非法返回 null。
 *
 * 统一格式是持久化键与 fastresume 文件名安全的前提（**防路径注入**：
 * ih 会拼进 `.resume/<ih>.fastresume` 的文件名）。
 */
fun normalizeInfoHash(raw: String?): String? {
  val s = raw?.trim()?.lowercase() ?: return null
  if (s.length != 40 && s.length != 64) return null
  if (!s.all { it in "0123456789abcdef" }) return null
  return s
}

/** 该键能否作为 fastresume 文件名（40/64 位 hex）。 */
fun isResumeKey(key: String): Boolean = normalizeInfoHash(key) != null

/**
 * 一条持久化任务记录（桌面 `core/taskstore.py: task_from_result` + `normalize_task` 的字段对齐）。
 */
data class TaskRecord(
  val infoHash: String,
  val source: String = "unknown",
  val name: String = "",
  val totalSize: Long = 0L,
  /** 全量文件表（含 `.pad`，索引必须与 libtorrent 一致——重启后靠它直接展示清单）。 */
  val files: List<TorrentFile> = emptyList(),
  /** 勾选文件路径集（本 demo 默认全选）。 */
  val selected: List<String> = emptyList(),
  val state: String = STATE_QUEUED,
  val priority: Int = 0,
  val savePath: String = "",
  val error: String = "",
  val retries: Int = 0,
  val createdAt: Double? = null,
  val finishedAt: Double? = null,
  val seed: Boolean = false,
  /** 是否**下载型**任务（false = 只看清单/预览的解析态，重启不续传）。 */
  val download: Boolean = true,
) {
  /** 界面展示用文件列表（过滤 .pad）。 */
  val viewFiles: List<TorrentFile> get() = files.filter { !it.isPad }
}

// ---------------------------------------------------------------- 序列化（纯函数）

fun encodeFile(f: TorrentFile): Jv =
  Jv.Obj(
    linkedMapOf(
      "index" to Jv.Num(f.index.toDouble()),
      "path" to Jv.Str(f.path),
      "size" to Jv.Num(f.size.toDouble()),
      "offset" to Jv.Num(f.offset.toDouble()),
      "start_piece" to Jv.Num(f.startPiece.toDouble()),
      "end_piece" to Jv.Num(f.endPiece.toDouble()),
    )
  )

/** 单条文件记录解码：字段缺失/类型错误 → null（单条容错，不影响其余文件）。 */
fun decodeFile(raw: Jv?): TorrentFile? {
  val path = raw?.strOr("path", "") ?: return null
  if (path.isEmpty()) return null
  return TorrentFile(
    index = raw.intOr("index"),
    path = path,
    size = raw.longOr("size"),
    offset = raw.longOr("offset"),
    startPiece = raw.intOr("start_piece"),
    endPiece = raw.intOr("end_piece"),
  )
}

fun encodeTask(t: TaskRecord): Jv =
  Jv.Obj(
    linkedMapOf(
      "info_hash" to Jv.Str(t.infoHash),
      "source" to Jv.Str(t.source),
      "name" to Jv.Str(t.name),
      "total_size" to Jv.Num(t.totalSize.toDouble()),
      "files" to Jv.Arr(t.files.map { encodeFile(it) }),
      "selected" to Jv.Arr(t.selected.map { Jv.Str(it) }),
      "state" to Jv.Str(t.state),
      "priority" to Jv.Num(t.priority.toDouble()),
      "save_path" to Jv.Str(t.savePath),
      "error" to Jv.Str(t.error),
      "retries" to Jv.Num(t.retries.toDouble()),
      "created_at" to (t.createdAt?.let { Jv.Num(it) } ?: Jv.Null),
      "finished_at" to (t.finishedAt?.let { Jv.Num(it) } ?: Jv.Null),
      "seed" to Jv.Bool(t.seed),
      "download" to Jv.Bool(t.download),
    )
  )

/**
 * 解码并归一化一条磁盘记录；非法（info_hash 缺失/格式不符）返回 null。
 *
 * 绝不信任磁盘上的原始类型（文件可被手工编辑/损坏）：已知键做类型钳制，
 * 未知键丢弃（结构由本模块掌控）；`files` / `selected` 逐条容错。
 */
fun decodeTask(raw: Jv?): TaskRecord? {
  val obj = raw.asObj() ?: return null
  val ih = normalizeInfoHash(obj.strOr("info_hash").ifEmpty { obj.strOr("id", "") }) ?: return null
  val name = obj.strOr("name", "").ifEmpty { ih }
  return TaskRecord(
    infoHash = ih,
    source = obj.strOr("source", "unknown"),
    name = name,
    totalSize = obj.longOr("total_size"),
    files = obj.listOrEmpty("files").mapNotNull { decodeFile(it) },
    selected = obj.listOrEmpty("selected").mapNotNull { (it as? Jv.Str)?.value },
    state = obj.strOr("state", "unknown"),
    priority = obj.intOr("priority"),
    savePath = obj.strOr("save_path"),
    error = obj.strOr("error"),
    retries = obj.intOr("retries"),
    createdAt = obj.doubleOrNull("created_at"),
    finishedAt = obj.doubleOrNull("finished_at"),
    seed = obj.boolOr("seed", false),
    download = obj.boolOr("download", true),
  )
}

/** 整份清单 → JSON 文本（键排序 + 2 空格缩进，与桌面 `sort_keys=True` 同形）。 */
fun encodeTasks(tasks: Map<String, TaskRecord>): String {
  val payload =
    Jv.Obj(
      linkedMapOf(
        "version" to Jv.Num(TASK_SCHEMA_VERSION.toDouble()),
        "tasks" to Jv.Obj(LinkedHashMap(tasks.mapValues { encodeTask(it.value) })),
      )
    )
  return payload.encode(pretty = true, sortKeys = true)
}

// ---------------------------------------------------------------- 原子写

/**
 * 原子写字节：**同目录临时文件 + fsync + rename**（桌面 `taskstore.atomic_write_bytes` 同款）。
 *
 * 中途失败（写盘/rename 抛错）不破坏已存在的目标文件；成功后磁盘上不存在 `.tmp` 残留。
 * 失败抛 [IOException]（调用方决定告警还是上抛）。
 *
 * 两处加固（安卓侧实测踩到过）：
 * - 临时文件名**唯一**（`<name>.<nanotime>.tmp`）：两次并发落盘不会在同一个 `.tmp` 上打架
 *   （实测现象：`rename 失败：….tasks.json.tmp → ….tasks.json`）；
 * - 同一目标文件的写操作在进程内**串行化**（按绝对路径加锁）：并发调用只会看到完整快照。
 */
fun atomicWriteBytes(target: File, data: ByteArray): File {
  val lock = writeLocks.computeIfAbsent(target.absolutePath) { Any() }
  return synchronized(lock) { writeLocked(target, data) }
}

private val writeLocks = java.util.concurrent.ConcurrentHashMap<String, Any>()

private fun writeLocked(target: File, data: ByteArray): File {
  val dir = target.parentFile ?: throw IOException("目标没有父目录：$target")
  if (!dir.exists() && !dir.mkdirs()) throw IOException("无法创建目录：$dir")
  if (!dir.isDirectory) throw IOException("目录路径被文件占用：$dir")
  val tmp = File(dir, target.name + "." + System.nanoTime() + ".tmp")
  try {
    FileOutputStream(tmp).use { out ->
      out.write(data)
      out.flush()
      out.fd.sync()
    }
  } catch (e: IOException) {
    tmp.delete()
    throw IOException("写临时文件失败：${tmp.path}（${e.message}）", e)
  }
  // 原子替换：优先 ATOMIC_MOVE（文件系统不支持时抛 AtomicMoveNotSupportedException → 回落 renameTo）
  try {
    Files.move(
      tmp.toPath(),
      target.toPath(),
      StandardCopyOption.REPLACE_EXISTING,
      StandardCopyOption.ATOMIC_MOVE,
    )
  } catch (e: Throwable) {
    if (!tmp.renameTo(target)) {
      try {
        Files.move(tmp.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING)
      } catch (e2: Throwable) {
        tmp.delete()
        throw IOException("rename 失败：${tmp.path} → ${target.path}（${e2.message}）", e2)
      }
    }
  }
  return target
}

/** 原子写任务清单；返回写出的文件路径。失败抛 [IOException]。 */
fun saveTasks(cacheDir: File, tasks: Map<String, TaskRecord>): File =
  atomicWriteBytes(taskFilePath(cacheDir), encodeTasks(tasks).toByteArray(Charsets.UTF_8))

/**
 * 落盘入口（**桌面 `fetcher.persist_tasks` 语义：失败只告警、绝不阻断主流程**）。
 *
 * @return 写出的文件路径；失败返回 null（原因经 [warn] 上报）。
 */
fun saveTasksQuietly(cacheDir: File, tasks: Map<String, TaskRecord>, warn: (String) -> Unit): File? =
  try {
    saveTasks(cacheDir, tasks)
  } catch (e: Throwable) {
    warn("任务清单落盘失败（已忽略）：" + (e.message ?: e.javaClass.simpleName))
    null
  }

// ---------------------------------------------------------------- 读（损坏容错）

/** 读取任务清单 → `Map<info_hash, TaskRecord>`；任何损坏形态都返回空表（绝不抛）。 */
fun loadTasks(cacheDir: File, warn: (String) -> Unit = {}): Map<String, TaskRecord> {
  val path = taskFilePath(cacheDir)
  if (!path.isFile) return emptyMap()
  val text =
    try {
      path.readText(Charsets.UTF_8)
    } catch (e: Throwable) {
      warn("任务清单不可读，已按空表处理：" + (e.message ?: e.javaClass.simpleName))
      return emptyMap()
    }
  val root =
    parseJv(text)
      ?: run {
        warn("任务清单损坏（非 JSON / 截断），已按空表处理")
        return emptyMap()
      }
  val obj = root.asObj()
  if (obj == null) {
    warn("任务清单顶层不是对象，已按空表处理")
    return emptyMap()
  }
  if (obj.intOr("version", -1) != TASK_SCHEMA_VERSION) {
    warn("任务清单版本不符（${obj.strOr("version", "?")} ≠ $TASK_SCHEMA_VERSION），拒读并重建")
    return emptyMap()
  }
  val raw = obj.field("tasks")
  val tasksObj = raw.asObj()
  if (tasksObj == null) {
    warn("任务清单缺 tasks 对象，已按空表处理")
    return emptyMap()
  }
  val out = LinkedHashMap<String, TaskRecord>()
  for ((key, entry) in tasksObj.fields) {
    val t = decodeTask(entry)
    if (t == null || t.infoHash != key.trim().lowercase()) {
      warn("任务记录非法，已丢弃：'$key'")
      continue
    }
    out[t.infoHash] = t
  }
  return out
}

// ---------------------------------------------------------------- 变更操作（纯函数）

/** 按 info_hash 去重写入（后写覆盖）；ih 非法抛 [IllegalArgumentException]（内存数据不该非法）。 */
fun upsertTask(tasks: Map<String, TaskRecord>, task: TaskRecord): Map<String, TaskRecord> {
  val ih = normalizeInfoHash(task.infoHash) ?: throw IllegalArgumentException("任务记录非法：info_hash 格式不符")
  val out = LinkedHashMap(tasks)
  out[ih] = task.copy(infoHash = ih)
  return out
}

/** 按 info_hash 移除任务记录；ih 非法或不存在时原样返回。 */
fun removeTask(tasks: Map<String, TaskRecord>, infoHash: String): Map<String, TaskRecord> {
  val ih = normalizeInfoHash(infoHash) ?: return tasks
  if (!tasks.containsKey(ih)) return tasks
  val out = LinkedHashMap(tasks)
  out.remove(ih)
  return out
}

/** 由一次解析结果构造任务记录（桌面 `task_from_result` 的安卓落点）。 */
fun taskFromResult(
  result: ParseResult,
  source: String,
  savePath: String,
  state: String = STATE_READY,
  download: Boolean = true,
  priority: Int = 0,
  error: String = "",
  createdAt: Double? = null,
): TaskRecord {
  val ih = normalizeInfoHash(result.infoHash) ?: throw IllegalArgumentException("解析结果缺少合法 info_hash")
  return TaskRecord(
    infoHash = ih,
    source = source,
    name = result.name,
    totalSize = result.totalSize,
    files = result.files,
    selected = result.viewFiles.map { it.path },
    state = state,
    priority = priority,
    savePath = savePath,
    error = error,
    retries = 0,
    createdAt = createdAt,
    finishedAt = null,
    seed = false,
    download = download,
  )
}

// ---------------------------------------------------------------- 路径安全

/**
 * [path] 是否位于 [root] 内（**canonical path + 分隔符前缀**判定）。
 *
 * 为什么不用字符串 `startsWith`：同前缀兄弟目录（`cacheT` vs `cacheT_evil`）会被误判为「在内」。
 * canonical 化同时把 `..` 折叠掉，所以 `root/../evil` 也判 false。
 */
fun isWithin(root: File, path: File): Boolean {
  return try {
    val r = root.canonicalPath.trimEnd(File.separatorChar)
    val p = path.canonicalPath
    p == r || p.startsWith(r + File.separator)
  } catch (_: Throwable) {
    false
  }
}

/**
 * 磁盘任务记录的 save_path 消毒：逃出受管范围（cacheDir / 默认目录）则**回落默认目录**。
 *
 * 与桌面 `persist.safe_task_save_path` 同款——磁盘可被外部改坏，坏路径绝不允许
 * 让 libtorrent 往应用私有目录之外写数据。
 */
fun safeTaskSavePath(cacheDir: File, defaultSaveDir: File, savePath: String): File {
  val raw = savePath.trim()
  if (raw.isEmpty()) return defaultSaveDir
  val f = File(raw)
  return if (isWithin(cacheDir, f) || isWithin(defaultSaveDir, f)) f else defaultSaveDir
}
