"""libtorrent 会话生命周期与后台告警循环（fetcher 重构阶段 2 抽出）。

职责边界（阶段 2）：
    本模块只负责**会话本身**——会话配置构造与创建（端口冲突回退随机端口）、
    代理/限速的热更新、退出清理（落盘 + 摘句柄 + 置空），以及后台 alert 循环
    的三件事：告警分发、per-task 元数据超时看门狗、fastresume 周期脏写。

    任务注册表增删改、任务 CRUD、持久化、``torrent_info`` 解析都不归它管：
    这些能力由调用方**显式注入**（见 :class:`SessionDeps`），因此本模块
    **不反向依赖 fetcher**，杜绝循环导入。

为什么依赖都用 getter/setter 而不是持有 SessionManager：
    会话成员（``_ses`` / ``_running`` / ``_thread`` / ``_last_resume_sweep``）
    会被整体重绑定或置空，只有运行时取值才拿得到最新对象；写成 ``ses_get()``
    这类回调也让重试逻辑（端口回退）与测试替身都成了可能。

失败策略：生命周期路径上的异常一律就地 ``log_warning`` / ``log_exception``——
启动要能退化成可用会话（端口回退），退出绝不能卡住进程或吞掉用户数据。
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import libtorrent as lt

from . import proxycheck
from .config import lt_proxy_settings
from .logutil import log_exception, log_warning
from .states import (STATE_COMPLETED, STATE_DOWNLOADING, STATE_FAILED,
                     STATE_PAUSED, STATE_STOPPED)
from .taskstore import atomic_write_bytes

ALERT_POLL_INTERVAL = 0.15   # 告警轮询间隔（秒）
SHUTDOWN_JOIN_TIMEOUT = 2.0  # 等告警线程消化残留告警的上限（秒）
RESUME_SWEEP_INTERVAL = 60.0  # fastresume 周期脏写间隔（libtorrent 建议 ≥1min）

DHT_STATE_FILE = ".dht_state"
# 只持久化 DHT 路由表（= 已知节点），**不带** settings —— 会话配置由
# build_session_settings 全权构造，从磁盘回灌旧 settings 会盖掉本次的
# 限速/代理/吞吐参数（历史教训：代理切回直连后 tracker 仍走旧代理）。
DHT_STATE_FLAGS = int(lt.save_state_flags_t.save_dht_state)

# 吞吐/响应专项（性能专项 P-2）：基线只给了 listen / dht / connections_limit
# 三件套，其余全吃 libtorrent 默认值——那些默认值面向「做种型客户端」调优，
# 与本项目「只收不做种 + 边下边播」的画像不符。逐项依据：
#   connections_limit    300→400：peer 上限，配合 connection_speed 提升爬坡；
#   connection_speed      30→60：每秒新建连接数，直接决定「多久铺满 peer」；
#   unchoke_slots_limit    8→20：我们 unchoke 的对端数。BitTorrent 是互惠
#                                协议，上传槽太少 → 对端不回传 → 下载饿死；
#   num_optimistic_unchoke_slots 0→2：乐观 unchoke（默认关闭），定期给新对端
#                                一次机会，用于发现更快的上传者；
#   file_pool_size        40→240：多任务并发写盘的打开文件句柄池，不足会
#                                反复开关文件（本项目默认 3 并发 + 预览）；
#   cache_size         2048→4096：磁盘写缓存（16KiB 块，约 64MB），削峰填谷，
#                                减少写盘抖动导致的请求排队；
#   prioritize_partial_pieces False→True：**边下边播关键项**——优先补完已部分
#                                下载的分块，连续可读前缀更快凑齐；
#   tick_interval        500→250：引擎内部节拍（ms），决定 piece deadline /
#                                限速的反应粒度，500ms 对百毫秒级点播偏粗；
#   auto_manage_interval   30→5：队列调度（active_downloads）的重新评估间隔，
#                                暂停/恢复任务的响应更快；
#   max_peerlist_size  3000→6000：缓存的已知 peer 数，重启后重连更快（重启
#                                恢复靠的就是这张表）。
# 第二批（2026-09-17 等待超时专项，键存在性已由 diag_wait_timeout.py D4
# 用真会话 get_settings() 逐项复核——拼错的键会被 libtorrent 静默丢弃）：
#   aio_threads            4→8：磁盘异步 IO 线程数。本项目是「稀疏文件 +
#                              多任务 + 边下边播随机块写」，写盘排队会直接
#                              反压下载吞吐；8 线程给足并发写余量。
#   hashing_threads        1→2：分块校验线程。校验是纯 CPU 重活（100MB/s 级），
#                              单线程时校验队列一堆积，已到手的块迟迟不入
#                              「have」位图 —— 表现为下载速度不低但**连续前缀
#                              不涨**，即「等待头部数据」迟迟不满足。
#   piece_extent_affinity False→True：优先抓取与**刚下载块同 extent** 的邻近块。
#                              默认关闭是为「随机顺序型」做种客户端优化；本项目
#                              要的是顺序性（连续前缀才可播），打开后连续前缀
#                              成形更快、seek 后也更少出现「东一块西一块」。
#   max_out_request_queue  500→1500：单 peer 在途块请求上限（**在途窗口**）。
#                              高延迟链路下 500 条不足以填满带宽时延积（BDP）：
#                              1500 × 16KiB = 23.4MB，覆盖到 200ms RTT 的千兆
#                              BDP（diag_throughput.py T6 的判据就钉在这里）。
#                              对内存影响可忽略——它数的是请求描述符，不是数据
#                              缓冲，且本项目 peer 数少、队列极少被打满。
#   max_failcount          3→5：单块在放弃前允许的失败次数。慢链路上做种端
#                              偶发超时会把块提前判死并进入「不可再请求」态，
#                              放宽后是**纯容错增益**（仍以 piece 超时兜底）。
THROUGHPUT_SETTINGS: dict = {
    "connections_limit": 400,
    "connection_speed": 60,
    "unchoke_slots_limit": 20,
    "num_optimistic_unchoke_slots": 2,
    "file_pool_size": 240,
    "cache_size": 4096,
    "prioritize_partial_pieces": True,
    "tick_interval": 250,
    "auto_manage_interval": 5,
    "max_peerlist_size": 6000,
    "dht_aggressive_lookups": True,
    "aio_threads": 8,
    "hashing_threads": 2,
    "piece_extent_affinity": True,
    "max_out_request_queue": 1500,
    "max_failcount": 5,
    # 第三批（2026-09-17 千兆吞吐专项）。键存在性与默认值由
    # `diag_throughput.py T1` 用真会话 get_settings() 逐项复核（拼错的键会被
    # libtorrent 静默丢弃，不报错），每条都附「为什么是本项而非别的」：
    #   recv_socket_buffer_size  0→2MB：**千兆第一瓶颈**。默认 0 = 让 OS 决定，
    #                             本机实测未连接默认 SO_RCVBUF = 64KB；单连接
    #                             吞吐上限 ≈ 缓冲 ÷ RTT，64KB @50ms = **1.25MB/s**
    #                             → 跑满千兆要 90 条连接（T5 实测）。真实磁力链
    #                             swarm 的 peer 数远达不到，故必须显式放大。
    #                             取 2MB 而非理论 BDP(6MB)：多连接分摊后 2MB/连接
    #                             ×400 连接 = 800MB 已是内存上限，再大是浪费。
    #   send_socket_buffer_size  0→1MB：BT 是互惠协议（tit-for-tat），上行发送
    #                             缓冲憋住 → 对端按我们的上传速率 choke 我们。
    #                             「只收不做种」的客户端最容易吃这个亏，而
    #                             choke 一旦发生，表现就是「有 peer 但没速度」。
    #   send_buffer_watermark 512KB→3MB / factor 50→150 / low 10KB→64KB：上行水位
    #                             线三件套，服务同上一条的互惠目标。
    #   request_queue_time       3→10s：请求在发送队列里排队超过该值即被判超时并
    #                             重发。高 BDP（大缓冲 + 高 RTT）下 3s 太短，
    #                             会退化成「重发—重复—再重发」的请求风暴，
    #                             带宽全花在重复块上。
    #   min_reconnect_time 60→20s、peer_connect_timeout 15→8s：**爬坡项**。
    #                             冷启动首批 peer 的失败率很高（对端下线 / NAT
    #                             不可达 / 端口未开），60s 冷却把「第一次没连上」
    #                             升级成「一分钟内不再尝试」；黑洞 peer 还会白占
    #                             15s 连接槽。
    #   hashing_threads 2→4、aio_threads→12：校验与写盘并发。本机实测 SHA1
    #                             单线程 2580MB/s、4 线程 9823MB/s（T3），千兆只
    #                             需 0.04 核 → 线程数是**余量**不是瓶颈；但校验
    #                             队列一旦堆积，症状是「速率不低而 have 不涨」
    #                             （见本文件 P-2 批次的同一论证）。
    #   announce_to_all_trackers / announce_to_all_tiers False→True：**peer 发现
    #                             面**。磁力链的多 tracker 常同处一个 tier，默认
    #                             只问一个；全问一遍等于发现面翻倍——而发现到的
    #                             peer 数就是吞吐上限（回环实测本机栈能跑 127MB/s，
    #                             外网速率完全由 swarm 供给决定）。
    "recv_socket_buffer_size": 2 * 1024 * 1024,
    "send_socket_buffer_size": 1024 * 1024,
    "send_buffer_watermark": 3 * 1024 * 1024,
    "send_buffer_watermark_factor": 150,
    "send_buffer_low_watermark": 64 * 1024,
    "request_queue_time": 10,
    "min_reconnect_time": 20,
    "peer_connect_timeout": 8,
    "hashing_threads": 4,
    "aio_threads": 12,
    "announce_to_all_trackers": True,
    "announce_to_all_tiers": True,
}


# peer 发现路径（千兆吞吐专项）。**不属于 THROUGHPUT_SETTINGS**：那颗字典是
# 「数值型吞吐调参」且被 wait_timeout_test 用 `int()` 逐键比对，字符串值会炸。
#
# dht_bootstrap_nodes：libtorrent 默认只有 `dht.libtorrent.org:25401` **一个**
# 节点。单点 bootstrap 在受限网络里一旦不可达（国内链路上这是常态），DHT 路由表
# 就从零开始爬，磁力链的 peer 发现退化为「只等 tracker 应答」——用户看到的正是
# 「几十 KB/s 甚至长时间 0」。多写几个公共 router：任何一个通，路由表就能起步，
# 冗余本身就是收益（启动期并发 bootstrap，先应答者胜）。
DHT_BOOTSTRAP_NODES = ",".join((
    "dht.libtorrent.org:25401",
    "dht.transmissionbt.com:6881",
    "router.bittorrent.com:6881",
    "router.utorrent.com:6881",
    "dht.aelitis.com:6881",
    "router.bitcomet.com:6881",
))

PEER_DISCOVERY_SETTINGS: dict = {
    "dht_bootstrap_nodes": DHT_BOOTSTRAP_NODES,
}


# ---------------------------------------------------------------- 纯函数层
# 不依赖宿主状态，单测可直接调用（不需要真实会话）。

def alert_category_mask() -> int:
    """订阅必要告警类别的位掩码（默认掩码过窄会漏掉 metadata / file_progress）。"""
    cat = lt.alert.category_t
    mask = 0
    for name in ("status_notification", "error_notification",
                 "file_progress_notification", "storage_notification",
                 "tracker_notification", "connect_notification"):
        try:
            mask |= int(getattr(cat, name))
        except (AttributeError, TypeError, ValueError):
            # 类别属性缺失（AttributeError）或取值无法转 int（TypeError/ValueError）
            # 时按 0 处理，不阻断掩码构造；libtorrent 版本差异不阻塞启动
            pass
    return mask


def _write_error_kind(a) -> str | None:
    """写盘失败类告警识别：命中返回类型名，否则 None。

    ``file_error_alert``（1.2+ 现名，吞并了旧 ``storage_failed_alert``）与
    ``storage_failed_alert``（1.1 旧名）都要认——getattr 探测，本环境
    libtorrent 2.1 没有后者也不报错；两代版本与测试替身共用此入口。
    """
    for name in ("file_error_alert", "storage_failed_alert"):
        cls = getattr(lt, name, None)
        if cls is not None and isinstance(a, cls):
            return name
    return None


def _listen_host(listen_interface: str = "") -> str:
    """解析监听网卡 IP：留空 / 纯空白一律回落 ``0.0.0.0``（= 所有网卡）。"""
    return str(listen_interface or "").strip() or "0.0.0.0"


def build_session_settings(listen_port: int, active_downloads: int,
                           proxy: dict | None = None,
                           port_mapping: bool = False,
                           listen_interface: str = "") -> dict:
    """会话配置字典（libtorrent 2.1.x：dict 配置已取代 settings_pack）。

    UPnP/NAT-PMP 由 ``port_mapping`` 控制，**默认 False（保持既有行为）**：
    本应用默认只「收」不做种，端口映射徒增暴露面。需要入站连接时必须置
    True——2026-09-18 实测诊断：本机外网 IP 为公网地址且 UPnP 映射可成功，
    但在关闭映射的情况下会同时踩两个坑：① 连不上同为 NAT 后的 peer
    （swarm 里无开放端口的 peer 占比很高，可连候选会一路枯竭）；② 做种时
    无人能连入 → 客户端报「做种失败 / 无连接」。

    ``listen_interface`` 用于多网卡环境（实测本机有 vgate0 VPN、Hyper-V、
    TAP 等虚拟网卡）显式绑定出口网卡 IP；留空 = 0.0.0.0（旧行为），避免
    libtorrent 从 VPN / 虚拟网卡绕行出境。

    吞吐项见 :data:`THROUGHPUT_SETTINGS`、发现项见
    :data:`PEER_DISCOVERY_SETTINGS`：先铺基线、再叠吞吐与发现、最后叠代理
    —— 顺序有意义，代理设置必须最后写入（它能覆盖 proxy_* 相关键）。
    """
    settings = {
        "listen_interfaces": f"{_listen_host(listen_interface)}:{listen_port}",
        "enable_dht": True,
        "enable_lsd": True,
        "enable_upnp": bool(port_mapping),
        "enable_natpmp": bool(port_mapping),
        "connections_limit": 300,
        "alert_queue_size": 5000,
        "active_downloads": active_downloads,
    }
    settings.update(PEER_DISCOVERY_SETTINGS)
    settings.update(THROUGHPUT_SETTINGS)
    settings.update(lt_proxy_settings(proxy))
    mask = alert_category_mask()
    if mask:
        settings["alert_mask"] = mask
    return settings


# ---------------------------------------------------------------- 依赖注入

@dataclass
class SessionDeps:
    """``SessionCore`` 所需的宿主状态与能力，由 SessionManager 注入。

    约定：名字以 ``_get`` / ``_set`` 结尾的成对回调用于宿主**会被重绑/置空**
    的成员；``clear_*`` 与部分 ``Get`` 回调要求调用方已持 ``lock``（注释写明），
    避免重复获取非可重入锁导致自锁。
    """
    listen_port: int
    active_downloads: int
    lock: Any                                # threading.Lock（阶段 3 后归 registry）
    # --- 会话状态 ---
    ses_get: Callable[[], Any]               # -> lt.session | None
    ses_set: Callable[[Any], None]
    running_get: Callable[[], bool]
    running_set: Callable[[bool], None]
    thread_get: Callable[[], Any]            # -> threading.Thread | None
    thread_set: Callable[[Any], None]
    last_sweep_get: Callable[[], float]      # -> 上次 fastresume 脏写时刻
    last_sweep_set: Callable[[float], None]
    metadata_timeout_get: Callable[[], float]
    metadata_timeout_set: Callable[[float], None]
    # --- 注册表 / 任务清单 ---
    torrents_get: Callable[[], dict]         # -> {ih: TaskRecord}
    tasks_get: Callable[[], dict]            # -> {ih: task dict}
    tasks_set: Callable[[dict], None]
    hash_key: Callable[[Any], str]           # torrent_handle -> info_hash
    find_record: Callable[[Any], Any]        # -> TaskRecord | None
    current_record: Callable[[], Any]        # 不持锁；调用方自行决定是否加锁
    # --- 启动 / 退出编排 ---
    tasks_loader: Callable[[], dict]         # -> 磁盘任务清单
    restore_task: Callable[[dict], bool]     # 单任务启动恢复
    thread_factory: Callable[[Any], Any]     # target -> Thread-like（可替换为假线程）
    scheduler_get: Callable[[], Any]         # -> PreviewScheduler
    # --- 宿主能力回调 ---
    persist_tasks: Callable[[], None]
    write_resume_from_alert: Callable[[Any], None]
    request_resume: Callable[[Any], None]
    drain_resume_alerts: Callable[[], None]
    on_metadata_received: Callable[[Any], None]
    on_download_finished: Callable[[Any], None]
    emit_error: Callable[[str], None]
    clear_resolving: Callable[[], None]      # 须持锁：清「当前」解析计时
    clear_runtime_state: Callable[[], None]  # 须持锁：清注册表与「当前」别名
    # 性能专项 P-4：DHT 路由表持久化路径（空串 = 不启用，行为与基线一致）。
    # 冷启动的 DHT 只有 3 个硬编码 bootstrap 节点，找 peer 要从零爬路由表；
    # 回灌上次会话的已知节点可显著缩短首次 get_peers 的耗时。
    dht_state_path: str = ""
    # 入站能力（诊断 D-2026-09-18）：port_mapping 开 UPnP/NAT-PMP，
    # listen_interface 显式绑定出口网卡 IP（空 = 0.0.0.0）。二者均带默认
    # 值，老构造点不传即保持「只收不做种 + 全网卡监听」的既有行为。
    port_mapping: bool = False
    listen_interface: str = ""
    # 代理可用性自检（D-2026-09-18 加固）。签名 ``(proxy, timeout) -> (ok, reason)``。
    #
    # **默认 None = 不探测**，这是刻意的：本字段是「新增能力」，而既有测试与
    # 替身构造 SessionDeps 时都不传它——默认 None 保证它们既不联网、也不产生
    # 额外 emit_error，行为与改动前逐字一致。生产侧由 SessionManager 显式注入
    # ``core.proxycheck.probe``。
    proxy_probe: Any = None


class SessionCore:
    """面向单个 SessionManager 实例的会话生命周期服务。"""

    def __init__(self, deps: SessionDeps):
        self.deps = deps
        # 最近一次生效的代理配置 + 自检结论（供「元数据超时」文案点明根因）。
        # 只在真配置了代理时非 None —— 见 _start_proxy_check。
        self._proxy: dict | None = None
        self._proxy_reason: str = ""

    # ---------- 启动 ----------

    def start(self, proxy: dict | None = None,
              metadata_timeout: float | None = None) -> None:
        """启动会话：建会话 → 恢复任务清单 → 起告警线程。"""
        d = self.deps
        if metadata_timeout:
            d.metadata_timeout_set(float(metadata_timeout))
        settings = build_session_settings(
            d.listen_port, d.active_downloads, proxy,
            port_mapping=getattr(d, "port_mapping", False),
            listen_interface=getattr(d, "listen_interface", ""),
        )
        try:
            ses = lt.session(settings)
        except (OSError, RuntimeError) as e:
            # OSError：端口占用等系统层失败；RuntimeError：libtorrent 绑定实测
            # （会话构造失败时 lt 抛 RuntimeError）。默认端口被占用（如 6881）
            # 会直接崩溃：回退随机端口再试一次
            log_warning("fetcher.start",
                        f"监听端口 {d.listen_port} 启动失败（{e}），回退随机端口")
            settings["listen_interfaces"] = \
                f"{_listen_host(getattr(d, 'listen_interface', ''))}:0"
            try:
                ses = lt.session(settings)
            except (OSError, RuntimeError) as e2:
                # 审计 2026-09-29（P2）：随机端口回退的第二次构造原先裸调——
                # 再失败（端口只是最常见原因，还有资源耗尽/参数组合等）会以
                # 无上下文的裸异常直接崩掉启动路径。此处无法再降级（没有第三
                # 条路），记日志后带着两级原因重抛，让上层走既有的启动失败上报。
                log_warning(
                    "fetcher.start",
                    f"随机端口回退仍失败（{e2}）；首次原因：{e}")
                raise RuntimeError(
                    f"libtorrent 会话启动失败：固定端口 {e}；随机端口 {e2}") from e2
        d.ses_set(ses)
        self._load_dht_state(ses)
        # 启动恢复：加载任务清单 + 逐任务恢复（损坏绝不阻断启动）
        tasks = d.tasks_loader()
        d.tasks_set(tasks)
        for t in tasks.values():
            try:
                d.restore_task(t)
            except Exception as e:
                # 兜底（有意宽泛）：单任务恢复损坏绝不阻断整个启动，
                # 异常类型不可枚举（句柄失效/清单畸形/磁盘缺失等混合），
                # 逐个 log_exception 后继续恢复下一个任务
                log_exception("fetcher.restore", e)
        d.last_sweep_set(time.time())
        d.running_set(True)
        thread = d.thread_factory(self.alert_loop)
        d.thread_set(thread)
        thread.start()
        # 代理自检放最后：会话已就绪、告警线程已在跑，此时探测失败也能安全 emit。
        self._start_proxy_check(proxy)

    # ---------- 代理可用性自检（D-2026-09-18 加固） ----------

    def _start_proxy_check(self, proxy: dict | None) -> None:
        """后台自检代理可达性；不可达则经 emit_error 给出**精确**报错。

        为什么必须存在：实测事故里 ``socks5://1.2.3.4:1080`` 静默生效，三条
        peer 发现通道(peer/tracker/DHT 引导解析)全灭，而 90 秒后弹出的却是
        「该资源可能已无做种」——文案指向资源，用户永远查不到代理。本方法把
        「配置错误」在**启动后数秒内**就标注出来，不再让它伪装成资源问题。

        三条不变量：
        - 只在**真配置了代理**时动作（直连不是故障，不打扰用户）；
        - **不阻塞启动**：探测走独立 daemon 线程（黑洞地址实测 TCP 要 6s 超时，
          放主线程会冻住 UI）；
        - 探测函数缺失（既有测试替身不传 ``proxy_probe``）时静默跳过，
          保证老构造点的行为与改动前逐字一致。
        """
        d = self.deps
        if not proxycheck.is_configured(proxy):
            self._proxy, self._proxy_reason = None, ""
            return
        self._proxy, self._proxy_reason = proxy, ""
        fn = getattr(d, "proxy_probe", None)
        if fn is None:
            return

        def _run():
            try:
                ok, reason = fn(proxy, proxycheck.DEFAULT_TIMEOUT)
            except Exception as e:
                # 兜底（有意宽泛）：探测函数由宿主注入（socket/DNS/超时混合异常，
                # 类型不可枚举），探测失败只丢自检信号，绝不能杀掉后台线程
                log_warning("fetcher.proxy_check", f"代理探测异常：{e}")
                return
            if ok:
                return
            self._proxy_reason = str(reason or "")
            log_warning("fetcher.proxy_check",
                        f"代理 {proxycheck.address_text(proxy)} 不可达：{reason}")
            try:
                d.emit_error(proxycheck.warning_text(proxy, reason))
            except Exception as e:
                # 兜底（有意宽泛）：emit_error 是 UI 注入的回调边界（Qt 信号
                # 发射异常类型不可枚举），报错上报失败也不能拖垮探测线程
                log_exception("fetcher.proxy_check.emit", e)

        try:
            threading.Thread(target=_run, daemon=True).start()
        except RuntimeError as e:
            # 线程起不来（资源耗尽）实测抛 RuntimeError，不能拖垮启动
            log_warning("fetcher.proxy_check.spawn", f"{e}")

    # ---------- DHT 路由表持久化（性能专项 P-4） ----------

    def _load_dht_state(self, ses) -> None:
        """回灌上次会话的 DHT 已知节点（缺失/损坏/版本不兼容一律静默跳过）。

        只读 ``dht state`` 一个键（``DHT_STATE_FLAGS``），绝不回灌 settings：
        磁盘上的旧 settings 会盖掉本次启动的限速/代理/吞吐参数。
        """
        path = self.deps.dht_state_path
        if not path or ses is None:
            return
        try:
            if not os.path.isfile(path):
                return
            with open(path, "rb") as f:
                blob = f.read()
            entry = lt.bdecode(blob)
            if isinstance(entry, dict) and entry:
                ses.load_state(entry, DHT_STATE_FLAGS)
        except (OSError, RuntimeError, ValueError, AttributeError) as e:
            # OSError：文件读取失败；RuntimeError：lt.bdecode 对畸形数据实测抛
            # （已实测），load_state 会话失效同样 RuntimeError；ValueError 兜底；
            # AttributeError：绑定版本无 load_state 时静默跳过（契约如此）
            log_warning("fetcher.dht_state.load", f"{e}")

    def _save_dht_state(self, ses) -> None:
        """落盘 DHT 已知节点（原子写；失败仅告警，绝不阻断退出）。"""
        path = self.deps.dht_state_path
        if not path or ses is None:
            return
        try:
            state = ses.save_state(DHT_STATE_FLAGS)
            if not state:
                return          # 路由表仍为空（未 bootstrap）时不必写空壳
            atomic_write_bytes(path, lt.bencode(state))
        except (OSError, RuntimeError, TypeError, ValueError,
                AttributeError) as e:
            # OSError：原子写失败；RuntimeError：save_state/bencode 会话失效
            # 实测抛；TypeError/ValueError：bencode 序列化畸形数据；
            # AttributeError：绑定版本无 save_state 时静默跳过；失败仅告警
            log_warning("fetcher.dht_state.save", f"{e}")

    # ---------- 运行期热更新 ----------

    def apply_proxy(self, proxy: dict) -> None:
        """运行时切换代理（无需重建会话）。

        切换后同样跑一次自检：用户刚改完代理就出错是最容易定位的时刻，
        此时给出「代理不可达」比 90 秒后收到「该资源已无做种」有用得多。
        """
        d = self.deps
        ses = d.ses_get()
        if ses is None:
            return
        try:
            ses.apply_settings(lt_proxy_settings(proxy))
        except (RuntimeError, TypeError, ValueError) as e:
            # RuntimeError：libtorrent 会话失效实测；TypeError/ValueError：
            # 代理参数畸形时 lt_proxy_settings 转换失败。应用失败不影响主会话
            log_warning("fetcher.apply_proxy", f"代理设置应用失败：{e}")
        self._start_proxy_check(proxy)

    def apply_rate_limit(self, kbps: int) -> None:
        """会话级下载限速（KB/s，0=不限）；热更新，无需重建会话。

        libtorrent 单位是字节/秒，配置层用 KB/s 对用户友好。
        """
        d = self.deps
        ses = d.ses_get()
        if ses is None:
            return
        try:
            ses.apply_settings({"download_rate_limit": max(0, int(kbps)) * 1024})
        except (RuntimeError, TypeError, ValueError) as e:
            # RuntimeError：libtorrent 会话失效实测；TypeError/ValueError：
            # kbps 无法转 int 时 int() 失败
            log_warning("fetcher.apply_rate_limit", f"限速设置应用失败：{e}")

    def apply_port_mapping(self, enabled: bool) -> None:
        """运行时开关 UPnP/NAT-PMP 端口映射（热更新，无需重建会话）。

        诊断 D-2026-09-18：端口映射关闭时，① 连不上同为 NAT 后的 peer，
        swarm 里可连候选会一路枯竭；② 做种时无人能连入 → 报「做种失败」。
        实测本机外网 IP 为公网地址且 UPnP 映射可成功，故开放此开关。

        注：libtorrent 的 UPnP 是异步发现过程，开启后需要数秒才能拿到映射，
        期间 ``portmap_alert`` / ``portmap_error_alert`` 是唯一的成败信号。
        """
        d = self.deps
        ses = d.ses_get()
        if ses is None:
            return
        try:
            ses.apply_settings({"enable_upnp": bool(enabled),
                                "enable_natpmp": bool(enabled)})
        except RuntimeError as e:  # libtorrent 会话失效时实测抛 RuntimeError
            log_warning("fetcher.apply_port_mapping",
                        f"端口映射设置应用失败：{e}")

    # ---------- 退出 ----------

    def shutdown(self) -> None:
        """停会话：落盘任务清单与 fastresume，再清全部句柄，最后 join 线程。"""
        d = self.deps
        d.scheduler_get().stop()
        self._save_dht_state(d.ses_get())
        # 1) 落盘任务清单 + 请求全量 fastresume（异步告警，由 alert 循环消化）
        # 锁内**只取快照**：落盘（含 fsync）与 libtorrent 调用一律出锁。
        # plan/11 R-6b：旧实现在 `with d.lock` 内直接调 persist_tasks，
        # 既把毫秒级同步 I/O 挂进全局锁（卡 HTTP 线程的 _dir_index 与告警线程），
        # 又与 persist_tasks 新增的「锁内取快照」自锁死（Lock 不可重入）。
        with d.lock:
            has_tasks = bool(d.tasks_get())
            ses = d.ses_get()
            pending = [rec for rec in d.torrents_get().values()
                       if rec.download and rec.handle is not None
                       and rec.result is not None]
        if has_tasks:
            d.persist_tasks()      # 内部已吞异常：写失败只告警；自带锁内快照
        if ses is not None:
            for rec in pending:
                try:
                    # 性能专项 P-1b：带 save_info_dict（与
                    # persist.request_resume 同参数），否则落盘的
                    # resume data 不含 info 段，重启恢复要重拉元数据。
                    # 句柄可能已被并发移除 → save_resume_data 抛错由本 try 兜住
                    # （既有语义不变；此处出锁调用反而更安全）。
                    rec.handle.save_resume_data(
                        lt.save_resume_flags_t.flush_disk_cache
                        | lt.save_resume_flags_t.save_info_dict)
                except RuntimeError as e:
                    log_warning("fetcher.shutdown.save_resume", f"{e}")
        # 2) 通知告警线程退出并等待其消化告警
        d.running_set(False)
        thread = d.thread_get()
        if thread is not None:
            try:
                thread.join(timeout=SHUTDOWN_JOIN_TIMEOUT)
            except RuntimeError as e:  # 线程不可 join（未启动/当前线程自身）实测抛
                log_warning("fetcher.shutdown.join", f"{e}")
        # 3) 残留 fastresume 告警直取（可能在线程退出后才到达）
        d.drain_resume_alerts()
        # 4) 移除全部句柄并复位（options=0：保留磁盘文件——下载任务是用户数据，
        #    remove_torrent 的 delete_files=1 会异步删文件，shutdown 绝不可用）
        with d.lock:
            ses = d.ses_get()
            if ses is not None:
                for rec in d.torrents_get().values():
                    if rec.handle is not None:
                        try:
                            ses.remove_torrent(rec.handle, 0)
                        except RuntimeError as e:  # 句柄/会话失效时 lt 实测抛
                            log_warning("fetcher.shutdown.remove_torrent", f"{e}")
            d.clear_runtime_state()
            d.ses_set(None)

    # ---------- 告警循环 ----------

    def alert_loop(self) -> None:
        """后台线程主循环：分发告警 → 看门狗 → 周期脏写 → 等待下一批。"""
        d = self.deps
        while d.running_get() and d.ses_get() is not None:
            ses = d.ses_get()
            try:
                for a in ses.pop_alerts():
                    # 单条告警处理失败不得中断整批，否则 metadata_received_alert
                    # 会被丢弃（历史上吞掉整批异常导致元数据永不回调）
                    try:
                        self.handle_alert(a)
                    except Exception as e:
                        # 兜底（最终兜底点#1：alert 循环）——有意宽泛：分发面
                        # 覆盖全部告警类型与注入回调（on_metadata_received 等
                        # 由宿主提供，异常类型不可枚举），任何单条异常都不得
                        # 杀死后台线程，log_exception 后继续处理同批下一条
                        log_exception("fetcher.alert.handle", e)
            except Exception as e:
                # 兜底（最终兜底点#1：alert 循环）——有意宽泛：pop_alerts 失败
                # （会话销毁竞态等）不得终止循环线程，看门狗/周期脏写必须照常
                # 执行，否则卡住的解析永远等不到超时判定
                log_exception("fetcher.alert.pop", e)
            now = time.time()
            self.metadata_watchdog(now)
            self.resume_sweep(now)
            self._wait_alerts(ses)

    def _wait_alerts(self, ses) -> None:
        """让出 CPU 直到「有告警」或「最多 ALERT_POLL_INTERVAL 秒」。

        性能专项 P-3：旧实现是死等 ``time.sleep(0.15)`` 再 pop —— 告警恰好在
        sleep 刚开始时到达也要白等一整拍，**平均 75ms、最坏 150ms** 的空延迟
        全部落在「元数据到达 → 界面出文件树」这条用户最敏感的链路上。

        改用 ``wait_for_alert`` 阻塞等待后：告警一到线程立刻被唤醒（延迟降到
        亚毫秒级），没有告警时才睡满一拍——总周期上限不变，看门狗与周期
        fastresume 脏写的节奏也完全不变，只是把「白等」还给了 CPU。

        会话对象没有该方法（测试替身 / 极旧绑定）时自动退回 sleep，
        行为与基线逐字一致。
        """
        wait = getattr(ses, "wait_for_alert", None)
        if wait is None:
            time.sleep(ALERT_POLL_INTERVAL)
            return
        try:
            wait(int(ALERT_POLL_INTERVAL * 1000))
        except RuntimeError:
            # 会话销毁竞态下 wait_for_alert 实测抛 RuntimeError：退回 sleep，
            # 下一轮循环的 ses_get 判空自然退出
            time.sleep(ALERT_POLL_INTERVAL)

    def handle_alert(self, a) -> None:
        """单条告警的归属分发（查不到归属 = 已移除任务的迟到告警，丢弃）。"""
        d = self.deps
        if isinstance(a, lt.metadata_received_alert):
            rec = d.find_record(a.handle)
            if rec is not None:
                d.on_metadata_received(rec)
        elif isinstance(a, lt.file_completed_alert):
            # 只服务当前任务（预览）的文件完成事件
            rec = d.find_record(a.handle)
            sched = d.scheduler_get()
            cb = getattr(sched, "on_file_completed", None)
            if rec is not None and rec is d.current_record() and cb:
                cb(a.index)
        elif isinstance(a, lt.torrent_finished_alert):
            rec = d.find_record(a.handle)
            if rec is not None and rec.download:
                d.on_download_finished(rec)
        elif isinstance(a, lt.save_resume_data_alert):
            d.write_resume_from_alert(a)
        elif isinstance(a, lt.save_resume_data_failed_alert):
            log_warning("fetcher.resume.failed",
                        f"{d.hash_key(a.handle)[:12]}…")
        elif _write_error_kind(a) is not None:
            # 阶段 D D1：磁盘满/写失败告警（file_error / storage_failed）。
            # 以前无分支 → 后台满速缓存撞盘时任务静默卡 DOWNLOADING。
            self._handle_write_error(a)

    def _handle_write_error(self, a) -> None:
        """写盘失败告警（磁盘满/权限/IO 错误）→ 归属任务标错。

        磁盘满或写失败后 libtorrent 会暂停对应文件 IO，但不会自己把任务
        置 FAILED——不处理就是「卡 DOWNLOADING 假死」（plan/06 关键事实 6）。
        R-1 纪律：rec.state/rec.error 与 tasks 清单的旁路写入必须在
        registry 锁内（告警线程与主线程交错），与 watchdog 下载分支同款
        编排：锁内改状态+同步清单，出锁再 persist / emit_error（P1-4）。
        查不到归属 = 已移除任务的迟到告警，丢弃（与 handle_alert 一致）。
        D5-A 终态守卫（仿 watchdog 状态过滤 :366-367）：COMPLETED/FAILED/
        PAUSED/STOPPED 一律跳过——迟到 file_error 不得把重启后恢复的完成态
        拉回 FAILED；同文案重复告警去重（rec.error == msg 即返回，不引入
        新字段），杜绝重复 persist 与重复弹窗。
        ``error``/``filename`` 可能缺失（no_files / metadata 阶段告警），
        一律 getattr 兜底，绝不因取字段失败而漏标错。
        """
        d = self.deps
        kind = _write_error_kind(a)
        rec = d.find_record(a.handle)
        if rec is None:
            return
        err = getattr(a, "error", None)
        detail = str(err) if err is not None else "磁盘错误或空间不足"
        filename = getattr(a, "filename", "") or ""
        label = "存储" if kind == "storage_failed_alert" else "写入"
        if filename:
            tail = f"（文件：{filename}）"
        else:
            # Minor-e：filename 缺失（storage_failed 无此字段）回退 operation，
            # 再缺则不留尾注——文案宁可少一截，不编造。
            op = getattr(a, "operation", "") or ""
            tail = f"（操作：{op}）" if op else ""
        msg = f"{label}失败：{detail}{tail}"
        with d.lock:
            # D5-A：守卫与去重必须在锁内、任何写之前——判定与写同区间，
            # 不与并发状态迁移交错出「判时非终态、写时已终态」的窗口。
            if rec.state in (STATE_COMPLETED, STATE_FAILED,
                             STATE_PAUSED, STATE_STOPPED):
                return
            if rec.error == msg:      # 同文案重复告警（锁内去重，无新字段）
                return
            key = d.hash_key(rec.handle) if rec.handle is not None else ""
            rec.state = STATE_FAILED
            rec.error = msg
            is_download = rec.download
            is_current = rec is d.current_record()
            was_resolving = rec.resolving
            if was_resolving:
                rec.resolving = False
                rec.resolve_started = 0.0
                if is_current:
                    d.clear_resolving()
            if is_download:
                tasks = d.tasks_get()
                if key and key in tasks:
                    tasks[key]["state"] = STATE_FAILED
                    tasks[key]["error"] = msg
        log_warning("fetcher.storage_error",
                    f"{(key[:12] + '…') if key else '?'} {msg}")
        if is_download:
            d.persist_tasks()      # 出锁落盘（P1-4：同步 I/O 不在锁内）
        elif not was_resolving:
            # 预览任务：无下载清单可写，经 emit_error 通道弹 UI（仿 watchdog
            # 非下载分支）；已在 resolving（元数据阶段）的预览错误不重复弹窗。
            d.emit_error(msg)

    def metadata_watchdog(self, now: float | None = None) -> None:
        """per-task 元数据超时看门狗（替代旧的全局单计时）。

        遍历注册表里仍 ``resolving`` 的任务，各自对照自己的 ``resolve_started``
        与超时（记录级 ``timeout`` 覆盖会话级），超时任务独立 pause + FAILED，
        互不影响；暂停/停止/完成/失败态任务不看门（用户暂停元数据获取是合法动作）。

        文案取数与判定同源：用命中记录的**有效超时**（rec.timeout 优先，
        否则会话级），D5 修复——旧实现恒用会话级，记录级超时任务会弹错秒数。
        """
        d = self.deps
        now = time.time() if now is None else now
        expired = []
        with d.lock:
            for rec in d.torrents_get().values():
                if not rec.resolving or rec.resolve_started <= 0:
                    continue
                if rec.state in (STATE_PAUSED, STATE_STOPPED,
                                 STATE_COMPLETED, STATE_FAILED):
                    continue
                limit = (rec.timeout if rec.timeout is not None
                         else d.metadata_timeout_get())
                if now - rec.resolve_started > limit:
                    expired.append((rec, limit))
        for rec, limit in expired:
            with d.lock:
                if not (rec.resolving and rec.resolve_started > 0):
                    continue   # 已被其他路径处理（如元数据刚到达）
                rec.resolving = False
                rec.resolve_started = 0.0
                rec.state = STATE_FAILED
                is_current = rec is d.current_record()
                if is_current:
                    d.clear_resolving()
            # 根因提示（D-2026-09-18 加固）：原文案只指向资源（「已无做种」），
            # 而实测事故的根因是客户端代理不可达——用户按文案去换种子永远
            # 查不出来。已配置代理时追加一句指向代理的提示；_proxy_reason
            # 非空则说明启动自检已确认不可达，直接点明。
            # 必须**在两条分支之前**算好：预览走 emit_error、下载走 rec.error，
            # 两处都要用（放在分支内会 UnboundLocalError）。
            hint = proxycheck.hint_text(self._proxy, self._proxy_reason)
            if not rec.download:
                if is_current:
                    msg = (f"获取元数据超时（>{int(limit)} 秒）："
                           f"该资源可能已无做种/无在线 Peer{hint}")
                else:
                    key = d.hash_key(rec.handle) \
                        if rec.handle is not None else "?"
                    msg = (f"[{key[:12]}…] 获取元数据超时"
                           f"（>{int(limit)} 秒）："
                           f"该资源可能已无做种/无在线 Peer{hint}")
                d.emit_error(msg)
            with d.lock:
                if rec.handle is not None:
                    try:
                        rec.handle.pause()
                    except (AttributeError, RuntimeError) as e:
                        # RuntimeError：句柄失效时 lt 实测抛；AttributeError：
                        # 替身句柄缺 pause（proxycheck_test 的 SimpleNamespace）
                        log_warning("fetcher.timeout.pause", f"{e}")
            if rec.download:
                with d.lock:
                    rec.error = (f"获取元数据超时"
                                 f"（>{int(limit)} 秒）："
                                 f"该资源可能已无做种/无在线 Peer{hint}")
                    key = d.hash_key(rec.handle) \
                        if rec.handle is not None else ""
                    tasks = d.tasks_get()
                    if key in tasks:
                        tasks[key]["state"] = STATE_FAILED
                        tasks[key]["error"] = rec.error
                d.persist_tasks()

    def resume_sweep(self, now: float | None = None) -> None:
        """fastresume 周期脏写（仅下载中的任务；libtorrent 建议间隔 ≥1min）。"""
        d = self.deps
        now = time.time() if now is None else now
        if now - d.last_sweep_get() < RESUME_SWEEP_INTERVAL:
            return
        d.last_sweep_set(now)
        with d.lock:
            for rec in d.torrents_get().values():
                if rec.download and rec.handle is not None \
                        and rec.state == STATE_DOWNLOADING:
                    d.request_resume(rec)
