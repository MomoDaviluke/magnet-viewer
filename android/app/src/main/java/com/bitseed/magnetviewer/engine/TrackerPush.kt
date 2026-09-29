package com.bitseed.magnetviewer.engine

/**
 * 给**已在跑的任务**追加 tracker（兜底通道的即时生效路径）。
 *
 * 为什么要有这一层
 * ----------------
 * `extra_trackers` 的原始落点只在加种时（`AddTorrentParams.setTrackers`）生效，也就是
 * 「开了开关还得把磁力链重新添加一遍」——这在现场很难被用户想到，等于开关形同虚设。
 *
 * 复核（javap，libtorrent4j 2.1.0-39）确认这些方法**都已绑定**，可以直接作用于运行中的句柄：
 * ```
 * public java.util.List<AnnounceEntry> trackers();
 * public void addTracker(AnnounceEntry);
 * public void replaceTrackers(java.util.List<AnnounceEntry>);
 * public AnnounceEntry(String);
 * ```
 *
 * 为什么用 `addTracker` 而**不是** `replaceTrackers`
 * ------------------------------------------------
 * `replaceTrackers` 是**整体替换**：会把磁力链自带的 `tr=`（以及别的来源）一并抹掉。
 * 用户的资源本来能连上，我们一开开关反而把它自己的 tracker 删了 —— 那是净损失。
 * 因此这里的口径是**只做加法**：按 URL 去重后追加缺失项，永不做减法。
 * 副作用是「关闭开关不会移除已追加的条目」（没有减法），文案必须如实说明。
 *
 * 幂等：重复调用不会累积重复条目（`missingTrackers` 会按现有 URL 过滤）。
 */

/**
 * 只暴露「追加 tracker」所需三件事的**窄接口**。
 *
 * 刻意不并进 `PreviewHandleOps`：那个接口方法很多，测试替身都实现了它，
 * 往里加方法会连带打断一堆替身；窄接口让本文件的逻辑可以只靠假替身测。
 */
interface TrackerTarget {
  fun isValid(): Boolean

  /** 当前句柄上已有的 tracker URL（拿不到时返回空表，调用方按「全都缺」处理）。 */
  fun trackerUrls(): List<String>

  /** `torrent_handle.add_tracker(AnnounceEntry(url))`：**追加**一条，不做替换。 */
  fun addTracker(url: String)
}

/** 追加结果：处理过的有效句柄数 / 实际新增的 tracker 条目数。 */
data class TrackerPushResult(
  /** 有效（`isValid()`）且被成功处理的句柄数，含「本来就齐、无需改动」的那些。 */
  val handlesTouched: Int,
  /** 真正新增到句柄上的条目总数。 */
  val urlsAdded: Int,
)

/**
 * 目标 tracker 里**还缺**的那些（纯函数，保持 [desired] 的原始顺序）。
 *
 * 去重按「已有集合」判定：调用方传进来的 desired 自身若含重复项，也会在这里被压成一条
 * （重复注入同一条 URL 没有意义）。
 */
fun missingTrackers(desired: List<String>, existing: List<String>): List<String> {
  if (desired.isEmpty()) return emptyList()
  val have = existing.map { it.trim() }.toSet()
  val out = ArrayList<String>(desired.size)
  for (raw in desired) {
    val u = raw.trim()
    if (u.isEmpty() || u in have || u in out) continue
    out.add(u)
  }
  return out
}

/**
 * 把 [desired] 中缺失的条目追加到每个句柄上。
 *
 * 三条容错（与 `applyRateLimitToHandles` 同款，都是「旁路设施不许把主流程搞挂」）：
 * - 失效句柄跳过 —— swig 调用对失效句柄会原生崩溃；
 * - 单个句柄的 `trackerUrls()` 取不到 → 按「全都缺」处理，不中止其余句柄；
 * - 单条 `addTracker` 失败 → 跳过该条，继续下一条。
 */
fun pushTrackers(desired: List<String>, targets: List<TrackerTarget>): TrackerPushResult {
  if (desired.isEmpty()) return TrackerPushResult(0, 0)
  var touched = 0
  var added = 0
  for (t in targets) {
    val valid =
      try {
        t.isValid()
      } catch (_: Throwable) {
        false
      }
    if (!valid) continue
    val existing =
      try {
        t.trackerUrls()
      } catch (_: Throwable) {
        emptyList()
      }
    touched++
    for (u in missingTrackers(desired, existing)) {
      try {
        t.addTracker(u)
        added++
      } catch (_: Throwable) {
        // 单条失败不影响其余（tracker 追加是尽力而为的旁路）
      }
    }
  }
  return TrackerPushResult(touched, added)
}

/** 开关后上屏文案（把「只做加法」这条语义讲清楚，否则用户会问为什么关了还在连）。 */
fun extraTrackersNote(enabled: Boolean, result: TrackerPushResult): String =
  if (enabled) {
    "兜底 tracker 已开启：新增 ${result.urlsAdded} 条到 ${result.handlesTouched} 个任务"
  } else {
    "兜底 tracker 已关闭：新任务只注入 5 条；已追加的条目不会移除（重启后回到基线）"
  }

/** 会话未运行时的提示（与代理/限速的同款语义）。 */
const val EXTRA_TRACKERS_PENDING_NOTE: String =
  "兜底 tracker 已记录，将在下次添加任务时生效"
