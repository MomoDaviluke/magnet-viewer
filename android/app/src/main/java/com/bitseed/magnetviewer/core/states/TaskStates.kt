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

/** 元数据获取超时（秒）：超时判定为资源无做种（桌面 `registry.METADATA_TIMEOUT`）。 */
const val METADATA_TIMEOUT_SECONDS: Int = 90
