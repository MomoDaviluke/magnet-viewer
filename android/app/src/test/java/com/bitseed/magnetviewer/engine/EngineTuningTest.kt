package com.bitseed.magnetviewer.engine

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 吞吐调参表取值冻结（JVM；[EngineTuning] 是纯数据，不依赖 native）。
 *
 * 这些数字是**承诺值**：改它们等于改会话的对外行为，必须同步桌面
 * `core/session.py::THROUGHPUT_SETTINGS` 与两边文档——这里红掉就是提醒。
 */
class EngineTuningTest {

  @Test
  fun constants_areFrozen() {
    assertEquals(400, EngineTuning.CONNECTIONS_LIMIT)
    assertEquals(60, EngineTuning.CONNECTION_SPEED)
    assertEquals(20, EngineTuning.UNCHOKE_SLOTS_LIMIT)
    assertEquals(2, EngineTuning.NUM_OPTIMISTIC_UNCHOKE_SLOTS)
    assertEquals(240, EngineTuning.FILE_POOL_SIZE)
    assertEquals(250, EngineTuning.TICK_INTERVAL)
    assertEquals(5, EngineTuning.AUTO_MANAGE_INTERVAL)
    assertEquals(6000, EngineTuning.MAX_PEERLIST_SIZE)
    assertTrue("边下边播关键项：必须为真", EngineTuning.PRIORITIZE_PARTIAL_PIECES)
    assertTrue(EngineTuning.DHT_AGGRESSIVE_LOOKUPS)
    // 第二批（等待超时专项，2026-10-13 补齐；与桌面 THROUGHPUT_SETTINGS 逐值对齐）
    assertEquals(12, EngineTuning.AIO_THREADS)
    assertEquals(4, EngineTuning.HASHING_THREADS)
    assertEquals(1500, EngineTuning.MAX_OUT_REQUEST_QUEUE)
    assertEquals(10, EngineTuning.REQUEST_QUEUE_TIME)
    assertEquals(5, EngineTuning.MAX_FAILCOUNT)
    assertEquals(20, EngineTuning.MIN_RECONNECT_TIME)
    assertEquals(8, EngineTuning.PEER_CONNECT_TIMEOUT)
    assertTrue(EngineTuning.PIECE_EXTENT_AFFINITY)
    // 第三批（千兆吞吐专项，2026-10-13 补齐）
    assertEquals(2 * 1024 * 1024, EngineTuning.RECV_SOCKET_BUFFER_SIZE)
    assertEquals(1024 * 1024, EngineTuning.SEND_SOCKET_BUFFER_SIZE)
    assertEquals(3 * 1024 * 1024, EngineTuning.SEND_BUFFER_WATERMARK)
    assertEquals(150, EngineTuning.SEND_BUFFER_WATERMARK_FACTOR)
    assertEquals(64 * 1024, EngineTuning.SEND_BUFFER_LOW_WATERMARK)
    assertTrue(EngineTuning.ANNOUNCE_TO_ALL_TRACKERS)
    assertTrue(EngineTuning.ANNOUNCE_TO_ALL_TIERS)
  }

  @Test
  fun maps_areConsistentWithConstants() {
    assertEquals(EngineTuning.CONNECTION_SPEED, EngineTuning.ints["connection_speed"])
    assertEquals(EngineTuning.UNCHOKE_SLOTS_LIMIT, EngineTuning.ints["unchoke_slots_limit"])
    assertEquals(
      EngineTuning.NUM_OPTIMISTIC_UNCHOKE_SLOTS,
      EngineTuning.ints["num_optimistic_unchoke_slots"],
    )
    assertEquals(EngineTuning.FILE_POOL_SIZE, EngineTuning.ints["file_pool_size"])
    assertEquals(EngineTuning.TICK_INTERVAL, EngineTuning.ints["tick_interval"])
    assertEquals(EngineTuning.AUTO_MANAGE_INTERVAL, EngineTuning.ints["auto_manage_interval"])
    assertEquals(EngineTuning.MAX_PEERLIST_SIZE, EngineTuning.ints["max_peerlist_size"])
    assertEquals(EngineTuning.AIO_THREADS, EngineTuning.ints["aio_threads"])
    assertEquals(EngineTuning.HASHING_THREADS, EngineTuning.ints["hashing_threads"])
    assertEquals(EngineTuning.MAX_OUT_REQUEST_QUEUE, EngineTuning.ints["max_out_request_queue"])
    assertEquals(EngineTuning.REQUEST_QUEUE_TIME, EngineTuning.ints["request_queue_time"])
    assertEquals(EngineTuning.MAX_FAILCOUNT, EngineTuning.ints["max_failcount"])
    assertEquals(EngineTuning.MIN_RECONNECT_TIME, EngineTuning.ints["min_reconnect_time"])
    assertEquals(EngineTuning.PEER_CONNECT_TIMEOUT, EngineTuning.ints["peer_connect_timeout"])
    assertEquals(EngineTuning.RECV_SOCKET_BUFFER_SIZE, EngineTuning.ints["recv_socket_buffer_size"])
    assertEquals(EngineTuning.SEND_SOCKET_BUFFER_SIZE, EngineTuning.ints["send_socket_buffer_size"])
    assertEquals(EngineTuning.SEND_BUFFER_WATERMARK, EngineTuning.ints["send_buffer_watermark"])
    assertEquals(
      EngineTuning.SEND_BUFFER_WATERMARK_FACTOR,
      EngineTuning.ints["send_buffer_watermark_factor"],
    )
    assertEquals(EngineTuning.SEND_BUFFER_LOW_WATERMARK, EngineTuning.ints["send_buffer_low_watermark"])
    assertEquals(EngineTuning.PRIORITIZE_PARTIAL_PIECES, EngineTuning.bools["prioritize_partial_pieces"])
    assertEquals(EngineTuning.DHT_AGGRESSIVE_LOOKUPS, EngineTuning.bools["dht_aggressive_lookups"])
    assertEquals(EngineTuning.PIECE_EXTENT_AFFINITY, EngineTuning.bools["piece_extent_affinity"])
    assertEquals(EngineTuning.ANNOUNCE_TO_ALL_TRACKERS, EngineTuning.bools["announce_to_all_trackers"])
    assertEquals(EngineTuning.ANNOUNCE_TO_ALL_TIERS, EngineTuning.bools["announce_to_all_tiers"])
    assertEquals(
      "键名必须与 BtEngine.intTypeOf 的映射一一对应（漏了会被记日志并跳过）",
      setOf(
        "connection_speed",
        "unchoke_slots_limit",
        "num_optimistic_unchoke_slots",
        "file_pool_size",
        "tick_interval",
        "auto_manage_interval",
        "max_peerlist_size",
        "aio_threads",
        "hashing_threads",
        "max_out_request_queue",
        "request_queue_time",
        "max_failcount",
        "min_reconnect_time",
        "peer_connect_timeout",
        "recv_socket_buffer_size",
        "send_socket_buffer_size",
        "send_buffer_watermark",
        "send_buffer_watermark_factor",
        "send_buffer_low_watermark",
      ),
      EngineTuning.ints.keys,
    )
    assertEquals(
      "键名必须与 BtEngine.boolTypeOf 的映射一一对应",
      setOf(
        "prioritize_partial_pieces",
        "dht_aggressive_lookups",
        "piece_extent_affinity",
        "announce_to_all_trackers",
        "announce_to_all_tiers",
      ),
      EngineTuning.bools.keys,
    )
  }
}
