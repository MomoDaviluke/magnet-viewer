package com.bitseed.magnetviewer.engine

/**
 * 端口映射（UPnP/NAT-PMP）的**纯决策层**（诊断 D-2026-09-18；JVM 可测，不碰 libtorrent）。
 *
 * 与 `ProxyConfig.kt` / `RateLimit.kt` 同一种切分：**纯函数放这里**（单测逐分支断言），
 * 到 `SettingsPack` 的 swig 翻译放 [BtEngine]（那里一碰 swig 常量就要加载原生库）。
 *
 * 背景：改造前 `enable_upnp` / `enable_natpmp` 被**硬编码为 false**（当年「只收不做种、
 * 减暴露面」的取舍）。现场代价有两条：
 * 1. 连不上同为 NAT 后的 peer —— swarm 里无开放端口的 peer 占比很高，可连候选一路枯竭，
 *    症状是「peer 数不少但速度起不来」；
 * 2. 做种/上传时无人能连入 → 客户端侧表现就是「做种失败」。
 *
 * 安卓侧比桌面更需要这个开关：手机通常处于运营商 NAT 之后，用户**无法手动做端口转发**
 * （桌面还能引导用户去路由器上转发），客户端开关是唯一手段。
 */
data class PortMappingPlan(
  /** 是否下发 `enable_upnp`。 */
  val enableUpnp: Boolean,
  /** 是否下发 `enable_natpmp`。 */
  val enableNatpmp: Boolean,
)

/**
 * 开关 → 要下发的两个布尔。
 *
 * **两项恒等，绝不允许只开一个**：UPnP 与 NAT-PMP 是两条并列的映射协议（前者走 SSDP
 * 组播，后者走单播 UDP 到网关 5351），路由器支持哪条不确定 —— 只开一条会在另一类
 * 路由器上静默失效（桌面诊断实测本机 SSDP 无响应、网关 5351 回 ICMP 不可达，
 * 两条都不通时只能手动转发）。两条同开是「有胜于无」的最小代价。
 */
fun portMappingPlan(enabled: Boolean): PortMappingPlan =
  PortMappingPlan(enableUpnp = enabled, enableNatpmp = enabled)

/**
 * 开关后的用户可见提示。
 *
 * 蜂窝网络下路由器侧没有 IGD，映射多数会失败 —— 这是**预期结果**而非故障，文案要
 * 先把预期讲清楚，否则用户会以为自己设置错了（这是本项最容易被误报的地方）。
 */
fun portMappingToggleNote(enabled: Boolean): String =
  if (enabled) {
    "端口映射已开启（UPnP/NAT-PMP，需数秒生效）"
  } else {
    "端口映射已关闭：连不上 NAT 后的 peer"
  }

/** 会话未运行时保存设置的提示（与代理/限速的同款语义：记下来，启动时生效）。 */
const val PORT_MAPPING_PENDING_NOTE: String = "端口映射已记录，将在会话启动时生效"

/** 会话启动日志里的一行摘要（事后能核对「开关真的下发了」）。 */
fun portMappingSummary(enabled: Boolean): String = "端口映射=$enabled"
