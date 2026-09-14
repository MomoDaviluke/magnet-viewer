package com.bitseed.magnetviewer.engine

/**
 * 引擎活动阶段（前台服务驻留与唤醒锁的**唯一判据**；纯字符串常量 → JVM 可测）。
 */
const val PHASE_IDLE = "idle"
/** 解析：正在通过 DHT/tracker 取元数据（不下载本体）。 */
const val PHASE_META = "meta"
/** 预览：边下边播的下载进行中。 */
const val PHASE_PREVIEW = "preview"
/** 预览已暂停（清 deadline + 优先级 0 + pause），等用户「继续」。 */
const val PHASE_PAUSED = "paused"
/** convert 档释放后仍在后台缓存（预览转正的持久下载）。 */
const val PHASE_DOWNLOAD = "download"

/**
 * 一次「引擎在干什么」的快照（引擎写、宿主/前台服务读）。
 *
 * 两个派生判据分得开，因为它们对应两件不同的事：
 * - [busy]：**有任务**，前台服务必须驻留（解析/预览/暂停等待继续/后台缓存都算任务）；
 * - [transferActive]：**有活跃传输**，只有这一档才允许持有 PARTIAL_WAKE_LOCK。
 *   解析（只走 DHT 拉元数据）与暂停（句柄已 pause）都不持有唤醒锁。
 */
data class EngineActivity(
  val phase: String = PHASE_IDLE,
  /** 通知标题：预览文件名（或种子名）。 */
  val title: String = "",
  /** 进度百分比 0..100。 */
  val percent: Int = 0,
  val speedKbps: Int = 0,
  val peers: Int = 0,
  /** 给用户的一句话（暂停原因、超时提示等）。 */
  val note: String = "",
) {
  val busy: Boolean
    get() = phase != PHASE_IDLE

  val transferActive: Boolean
    get() = phase == PHASE_PREVIEW || phase == PHASE_DOWNLOAD
}

/**
 * 引擎 → 宿主的**控制面**（前台服务/通知动作调用；实现方是 [BtEngine]）。
 *
 * 为什么要有这一层：前台服务活在应用进程里，但引擎实例由 ViewModel 持有，
 * 服务不能直接抓引擎；两者只通过 [EngineHub] 的这个窄接口耦合。
 */
interface EngineControl {
  /** 周期心跳（1s）：推进预览窗口 + 刷新状态 + 超时判定。 */
  fun tick()

  /** 通知「暂停」：暂停预览（hold 档语义）。 */
  fun onPauseRequest()

  /** 通知「继续」：恢复预览。 */
  fun onResumeRequest()

  /** 通知「停止」：停掉当前任务（按 previewCacheMode 的语义）。 */
  fun onStopRequest()

  /** 前台服务超时（Android 15+ dataSync 6h/24h 上限）：优雅暂停任务并提示。 */
  fun onTimeoutPause()
}

/**
 * 进程级活动快照 + 控制面（单例）。
 *
 * 单例是刻意的：前台服务不属于任何 ViewModel，而引擎实例属于；两者之间的
 * 「谁在干活」只能放在进程级。多引擎实例时**后挂载者胜**（本 demo 只有一个）。
 * 线程安全走 `@Volatile`：写方是引擎（任意线程），读方是服务主线程。
 */
object EngineHub {
  @Volatile
  var activity: EngineActivity = EngineActivity()
    private set

  @Volatile
  var control: EngineControl? = null
    private set

  fun publish(a: EngineActivity) {
    activity = a
  }

  fun attach(c: EngineControl?) {
    control = c
  }

  /** 心跳：返回 true 表示**有引擎挂载**（服务据此判断状态是否可信）。 */
  fun tick(): Boolean {
    val c = control ?: return false
    c.tick()
    return true
  }

  fun requestPause() {
    control?.onPauseRequest()
  }

  fun requestResume() {
    control?.onResumeRequest()
  }

  fun requestStop() {
    control?.onStopRequest()
  }

  fun requestTimeoutPause() {
    control?.onTimeoutPause()
  }

  /** 引擎关闭 / 服务收尾：清空活动态（回到 idle，服务下一次心跳即 stopSelf）。 */
  fun reset() {
    activity = EngineActivity()
  }
}
