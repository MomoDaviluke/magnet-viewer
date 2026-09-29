package com.bitseed.magnetviewer.engine

/**
 * 会话吞吐调参表（性能专项 P-2；与桌面 `core/session.py::THROUGHPUT_SETTINGS` 对齐）。
 *
 * 背景：改造前的会话配置只给了 listen / DHT / LSD / connectionsLimit 三件套，
 * 其余全吃 libtorrent 默认值。那些默认值面向**做种型客户端**调优，与本项目
 * 「只收不做种 + 边下边播」的画像不符——最典型的是 `unchoke_slots_limit=8`
 * （上传槽太少 → 对端不回传 → 下载饿死）与 `prioritize_partial_pieces=false`
 * （半截块不优先补完 → 连续可读前缀凑不齐 → 边下边播卡缓冲）。
 *
 * 本文件只放**纯数据**（键名 + 取值），到 `SettingsPack` 的映射在 [BtEngine]
 * ——那边要用 swig 枚举（native 类），放这里会让 JVM 单测加载不了 native 库。
 * 键名用的就是 `settings_pack.int_types` / `bool_types` 里的 swig 成员名，
 * 拼错会在 BtEngine 的 `when` 里落到 `else` 分支并记日志（不会静默丢弃）。
 *
 * **Android 与桌面的一处已知差异**：libtorrent4j 2.1.0-39 未绑定
 * `cache_size`（磁盘写缓存，桌面设 4096 ≈ 64MB），故此项缺席；Android 侧
 * 靠 `file_pool_size` + `aio_threads` 默认值兜底，属可接受的平台差异。
 */
object EngineTuning {

  /** 连接与 peer 发现（爬坡速度）。 */
  const val CONNECTIONS_LIMIT: Int = 400
  const val CONNECTION_SPEED: Int = 60

  /** 上传槽：BitTorrent 是互惠协议，槽太少 → 对端不回传 → 下载饿死。 */
  const val UNCHOKE_SLOTS_LIMIT: Int = 20
  const val NUM_OPTIMISTIC_UNCHOKE_SLOTS: Int = 2

  /** 并发写盘的打开文件句柄池（默认 40，多任务+预览时不足会反复开关文件）。 */
  const val FILE_POOL_SIZE: Int = 240

  /** 引擎内部节拍（ms）：决定 piece deadline / 限速的反应粒度。 */
  const val TICK_INTERVAL: Int = 250

  /** 队列（active_downloads）重新评估间隔：暂停/恢复任务响应更快。 */
  const val AUTO_MANAGE_INTERVAL: Int = 5

  /** 缓存的已知 peer 数：重启后重连更快。 */
  const val MAX_PEERLIST_SIZE: Int = 6000

  /** 边下边播关键项：优先补完已部分下载的分块。 */
  const val PRIORITIZE_PARTIAL_PIECES: Boolean = true

  /** 显式声明（默认已为真，写出来是为了意图可见）。 */
  const val DHT_AGGRESSIVE_LOOKUPS: Boolean = true

  /**
   * `settings_pack.int_types` 成员名 → 取值（**不含 connections_limit**：它有专用
   * setter `SettingsPack.connectionsLimit()`，随基线设置一起下发，避免两处写同一个键）。
   */
  val ints: Map<String, Int> = linkedMapOf(
    "connection_speed" to CONNECTION_SPEED,
    "unchoke_slots_limit" to UNCHOKE_SLOTS_LIMIT,
    "num_optimistic_unchoke_slots" to NUM_OPTIMISTIC_UNCHOKE_SLOTS,
    "file_pool_size" to FILE_POOL_SIZE,
    "tick_interval" to TICK_INTERVAL,
    "auto_manage_interval" to AUTO_MANAGE_INTERVAL,
    "max_peerlist_size" to MAX_PEERLIST_SIZE,
  )

  /** `settings_pack.bool_types` 成员名 → 取值。 */
  val bools: Map<String, Boolean> = linkedMapOf(
    "prioritize_partial_pieces" to PRIORITIZE_PARTIAL_PIECES,
    "dht_aggressive_lookups" to DHT_AGGRESSIVE_LOOKUPS,
  )

  /** 调参表摘要（日志用）：写进启动日志，事后能核对「参数真的下发了」。 */
  fun summary(): String =
    "吞吐调参 ints=$ints bools=$bools"
}
