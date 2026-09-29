package com.bitseed.magnetviewer.data

import java.io.ByteArrayOutputStream
import java.io.File
import java.io.IOException
import java.util.concurrent.atomic.AtomicInteger

/**
 * 磁力链**元数据缓存**：`<cacheDir>/.meta/<ih>.torrent`（桌面 `core/metacache.py` 同款）。
 *
 * 为什么需要它（性能专项 P-1，本模块是这次优化的最大收益点）
 * ---------------------------------------------------------
 * 磁力链只含 info_hash，文件清单（metadata）必须向 DHT / peer 索取：一次完整的
 * DHT 查找 + ut_metadata 握手在现网通常是**数秒到数十秒**，冷门资源直接撞
 * `metadata_timeout`（默认 90s）超时。改造前本程序对元数据**零缓存**——
 * 同一条磁力链每次解析都要重跑一遍完整网络往返；重启恢复也只能靠 resume data
 * 里的 info 段（幸而 [ResumeStore] 已带 `SAVE_INFO_DICT`）。
 *
 * 本模块把「已拿到手的 metadata」按 info_hash 落盘成**标准 .torrent**
 * （info 段 + tracker 列表），解析前先查缓存，命中即注入句柄 —— 网络往返直接
 * 从关键路径上摘掉。桌面版实测：同一磁力链二次解析 0.31s → 0.005s。
 *
 * 设计约束（与桌面逐条对齐）
 * -------------------------
 * - **只缓存 info 段与 tracker 列表**（几 KB~几 MB），不缓存任何用户数据；
 * - 文件名走 [normalizeInfoHash] 强校验（40/64 位 hex），非法键一律按「未命中」
 *   处理，绝不拼路径（防穿越）；
 * - 写盘走 [atomicWriteBytes]（临时文件 + 原子替换），崩溃不留半截文件；
 * - **失败绝不向上抛**：缓存是旁路加速设施，任何异常只记日志/统计，
 *   主流程该走网络还走网络；
 * - 超量按 mtime **LRU 淘汰**（[prune]），命中刷新 mtime。
 *
 * 与桌面的唯一差异：Android 侧拿不到 `TorrentInfo.trackers()`（libtorrent4j
 * 2.1.0-39 未绑定），tracker 由调用方传入（磁力链自带 + BOOTSTRAP 兜底），
 * 因此 [encodeTorrent] 的 trackers 是**可选**参数。
 *
 * 本文件**不碰 native**（纯路径 + 编码），可在 JVM 单测里整跑。
 */
object MetaCache {

  const val META_SUBDIR: String = ".meta"
  const val SUFFIX: String = ".torrent"
  const val MAX_FILES: Int = 1000
  const val MAX_BYTES: Int = 8 * 1024 * 1024
  const val CREATED_BY: String = "magnet-viewer"
  const val COMMENT: String = "magnet-viewer metadata cache"

  private val hits = AtomicInteger()
  private val misses = AtomicInteger()
  private val writes = AtomicInteger()
  private val pruned = AtomicInteger()

  /** 元数据缓存目录：`<cacheDir>/.meta`。 */
  fun dir(cacheDir: File): File = File(cacheDir, META_SUBDIR)

  /** 单条元数据文件；info_hash 非法（非 40/64 位 hex）返回 null，绝不拼路径。 */
  fun path(cacheDir: File, infoHash: String?): File? {
    val ih = normalizeInfoHash(infoHash) ?: return null
    return File(dir(cacheDir), ih + SUFFIX)
  }

  /** 缓存中是否已有该 info_hash 的元数据（非法键返回 false）。 */
  fun has(cacheDir: File, infoHash: String?): Boolean = path(cacheDir, infoHash)?.isFile == true

  /** 读元数据字节并计一次命中；不存在 / 读失败返回 null 并计一次未命中。 */
  fun read(cacheDir: File, infoHash: String?): ByteArray? {
    val f = path(cacheDir, infoHash)
    if (f == null) {
      misses.incrementAndGet()
      return null
    }
    val bytes = try {
      if (f.isFile) f.readBytes() else null
    } catch (_: Throwable) {
      null
    }
    if (bytes == null || bytes.isEmpty()) {
      misses.incrementAndGet()
      return null
    }
    hits.incrementAndGet()
    // 命中即续命（LRU 语义：常看的资源不会被淘汰）
    runCatching { f.setLastModified(System.currentTimeMillis()) }
    return bytes
  }

  /** 删除单条（不存在也算成功）。损坏自愈用。 */
  fun delete(cacheDir: File, infoHash: String?): Boolean {
    val f = path(cacheDir, infoHash) ?: return false
    return runCatching { !f.exists() || f.delete() }.getOrDefault(false)
  }

  /** 原子写元数据（**只告警**语义）；非法键 / 超上限 / 写失败返回 null。 */
  fun writeQuietly(
    cacheDir: File,
    infoHash: String?,
    data: ByteArray?,
    warn: (String) -> Unit,
  ): File? {
    val f = path(cacheDir, infoHash) ?: return null
    if (data == null || data.isEmpty()) return null
    if (data.size > MAX_BYTES) {
      warn("元数据过大（${data.size} 字节 > $MAX_BYTES），不入缓存：" + f.name)
      return null
    }
    return try {
      val out = atomicWriteBytes(f, data)
      writes.incrementAndGet()
      prune(cacheDir)
      out
    } catch (e: Throwable) {
      warn("元数据落盘失败（已忽略）：" + (e.message ?: e.javaClass.simpleName))
      null
    }
  }

  /**
   * LRU 淘汰：条目数超 [maxFiles] 时删最旧的（按 mtime），返回删除数。
   *
   * 只在超限时才扫目录（每次写入一次 listFiles，元数据写入频率极低）；
   * `maxFiles <= 0` 视为不限制。**绝不抛异常**——清理是旁路设施。
   */
  fun prune(cacheDir: File, maxFiles: Int = MAX_FILES): Int {
    if (maxFiles <= 0) return 0
    val files = try {
      dir(cacheDir).listFiles { f -> f.isFile && f.name.endsWith(SUFFIX) }?.toList().orEmpty()
    } catch (_: Throwable) {
      return 0
    }
    if (files.size <= maxFiles) return 0
    val victims = files.sortedWith(compareBy({ it.lastModified() }, { it.name }))
      .take(files.size - maxFiles)
    var removed = 0
    for (v in victims) {
      if (runCatching { v.delete() }.getOrDefault(false)) removed++
    }
    if (removed > 0) pruned.addAndGet(removed)
    return removed
  }

  /** 命中统计快照（hits/misses/writes/pruned）。 */
  data class Stats(val hits: Int, val misses: Int, val writes: Int, val pruned: Int) {
    val hitRatePercent: Int get() = if (hits + misses == 0) 0 else hits * 100 / (hits + misses)
  }

  fun stats(): Stats = Stats(hits.get(), misses.get(), writes.get(), pruned.get())

  /** 命中统计单行文本（日志用）：把「缓存命中率」变成可观察指标。 */
  fun statsLine(): String {
    val s = stats()
    return "命中=${s.hits} 未命中=${s.misses} 命中率=${s.hitRatePercent}% " +
      "写入=${s.writes} 淘汰=${s.pruned}"
  }

  /** 清零统计（测试用）。 */
  fun resetStats() {
    hits.set(0)
    misses.set(0)
    writes.set(0)
    pruned.set(0)
  }

  // ---------------------------------------------------------------- 编码

  /**
   * 组装标准 .torrent 字节：`d announce… announce-list… comment… created by…
   * creation date… info <原始 info 段> e`。
   *
   * **info 段原样搬运**（BEP-3/BEP-52 通吃），绝不重算 piece hash —— 重编码
   * 只要有一个字节不同，info_hash 就变了，缓存出来的种子会指向不存在的资源。
   *
   * 为什么不走「通用 bencode 编码器 + bytes 值」：bencode 里 bytes 值是
   * **长度前缀字符串**，把已编码的 info 段当值再编码会变成
   * `4:info588:d9:file tree…`，libtorrent 反解报
   * `missing or invalid 'info' section`（桌面 Python 绑定同坑，实测过）。
   * 这里手工按 bencode **键序**（字典键必须升序）拼接，info 段以裸字节插入。
   *
   * @param trackers 可选；为空时不写 announce / announce-list（键序仍然升序）。
   */
  fun encodeTorrent(
    infoSection: ByteArray,
    trackers: List<String> = emptyList(),
    createdBy: String = CREATED_BY,
    comment: String = COMMENT,
    epochSeconds: Long = System.currentTimeMillis() / 1000L,
  ): ByteArray {
    if (infoSection.isEmpty()) throw IOException("info 段为空，拒绝编码 .torrent")
    val urls = trackers.mapNotNull { it.trim().takeIf { u -> u.isNotEmpty() } }.distinct().take(32)
    val out = ByteArrayOutputStream(infoSection.size + 256)
    out.write('d'.code)
    if (urls.isNotEmpty()) {
      out.write(bencodeString("announce"))
      out.write(bencodeString(urls.first()))
      out.write(bencodeString("announce-list"))
      out.write('l'.code)
      for (u in urls) {
        out.write('l'.code)
        out.write(bencodeString(u))
        out.write('e'.code)
      }
      out.write('e'.code)
    }
    if (comment.isNotEmpty()) {
      out.write(bencodeString("comment"))
      out.write(bencodeString(comment))
    }
    out.write(bencodeString("created by"))
    out.write(bencodeString(createdBy))
    out.write(bencodeString("creation date"))
    out.write(bencodeInteger(epochSeconds))
    out.write(bencodeString("info"))
    out.write(infoSection) // 裸插：不再套长度前缀
    out.write('e'.code)
    return out.toByteArray()
  }

  /** bencode 字符串：`<字节长度>:<utf8 字节>`。 */
  private fun bencodeString(s: String): ByteArray {
    val body = s.toByteArray(Charsets.UTF_8)
    return ByteArrayOutputStream(body.size + 8).apply {
      write(body.size.toString().toByteArray(Charsets.US_ASCII))
      write(':'.code)
      write(body)
    }.toByteArray()
  }

  /** bencode 整数：`i<十进制>e`。 */
  private fun bencodeInteger(v: Long): ByteArray =
    ("i" + v.toString() + "e").toByteArray(Charsets.US_ASCII)
}
