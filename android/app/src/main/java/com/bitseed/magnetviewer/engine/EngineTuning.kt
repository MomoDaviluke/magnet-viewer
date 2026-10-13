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
 * **分批说明（与桌面同源，逐值对齐）**：
 * - 第一批（P-2 基线）：连接/上传槽/句柄池/节拍/peer 表，本文件初版即有。
 * - 第二批（等待超时专项）：aio/hashing 线程、请求流水线、重连节奏。
 * - 第三批（千兆吞吐专项）：socket 收发缓冲、上行水位线、announce 全发。
 * 2026-10-13 性能排查发现安卓此前**只镜像了第一批**，第二/三批整体缺席——
 * 这是安卓下载速率明显低于桌面的真实根因之一（尤其 `recv_socket_buffer_size`
 * 默认 0=OS 决定，实测单连接吞吐被 64KB 缓冲卡死）。本批全部键的存在性已用
 * `javap` 直读 libtorrent4j 2.1.0-39 jar 逐个核对（见 NOTES §8.3 的核对方法）。
 *
 * **Android 与桌面的一处已知差异**：libtorrent4j 2.1.0-39 未绑定
 * `cache_size`（磁盘写缓存，桌面设 4096 ≈ 64MB），故此项缺席；Android 侧
 * 靠 `file_pool_size` + `aio_threads` 默认值兜底，属可接受的平台差异。
 */
object EngineTuning {

  // ------------------------------------------------------------ 第一批（P-2 基线）

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

  // ------------------------------------------------------------ 第二批（等待超时专项）

  /** 磁盘异步 IO 线程数：稀疏文件 + 多任务 + 边下边播随机块写，写盘排队会反压吞吐。 */
  const val AIO_THREADS: Int = 12

  /** 分块校验线程：校验队列堆积的症状是「速率不低而 have 不涨」（连续前缀不推进）。 */
  const val HASHING_THREADS: Int = 4

  /** 单 peer 在途块请求上限（在途窗口）：高延迟链路下 500 不足以填满 BDP，1500 覆盖千兆。 */
  const val MAX_OUT_REQUEST_QUEUE: Int = 1500

  /** 请求在发送队列里排队超时阈值（秒）：高 BDP 下 3s 太短会退化成重发风暴。 */
  const val REQUEST_QUEUE_TIME: Int = 10

  /** 单块放弃前允许的失败次数：慢链路做种端偶发超时的纯容错增益。 */
  const val MAX_FAILCOUNT: Int = 5

  /** 最小重连间隔（秒）：冷启动首批 peer 失败率高，60s 冷却会把「没连上」升级成「一分钟不再试」。 */
  const val MIN_RECONNECT_TIME: Int = 20

  /** peer 连接超时（秒）：黑洞 peer 不该白占连接槽。 */
  const val PEER_CONNECT_TIMEOUT: Int = 8

  /** 优先抓取与刚下载块同 extent 的邻近块：边下边播要的是顺序性，连续前缀成形更快。 */
  const val PIECE_EXTENT_AFFINITY: Boolean = true

  // ------------------------------------------------------------ 第三批（千兆吞吐专项）

  /**
   * 接收 socket 缓冲（字节）：**千兆第一瓶颈**。默认 0 = OS 决定（实测未连接默认
   * SO_RCVBUF = 64KB），单连接吞吐上限 ≈ 缓冲 ÷ RTT → 64KB@50ms = 1.25MB/s，
   * 跑满千兆要 90 条连接，真实 swarm 的 peer 数远达不到，必须显式放大。
   * 取 2MB 而非理论 BDP(6MB)：多连接分摊后 2MB×400 连接已是内存上限。
   */
  const val RECV_SOCKET_BUFFER_SIZE: Int = 2 * 1024 * 1024

  /** 发送 socket 缓冲（字节）：BT 是互惠协议，上行缓冲憋住 → 对端按上传速率 choke 我们。 */
  const val SEND_SOCKET_BUFFER_SIZE: Int = 1024 * 1024

  /** 上行水位线三件套：服务同一互惠目标（避免被 choke）。 */
  const val SEND_BUFFER_WATERMARK: Int = 3 * 1024 * 1024
  const val SEND_BUFFER_WATERMARK_FACTOR: Int = 150
  const val SEND_BUFFER_LOW_WATERMARK: Int = 64 * 1024

  /**
   * 向所有 tracker / 所有 tier 宣告：磁力链的多 tracker 常同处一个 tier，默认只问一个；
   * 全问一遍等于发现面翻倍——发现到的 peer 数就是吞吐上限。
   */
  const val ANNOUNCE_TO_ALL_TRACKERS: Boolean = true
  const val ANNOUNCE_TO_ALL_TIERS: Boolean = true

  /**
   * `settings_pack.int_types` 成员名 → 取值（**不含 connections_limit**：它有专用
   * setter `SettingsPack.connectionsLimit()`，随基线设置一起下发，避免两处写同一个键）。
   */
  val ints: Map<String, Int> = linkedMapOf(
    // 第一批
    "connection_speed" to CONNECTION_SPEED,
    "unchoke_slots_limit" to UNCHOKE_SLOTS_LIMIT,
    "num_optimistic_unchoke_slots" to NUM_OPTIMISTIC_UNCHOKE_SLOTS,
    "file_pool_size" to FILE_POOL_SIZE,
    "tick_interval" to TICK_INTERVAL,
    "auto_manage_interval" to AUTO_MANAGE_INTERVAL,
    "max_peerlist_size" to MAX_PEERLIST_SIZE,
    // 第二批（等待超时专项）
    "aio_threads" to AIO_THREADS,
    "hashing_threads" to HASHING_THREADS,
    "max_out_request_queue" to MAX_OUT_REQUEST_QUEUE,
    "request_queue_time" to REQUEST_QUEUE_TIME,
    "max_failcount" to MAX_FAILCOUNT,
    "min_reconnect_time" to MIN_RECONNECT_TIME,
    "peer_connect_timeout" to PEER_CONNECT_TIMEOUT,
    // 第三批（千兆吞吐专项）
    "recv_socket_buffer_size" to RECV_SOCKET_BUFFER_SIZE,
    "send_socket_buffer_size" to SEND_SOCKET_BUFFER_SIZE,
    "send_buffer_watermark" to SEND_BUFFER_WATERMARK,
    "send_buffer_watermark_factor" to SEND_BUFFER_WATERMARK_FACTOR,
    "send_buffer_low_watermark" to SEND_BUFFER_LOW_WATERMARK,
  )

  /** `settings_pack.bool_types` 成员名 → 取值。 */
  val bools: Map<String, Boolean> = linkedMapOf(
    "prioritize_partial_pieces" to PRIORITIZE_PARTIAL_PIECES,
    "dht_aggressive_lookups" to DHT_AGGRESSIVE_LOOKUPS,
    "piece_extent_affinity" to PIECE_EXTENT_AFFINITY,
    "announce_to_all_trackers" to ANNOUNCE_TO_ALL_TRACKERS,
    "announce_to_all_tiers" to ANNOUNCE_TO_ALL_TIERS,
  )

  /** 调参表摘要（日志用）：写进启动日志，事后能核对「参数真的下发了」。 */
  fun summary(): String =
    "吞吐调参 ints=$ints bools=$bools"
}
