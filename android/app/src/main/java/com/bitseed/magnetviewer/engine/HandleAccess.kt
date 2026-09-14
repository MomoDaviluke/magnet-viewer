package com.bitseed.magnetviewer.engine

/**
 * 句柄状态读取的窄接口。**本文件不引用 android / libtorrent4j 任何类型**，
 * 因此可以在 JVM 单测（`:app:testDebugUnitTest`，无需设备）里用纯 Kotlin 替身验证
 * 「无效句柄不调 status()」这条分支。
 *
 * 为什么必须这样切：`TorrentHandle.status()` 是 swig 原生调用，对**失效句柄**调用会
 * 在 `std::__shared_weak_count::lock()` 上 SIGSEGV（原生崩溃，`try/catch(Throwable)`
 * **接不住**、进程直接死）。唯一防线是调用前先问 `TorrentHandle.isValid()`
 * （javadoc：`is_valid` 永不抛）。
 */
interface HandleProbe {
  /** 对应 `TorrentHandle.isValid()`。 */
  fun isValid(): Boolean

  /** 只在 [isValid] 返回 true 时被调用（读取当前一个 status 快照）。 */
  fun statusReading(): StatusReading
}

/**
 * [HandleProbe.statusReading] 的结果（只含普通类型，便于单测断言）。
 *
 * [progress] 是 `TorrentStatus.progress()`（0..1，按「想要的数据」计）：前台服务用它
 * 判断 convert 档释放后的后台缓存是否已经下完（>= 1 即无任务）。默认 1f = 当作已完成，
 * 这样旧调用点（只关心 peer/速度）不受影响。
 */
data class StatusReading(
  val peers: Int,
  val seeds: Int,
  val downloadRateKbps: Int,
  val progress: Float = 1f,
)

/** 句柄读取结果。 */
sealed interface HandleReadResult {
  /** 还没有任务（没有句柄）。 */
  object NoHandle : HandleReadResult

  /** 读到了状态。 */
  data class Ok(val reading: StatusReading) : HandleReadResult

  /** 句柄失效：**没有**触碰 status()；宿主应停止轮询并报错。 */
  data class Invalid(val message: String) : HandleReadResult

  /** 读状态时抛了 Java 异常（已被接住，进程不会死）。 */
  data class Failed(val message: String) : HandleReadResult
}

/** 句柄失效时的中文错误文本（UI 直接显示）。 */
const val HANDLE_INVALID_TEXT: String = "会话句柄已失效（种子被移除或会话已停止）"

/**
 * **纯函数**：先判句柄有效，再读状态。
 *
 * 顺序是硬约束：`isValid() == false` 时**绝不**调用 [HandleProbe.statusReading]，
 * 否则会走到 libtorrent4j 的原生 status() 上崩溃。
 */
fun readHandleStatus(probe: HandleProbe?): HandleReadResult {
  if (probe == null) return HandleReadResult.NoHandle
  val valid =
    try {
      probe.isValid()
    } catch (e: Throwable) {
      // swig 包装对象被 delete() 之后调方法会抛 IllegalStateException 之类
      return HandleReadResult.Failed(e.let { it.javaClass.simpleName + ": " + (it.message ?: "") })
    }
  if (!valid) return HandleReadResult.Invalid(HANDLE_INVALID_TEXT)
  return try {
    HandleReadResult.Ok(probe.statusReading())
  } catch (e: Throwable) {
    HandleReadResult.Failed(e.let { it.javaClass.simpleName + ": " + (it.message ?: "") })
  }
}
