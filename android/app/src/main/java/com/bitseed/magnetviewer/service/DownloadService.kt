package com.bitseed.magnetviewer.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import androidx.core.app.NotificationCompat
import com.bitseed.magnetviewer.MainActivity
import com.bitseed.magnetviewer.R
import com.bitseed.magnetviewer.data.AppLog
import com.bitseed.magnetviewer.engine.EngineActivity
import com.bitseed.magnetviewer.engine.EngineHub
import com.bitseed.magnetviewer.engine.PHASE_META
import com.bitseed.magnetviewer.engine.PHASE_PAUSED

/**
 * 下载/预览前台服务（`foregroundServiceType="dataSync"`）。
 *
 * 存在理由：解析/下载/预览必须在**灭屏 / 退到后台**时继续跑，否则系统随时把进程冻结。
 *
 * 三条纪律：
 * 1. **驻留判据是引擎的活动快照**（[EngineHub.activity]）：有任务（解析/预览/暂停等待继续/
 *    convert 后台缓存）就驻留，`busy == false` 连续若干拍后 `stopSelf()`；
 * 2. **只在有活跃传输时持 PARTIAL_WAKE_LOCK**（`transferActive`）：解析（只走 DHT 拿元数据）
 *    与暂停（句柄已 pause）都不持锁；退出路径（onDestroy / 超时 / stopSelf）**必释放**；
 * 3. **onTimeout 必须实现**：Android 15+ 对 dataSync 有 6h/24h 累计上限，超时回调到达后
 *    应用只有几秒钟自行退场，否则 ANR/崩溃。这里选择「优雅暂停任务 + 提示 + 自退」。
 *
 * 通知动作用 `PendingIntent.getService`（服务已在运行，投递意图不受后台启动限制）。
 */
class DownloadService : Service() {

  private val handler = Handler(Looper.getMainLooper())
  private var wakeLock: PowerManager.WakeLock? = null
  private var foregroundStarted = false
  private var idleTicks = 0
  private var ticksWithoutControl = 0

  /** 1 秒心跳：拉引擎刷新 → 更新通知 → 同步唤醒锁 → 判断是否该退场。 */
  private val heartbeat =
    object : Runnable {
      override fun run() {
        val hasControl =
          try {
            EngineHub.tick()
          } catch (e: Throwable) {
            log("心跳异常：" + e.message)
            false
          }
        val a = EngineHub.activity
        try {
          ensureForeground(a)
        } catch (e: Throwable) {
          log("更新前台通知失败：" + e.message)
        }
        syncWakeLock(a)
        if (!hasControl) {
          // 引擎没挂上来（进程里没有活跃引擎）：状态不可信，短暂宽限后退场
          ticksWithoutControl++
          if (ticksWithoutControl >= CONTROL_GRACE_TICKS) {
            log("无引擎挂载 ${ticksWithoutControl}s：退出前台服务")
            stopSelf()
            return
          }
        } else {
          ticksWithoutControl = 0
        }
        if (a.busy) {
          idleTicks = 0
        } else {
          idleTicks++
          if (idleTicks >= IDLE_TICKS_TO_STOP) {
            log("无任务（连续 ${idleTicks}s 空闲）：stopSelf()")
            stopSelf()
            return
          }
        }
        handler.postDelayed(this, TICK_MS)
      }
    }

  override fun onCreate() {
    super.onCreate()
    ensureChannels()
  }

  override fun onBind(intent: Intent?): IBinder? = null

  override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    ensureChannels()
    when (intent?.action) {
      ACTION_STOP -> {
        log("收到「停止」动作")
        EngineHub.requestStop()
      }
      ACTION_PAUSE -> {
        log("收到「暂停」动作")
        EngineHub.requestPause()
      }
      ACTION_RESUME -> {
        log("收到「继续」动作")
        EngineHub.requestResume()
      }
      else -> {}
    }
    // 必须在 startForegroundService 后 5 秒内进入前台；这里同步完成
    ensureForeground(EngineHub.activity)
    handler.removeCallbacks(heartbeat)
    handler.post(heartbeat)
    return START_NOT_STICKY
  }

  override fun onDestroy() {
    log("onDestroy：释放唤醒锁并移除心跳")
    handler.removeCallbacks(heartbeat)
    releaseWakeLock()
    super.onDestroy()
  }

  /**
   * Android 14（API 34）的超时回调。
   */
  override fun onTimeout(startId: Int) {
    onForegroundTimeout()
  }

  /**
   * Android 15+（API 35）的带类型超时回调：`dataSync` 类型 24 小时内累计 6 小时上限到达时
   * 由系统调用。**必须在几秒内自行退场**，否则会 ANR / 崩溃。
   */
  override fun onTimeout(startId: Int, fgsType: Int) {
    onForegroundTimeout()
  }

  /** 超时处理：优雅暂停任务 → 提示用户 → 释放唤醒锁 → 摘掉前台通知并退出。 */
  private fun onForegroundTimeout() {
    log("前台服务超时（dataSync 6h/24h 上限）：暂停任务并退场")
    try {
      EngineHub.requestTimeoutPause()
    } catch (e: Throwable) {
      log("超时暂停任务失败：" + e.message)
    }
    try {
      notifyTimeout()
    } catch (e: Throwable) {
      log("超时提示通知失败：" + e.message)
    }
    releaseWakeLock()
    try {
      if (foregroundStarted) stopForeground(STOP_FOREGROUND_REMOVE)
    } catch (e: Throwable) {
      log("stopForeground 异常：" + e.message)
    }
    foregroundStarted = false
    stopSelf()
  }

  // ---------------------------------------------------------------- 前台通知

  private fun ensureForeground(a: EngineActivity) {
    val n = buildNotification(a)
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
      startForeground(NOTIFY_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
    } else {
      startForeground(NOTIFY_ID, n)
    }
    foregroundStarted = true
  }

  /**
   * 通知内容：文件名（标题）/ 进度 % / 速度 / peer，附「暂停-继续」与「停止」两个动作。
   */
  private fun buildNotification(a: EngineActivity): Notification {
    val title = a.title.ifEmpty { getString(R.string.app_name) }
    val phaseText =
      when (a.phase) {
        PHASE_PAUSED -> "已暂停"
        PHASE_META -> "解析中"
        else -> "${a.percent}% · ${a.speedKbps} KB/s · peer ${a.peers}"
      }
    val text = if (a.note.isNotEmpty()) "${a.note}｜$phaseText" else phaseText
    val builder =
      NotificationCompat.Builder(this, CHANNEL_ID)
        .setSmallIcon(android.R.drawable.stat_sys_download)
        .setContentTitle(title)
        .setContentText(text)
        .setStyle(NotificationCompat.BigTextStyle().bigText(text))
        .setOngoing(true)
        .setOnlyAlertOnce(true)
        .setShowWhen(false)
        .setCategory(NotificationCompat.CATEGORY_SERVICE)
        .setContentIntent(contentIntent())
    if (a.phase == PHASE_PAUSED) {
      builder.setProgress(100, a.percent, false)
      builder.addAction(0, "继续", serviceIntent(ACTION_RESUME, REQ_RESUME))
    } else {
      // 进度不确定（元数据阶段）时用 indeterminate，避免假装 0%
      builder.setProgress(100, a.percent, a.phase == PHASE_META)
      builder.addAction(0, "暂停", serviceIntent(ACTION_PAUSE, REQ_PAUSE))
    }
    builder.addAction(0, "停止", serviceIntent(ACTION_STOP, REQ_STOP))
    return builder.build()
  }

  private fun contentIntent(): PendingIntent {
    val i = Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    return PendingIntent.getActivity(this, REQ_CONTENT, i, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
  }

  private fun serviceIntent(action: String, requestCode: Int): PendingIntent {
    val i = Intent(this, DownloadService::class.java).setAction(action)
    return PendingIntent.getService(this, requestCode, i, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
  }

  /** 超时提示：普通（非前台）通知，告诉用户「下载已被系统时限暂停」。 */
  private fun notifyTimeout() {
    val n =
      NotificationCompat.Builder(this, CHANNEL_ALERT_ID)
        .setSmallIcon(android.R.drawable.stat_sys_download)
        .setContentTitle(getString(R.string.app_name))
        .setContentText(FGS_TIMEOUT_TEXT)
        .setStyle(NotificationCompat.BigTextStyle().bigText(FGS_TIMEOUT_TEXT))
        .setAutoCancel(true)
        .setContentIntent(contentIntent())
        .build()
    val nm = getSystemService(NotificationManager::class.java)
    nm?.notify(TIMEOUT_NOTIFY_ID, n)
  }

  private fun ensureChannels() {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
    val nm = getSystemService(NotificationManager::class.java) ?: return
    val fg = NotificationChannel(CHANNEL_ID, "下载/预览运行中", NotificationManager.IMPORTANCE_LOW)
    fg.description = "磁力链解析、下载与预览期间常驻通知（带暂停/停止）"
    fg.setShowBadge(false)
    val alert = NotificationChannel(CHANNEL_ALERT_ID, "下载状态提醒", NotificationManager.IMPORTANCE_DEFAULT)
    alert.description = "前台服务到达系统时限等需要用户知道的提示"
    nm.createNotificationChannels(listOf(fg, alert))
  }

  // ---------------------------------------------------------------- 唤醒锁

  /** 只在有**活跃传输**时持锁（解析/暂停都不持）；心跳每秒续一次超时。 */
  private fun syncWakeLock(a: EngineActivity) {
    if (a.transferActive) acquireWakeLock() else releaseWakeLock()
  }

  private fun acquireWakeLock() {
    try {
      val pm = getSystemService(PowerManager::class.java) ?: return
      val lock =
        wakeLock
          ?: pm
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, WAKE_LOCK_TAG)
            .also {
              it.setReferenceCounted(false)
              wakeLock = it
            }
      if (!lock.isHeld) log("获取 PARTIAL_WAKE_LOCK（活跃下载中）")
      lock.acquire(WAKE_LOCK_MS)
    } catch (e: Throwable) {
      log("获取唤醒锁失败：" + e.message)
    }
  }

  private fun releaseWakeLock() {
    val lock = wakeLock ?: return
    try {
      if (lock.isHeld) {
        lock.release()
        log("释放 PARTIAL_WAKE_LOCK")
      }
    } catch (e: Throwable) {
      log("释放唤醒锁异常：" + e.message)
    } finally {
      wakeLock = null
    }
  }

  /** 统一日志出口：走 [AppLog]（设置页「运行日志」的真开关，修 I-02）。 */
  private fun log(message: String) = AppLog.log(message)

  companion object {
    private const val TAG = "MagnetViewer"
    private const val CHANNEL_ID = "magnetviewer.foreground"
    private const val CHANNEL_ALERT_ID = "magnetviewer.alert"
    const val NOTIFY_ID = 0x4D47_01 // 前台通知
    const val TIMEOUT_NOTIFY_ID = 0x4D47_02
    const val FGS_TIMEOUT_TEXT = "前台服务已到系统时限，下载已暂停"

    const val ACTION_START = "com.bitseed.magnetviewer.action.START"
    const val ACTION_STOP = "com.bitseed.magnetviewer.action.STOP"
    const val ACTION_PAUSE = "com.bitseed.magnetviewer.action.PAUSE"
    const val ACTION_RESUME = "com.bitseed.magnetviewer.action.RESUME"

    private const val REQ_CONTENT = 1
    private const val REQ_STOP = 2
    private const val REQ_PAUSE = 3
    private const val REQ_RESUME = 4

    private const val TICK_MS = 1000L
    private const val IDLE_TICKS_TO_STOP = 3

    /** 引擎未挂载的宽限拍数（UI 先起服务、引擎随后才建号的竞态窗口）。 */
    private const val CONTROL_GRACE_TICKS = 10
    private const val WAKE_LOCK_TAG = "magnetviewer:download"
    private const val WAKE_LOCK_MS = 60_000L

    /**
     * 静态入口：开始驻留（解析/下载/预览开始时调用，**必须在应用处于前台时调用**）。
     * 幂等；已在前台服务状态时只是把心跳重新排一遍。
     */
    fun start(context: Context) {
      try {
        val i = Intent(context, DownloadService::class.java).setAction(ACTION_START)
        context.startForegroundService(i)
        AppLog.log("DownloadService.start：已请求前台服务")
      } catch (e: Throwable) {
        // 后台启动前台服务会被系统拒绝（ForegroundServiceStartNotAllowedException）：
        // 不崩溃，只记日志（任务本身仍在应用进程里跑，只是不驻留）
        AppLog.warn("DownloadService.start 失败（可能处于后台）：" + e.message)
      }
    }

    /**
     * 静态入口：**无任务**时让服务退场（有任务时什么都不做）。
     * 用 stopService 而不是再投递意图：不需要（也不应该）把可能没在跑的服务拉起来。
     */
    fun stopIfIdle(context: Context) {
      if (EngineHub.activity.busy) {
        AppLog.log("DownloadService.stopIfIdle：仍有任务，继续驻留")
        return
      }
      try {
        context.stopService(Intent(context, DownloadService::class.java))
        AppLog.log("DownloadService.stopIfIdle：无任务，已退场")
      } catch (e: Throwable) {
        AppLog.warn("DownloadService.stopIfIdle 失败：" + e.message)
      }
    }
  }
}
