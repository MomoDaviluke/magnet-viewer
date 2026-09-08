"""libtorrent 会话管理：磁力链元数据获取、种子加入、预览调度、状态快照。

本模块不依赖 Qt：回调由后台线程触发，UI 层负责通过 Qt 信号转发。

多任务结构（t1 落地）：``_torrents`` 任务注册表（info_hash -> TaskRecord）为权威，
``_handle/_result`` 为「当前任务/当前预览」别名，预览与状态路径零改动；
alert 归属按 info_hash 查注册表，元数据看门狗按任务独立计时。

持久化与目录隔离（t5 落地）：下载任务（add_task）落盘 ``<下载根>/<ih>/``
（默认 cache_dir/downloads，download_dir 配置可改）；解析/预览（resolve）落盘
``cache_dir/.preview/<ih>/``；任务清单 ``.tasks.json`` 与 fastresume
``.resume/<ih>.fastresume`` 原子写，启动恢复注入 resume data + 隐式校验，
损坏静默降级全新加入，绝不阻断启动。

重构阶段 1：任务持久化已整体迁入 ``core.persist``（``TaskPersistence``，
依赖以 getter/回调注入，不反向依赖本模块），本类对应方法降级为薄委托；
任务生命周期常量与 bootstrap tracker 下沉到 ``core.states``（本模块再导出，
``from core.fetcher import STATE_*`` 的历史写法不受影响）。
"""
from __future__ import annotations

import os
import shutil
import threading
import time

import libtorrent as lt

from .cache_guard import ensure_cache_dir
from .logutil import log_exception, log_warning
from .models import ParseResult, PieceMap, TorrentFile, safe_rel_path
from .parser import is_torrent_path, parse_torrent_file
from .persist import PersistDeps, TaskPersistence
from .persist import is_within as persist_is_within
from .persist import safe_task_save_path as persist_safe_task_save_path
from .persist import save_subdir_of as persist_save_subdir_of
from .persist import task_dir as persist_task_dir
from .scheduler import PreviewScheduler
from .session import SessionCore, SessionDeps
# 再导出：历史写法 from core.fetcher import TaskRecord / METADATA_TIMEOUT /
# *_SUBDIR 仍须可用（persist_test/session_test/registry_test 在用）；本体已
# 下沉 core.registry（R-2：TaskRecord 是注册表行结构，寄居 fetcher 是历史债）。
from .registry import (DOWNLOADS_SUBDIR, METADATA_TIMEOUT,  # noqa: F401
                       PREVIEW_SUBDIR, TaskRecord, TaskRegistry)
from .registry import hash_key as registry_hash_key
from .registry import ih_from_params as registry_ih_from_params
from .taskops import TaskOps
# 再导出：历史写法 from core.fetcher import STATE_* 仍须可用（download_mgr_test
# 在用）；常量本体已下沉到 core.states，避免 fetcher → persist → fetcher 环。
from .states import (BOOTSTRAP_TRACKERS, DOWNLOAD_STATES,  # noqa: F401
                     STATE_COMPLETED, STATE_DELETED, STATE_DOWNLOADING,
                     STATE_FAILED, STATE_META_FETCH, STATE_PAUSED,
                     STATE_QUEUED, STATE_READY, STATE_SEEDING, STATE_STOPPED,
                     STATE_VALIDATE)
from .taskstore import load_tasks

STATE_NAMES = {
    getattr(lt.torrent_status, k, None): k.replace("_", " ")
    for k in ("checking_files", "downloading_metadata", "downloading",
              "finished", "seeding", "allocating", "checking_resume_data")
    if getattr(lt.torrent_status, k, None) is not None
}


class SessionManager:
    """持有 libtorrent 会话与后台 alert 循环线程。"""

    def __init__(self, cache_dir: str, listen_port: int = 6881,
                 download_dir: str | None = None,
                 active_downloads: int = 3):
        self.cache_dir = os.path.abspath(cache_dir)
        self.listen_port = listen_port
        # 下载根目录（决策 D4：允许任意位置，默认 cache_dir/downloads）
        self._download_dir = (os.path.abspath(download_dir)
                              if download_dir
                              else os.path.join(self.cache_dir, DOWNLOADS_SUBDIR))
        self._active_downloads = int(active_downloads) if active_downloads else 3
        # 缓存目录守卫：拒绝盘符根/用户数据目录（清理入口同样受守卫约束）
        ensure_cache_dir(self.cache_dir)

        self.on_metadata = None   # callback(ParseResult) —— 后台线程触发
        self.on_error = None      # callback(str)
        self.on_file_completed = None  # callback(int file_index) 预览文件完成

        self._ses: lt.session | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_resume_sweep = 0.0         # 60s 周期 fastresume 脏写

        # 任务注册表（阶段 3 抽出）：**单锁归属**（R1 决策）。本类历史成员
        # _lock/_torrents/_tasks/_current_ih/_handle/_result/_resolving/
        # _resolve_started/_gen/_metadata_timeout 全部改为 property 代理，
        # 本体唯一存于 registry——UI/测试的直访语义不变（兼容决策），而
        # _emit_error 裸写 _resolving 的 R-1 缺陷经由 clear_resolving_safe
        # 收编进锁段。property 读写一律**不持锁**（复合临界区由调用方
        # with self._lock 包裹；自持锁入口见 registry 的 *_safe/snapshot）。
        self._registry = TaskRegistry(cache_dir=self.cache_dir,
                                      ses_get=lambda: self._ses)

        self.scheduler = PreviewScheduler()

        # 持久化服务（阶段 1 抽出）：依赖以 getter/回调注入，persist 不反向依赖
        # 本类——因此 _tasks/_torrents/_ses 的重绑与置空都能实时反映。
        self._persist = TaskPersistence(PersistDeps(
            cache_dir=self.cache_dir, download_dir=self._download_dir,
            lock=self._lock,
            tasks_get=lambda: self._tasks,
            torrents_get=lambda: self._torrents,
            gen_get=lambda: self._gen,
            ses_get=lambda: self._ses,
            hash_key=registry_hash_key, find_record=self._find_record,
            put_record=self._put_record, record_cls=TaskRecord,
            result_from_torrent_info=self._result_from_torrent_info,
            activate_download=self._activate_download))

        # 会话核心（阶段 2 抽出）：会话构造/热更新/退出清理/告警循环/看门狗。
        # 与 persist 同一套注入约定：宿主成员（_ses/_running/_thread/…）经
        # getter/setter 闭包读写，session 不反向依赖本类；_metadata_timeout 等
        # UI/测试直访的属性仍留在宿主上（兼容决策）。
        self._sess = SessionCore(SessionDeps(
            listen_port=self.listen_port, active_downloads=self._active_downloads,
            lock=self._lock,
            ses_get=lambda: self._ses,
            ses_set=lambda v: setattr(self, "_ses", v),
            running_get=lambda: self._running,
            running_set=lambda v: setattr(self, "_running", v),
            thread_get=lambda: self._thread,
            thread_set=lambda v: setattr(self, "_thread", v),
            last_sweep_get=lambda: self._last_resume_sweep,
            last_sweep_set=lambda v: setattr(self, "_last_resume_sweep", v),
            metadata_timeout_get=lambda: self._metadata_timeout,
            metadata_timeout_set=lambda v: setattr(self, "_metadata_timeout", v),
            torrents_get=lambda: self._torrents,
            tasks_get=lambda: self._tasks,
            tasks_set=lambda v: setattr(self, "_tasks", v),
            hash_key=registry_hash_key, find_record=self._registry.find_record,
            current_record=self._registry.current_record,
            tasks_loader=lambda: load_tasks(
                self.cache_dir,
                warn=lambda m: log_warning("fetcher.restore.tasks", m)),
            restore_task=self._restore_task,
            thread_factory=lambda target: threading.Thread(
                target=target, daemon=True),
            scheduler_get=lambda: self.scheduler,
            persist_tasks=self._persist_tasks,
            write_resume_from_alert=self._write_resume_from_alert,
            request_resume=self._request_resume,
            drain_resume_alerts=self._drain_resume_alerts,
            on_metadata_received=self._on_metadata_received,
            on_download_finished=self._on_download_finished,
            emit_error=self._emit_error,
            clear_resolving=self._clear_resolving,
            clear_runtime_state=self._clear_runtime_state))

        # 任务 CRUD（阶段 4 抽出）：允许直接持 registry/persist 引用（无环
        # 下游）；_ses/scheduler 仍走回调（会被重绑/置空）。persist 的
        # activate_download 回调经本类薄委托打到 taskops（构造顺序无关）。
        self._taskops = TaskOps(reg=self._registry, persist=self._persist,
                                ses_get=lambda: self._ses,
                                scheduler_get=lambda: self.scheduler,
                                download_dir=self._download_dir)

    # ---------- 注册表别名（property 代理，本体在 self._registry） ----------
    # 读写不带锁：与迁移前的裸成员语义逐字节一致；需要原子性的复合操作
    # 必须在 with self._lock: 段内使用（既有调用点全部如此）。

    @property
    def _lock(self):
        return self._registry.lock

    @property
    def _torrents(self):
        return self._registry.torrents

    @property
    def _tasks(self):
        return self._registry.tasks

    @_tasks.setter
    def _tasks(self, v):
        self._registry.tasks = v

    @property
    def _current_ih(self):
        return self._registry.current_ih

    @_current_ih.setter
    def _current_ih(self, v):
        self._registry.current_ih = v

    @property
    def _handle(self):
        return self._registry.handle

    @_handle.setter
    def _handle(self, v):
        self._registry.handle = v

    @property
    def _result(self):
        return self._registry.result

    @_result.setter
    def _result(self, v):
        self._registry.result = v

    @property
    def _resolving(self):
        return self._registry.resolving

    @_resolving.setter
    def _resolving(self, v):
        self._registry.resolving = v

    @property
    def _resolve_started(self):
        return self._registry.resolve_started

    @_resolve_started.setter
    def _resolve_started(self, v):
        self._registry.resolve_started = v

    @property
    def _gen(self):
        return self._registry.gen

    @_gen.setter
    def _gen(self, v):
        self._registry.gen = v

    @property
    def _metadata_timeout(self):
        return self._registry.metadata_timeout

    @_metadata_timeout.setter
    def _metadata_timeout(self, v):
        self._registry.metadata_timeout = v

    # ---------- 生命周期 ----------

    def start(self, proxy: dict | None = None,
              metadata_timeout: float | None = None):
        """启动会话。proxy 见 core.config.lt_proxy_settings 的输入格式。

        实现见 core.session.SessionCore.start：会话配置构造 / 端口冲突回退 /
        任务清单恢复 / 告警线程拉起。
        """
        self._sess.start(proxy, metadata_timeout)

    @property
    def metadata_timeout(self) -> float:
        return self._metadata_timeout

    @property
    def download_dir(self) -> str:
        """任务下载根目录（绝对路径）。"""
        return self._download_dir

    def apply_proxy(self, proxy: dict) -> None:
        """运行时切换代理（无需重建会话）。实现见 core.session。"""
        self._sess.apply_proxy(proxy)

    def apply_rate_limit(self, kbps: int) -> None:
        """会话级下载限速（KB/s，0 = 不限）。热更新，无需重建会话。

        底层能力已在验收 §9 实证（download_mgr_test）；此处接线应用层
        配置项。libtorrent 单位为字节/秒，配置层用 KB/s 对用户友好。
        实现见 core.session。
        """
        self._sess.apply_rate_limit(kbps)

    def protected_dirs(self) -> set[str]:
        """配额清理保护名单：所有已注册记录的落盘目录（含预览与下载）。

        供 core.cache_quota 的 LRU 清理跳过活跃句柄目录——预览中的
        文件被占用，且活跃任务的预览数据删除后句柄读盘会失败。
        实现见 core.registry.TaskRegistry.protected_dirs。
        """
        return self._registry.protected_dirs()

    def shutdown(self):
        """停会话：落盘任务清单与 fastresume，再清全部句柄，最后 join 线程。

        实现见 core.session.SessionCore.shutdown。
        """
        self._sess.shutdown()

    def _clear_resolving(self):
        """清「当前解析」别名（须持锁，供 session 看门狗调用）。实现见 core.registry。"""
        self._registry.clear_resolving_locked()

    def _clear_runtime_state(self):
        """复位全部运行时状态（须持锁，供 session shutdown 调用）。实现见 core.registry。"""
        self._registry.clear_runtime_state_locked()

    # ---------- 解析入口（预览/查看清单，落盘 .preview/<ih>） ----------

    def resolve(self, source: str):
        """解析磁力链或 .torrent 文件（仅查看清单，不下载）。

        本地 .torrent 的 bencode 解析可能耗时（大文件），同样放到后台线程，
        避免卡死 UI 线程；磁力链本身由 libtorrent 后台完成。
        """
        self._begin_resolve()
        gen = self._gen
        if is_torrent_path(source):
            threading.Thread(target=self._resolve_torrent_file,
                             args=(source, gen), daemon=True).start()
        else:
            self._resolve_magnet(source)

    def _begin_resolve(self):
        """新解析前的换代：旧「当前任务」降级/让位，gen 递增防竞态。

        与旧 _reset_current 的差异：**不再无差别 remove 旧句柄**——
        - 下载任务（含 META_FETCH 等待中）：保留运行、继续下载，仅更换「当前」；
        - 已就绪（仅查看清单/预览）的 review 任务：保留在注册表
          （pause + upload_mode，供「转下载」复用分块），仅从「当前」降级；
        - 仍在解析（无结果）的 review 任务：等同旧行为移除（未成任务，不算删除）；
        - 真正移除句柄只发生在显式 remove_task()。
        """
        with self._lock:
            self.scheduler.stop()
            rec = self._current_record()
            if rec is not None and rec.download:
                pass  # 下载任务：保留运行（含 META_FETCH 等待元数据）
            elif rec is not None and rec.handle is not None and rec.result is not None:
                self._detach_record(rec)      # 仅查看清单：保留在 map
            else:
                if rec is not None and rec.handle is not None and self._ses is not None:
                    try:
                        self._ses.remove_torrent(rec.handle, 1)
                    except Exception as e:
                        log_warning("fetcher.begin_resolve.remove_torrent", f"{e}")
                if rec is not None and self._current_ih is not None:
                    self._torrents.pop(self._current_ih, None)
                # 游离句柄兜底（无注册表记录，不应出现）
                if self._handle is not None and self._ses is not None:
                    try:
                        self._ses.remove_torrent(self._handle, 1)
                    except Exception as e:
                        log_warning("fetcher.begin_resolve.remove_torrent", f"{e}")
            self._handle = None
            self._result = None
            self._resolving = True
            self._gen += 1          # 换代：旧解析任务完成后必须自弃
            self._resolve_started = time.time()
            self._current_ih = None

    def _detach_record(self, rec: TaskRecord) -> None:
        """把任务降级为「仅查看清单」（须持锁）。实现见 core.registry。"""
        self._registry.detach_record_locked(rec)

    def _preview_dir(self, ih: str) -> str:
        """review/预览落盘目录 ``cache_dir/.preview/<ih>``。见 core.registry。"""
        return self._registry.preview_dir(ih)

    def _put_record(self, ih: str, rec: TaskRecord,
                    make_current: bool = False) -> None:
        """写入注册表（须持锁）。实现见 core.registry.put_record_locked。"""
        self._registry.put_record_locked(ih, rec, make_current)

    def _register_current(self, handle, result: ParseResult | None,
                          gen: int) -> TaskRecord:
        """登记新解析句柄为「当前任务」（须持锁）。见 core.registry。"""
        return self._registry.register_current_locked(handle, result, gen)

    def _focus_existing_download(self, ih: str) -> TaskRecord | None:
        """resolve() 命中了已是下载任务的 ih：不重复添加句柄，焦点切到该任务。

        旧 UI 语义保持：有结果则把文件树/预览指向它（重新发射 on_metadata）；
        无结果（仍在 META_FETCH）则仅切换焦点。返回记录（未命中返回 None）。
        焦点切换本身（含换代）由 registry.focus_current 自持锁完成。
        """
        rec = self._registry.focus_current(ih)
        if rec is not None and rec.result is not None:
            self._emit_metadata(rec.result)
        return rec

    def _resolve_torrent_file(self, path: str, gen: int):
        try:
            result = parse_torrent_file(path)
        except Exception as e:
            if gen == self._gen:   # 旧代次的失败不打扰新会话
                log_exception("fetcher.resolve_torrent.parse", e)
                self._emit_error(f"种子文件解析失败：{e}")
            return
        # 该 ih 已是下载任务：切焦点复用句柄，不重复添加（去重边界 D2-1）
        if self._focus_existing_download(result.info_hash) is not None:
            return
        # 必须注入 cache_dir：主窗口据此建立「磁盘绝对路径 -> TorrentFile」映射，
        # 分块可用性判定与按需补拉都依赖它；缺失会导致键退化成相对路径而全部查不到，
        # 预览随即退化为「按完整静态文件服务」，把未下载的稀疏零数据喂给播放器。
        result.cache_dir = self.cache_dir
        result.save_subdir = self._save_subdir_of(
            self._preview_dir(result.info_hash))
        if gen != self._gen:
            return   # 期间用户已发起新解析：放弃本次结果
        atp = lt.add_torrent_params()
        atp.ti = lt.torrent_info(path)
        atp.save_path = self._preview_dir(result.info_hash)
        atp.flags |= lt.torrent_flags.upload_mode  # 只解析不下载
        try:
            handle = self._ses.add_torrent(atp)
            handle.pause()
        except Exception as e:
            if gen == self._gen:
                log_exception("fetcher.resolve_torrent.add", e)
                self._emit_error(f"加入会话失败：{e}")
            return
        with self._lock:
            if gen != self._gen:
                # 竞态兜底：换代后不再占用会话，句柄让位
                try:
                    self._ses.remove_torrent(handle, 1)
                except Exception as e:
                    log_warning("fetcher.resolve_torrent.genconflict", f"{e}")
                return
            self._register_current(handle, result, gen)
        self._emit_metadata(result)

    def connect_peer(self, ip: str, port: int, wait_handle: float = 5.0,
                     task_id: str | None = None) -> None:
        """手动添加 Peer（跳过 DHT 发现）。

        用于：本地回环验证、已知 Peer 直连，或网络屏蔽 DHT 时提高成功率。

        注意：本地 .torrent 解析已异步化（后台线程 add_torrent），
        调用方在 resolve() 后立即 connect 时 handle 可能尚未就绪，
        因此这里最多等待 wait_handle 秒直到 handle 出现（磁力链场景
        add_torrent 同步完成，等待立即返回）。

        ``task_id``：可选——指定要直连的任务（下载任务/重启恢复的任务并
        不必然是「当前任务」，此时必须按 task_id 定位其句柄；缺省沿用旧
        语义等待当前任务别名 _handle）。
        """
        key = (task_id or "").strip().lower() if task_id else None
        deadline = time.time() + max(0.0, wait_handle)
        while True:
            with self._lock:
                if key is not None:
                    rec = self._torrents.get(key)
                    handle = rec.handle if rec is not None else None
                else:
                    handle = self._handle
            if handle is not None:
                break
            if time.time() >= deadline:
                log_warning("fetcher.connect_peer",
                            f"等待会话 handle 超时，放弃直连 {ip}:{port}")
                return
            time.sleep(0.05)
        try:
            handle.connect_peer((ip, int(port)))
        except Exception as e:
            log_warning("fetcher.connect_peer", f"直连 Peer 失败：{e}")

    def _resolve_magnet(self, uri: str):
        try:
            p = lt.parse_magnet_uri(uri)
        except Exception as e:
            log_exception("fetcher.resolve_magnet.parse", e)
            self._emit_error(f"磁力链接无效：{e}")
            return
        ih_known = self._ih_from_params(p)
        if ih_known is not None \
                and self._focus_existing_download(ih_known) is not None:
            return   # 已是下载任务：切焦点复用句柄，不重复添加
        save_dir = self._preview_dir(ih_known or "")
        p.save_path = save_dir
        p.flags |= lt.torrent_flags.upload_mode  # 只取元数据，不下载资源
        if hasattr(p, "trackers") and not p.trackers:
            p.trackers = BOOTSTRAP_TRACKERS
        try:
            handle = self._ses.add_torrent(p)
        except Exception as e:
            log_exception("fetcher.resolve_magnet.add", e)
            self._emit_error(f"加入 DHT 会话失败：{e}")
            return
        with self._lock:
            rec = self._register_current(handle, None, self._gen)
        # 重复解析同一磁力链：libtorrent 返回既有句柄，元数据可能已就绪，
        # 不会再发 metadata_received_alert——此处直接走「元数据到达」快路径
        try:
            if rec.handle is not None and rec.handle.torrent_file() is not None:
                self._on_metadata_received(rec)
        except Exception as e:
            log_warning("fetcher.resolve_magnet.ready", f"{e}")

    # ---------- 下载任务 API（t5；阶段 4 抽出，实现见 core.taskops.TaskOps） ----------

    def add_task(self, source: str, save_subdir: str | None = None,
                 priority: int = 0, seed: bool = False) -> str:
        """添加下载任务（磁力链或 .torrent 路径）。见 core.taskops。"""
        return self._taskops.add_task(source, save_subdir, priority, seed)

    def _task_dir(self, ih: str, save_subdir: str | None = None) -> str:
        """下载任务落盘目录：``<下载根>/<子目录>``（默认子目录 = info_hash）。

        save_subdir 只允许单层干净相对段（防穿越）；不在 cache_dir 内时
        由 tasks() 以绝对路径暴露。实现见 core.persist.task_dir。
        """
        return persist_task_dir(self._download_dir, ih, save_subdir)

    def _convert_to_download(self, rec: TaskRecord, ih: str, source: str,
                             save_subdir: str | None, priority: int,
                             seed: bool) -> str:
        """review 记录转正为下载任务（D10，**须持锁**）。见 core.taskops。"""
        return self._taskops._convert_to_download_locked(
            rec, ih, source, save_subdir, priority, seed)

    def _activate_download(self, rec: TaskRecord) -> None:
        """让下载任务真正开始。实现见 core.taskops.TaskOps.activate_download。"""
        self._taskops.activate_download(rec)

    # ---------- 任务操作 API（实现见 core.taskops） ----------

    def set_priority(self, task_id: str, priority: int) -> bool:
        """设置任务优先级（0~3，0=默认/最低）。实现见 core.taskops。"""
        return self._taskops.set_priority(task_id, priority)

    def pause_task(self, task_id: str) -> bool:
        """暂停下载任务（pause + 撤 auto_managed）。实现见 core.taskops。"""
        return self._taskops.pause_task(task_id)

    def resume_task(self, task_id: str) -> bool:
        """恢复下载任务（含失败重试）。实现见 core.taskops。"""
        return self._taskops.resume_task(task_id)

    def remove_task(self, task_id: str, delete_files: bool = False) -> bool:
        """显式移除任务（唯一真正摘句柄的入口；D9 守卫删文件）。见 core.taskops。"""
        return self._taskops.remove_task(task_id, delete_files)

    def _delete_task_files(self, key: str, path: str) -> None:
        """删除任务落盘目录（受管守卫）。实现见 core.taskops。"""
        self._taskops.delete_task_files(key, path)

    def focus_task(self, task_id: str) -> bool:
        """把某下载任务设为「当前」。实现见 core.taskops。"""
        return self._taskops.focus_task(task_id)

    def tasks(self) -> list[dict]:
        """全任务快照（含派生字段 progress/down_rate/eta）。见 core.taskops。

        R-3：锁内一次快照拷贝，句柄 status() 出锁派生。
        """
        return self._taskops.tasks()

    # ---------- 预览 ----------

    def start_preview(self, f: TorrentFile):
        """开始预览某个文件（边下边播 / 图片下载）。"""
        with self._lock:
            handle, result = self._handle, self._result
        if handle is None or result is None:
            raise RuntimeError("请先解析种子")
        self.scheduler.begin(handle, f)

    def stop_preview(self):
        self.scheduler.stop()

    # ---------- 状态 ----------

    def _find_record_for_path(self, disk_path: str):
        """磁盘路径 -> (TaskRecord, TorrentFile) 反查：匹配任务落盘目录前缀。

        供流服务回调按实际磁盘路径定位任务句柄（下载中任务文件的分块
        可用性判定），返回 None 表示未知/无句柄（调用方不得降级为全量）。
        """
        np_ = os.path.normpath(disk_path)
        with self._lock:
            recs = list(self._torrents.values())
        for rec in recs:
            if not rec.save_path or rec.handle is None or rec.result is None:
                continue
            base = os.path.normpath(rec.save_path)
            if np_ == base or np_.startswith(base + os.sep):
                rel = os.path.relpath(np_, base).replace("\\", "/")
                for f in rec.result.files:
                    if f.path == rel:
                        return rec, f
        return None

    def piece_map_for_path(self, disk_path: str):
        """按磁盘路径提供分块可用性映射（下载任务文件；未知返回 None）。

        None 表示「无法判定可用性」——调用方（流服务 pieces_cb）绝不
        能把 None 当作整文件可用：那会把未下载的稀疏零数据喂给播放器
        （历史「partial file / Invalid data」缺陷的直接根源）。
        """
        hit = self._find_record_for_path(disk_path)
        if hit is None:
            return None
        rec, f = hit
        handle = rec.handle
        try:
            pl = handle.torrent_file().piece_length()
        except Exception as e:
            log_warning("fetcher.piece_map.piece_length", f"{e}")
            return None
        have = handle.have_piece
        return PieceMap(piece_length=pl, offset=f.offset,
                        start_piece=f.start_piece, end_piece=f.end_piece,
                        size=f.size, have=have)

    def demand_for_path(self, disk_path: str, start_byte: int,
                        end_excl: int) -> bool:
        """按磁盘路径触发任务级按需补拉（播放器要哪段就先下哪段）。

        与 scheduler.request_range 语义一致，但作用于任意下载任务句柄
        （流服务 demand_cb 对非预览文件的请求也生效）。
        """
        hit = self._find_record_for_path(disk_path)
        if hit is None:
            return False
        rec, f = hit
        try:
            pl = rec.handle.torrent_file().piece_length()
            if pl <= 0:
                return False
            first = max(f.start_piece,
                        f.start_piece + max(0, start_byte) // pl)
            last = min(f.end_piece,
                       f.start_piece + max(0, end_excl - 1) // pl)
            for p in range(first, last + 1):
                rec.handle.set_piece_deadline(p, 0)
            return True
        except Exception as e:
            log_warning("fetcher.demand_for_path", f"{disk_path}: {e}")
            return False

    def task_result(self, task_id: str) -> ParseResult | None:
        """下载任务的解析结果（供 UI 载入文件树/预览）。"""
        key = str(task_id or "")
        with self._lock:
            rec = self._torrents.get(key)
            return rec.result if rec is not None else None

    def have_piece(self, piece: int) -> bool:
        """指定种子分块是否已完整落盘（供流服务/调度器判定可读区间）。"""
        with self._lock:
            handle = self._handle
        if handle is None:
            return False
        try:
            return bool(handle.have_piece(int(piece)))
        except Exception as e:
            log_warning("fetcher.have_piece", f"分块查询失败 piece={piece}: {e}")
            return False

    def piece_length(self) -> int | None:
        """当前种子分块大小；元数据未就绪时返回 None。"""
        with self._lock:
            handle = self._handle
        if handle is None:
            return None
        try:
            if not handle.has_metadata():
                return None
            return handle.torrent_file().piece_length()
        except Exception as e:
            log_warning("fetcher.piece_length", f"{e}")
            return None

    def status(self) -> dict | None:
        """线程安全的状态快照，供 UI 定时轮询。"""
        with self._lock:
            handle = self._handle
        if handle is None:
            return None
        try:
            s = handle.status()
        except Exception as e:
            log_warning("fetcher.status.handle", f"{e}")
            return None
        st = {
            "state": STATE_NAMES.get(s.state, str(s.state)),
            "num_peers": s.num_peers,
            "num_seeds": s.num_seeds,
            "download_rate": s.download_payload_rate,
            "total_done": s.total_done,
            "metadata_ready": handle.has_metadata(),
        }
        # contiguous 与 buffer 同源（连续可读前缀），一次扫描复用——
        # 避免每 700ms 重复两次 O(分片数) 的 have_piece 线性扫描
        contig = self.scheduler.contiguous_progress() if self.scheduler.active else 0
        pf = self.scheduler.file
        st["buffer"] = (min(1.0, contig / pf.size)
                        if self.scheduler.active and pf is not None and pf.size > 0
                        else 0.0)
        st["contiguous"] = contig
        st["tail_ready"] = self.scheduler.tail_ready() if self.scheduler.active else True
        st["preview_file"] = pf
        # R-1 家族（D4）：resolving/elapsed 成对读走 registry 一致快照，
        # 不再锁外分两次读别名（撕裂窗口）。
        st["resolving"], st["elapsed"] = self._registry.resolving_snapshot()
        try:
            st["file_progress"] = list(handle.file_progress())
        except Exception as e:
            log_warning("fetcher.status.file_progress", f"{e}")
            st["file_progress"] = []
        return st

    @property
    def current_result(self) -> ParseResult | None:
        with self._lock:
            return self._result

    # ---------- 任务注册表 ----------

    @staticmethod
    def _hash_key(handle) -> str:
        """句柄的注册表键。实现见 core.registry.hash_key。"""
        return registry_hash_key(handle)

    @staticmethod
    def _ih_from_params(p) -> str | None:
        """add_torrent_params 的 info_hash 键提取。见 core.registry.ih_from_params。"""
        return registry_ih_from_params(p)

    def _current_record(self) -> TaskRecord | None:
        """当前任务注册表记录（调用方自行决定是否持锁）。实现见 core.registry。"""
        return self._registry.current_record()

    def _find_record(self, handle) -> TaskRecord | None:
        """按句柄查注册表（alert 归属校验用，自持锁）。实现见 core.registry。"""
        return self._registry.find_record(handle)

    # ---------- 目录与持久化辅助（实现在 core.persist） ----------

    @staticmethod
    def _is_within(root: str, path: str) -> bool:
        """path 是否位于 root 内（normcase + commonpath 前缀防护，契约 #9）。"""
        return persist_is_within(root, path)

    def _save_subdir_of(self, path: str) -> str:
        """落盘目录 -> tasks()['save_subdir']：cache_dir 内给相对路径，否则绝对。"""
        return persist_save_subdir_of(self.cache_dir, path)

    def _safe_task_save_path(self, ih: str, save_path: str) -> str:
        """磁盘任务记录的 save_path 消毒：逃出受管范围则回退默认目录。"""
        return persist_safe_task_save_path(self.cache_dir, self._download_dir,
                                           ih, save_path)

    def _persist_tasks(self) -> None:
        """原子写 .tasks.json（失败仅告警，不阻断任务操作）。"""
        self._persist.persist_tasks()

    def _read_resume(self, ih: str) -> bytes | None:
        """读单任务 fastresume 字节；不存在返回 None。"""
        return self._persist.read_resume(ih)

    def _request_resume(self, rec: TaskRecord) -> None:
        """请求写 fastresume（异步：save_resume_data_alert 落盘）。"""
        self._persist.request_resume(rec)

    def _write_resume_from_alert(self, a) -> None:
        """消费 save_resume_data_alert 落盘（归属校验由注册表完成）。"""
        self._persist.write_resume_from_alert(a)

    def _drain_resume_alerts(self, timeout: float = 3.0) -> None:
        """清理阶段直取残留告警，等待全部下载任务 fastresume 落盘（有界）。

        竞态背景与超时策略见 core.persist.TaskPersistence.drain_resume_alerts。
        """
        self._persist.drain_resume_alerts(timeout)

    def _restore_task(self, t: dict) -> bool:
        """启动恢复单个下载任务：resume_data 注入 + 隐式校验，损坏静默全新加入。

        任何失败只标记该任务 FAILED（不抛异常、绝不阻断启动）；
        实现见 core.persist.TaskPersistence.restore_task。
        """
        return self._persist.restore_task(t)

    def _on_download_finished(self, rec: TaskRecord):
        """torrent_finished_alert：任务完成；默认自动停止（D3），seed 则做种。"""
        key = self._hash_key(rec.handle) if rec.handle is not None else ""
        with self._lock:
            rec.resolving = False
            rec.resolve_started = 0.0
            if rec.seed:
                rec.state = STATE_SEEDING
            else:
                rec.state = STATE_COMPLETED
            if key in self._tasks:
                self._tasks[key]["state"] = rec.state
                self._tasks[key]["finished_at"] = time.time()
        try:
            if rec.seed:
                rec.handle.resume()
            else:
                rec.handle.pause()
                rec.handle.unset_flags(lt.torrent_flags.auto_managed)
        except Exception as e:
            log_warning("fetcher.finished", f"{e}")
        self._persist_tasks()
        with self._lock:
            self._request_resume(rec)

    def _on_metadata_received(self, rec: TaskRecord):
        """metadata_received_alert 处理：per-task 就绪。

        - review 记录：pause（拿到元数据即停，不下载），当前任务对外回调；
        - 下载任务：转入 DOWNLOADING（解除 upload_mode + 按所选文件优先级 +
          resume），仅当它是当前任务时才发全局 on_metadata（UI 展示）；
          状态机 META_FETCH → DOWNLOADING，暂停态保持暂停。
        """
        with self._lock:
            rec.resolving = False
            rec.resolve_started = 0.0
            is_current = rec is self._current_record()
            if is_current:
                self._resolving = False
                self._resolve_started = 0.0
            if rec.result is not None:
                return   # 已就绪（重复告警）：幂等跳过
            handle = rec.handle
            key = self._hash_key(handle) if handle is not None else ""
            was_paused = self._tasks.get(key, {}).get("state") == STATE_PAUSED
        if handle is None:
            return
        try:
            ti = handle.torrent_file()
            result = self._result_from_torrent_info(ti, str(handle.info_hash()))
        except Exception as e:
            log_exception("fetcher.metadata_received", e)
            with self._lock:
                rec.state = STATE_FAILED
                cur = rec is self._current_record()
                if rec.download:
                    rec.error = f"元数据处理失败：{e}"
                    if key in self._tasks:
                        self._tasks[key]["state"] = STATE_FAILED
                        self._tasks[key]["error"] = rec.error
            if cur and not rec.download:
                self._emit_error(f"元数据处理失败：{e}")
            if rec.download:
                self._persist_tasks()
            return
        result.cache_dir = self.cache_dir
        result.save_subdir = self._save_subdir_of(rec.save_path)
        with self._lock:
            rec.result = result
            if rec.download:
                if was_paused:
                    rec.state = STATE_PAUSED
                else:
                    rec.state = STATE_DOWNLOADING
                if key in self._tasks:
                    self._tasks[key].update({
                        "name": result.name,
                        "total_size": int(result.total_size),
                        "files": list(result.files),
                        "selected": [f.path for f in result.view_files],
                        "state": rec.state,
                        "error": "",
                    })
                    if rec.state == STATE_PAUSED:
                        self._tasks[key]["state"] = STATE_PAUSED
            else:
                rec.state = STATE_READY
            cur = rec is self._current_record()
            if cur:
                self._result = result
                if cur:
                    self._resolving = False
                    self._resolve_started = 0.0
            if rec.download:
                self._request_resume(rec)
        if rec.download:
            self._persist_tasks()
            if not was_paused:
                self._activate_download(rec)
        if cur:
            self._emit_metadata(result)

    def _result_from_torrent_info(self, ti, info_hash: str) -> ParseResult:
        fs = ti.files()
        pl = ti.piece_length()
        root = ti.name()
        files, offset = [], 0
        multi = fs.num_files() > 1
        for i in range(fs.num_files()):
            size = fs.file_size(i)
            fp = fs.file_path(i).replace("\\", "/")
            inner = fp if not multi else fp.split("/", 1)[-1] if "/" in fp else fp
            # safe_rel_path 兜底防御：即使 libtorrent 未净化恶意种子路径也不会逃出缓存目录
            # 单文件种子 libtorrent 存为 save_path/root，不能再套一层（否则预览 404）
            path = safe_rel_path(root, inner) if multi else safe_rel_path(root)
            start = offset // pl
            end = (offset + size - 1) // pl if size > 0 else start
            files.append(TorrentFile(i, path, size, offset, start, end))
            offset += size
        trackers = [e.url for e in ti.trackers()]
        return ParseResult(
            info_hash=info_hash,
            name=root,
            total_size=sum(f.size for f in files if not f.is_pad),
            piece_size=pl,
            num_pieces=ti.num_pieces(),
            files=files,
            trackers=trackers,
            comment=ti.comment(),
            created_by=ti.creator(),
            source="magnet",
        )

    # ---------- 回调发射 ----------

    def _emit_metadata(self, result: ParseResult):
        result.cache_dir = self.cache_dir
        if self.on_metadata:
            try:
                self.on_metadata(result)
            except Exception as e:
                log_exception("fetcher.emit_metadata", e)

    def _emit_error(self, msg: str):
        # R-1：裸写 _resolving（不持锁）的旧缺陷——经 registry 自持锁入口
        # 清除（告警线程与主线程交错的根治点，registry_test §G/§I 双向断言）。
        self._registry.clear_resolving_safe()
        if self.on_error:
            try:
                self.on_error(msg)
            except Exception as e:
                log_exception("fetcher.emit_error", e)