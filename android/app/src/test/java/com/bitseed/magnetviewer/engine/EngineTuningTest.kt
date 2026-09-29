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
    assertEquals(EngineTuning.PRIORITIZE_PARTIAL_PIECES, EngineTuning.bools["prioritize_partial_pieces"])
    assertEquals(EngineTuning.DHT_AGGRESSIVE_LOOKUPS, EngineTuning.bools["dht_aggressive_lookups"])
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
      ),
      EngineTuning.ints.keys,
    )
    assertEquals(
      setOf("prioritize_partial_pieces", "dht_aggressive_lookups"),
      EngineTuning.bools.keys,
    )
    assertEquals("connections_limit 走专用 setter，不在表里", null, EngineTuning.ints["connections_limit"])
  }
}
