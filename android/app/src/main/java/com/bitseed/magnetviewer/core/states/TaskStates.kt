package com.bitseed.magnetviewer.core.states

/** 任务生命周期状态机常量（桌面 `core/states.py` 的逐字对齐）。 */
const val STATE_QUEUED = "QUEUED"
const val STATE_META_FETCH = "META_FETCH"
const val STATE_VALIDATE = "VALIDATE"
const val STATE_DOWNLOADING = "DOWNLOADING"
const val STATE_PAUSED = "PAUSED"
const val STATE_COMPLETED = "COMPLETED"
const val STATE_STOPPED = "STOPPED"
const val STATE_FAILED = "FAILED"
const val STATE_SEEDING = "SEEDING"
const val STATE_DELETED = "DELETED"

/** 内部态：仅查看清单/预览（review 记录专用，不持久化）。 */
const val STATE_READY = "READY"

val DOWNLOAD_STATES: Set<String> =
  setOf(
    STATE_QUEUED,
    STATE_META_FETCH,
    STATE_VALIDATE,
    STATE_DOWNLOADING,
    STATE_PAUSED,
    STATE_COMPLETED,
    STATE_STOPPED,
    STATE_FAILED,
    STATE_SEEDING,
    STATE_DELETED,
  )

/** 公共 tracker，提升冷门磁力链的 peer 发现率。 */
val BOOTSTRAP_TRACKERS: List<String> =
  listOf(
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
  )

/**
 * 兜底 tracker（HTTP/HTTPS，**与 [BOOTSTRAP_TRACKERS] 分开**，逐字对齐桌面
 * `core/states.py: FALLBACK_TRACKERS`）。
 *
 * 为什么需要（诊断 D-2026-09-18，安卓侧同步）：本机实测 UDP 出站大面积受阻
 * （5 个公共 DHT 引导节点仅 1 个响应），`udp://` tracker 大概率同样不可达；
 * 而磁力链若不带 `tr=` 参数，peer 发现就只剩 DHT 一条路——在移动网络下
 * （运营商常对 UDP 限速/丢包）这条路会非常慢。这里补一组走 TCP 443/80 的
 * tracker 作为**第二条独立通道**，由 `extra_trackers` 开关控制（默认关闭，
 * 保持既有 5 条 udp 的行为与相关断言不变）。
 */
val FALLBACK_TRACKERS: List<String> =
  listOf(
    "http://tracker.opentrackr.org:1337/announce",
    "https://tracker.opentrackr.org:443/announce",
    "http://open.tracker.cl:80/announce",
    "http://tracker.openbittorrent.com:80/announce",
    "https://tracker.torrent.eu.org:443/announce",
  )

/**
 * 加种时要注入的 tracker 列表（**唯一决策点**，纯函数 → JVM 可测）。
 *
 * `extraTrackers=false` → 保持基线 5 条 udp（默认路径逐字不变）；
 * `true` → 追加 [FALLBACK_TRACKERS]，顺序与桌面 `resolver` 的
 * `BOOTSTRAP_TRACKERS + FALLBACK_TRACKERS` 完全一致（前 5 条仍是 udp）。
 *
 * 这里是**加种路径**的注入面；已在跑的任务由 `BtEngine.applyExtraTrackers` 经
 * `TrackerPush.pushTrackers` 追加（`torrent_handle.addTracker`，只做加法），
 * 所以用户开开关后**不必重新添加任务**。
 */
fun trackersFor(extraTrackers: Boolean): List<String> =
  if (extraTrackers) BOOTSTRAP_TRACKERS + FALLBACK_TRACKERS else BOOTSTRAP_TRACKERS

/** 元数据获取超时（秒）：超时判定为资源无做种（桌面 `registry.METADATA_TIMEOUT`）。 */
const val METADATA_TIMEOUT_SECONDS: Int = 90
