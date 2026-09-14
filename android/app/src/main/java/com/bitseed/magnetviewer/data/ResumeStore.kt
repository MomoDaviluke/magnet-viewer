package com.bitseed.magnetviewer.data

import java.io.File
import java.io.IOException

/**
 * fastresume（续传数据）落盘：`<cacheDir>/.resume/<ih>.fastresume`
 * （桌面 `core/resume.py` 的同名同布局约定）。
 *
 * 约定：
 * - 内容是 libtorrent resume data 的 bencode 字节（libtorrent 官方格式）；
 *   解码**不在本文件**——它要 native（`read_resume_data_ex`），属于引擎侧，
 *   本文件只管「字节怎么安全地落在哪儿」（纯 IO，可在 JVM 单测里跑）。
 * - 文件名强制 40/64 位 hex（[normalizeInfoHash]）：ih 会被拼进文件名，
 *   不校验就等于给了路径注入的入口（`../../x`）。
 * - 写盘走 [atomicWriteBytes]（原子替换，不产生半截文件）；读失败/不存在一律 null，
 *   **绝不抛**——「找不到 resume 文件」是正常态（元数据会重新拉），不是错误。
 */
object ResumeStore {

  const val SUFFIX: String = ".fastresume"

  /** fastresume 目录：`<cacheDir>/.resume`。 */
  fun dir(cacheDir: File): File = File(cacheDir, RESUME_SUBDIR)

  /** 单任务 fastresume 路径；ih 非法（非 40/64 位 hex）返回 null。 */
  fun path(cacheDir: File, infoHash: String): File? {
    val ih = normalizeInfoHash(infoHash) ?: return null
    return File(dir(cacheDir), ih + SUFFIX)
  }

  /** 读单任务 fastresume；不存在 / 读失败返回 null。 */
  fun read(cacheDir: File, infoHash: String): ByteArray? {
    val f = path(cacheDir, infoHash) ?: return null
    return try {
      if (!f.isFile) null else f.readBytes()
    } catch (_: Throwable) {
      null
    }
  }

  /** 原子写 fastresume；失败抛 [IOException]（是否只告警由调用方决定）。 */
  fun write(cacheDir: File, infoHash: String, data: ByteArray): File {
    val f = path(cacheDir, infoHash) ?: throw IOException("info_hash 非法，拒绝写 fastresume：$infoHash")
    return atomicWriteBytes(f, data)
  }

  /** 写入口（**只告警**语义）：失败返回 null 并把原因交给 [warn]。 */
  fun writeQuietly(cacheDir: File, infoHash: String, data: ByteArray, warn: (String) -> Unit): File? =
    try {
      write(cacheDir, infoHash, data)
    } catch (e: Throwable) {
      warn("fastresume 落盘失败（已忽略）：" + (e.message ?: e.javaClass.simpleName))
      null
    }

  /** 删除单任务 fastresume（不存在也算成功）。 */
  fun delete(cacheDir: File, infoHash: String): Boolean {
    val f = path(cacheDir, infoHash) ?: return false
    return try {
      !f.exists() || f.delete()
    } catch (_: Throwable) {
      false
    }
  }

  /** 已存在 fastresume 的 key 子集（启动恢复日志用）。 */
  fun presentKeys(cacheDir: File, keys: Collection<String>): List<String> =
    keys.filter { k -> path(cacheDir, k)?.isFile == true }

  /** 尚未落盘 fastresume 的 key 子集（逾时告警用，桌面 `missing_resume_keys` 同款）。 */
  fun missingKeys(cacheDir: File, keys: Collection<String>): List<String> =
    keys.filter { k -> path(cacheDir, k)?.isFile != true }
}
