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

import time
from dataclasses import dataclass
from typing import Any, Callable

import libtorrent as lt

from .config import lt_proxy_settings
from .logutil import log_exception, log_warning
from .states import (STATE_COMPLETED, STATE_DOWNLOADING, STATE_FAILED,
                     STATE_PAUSED, STATE_STOPPED)

ALERT_POLL_INTERVAL = 0.15   # 告警轮询间隔（秒）
SHUTDOWN_JOIN_TIMEOUT = 2.0  # 等告警线程消化残留告警的上限（秒）
RESUME_SWEEP_INTERVAL = 60.0  # fastresume 周期脏写间隔（libtorrent 建议 ≥1min）


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
        except Exception:
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


def build_session_settings(listen_port: int, active_downloads: int,
                           proxy: dict | None = None) -> dict:
    """会话配置字典（libtorrent 2.1.x：dict 配置已取代 settings_pack）。

    UPnP/NAT-PMP 默认关闭：本应用只「收」不做种，端口映射徒增暴露面。
    """
    settings = {
        "listen_interfaces": f"0.0.0.0:{listen_port}",
        "enable_dht": True,
        "enable_lsd": True,
        "enable_upnp": False,
        "enable_natpmp": False,
        "connections_limit": 300,
        "alert_queue_size": 5000,
        "active_downloads": active_downloads,
    }
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


class SessionCore:
    """面向单个 SessionManager 实例的会话生命周期服务。"""

    def __init__(self, deps: SessionDeps):
        self.deps = deps

    # ---------- 启动 ----------

    def start(self, proxy: dict | None = None,
              metadata_timeout: float | None = None) -> None:
        """启动会话：建会话 → 恢复任务清单 → 起告警线程。"""
        d = self.deps
        if metadata_timeout:
            d.metadata_timeout_set(float(metadata_timeout))
        settings = build_session_settings(d.listen_port, d.active_downloads,
                                          proxy)
        try:
            ses = lt.session(settings)
        except Exception as e:
            # 默认端口被占用（如 6881）会直接崩溃：回退随机端口再试一次
            log_warning("fetcher.start",
                        f"监听端口 {d.listen_port} 启动失败（{e}），回退随机端口")
            settings["listen_interfaces"] = "0.0.0.0:0"
            ses = lt.session(settings)
        d.ses_set(ses)
        # 启动恢复：加载任务清单 + 逐任务恢复（损坏绝不阻断启动）
        tasks = d.tasks_loader()
        d.tasks_set(tasks)
        for t in tasks.values():
            try:
                d.restore_task(t)
            except Exception as e:
                log_exception("fetcher.restore", e)
        d.last_sweep_set(time.time())
        d.running_set(True)
        thread = d.thread_factory(self.alert_loop)
        d.thread_set(thread)
        thread.start()

    # ---------- 运行期热更新 ----------

    def apply_proxy(self, proxy: dict) -> None:
        """运行时切换代理（无需重建会话）。"""
        d = self.deps
        ses = d.ses_get()
        if ses is None:
            return
        try:
            ses.apply_settings(lt_proxy_settings(proxy))
        except Exception as e:
            log_warning("fetcher.apply_proxy", f"代理设置应用失败：{e}")

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
        except Exception as e:
            log_warning("fetcher.apply_rate_limit", f"限速设置应用失败：{e}")

    # ---------- 退出 ----------

    def shutdown(self) -> None:
        """停会话：落盘任务清单与 fastresume，再清全部句柄，最后 join 线程。"""
        d = self.deps
        d.scheduler_get().stop()
        # 1) 落盘任务清单 + 请求全量 fastresume（异步告警，由 alert 循环消化）
        with d.lock:
            tasks = d.tasks_get()
            if tasks:
                d.persist_tasks()      # 内部已吞异常：写失败只告警
            ses = d.ses_get()
            if ses is not None:
                for rec in d.torrents_get().values():
                    if rec.download and rec.handle is not None \
                            and rec.result is not None:
                        try:
                            rec.handle.save_resume_data(
                                lt.save_resume_flags_t.flush_disk_cache)
                        except Exception as e:
                            log_warning("fetcher.shutdown.save_resume", f"{e}")
        # 2) 通知告警线程退出并等待其消化告警
        d.running_set(False)
        thread = d.thread_get()
        if thread is not None:
            try:
                thread.join(timeout=SHUTDOWN_JOIN_TIMEOUT)
            except Exception as e:
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
                        except Exception as e:
                            log_warning("fetcher.shutdown.remove_torrent", f"{e}")
            d.clear_runtime_state()
            d.ses_set(None)

    # ---------- 告警循环 ----------

    def alert_loop(self) -> None:
        """后台线程主循环：分发告警 → 看门狗 → 周期脏写 → 让出 CPU。"""
        d = self.deps
        while d.running_get() and d.ses_get() is not None:
            try:
                for a in d.ses_get().pop_alerts():
                    # 单条告警处理失败不得中断整批，否则 metadata_received_alert
                    # 会被丢弃（历史上吞掉整批异常导致元数据永不回调）
                    try:
                        self.handle_alert(a)
                    except Exception as e:
                        log_exception("fetcher.alert.handle", e)
            except Exception as e:
                log_exception("fetcher.alert.pop", e)
            now = time.time()
            self.metadata_watchdog(now)
            self.resume_sweep(now)
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
            if not rec.download:
                if is_current:
                    msg = (f"获取元数据超时（>{int(limit)} 秒）："
                           f"该资源可能已无做种/无在线 Peer")
                else:
                    key = d.hash_key(rec.handle) \
                        if rec.handle is not None else "?"
                    msg = (f"[{key[:12]}…] 获取元数据超时"
                           f"（>{int(limit)} 秒）："
                           f"该资源可能已无做种/无在线 Peer")
                d.emit_error(msg)
            with d.lock:
                if rec.handle is not None:
                    try:
                        rec.handle.pause()
                    except Exception as e:
                        log_warning("fetcher.timeout.pause", f"{e}")
            if rec.download:
                with d.lock:
                    rec.error = (f"获取元数据超时"
                                 f"（>{int(limit)} 秒）："
                                 f"该资源可能已无做种/无在线 Peer")
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
