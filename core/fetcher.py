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
import threading

import libtorrent as lt

from .cache_guard import ensure_cache_dir
from .logutil import log_exception, log_warning
from .models import ParseResult, TorrentFile
from .preview import PreviewCore
from .preview import STATE_NAMES  # noqa: F401  再导出（历史兼容）
from .registry import (DOWNLOADS_SUBDIR, METADATA_TIMEOUT,  # noqa: F401
                       PREVIEW_SUBDIR, TaskRecord, TaskRegistry)
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
from .registry import hash_key as registry_hash_key
from .registry import ih_from_params as registry_ih_from_params
from .resolver import ResolverCore
from .resolver import result_from_torrent_info as resolver_result_from_ti
from .taskops import TaskOps
# 再导出：历史写法 from core.fetcher import STATE_* 仍须可用（download_mgr_test
# 在用）；常量本体已下沉到 core.states，避免 fetcher → persist → fetcher 环。
from .states import (BOOTSTRAP_TRACKERS, DOWNLOAD_STATES,  # noqa: F401
                     STATE_COMPLETED, STATE_DELETED, STATE_DOWNLOADING,
                     STATE_FAILED, STATE_META_FETCH, STATE_PAUSED,
                     STATE_QUEUED, STATE_READY, STATE_SEEDING, STATE_STOPPED,
                     STATE_VALIDATE)
from .taskstore import load_tasks



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
            result_from_torrent_info=resolver_result_from_ti,
            activate_download=self._activate_download))

        # 会话核心（阶段 2 抽出）：会话构造/热更新/退出清理/告警循环/看门狗。
        # 与 persist 同一套注入约定：宿主成员（_ses/_running/_thread/…）经
        # getter/setter 闭包读写，session 不反向依赖本类；_metadata_timeout
        # 兼容 property 留在宿主（contract [3] 冻结的直读依赖；UI 写路径
        # 自阶段 6 起走公开方法 apply_metadata_timeout，R-4 收编）。
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

        # 解析编排（阶段 5 抽出）：resolve 两入口 / 换代 / connect_peer /
        # 两条 alert 处理链。emit_* 经本类委托回调（构造顺序无关）。
        self._resolver = ResolverCore(
            reg=self._registry, ops=self._taskops, cache_dir=self.cache_dir,
            ses_get=lambda: self._ses,
            scheduler_get=lambda: self.scheduler,
            persist_tasks=self._persist_tasks,
            request_resume=self._request_resume,
            emit_metadata=self._emit_metadata,
            emit_error=self._emit_error)

        # 预览桥（阶段 5 抽出）：磁盘路径反查 / PieceMap / 点播补拉 / 状态快照
        self._preview = PreviewCore(reg=self._registry,
                                    scheduler_get=lambda: self.scheduler)

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

    def apply_metadata_timeout(self, seconds: float) -> None:
        """运行时更新元数据超时（秒）。热生效：看门狗每轮读会话级值。

        R-4（P2-10 收编）：UI 经本方法更新，不再直写私有成员。
        """
        self._registry.metadata_timeout = float(seconds)

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

    # ---------- 解析入口（预览/查看清单，落盘 .preview/<ih>；阶段 5 抽出） ----------

    def resolve(self, source: str):
        """解析磁力链或 .torrent 文件（仅查看清单，不下载）。实现见 core.resolver。"""
        self._resolver.resolve(source)

    def _begin_resolve(self):
        """新解析换代（旧「当前任务」降级/让位）。见 core.resolver.begin_resolve。"""
        self._resolver.begin_resolve()

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
        """命中已有下载任务则切焦点。实现见 core.resolver。"""
        return self._resolver._focus_existing_download(ih)

    def _resolve_torrent_file(self, path: str, gen: int):
        """本地 .torrent 后台解析（实现见 core.resolver，线程入口保留签名）。"""
        self._resolver._resolve_torrent_file(path, gen)

    def connect_peer(self, ip: str, port: int, wait_handle: float = 5.0,
                     task_id: str | None = None) -> None:
        """手动添加 Peer（跳过 DHT）。实现见 core.resolver.ResolverCore。"""
        self._resolver.connect_peer(ip, port, wait_handle, task_id)

    def _resolve_magnet(self, uri: str):
        """磁力链解析（实现见 core.resolver）。"""
        self._resolver._resolve_magnet(uri)

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

    # ---------- 预览与状态（阶段 5 抽出，实现见 core.preview.PreviewCore） ----------

    def start_preview(self, f: TorrentFile):
        """开始预览某个文件（边下边播 / 图片下载）。实现见 core.preview。"""
        self._preview.start_preview(f)

    def stop_preview(self):
        self._preview.stop_preview()

    def _find_record_for_path(self, disk_path: str):
        """磁盘路径 -> (TaskRecord, TorrentFile) 反查。实现见 core.preview。"""
        return self._preview.find_record_for_path(disk_path)

    def piece_map_for_path(self, disk_path: str):
        """按磁盘路径提供分块可用性映射（None=不可判定）。见 core.preview。"""
        return self._preview.piece_map_for_path(disk_path)

    def demand_for_path(self, disk_path: str, start_byte: int,
                        end_excl: int) -> bool:
        """按磁盘路径触发任务级按需补拉。实现见 core.preview。"""
        return self._preview.demand_for_path(disk_path, start_byte, end_excl)

    def task_result(self, task_id: str) -> ParseResult | None:
        """下载任务的解析结果。实现见 core.preview。"""
        return self._preview.task_result(task_id)

    def have_piece(self, piece: int) -> bool:
        """指定分块是否已完整落盘（当前任务）。实现见 core.preview。"""
        return self._preview.have_piece(piece)

    def piece_length(self) -> int | None:
        """当前种子分块大小；元数据未就绪返回 None。见 core.preview。"""
        return self._preview.piece_length()

    def status(self) -> dict | None:
        """线程安全状态快照（UI 轮询）。实现见 core.preview.PreviewCore.status。"""
        return self._preview.status()

    @property
    def current_result(self) -> ParseResult | None:
        return self._preview.current_result()

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
        """torrent_finished 处理（D3）。实现见 core.resolver.on_download_finished。"""
        self._resolver.on_download_finished(rec)

    def _on_metadata_received(self, rec: TaskRecord):
        """metadata_received 处理（per-task 就绪）。见 core.resolver。"""
        self._resolver.on_metadata_received(rec)

    def _result_from_torrent_info(self, ti, info_hash: str) -> ParseResult:
        """torrent_info -> ParseResult。实现见 core.resolver（模块级纯函数）。"""
        return resolver_result_from_ti(ti, info_hash)

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