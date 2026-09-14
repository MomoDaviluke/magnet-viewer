package com.bitseed.magnetviewer.data

import android.content.Context
import com.bitseed.magnetviewer.core.model.TorrentFile
import java.io.File

/**
 * 落盘布局（与桌面一致）：`<root>/cache/.preview/<infohash>/<相对路径>`。
 *
 * [root] 用**应用内部存储** `context.filesDir/magnet`（而不是 `getExternalFilesDir`）。原因（2026-09-14 实测）：
 * 外部专属目录位于 `/storage/emulated/0/...`，所有读写都要过 MediaProvider 的 FUSE 层；在这台
 * Android 16 模拟器上，App 一旦读写该路径下的文件，**MediaProvider 自身会 SIGABRT**
 * （`libfuse_jni.so NodeTracker::CheckTracked` 断言失败），随后 vold 把"所有引用该挂载点的进程"
 * 一并杀掉 —— 表现为 App 点「预览」就整体死亡（崩溃栈里没有任何我们的代码）。
 * libtorrent 落盘与流服务读盘都走同一层 FUSE，因此换内部存储可彻底绕开；反正下载产物最终
 * 要落给用户时走 MediaStore 导出（阶段 5），不需要工作目录可被文件管理器看见。
 */
object MagnetStorage {

  const val ROOT_DIR_NAME = "magnet"

  fun root(context: Context): File = File(context.filesDir, ROOT_DIR_NAME)

  /** 缓存根：`<root>/cache`（libtorrent 的 save_path 前缀就是这里往下）。 */
  fun cacheRoot(context: Context): File {
    val d = File(root(context), "cache")
    if (!d.exists()) d.mkdirs()
    return d
  }

  /** 预览任务目录：`<cacheRoot>/.preview/<infohash>`（与 engine.addMagnet 的算法一致）。 */
  fun previewRoot(cacheRoot: File, infoHash: String): File {
    val sub = if (infoHash.isEmpty()) ".preview/tmp" else ".preview/$infoHash"
    val d = File(cacheRoot, sub)
    if (!d.exists()) d.mkdirs()
    return d
  }

  /** 种子内文件在磁盘上的绝对路径。 */
  fun fileDiskPath(previewRoot: File, file: TorrentFile): File =
    File(previewRoot, file.path.replace('/', File.separatorChar))
}
