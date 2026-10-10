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
任务生命周期常量与 bootstrap tracker 下沉到 ``core.states``。

facade 契约（Wave 2 瘦身，docs/ARCHITECTURE.md §2.2 / ADR-0002）：本模块是
UI 唯一直接触达的 core 入口，对外只承诺 ``SessionManager``（见 ``__all__``）
与其公开方法/薄委托。历史「再导出」兼容层已全部移除——``STATE_*`` /
``STATE_NAMES`` / ``DOWNLOAD_STATES`` / ``BOOTSTRAP_TRACKERS`` 从
``core.states`` import，``TaskRecord`` / ``METADATA_TIMEOUT`` / ``*_SUBDIR``
从 ``core.registry`` import，``load_tasks`` / ``save_tasks`` 等从
``core.taskstore`` import。仅保留两类例外：① ``persist_*`` /
``registry_*`` / ``resolver_*`` 注入别名（本模块委托方法体在用，且
persist_test/resolver_test 以「monkeypatch fetcher 模块属性」验证委托
路由，属依赖注入实用，非兼容层）；② ``cache_mode`` 两常量经本模块
再导出（UI 预览缓存模式文案/校验的唯一取值来源）。
"""
from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

import libtorrent as lt

# cache_guard 标记常量 + 四函数：ensure_cache_dir/sweep_tmp_files 本模块
# __init__/start 在用；其余三符号（CACHE_MARKER/clear_cache_contents/
# guard_ok_for_cleanup）是 facade 给 UI 的再导出（§2.2 白名单）——它们接受
# **显式 path** 参数（设置面板作用于「编辑框里尚未保存的新目录」），语义上
# 不是 SessionManager 的成员操作，故以模块级再导出而非委托方法暴露，
# 绑定 self.cache_dir 反而错。
from .cache_guard import (  # noqa: F401  CACHE_MARKER 等 = facade 再导出
    CACHE_MARKER, clear_cache_contents, ensure_cache_dir,
    guard_ok_for_cleanup, sweep_tmp_files)
# cache_mode 两常量：PREVIEW_CACHE_CONVERT 本模块 stop_preview 分支在用；
# PREVIEW_CACHE_MODES 供 UI（设置面板下拉值域）经 facade 取用——§2.2 规定
# ui/* 只准 import core.models / core.config / core.fetcher / core.logutil，
# 常量再导出在此是 facade 契约的一部分，不是历史兼容层。
from .cache_mode import (PREVIEW_CACHE_CONVERT,  # noqa: F401
                         PREVIEW_CACHE_MODES)
from .logutil import log_exception, log_warning
from .persist import (PersistDeps, TaskPersistence,
                      is_within as persist_is_within,
                      safe_task_save_path as persist_safe_task_save_path,
                      save_subdir_of as persist_save_subdir_of,
                      task_dir as persist_task_dir)
from .preview import PreviewCore
from .registry import (DOWNLOADS_SUBDIR, PREVIEW_SUBDIR, TaskRecord,
                       TaskRegistry, hash_key as registry_hash_key,
                       ih_from_params as registry_ih_from_params)
from .resolver import (ResolverCore,
                       result_from_torrent_info as resolver_result_from_ti)
from .scheduler import PreviewScheduler
from .session import DHT_STATE_FILE, SessionCore, SessionDeps
from .taskops import TaskOps

if TYPE_CHECKING:
    # 仅类型注解使用（__future__ annotations 下注解惰性求值，运行时不导入；
    # 移出顶部 import 面是 Wave 2 「相对 import ≤10」瘦身的组成部分）
    from .models import ParseResult, TorrentFile



class SessionManager:
    """持有 libtorrent 会话与后台 alert 循环线程。"""

    def __init__(self, cache_dir: str, listen_port: int = 6881,
                 download_dir: str | None = None,
                 active_downloads: int = 3,
                 cache_mode_get=None,
                 port_mapping_get=None,
                 listen_interface_get=None,
                 extra_trackers_get=None):
        """cache_mode_get: 可选回调 -> str，预览缓存模式（preview_cache_mode）
        的取值来源（UI 注入 AppConfig.get；core 层不 import Qt）。None/异常
        兜底为 convert（与 DEFAULTS 一致）。

        port_mapping_get / listen_interface_get: 可选回调 -> bool / str，
        入站能力配置的取值来源（同样由 UI 注入）。None/异常分别兜底
        False / ""，即「只收不做种 + 全网卡监听」的既有行为。二者在会话
        启动时固定；port_mapping 另可运行时经 apply_port_mapping 热更新。

        extra_trackers_get: 可选回调 -> bool，无 tr= 磁力链是否追加
        HTTP/HTTPS 兜底 tracker（UDP 出站受阻网络的第二条发现通道）。
        None/异常兜底 False，即仅注入既有 5 条 udp tracker。
        """
        self._cache_mode_get = cache_mode_get
        self._port_mapping_get = port_mapping_get
        self._listen_interface_get = listen_interface_get
        self._extra_trackers_get = extra_trackers_get
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
            tasks_loader=self._load_tasks_for_restore,
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
            clear_runtime_state=self._clear_runtime_state,
            # 入站能力（D-2026-09-18）：port_mapping 开 UPnP/NAT-PMP（做种与
            # 连 NAT 后 peer 的前提），listen_interface 显式绑定出口网卡。
            # 二者读一次固定；回调缺失/异常一律回落既有行为。
            port_mapping=self._cfg_port_mapping(),
            listen_interface=self._cfg_listen_interface(),
            # 代理自检（D-2026-09-18 加固）：注真实探测函数——只有生产侧走
            # 网络探测，测试替身不传该字段即保持「不探测、不额外报错」。
            # 经 _proxy_probe 薄委托延迟导入 proxycheck（Wave 2 facade 瘦身：
            # 顶部相对 import ≤10，proxycheck 只在探测线程里真正用到）。
            proxy_probe=self._proxy_probe,
            dht_state_path=os.path.join(self.cache_dir, DHT_STATE_FILE)))

        # 任务 CRUD（阶段 4 抽出）：允许直接持 registry/persist 引用（无环
        # 下游）；_ses/scheduler 仍走回调（会被重绑/置空）。persist 的
        # activate_download 回调经本类薄委托打到 taskops（构造顺序无关）。
        self._taskops = TaskOps(reg=self._registry, persist=self._persist,
                                ses_get=lambda: self._ses,
                                scheduler_get=lambda: self.scheduler,
                                download_dir=self._download_dir,
                                on_meta_ready=self._on_metadata_received)

        # 解析编排（阶段 5 抽出）：resolve 两入口 / 换代 / connect_peer /
        # 两条 alert 处理链。emit_* 经本类委托回调（构造顺序无关）。
        self._resolver = ResolverCore(
            reg=self._registry, ops=self._taskops, cache_dir=self.cache_dir,
            ses_get=lambda: self._ses,
            scheduler_get=lambda: self.scheduler,
            persist_tasks=self._persist_tasks,
            request_resume=self._request_resume,
            emit_metadata=self._emit_metadata,
            emit_error=self._emit_error,
            extra_trackers_get=self._extra_trackers_get)

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

    @staticmethod
    def _proxy_probe(px, t):
        """代理可达性探测（session 后台线程调用）。

        proxycheck 延迟到调用点导入：Wave 2 facade 瘦身把顶部相对 import
        压到 ≤10 行，而 proxycheck 在本模块仅此一处运行时引用（探测在
        session 的探测线程里执行，构造期不触发导入）。
        """
        from . import proxycheck
        return proxycheck.probe(px, t)

    def _load_tasks_for_restore(self):
        """启动恢复读任务清单（session.tasks_loader 注入点）。

        taskstore 延迟到调用点导入（同上，Wave 2 瘦身；调用时机是
        SessionCore.start 的恢复段，非构造期）。
        """
        from .taskstore import load_tasks
        return load_tasks(
            self.cache_dir,
            warn=lambda m: log_warning("fetcher.restore.tasks", m))

    def start(self, proxy: dict | None = None,
              metadata_timeout: float | None = None):
        """启动会话。proxy 见 core.config.lt_proxy_settings 的输入格式。

        实现见 core.session.SessionCore.start：会话配置构造 / 端口冲突回退 /
        任务清单恢复 / 告警线程拉起。
        """
        # plan/11 R-14：启动时清扫崩溃遗留的 ``*.tmp`` 孤儿（唯一临时名引入
        # 后，崩溃残留不再被覆盖、会累积）。启动时刻无任何并发写者，是最安全
        # 的清扫点；**绝不**放到写路径上（与 plan/08 D3-b 的结论冲突）。
        # 失败只告警：清扫是旁路设施，绝不阻断启动。
        try:
            stale = sweep_tmp_files(self.cache_dir)
            if stale:
                log_warning("fetcher.sweep_tmp",
                            f"清扫崩溃遗留临时文件 {stale} 个")
        except OSError as e:   # 清扫只涉文件系统操作；旁路设施，失败绝不阻断启动
            log_warning("fetcher.sweep_tmp", f"{e}")
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

    # ---- 入站能力（诊断 D-2026-09-18）----

    def _cfg_port_mapping(self) -> bool:
        """端口映射开关取值（供 __init__ 构造 SessionDeps）。

        回调缺失 / 抛异常一律兜底 False，保持「只收不做种」的既有默认。
        """
        if not self._port_mapping_get:
            return False
        try:
            return bool(self._port_mapping_get())
        except Exception as e:
            # 兜底：宿主回调边界（UI 注入的配置闭包，异常类型不可枚举），
            # 读取失败按 False 回落「只收不做种」既有默认
            log_warning("fetcher.port_mapping", f"读取端口映射配置失败：{e}")
            return False

    def _cfg_listen_interface(self) -> str:
        """出口网卡 IP 取值（供 __init__ 构造 SessionDeps）。

        回调缺失 / 抛异常一律兜底空串（= 0.0.0.0 全网卡监听，既有行为）。
        """
        if not self._listen_interface_get:
            return ""
        try:
            return str(self._listen_interface_get() or "").strip()
        except Exception as e:
            # 兜底：宿主回调边界（UI 注入的配置闭包，异常类型不可枚举），
            # 读取失败按空串回落 0.0.0.0 全网卡监听既有默认
            log_warning("fetcher.listen_interface", f"读取出口网卡配置失败：{e}")
            return ""

    def apply_port_mapping(self, enabled: bool) -> None:
        """运行时开关 UPnP/NAT-PMP 端口映射（热更新，无需重建会话）。

        做种与「连上 NAT 后 peer」都依赖入站端口：关闭时 swarm 里无开放
        端口的 peer 连不上，且做种时无人能连入 → 客户端报做种失败。
        实现见 core.session。
        """
        self._sess.apply_port_mapping(enabled)

    def protected_dirs(self) -> set[str]:
        """配额清理保护名单：所有已注册记录的落盘目录（含预览与下载）。

        供 core.cache_quota 的 LRU 清理跳过活跃句柄目录——预览中的
        文件被占用，且活跃任务的预览数据删除后句柄读盘会失败。
        实现见 core.registry.TaskRegistry.protected_dirs。
        """
        return self._registry.protected_dirs()

    # ---------- UI facade 委托（§2.2：ui/* 只准 import models/config/
    # fetcher/logutil，cache_guard/cache_mode/cache_quota/proxycheck/
    # stream_server/inbound 的调用面全部经本段收敛） ----------

    def preview_root(self) -> str:
        """预览缓存根目录 ``<cache_dir>/.preview``（UI 配额/清理路径入料）。"""
        return os.path.join(self.cache_dir, PREVIEW_SUBDIR)

    def enforce_preview_cache_limit(self, limit_mb: int, keep_dirs,
                                    warn=None) -> tuple[int, int]:
        """预览缓存配额执行（薄委托 core.cache_quota.enforce_preview_limit）。

        ``keep_dirs`` 透传集合或零参回调（TOCTOU 逐条复核语义由 cache_quota
        决定，本层不掺逻辑）。
        """
        from .cache_quota import enforce_preview_limit
        return enforce_preview_limit(self.preview_root(), limit_mb,
                                     keep_dirs, warn=warn)

    def preview_downloaded_bytes(self) -> int:
        """当前任务已下载字节（file_progress 汇总；薄委托 cache_quota +
        本类 status()，UI 状态栏「缓存占用」的唯一口径来源）。"""
        from .cache_quota import downloaded_bytes
        st = self.status()
        return downloaded_bytes((st or {}).get("file_progress") or [])

    def proxy_hint(self, proxy: dict | None) -> str:
        """已配置代理时的错误提示文案（薄委托 proxycheck.hint_text）。

        纯文案、不联网不阻塞——UI 的等待超时/失败提示拼接用。
        """
        from .proxycheck import hint_text
        return hint_text(proxy)

    def inbound_selfcheck(self, listen_port: int | None = None,
                          timeout: float = 3.0) -> dict:
        """入站能力自检报告（薄委托 core.inbound.inbound_report）。

        阻塞约 timeout 秒，调用方（UI）必须放后台线程。core.inbound 延迟
        导入：探测失败/模块异常经 ImportError 冒泡给调用方兜底（与 UI 既有
        except Exception 语义一致），构造期不引入该依赖。
        """
        from . import inbound
        port = (int(listen_port) if listen_port is not None
                else int(self.listen_port or 6881))
        return inbound.inbound_report(listen_port=port, timeout=timeout)

    def inbound_hint_text(self, report: dict) -> str:
        """入站报告 -> 可读文案（薄委托 core.inbound.format_hint）。"""
        from . import inbound
        return inbound.format_hint(report)

    def create_stream_server(self, pieces_cb=None, demand_cb=None):
        """构造本地流服务（薄委托 core.stream_server.StreamServer 工厂）。

        多根：cache_dir + 本会话 download_dir（配置在缓存外时下载任务文件
        仍可服务）。返回实例由调用方持有并负责 start/shutdown——UI 生命周期
        语义不变，本模块不注册它（会话停机顺序由 UI 编排）。
        """
        from .stream_server import StreamServer
        return StreamServer(self.cache_dir, pieces_cb=pieces_cb,
                            demand_cb=demand_cb,
                            bases=[self._download_dir])

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
                             seed: bool,
                             selected_files: list[str] | None = None) -> str:
        """review 记录转正为下载任务（D10，**须持锁**）。见 core.taskops。"""
        return self._taskops._convert_to_download_locked(
            rec, ih, source, save_subdir, priority, seed,
            selected_files=selected_files)

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
        """停预览。阶段 B 两档语义（preview_cache_mode，下次关闭预览时生效）：

        - ``convert``（默认）：锁内快照 (rec, ih, 预览文件) → 出锁 scheduler.stop
          (release_only=True)（只清 deadline/锚点，不 pause、不清优先级）→
          预览态记录自动转正（D10 复用 _convert_to_download，不复制
          逻辑；selected 写入预览文件路径，与实际下载集对齐——Critical-1）→
          出锁收尾 persist/activate/request_resume（P1-4 同款编排）。
          已是下载任务 / 元数据未就绪 / 无预览 → 仅 release，不重复转正。
        - ``hold``：基线行为，与迁移前逐字一致（冻结暂停）。

        转正后目录仍是 .preview/<ih>（沿用已落盘分块，零重下），天然进入
        protected_dirs() 保护名单；引擎继续按 begin() 置下的 file-priority 4
        全量缓存（A0 实证）。
        """
        try:
            mode = str(self._cache_mode_get()
                       if self._cache_mode_get else PREVIEW_CACHE_CONVERT)
        except Exception as e:
            # 兜底：宿主回调边界（UI 注入的配置闭包，类型不可枚举），
            # 读取失败按默认 convert 继续
            log_warning("fetcher.stop_preview.mode", f"{e}，按默认 convert")
            mode = PREVIEW_CACHE_CONVERT
        if mode != PREVIEW_CACHE_CONVERT:
            self._preview.stop_preview()      # hold：基线路径原样
            return
        reg = self._registry
        with reg.lock:                        # 锁内零 libtorrent：只取快照
            handle = self.scheduler.handle
            ih = registry_hash_key(handle) if handle is not None else None
            rec = reg.torrents.get(ih) if ih else None
            snap = None
            if (rec is not None and rec.handle is handle
                    and not rec.download and rec.result is not None):
                # 预览文件路径必须在 scheduler 还活着时快照（转正 selected
                # 的入料；stop 后 self.file 归 None）。
                sched_file = self.scheduler.file
                snap = (rec, ih,
                        sched_file.path if sched_file is not None else None)
        self._preview.stop_preview(release_only=True)
        if snap is None:
            return
        rec, ih, preview_path = snap
        # review 记录的 rec.source 从不回填（register_current 不设），兜底用
        # 磁力链参数重建来源（转正清单的 source 是重启恢复的入料）。
        source = rec.source or \
            f"magnet:?xt=urn:btih:{rec.result.info_hash}"
        with reg.lock:
            # TOCTOU 复核（审查 Minor-4）：release 期间 add_task 可能已把
            # 该记录转正（种子级全选）——再转会把它的 selected 刷没。
            if rec.download:
                return
            self._convert_to_download(
                rec, ih, source, None, rec.priority, bool(rec.seed),
                selected_files=[preview_path] if preview_path else None)
        self._persist.persist_tasks()
        self._taskops.activate_download(rec, preserve_files=True)
        self._persist.request_resume(rec)

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

    def buffered_segments_of_preview(self):
        """当前预览文件的已缓存分段（文件内字节区间 [start, end) 列表）。

        供进度条着色。plan/09 起走**增量视图**：小块用一次基线位图精确扫描，
        大块走降采样（≤512 次绑定调用），不再在 Python 里逐块索引位图
        （旧实现在 40 万块文件上实测 8.4ms/轮，且整个循环挂在 GUI 线程）。
        无预览 / 状态不可用 / 无视图且位图取不到时返回 None（进度条退化为不着色）。
        """
        with self._lock:
            active = self.scheduler.active
            f = self.scheduler.file
            handle = self.scheduler.handle
        if not active or f is None or handle is None:
            return None
        try:
            ti = handle.torrent_file()
            if ti is None:
                return None
            pl = ti.piece_length()
            if pl <= 0:
                return None
            # plan/09：分段是**唯一**仍需要「整片块状态」的用途，交给增量视图
            # ——全部走实时 have_piece 探测，探测点数封顶在 SEG_PROBE_BUDGET
            # （区间块数 ≤ 预算时逐块精确），**不再在 Python 里逐块索引位图**
            # （旧实现在 40 万块文件上实测 8.4ms/轮，且整个循环挂在 GUI 线程）。
            runs = None
            view = getattr(self.scheduler, "piece_view", None)
            if view is not None:
                runs = view.buffered_runs(f.start_piece, f.end_piece)
            if runs is None:
                # 无视图（未注入 / 测试替身 / 视图不可用）→ 退回位图精确扫描，
                # 行为与基线逐字一致
                pieces = getattr(handle.status(), "pieces", None)
                if not pieces:
                    return None
                runs = self._runs_from_bitmap(pieces, f.start_piece,
                                              f.end_piece)
            return [self._piece_range_bytes(a, b, pl, f) for a, b in runs]
        except (OSError, RuntimeError, ValueError, TypeError) as e:
            # 收窄：段计算 = lt 句柄调用（RuntimeError）+ 数值转换（ValueError/TypeError）
            log_warning("fetcher.buffered_segments", f"{e}")
            return None

    @staticmethod
    def _runs_from_bitmap(pieces, sa: int, sb: int) -> list[tuple[int, int]]:
        """位图 → ``[sa, sb]`` 内的连续已落盘段（块号闭区间列表）。

        基线口径（大块降采样可复算它作对照）；只在「无增量视图」时使用。
        """
        segs: list[tuple[int, int]] = []
        run = None                          # 连续已落盘段的起始块
        for p in range(sa, sb + 1):
            if pieces[p]:
                if run is None:
                    run = p
            elif run is not None:
                segs.append((run, p - 1))
                run = None
        if run is not None:
            segs.append((run, sb))
        return segs

    @staticmethod
    def _piece_range_bytes(first: int, last: int, pl: int, f) -> tuple[int, int]:
        """块号闭区间 [first, last] → 文件内字节区间 [start, end)。"""
        start = max(0, first * pl - f.offset)
        end = min(f.size, (last + 1) * pl - f.offset)
        return (start, max(start, end))

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
                # 兜底：UI 线程回调边界（Qt signal emit，异常类型不可枚举），
                # 回调失败不得打断告警线程
                log_exception("fetcher.emit_metadata", e)

    def _emit_error(self, msg: str):
        # R-1：裸写 _resolving（不持锁）的旧缺陷——经 registry 自持锁入口
        # 清除（告警线程与主线程交错的根治点，registry_test §G/§I 双向断言）。
        self._registry.clear_resolving_safe()
        if self.on_error:
            try:
                self.on_error(msg)
            except Exception as e:
                # 兜底：UI 线程回调边界（Qt signal emit，异常类型不可枚举），
                # 回调失败不得打断告警线程
                log_exception("fetcher.emit_error", e)


# facade 对外契约（ADR-0002 决策 2-②：把「facade 暴露什么」从隐式变显式）。
# 列的是 UI（§2.2 唯一外部消费者）实际经 core.fetcher 取用的符号全集：
# SessionManager 是主入口；cache_mode 两常量 + cache_guard 三函数/标记是
# 「路径参数化」的守卫/清理能力——settings_dialog 校验「编辑框里尚未保存的
# 新目录」、main_window 在 SessionManager 构造前就要 ensure_cache_dir，都
# 无法走绑定 self.cache_dir 的委托方法，故以模块级再导出提供（见上方 import
# 注释）。内部注入别名（persist_*/registry_*/resolver_*）与子系统类
# （SessionCore/TaskRegistry 等）不进 __all__——只在组装逻辑与测试白盒断言
# 里出现，不是 facade API。
__all__ = [
    "SessionManager",
    "CACHE_MARKER", "clear_cache_contents", "ensure_cache_dir",
    "guard_ok_for_cleanup",
    "PREVIEW_CACHE_CONVERT", "PREVIEW_CACHE_MODES",
]