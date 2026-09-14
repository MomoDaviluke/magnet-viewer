package com.bitseed.magnetviewer.engine

/**
 * 下载限速下发（**I-01 的引擎侧修复**；纯逻辑 + 窄接口，JVM 可测）。
 *
 * 两条路一起走：
 * 1. **会话级** `settings_pack.download_rate_limit`（`BtEngine.applyRateLimitNow` 已在下发）；
 * 2. **逐任务** `torrent_handle.set_download_limit(bytes)`（本文件）。
 *
 * 为什么必须补第 2 条（实测根据）：libtorrent 的会话级 `download_rate_limit` **只管远端 peer**，
 * 本机/局域网/回环对端走 `local_download_rate_limit`（默认 0 = 不限）。靶场做种端在
 * `10.0.2.2`（模拟器侧的局域网地址）→ 属于 local peer → 会话级限速对它**不生效**
 * （QA-2026-09-14 实测 64 KB/s 限制下跑出 1304 KB/s）。而 libtorrent4j 2.1.0-39 的
 * `settings_pack.int_types` 里**没有** `local_download_rate_limit`（javap 全量枚举复核，
 * 见报告），所以会话级这条路到此为止；逐任务限速是**能触达**的那条。
 */

/** KB/s（配置层单位）→ 字节/秒（libtorrent 单位）；负数按 0（不限）。 */
fun rateLimitBytes(kbps: Int): Int = maxOf(0, kbps) * 1024

/**
 * 把限速下发到每一个句柄（**逐任务**）。
 *
 * @return 成功下发的句柄数（失效句柄跳过 —— swig 调用对失效句柄会原生崩溃）。
 */
fun applyRateLimitToHandles(kbps: Int, handles: List<PreviewHandleOps>): Int {
  val bytes = rateLimitBytes(kbps)
  var applied = 0
  for (h in handles) {
    val valid =
      try {
        h.isValid()
      } catch (_: Throwable) {
        false
      }
    if (!valid) continue
    try {
      h.setDownloadLimit(bytes)
      applied++
    } catch (_: Throwable) {
      // 单个句柄失败不影响其余（限速是尽力而为的旁路设施）
    }
  }
  return applied
}
