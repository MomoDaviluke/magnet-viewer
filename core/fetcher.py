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
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from dataclasses import dataclass

import libtorrent as lt

from .cache_guard import ensure_cache_dir
from .config import lt_proxy_settings
from .logutil import log_exception, log_warning
from .models import ParseResult, PieceMap, TorrentFile, safe_rel_path
from .parser import is_torrent_path, parse_torrent_file
from .resume import resume_path, write_resume
from .scheduler import PreviewScheduler
from .taskstore import (load_tasks, normalize_info_hash, save_tasks,
                        task_from_result, upsert_task)

METADATA_TIMEOUT = 90.0  # 秒，超时判定为资源无做种

# 任务生命周期状态机（对齐 plan/t1 §3 与 downloads_pane 的 STATE_META 命名）：
# QUEUED → META_FETCH → VALIDATE → DOWNLOADING ⇄ PAUSED → COMPLETED → STOPPED；
# FAILED / DELETED 为终态；默认完成后自动停止（不做种，决策 D3）。
STATE_QUEUED = "QUEUED"
STATE_META_FETCH = "META_FETCH"
STATE_VALIDATE = "VALIDATE"
STATE_DOWNLOADING = "DOWNLOADING"
STATE_PAUSED = "PAUSED"
STATE_COMPLETED = "COMPLETED"
STATE_STOPPED = "STOPPED"
STATE_FAILED = "FAILED"
STATE_SEEDING = "SEEDING"
STATE_DELETED = "DELETED"
STATE_READY = "READY"        # 内部态：仅查看清单/预览（review 记录专用，
                             # 不持久化、不进入 tasks() 快照）

DOWNLOAD_STATES = {STATE_QUEUED, STATE_META_FETCH, STATE_VALIDATE,
                   STATE_DOWNLOADING, STATE_PAUSED, STATE_COMPLETED,
                   STATE_STOPPED, STATE_FAILED, STATE_SEEDING, STATE_DELETED}

# 节目目录名：下载任务根与预览缓存根（决策 D7）
DOWNLOADS_SUBDIR = "downloads"
PREVIEW_SUBDIR = ".preview"


@dataclass
class TaskRecord:
    """任务注册表条目：一个 info_hash 唯一对应一个 libtorrent 句柄。"""
    handle: lt.torrent_handle | None = None
    result: ParseResult | None = None      # 元数据就绪后的解析结果
    gen: int = 0                           # 创建代次（防旧解析覆盖新会话）
    resolving: bool = False                # 是否仍在等待元数据
    resolve_started: float = 0.0           # 本次解析开始时间（per-task 看门狗）
    state: str = STATE_META_FETCH
    timeout: float | None = None           # 覆盖默认元数据超时（None=会话级）
    download: bool = False                 # 是否为持久化下载任务（add_task 系）
    seed: bool = False                     # 完成后是否做种（D3 默认否）
    priority: int = 0                      # 任务优先级（0~3，0=默认）
    save_path: str = ""                    # 任务落盘目录（绝对路径）
    source: str = ""                       # 来源（磁力链或 .torrent 路径）
    error: str = ""                        # 最近错误（UI 可见）


# 公共 tracker，提升冷门磁力链的 peer 发现率
BOOTSTRAP_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
]

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
        self._lock = threading.Lock()

        # 任务注册表：info_hash_hex -> TaskRecord（权威数据）
        self._torrents: dict[str, TaskRecord] = {}
        self._current_ih: str | None = None   # 当前任务/当前预览的注册表键

        # 持久化下载任务清单：info_hash -> task dict（.tasks.json 内存镜像）
        self._tasks: dict[str, dict] = {}
        self._last_resume_sweep = 0.0         # 60s 周期 fastresume 脏写

        # 「当前任务」别名（预览与状态路径继续使用，语义不变）
        self._handle: lt.torrent_handle | None = None
        self._gen = 0              # 解析代次：后台解析完成后校验，防旧任务覆盖新会话
        self._metadata_timeout = METADATA_TIMEOUT
        self._result: ParseResult | None = None
        self._resolving = False
        self._resolve_started = 0.0

        self.scheduler = PreviewScheduler()

    # ---------- 生命周期 ----------

    def start(self, proxy: dict | None = None,
              metadata_timeout: float | None = None):
        """启动会话。proxy 见 core.config.lt_proxy_settings 的输入格式。"""
        # libtorrent 2.1.x：settings_pack 已被 session_params / dict 配置取代
        # UPnP/NAT-PMP 默认关闭：本应用只“收”不做种，端口映射徒增暴露面
        settings = {
            "listen_interfaces": f"0.0.0.0:{self.listen_port}",
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": False,
            "enable_natpmp": False,
            "connections_limit": 300,
            "alert_queue_size": 5000,
            "active_downloads": self._active_downloads,
        }
        if metadata_timeout:
            self._metadata_timeout = float(metadata_timeout)
        settings.update(lt_proxy_settings(proxy))
        # 显式订阅必要告警类别（默认掩码过窄可能漏掉 metadata / file_progress）
        cat = lt.alert.category_t
        mask = 0
        for name in ("status_notification", "error_notification",
                     "file_progress_notification", "storage_notification",
                     "tracker_notification", "connect_notification"):
            try:
                mask |= int(getattr(cat, name))
            except Exception:
                pass
        if mask:
            settings["alert_mask"] = mask
        try:
            self._ses = lt.session(settings)
        except Exception as e:
            # 默认端口被占用（如 6881）会直接崩溃：回退随机端口再试一次
            log_warning("fetcher.start",
                        f"监听端口 {self.listen_port} 启动失败（{e}），回退随机端口")
            settings["listen_interfaces"] = "0.0.0.0:0"
            self._ses = lt.session(settings)
        # 启动恢复：加载任务清单 + 逐任务恢复（损坏绝不阻断启动）
        self._tasks = load_tasks(
            self.cache_dir,
            warn=lambda m: log_warning("fetcher.restore.tasks", m))
        for t in self._tasks.values():
            try:
                self._restore_task(t)
            except Exception as e:
                log_exception("fetcher.restore", e)
        self._last_resume_sweep = time.time()
        self._running = True
        self._thread = threading.Thread(target=self._alert_loop, daemon=True)
        self._thread.start()

    @property
    def metadata_timeout(self) -> float:
        return self._metadata_timeout

    @property
    def download_dir(self) -> str:
        """任务下载根目录（绝对路径）。"""
        return self._download_dir

    def apply_proxy(self, proxy: dict) -> None:
        """运行时切换代理（无需重建会话）。"""
        if self._ses is None:
            return
        try:
            self._ses.apply_settings(lt_proxy_settings(proxy))
        except Exception as e:
            log_warning("fetcher.apply_proxy", f"代理设置应用失败：{e}")

    def apply_rate_limit(self, kbps: int) -> None:
        """会话级下载限速（KB/s，0 = 不限）。热更新，无需重建会话。

        底层能力已在验收 §9 实证（download_mgr_test）；此处接线应用层
        配置项。libtorrent 单位为字节/秒，配置层用 KB/s 对用户友好。
        """
        if self._ses is None:
            return
        try:
            self._ses.apply_settings(
                {"download_rate_limit": max(0, int(kbps)) * 1024})
        except Exception as e:
            log_warning("fetcher.apply_rate_limit", f"限速设置应用失败：{e}")

    def protected_dirs(self) -> set[str]:
        """配额清理保护名单：所有已注册记录的落盘目录（含预览与下载）。

        供 core.cache_quota 的 LRU 清理跳过活跃句柄目录——预览中的
        文件被占用，且活跃任务的预览数据删除后句柄读盘会失败。
        """
        with self._lock:
            out: set[str] = set()
            for rec in self._torrents.values():
                sp = getattr(rec, "save_path", "") or ""
                if sp:
                    out.add(sp)
            return out

    def shutdown(self):
        """停会话：落盘任务清单与 fastresume，再清全部句柄，最后 join 线程。"""
        self.scheduler.stop()
        # 1) 落盘任务清单 + 请求全量 fastresume（异步告警，由 alert 循环消化）
        with self._lock:
            if self._tasks:
                try:
                    save_tasks(self.cache_dir, self._tasks)
                except Exception as e:
                    log_warning("fetcher.shutdown.save_tasks", f"{e}")
            if self._ses is not None:
                for rec in self._torrents.values():
                    if (rec.download and rec.handle is not None
                            and rec.result is not None):
                        try:
                            rec.handle.save_resume_data(
                                lt.save_resume_flags_t.flush_disk_cache)
                        except Exception as e:
                            log_warning("fetcher.shutdown.save_resume", f"{e}")
        # 2) 通知告警线程退出并等待其消化告警
        self._running = False
        if self._thread is not None:
            try:
                self._thread.join(timeout=2)
            except Exception as e:
                log_warning("fetcher.shutdown.join", f"{e}")
        # 3) 残留 fastresume 告警直取（可能在线程退出后才到达）
        self._drain_resume_alerts()
        # 4) 移除全部句柄并复位（options=0：保留磁盘文件——下载任务是用户数据，
        #    remove_torrent 的 delete_files=1 会异步删文件，shutdown 绝不可用）
        with self._lock:
            if self._ses is not None:
                for rec in self._torrents.values():
                    if rec.handle is not None:
                        try:
                            self._ses.remove_torrent(rec.handle, 0)
                        except Exception as e:
                            log_warning("fetcher.shutdown.remove_torrent", f"{e}")
            self._tasks.clear()
            self._torrents.clear()
            self._current_ih = None
            self._handle = None
            self._result = None
            self._ses = None

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
        """把任务降级为「仅查看清单」：暂停 + upload_mode，保留在注册表。

        调用方须已持有 self._lock。scheduler.stop() 已撤 deadline/优先级/
        auto_managed，这里补回 upload_mode，确保不再有数据网络活动。
        """
        if rec.handle is None:
            return
        try:
            rec.handle.pause()
            rec.handle.set_flags(lt.torrent_flags.upload_mode)
            rec.handle.unset_flags(lt.torrent_flags.auto_managed)
        except Exception as e:
            log_warning("fetcher.detach_record", f"{e}")

    def _preview_dir(self, ih: str) -> str:
        """review/预览任务的落盘目录：``cache_dir/.preview/<ih>``（D7）。"""
        if not ih or ih.startswith("tmp-"):
            return self.cache_dir   # 无 btih 磁力链兜底：平铺
        return os.path.join(self.cache_dir,
                            *safe_rel_path(PREVIEW_SUBDIR, ih).split("/"))

    def _put_record(self, ih: str, rec: TaskRecord,
                    make_current: bool = False) -> None:
        """写入注册表；同 ih 旧句柄（不同对象）让位移除。

        调用方须已持有 self._lock。make_current=True 时同步「当前」别名。
        """
        old = self._torrents.get(ih)
        try:
            replace = (old is not None and old.handle is not None
                       and rec.handle is not None and self._ses is not None
                       and old.handle != rec.handle)
        except Exception:
            replace = False   # 句柄比较异常（失效句柄）按不替换处理
        if replace:
            try:
                self._ses.remove_torrent(old.handle, 1)
            except Exception as e:
                log_warning("fetcher.register.replace", f"{e}")
        self._torrents[ih] = rec
        if make_current:
            self._current_ih = ih
            self._handle = rec.handle
            self._result = rec.result
            self._resolving = rec.resolving
            self._resolve_started = rec.resolve_started

    def _register_current(self, handle, result: ParseResult | None,
                          gen: int) -> TaskRecord:
        """把新解析（review）的句柄登记为「当前任务」并写入注册表。"""
        ih = self._hash_key(handle)
        rec = TaskRecord(handle=handle, result=result, gen=gen,
                         resolving=result is None,
                         resolve_started=time.time() if result is None else 0.0,
                         state=STATE_META_FETCH if result is None else STATE_READY,
                         save_path=self._preview_dir(ih))
        self._put_record(ih, rec, make_current=True)
        if result is not None:
            self._resolving = False
            self._resolve_started = 0.0
        return rec

    def _focus_existing_download(self, ih: str) -> TaskRecord | None:
        """resolve() 命中了已是下载任务的 ih：不重复添加句柄，焦点切到该任务。

        旧 UI 语义保持：有结果则把文件树/预览指向它（重新发射 on_metadata）；
        无结果（仍在 META_FETCH）则仅切换焦点。返回记录（未命中返回 None）。
        """
        with self._lock:
            rec = self._torrents.get(ih)
            if rec is None or not rec.download:
                return None
            self._current_ih = ih
            self._handle = rec.handle
            self._result = rec.result
            self._resolving = rec.resolving
            self._resolve_started = rec.resolve_started
            self._gen += 1   # 焦点切换即换代：让路中的陈旧解析自弃
            has_result = rec.result is not None
        if has_result:
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

    # ---------- 下载任务 API（t5，命名对齐验收探测） ----------

    def add_task(self, source: str, save_subdir: str | None = None,
                 priority: int = 0, seed: bool = False) -> str:
        """添加下载任务（磁力链或 .torrent 路径）。

        - 返回任务 id（info_hash；纯 v2/无 btih 磁力链返回临时 task_id）；
        - 重复 info_hash：返回已存在任务 id，不重复添加（去重，边界 D2-1）；
        - 已解析为 review（仅查看清单/预览）的同 hash 资源：直接转正
          （决策 D10：沿用句柄与已落盘分块，零额外下载）；
        - ``save_subdir``：下载根目录下的落盘子目录，缺省自动用 info_hash；
        - ``priority`` 0~3；``seed`` 完成后做种（D3 默认自动停止）。
        """
        source = (source or "").strip()
        if not source:
            raise ValueError("下载来源为空")
        if self._ses is None:
            raise RuntimeError("会话未启动")
        if is_torrent_path(source):
            result = parse_torrent_file(source)   # 同步解析：返回 id 需要 info_hash
            ih = result.info_hash
            with self._lock:
                if ih in self._tasks:
                    return ih                     # 去重：已是下载任务
                rec = self._torrents.get(ih)
                if rec is not None and not rec.download:
                    return self._convert_to_download(rec, ih, source,
                                                      save_subdir, priority, seed)
            return self._add_torrent_file_task(source, result, ih,
                                               save_subdir, priority, seed)
        # 磁力链
        try:
            p = lt.parse_magnet_uri(source)
        except Exception as e:
            raise ValueError(f"磁力链接无效：{e}") from e
        ih = self._ih_from_params(p)
        with self._lock:
            if ih and ih in self._tasks:
                return ih
            if ih and ih in self._torrents and not self._torrents[ih].download:
                return self._convert_to_download(
                    self._torrents[ih], ih, source, save_subdir, priority, seed)
        return self._add_magnet_task(source, p, ih, save_subdir, priority, seed)

    def _task_dir(self, ih: str, save_subdir: str | None = None) -> str:
        """下载任务落盘目录：``<下载根>/<子目录>``（默认子目录 = info_hash）。

        save_subdir 只允许单层干净相对段（防穿越）；不在 cache_dir 内时
        由 tasks() 以绝对路径暴露。
        """
        sub = (save_subdir or ih or "").strip().strip("/\\")
        sub = safe_rel_path(sub) if sub else (ih or "")
        return os.path.join(self._download_dir, sub) if sub else self._download_dir

    def _add_magnet_task(self, source: str, p, ih: str | None,
                         save_subdir: str | None, priority: int,
                         seed: bool) -> str:
        """磁力链下载任务：以 META_FETCH 入表，元数据到达后转 DOWNLOADING。"""
        key = ih or f"tmp-{id(p)}"
        save_dir = self._task_dir(key, save_subdir)
        os.makedirs(save_dir, exist_ok=True)
        p.save_path = save_dir
        if hasattr(p, "trackers") and not p.trackers:
            p.trackers = BOOTSTRAP_TRACKERS
        try:
            handle = self._ses.add_torrent(p)
        except Exception as e:
            raise ValueError(f"加入 DHT 会话失败：{e}") from e
        with self._lock:
            rec = TaskRecord(handle=handle, result=None, gen=self._gen,
                             resolving=True, resolve_started=time.time(),
                             state=STATE_META_FETCH, download=True,
                             seed=seed, priority=int(priority or 0),
                             save_path=save_dir, source=source)
            self._put_record(key, rec,
                             make_current=self._current_ih is None)
            if ih:
                task = {"info_hash": ih, "source": source,
                        "name": "(获取元数据中)", "total_size": 0,
                        "files": [], "selected": [],
                        "state": STATE_META_FETCH,
                        "priority": int(priority or 0),
                        "save_path": save_dir, "error": "", "retries": 0,
                        "created_at": time.time(), "finished_at": None,
                        "seed": bool(seed)}
                self._tasks, _ = upsert_task(self._tasks, task)
        if ih:
            self._persist_tasks()
        handle.resume()
        return key

    def _add_torrent_file_task(self, source: str, result: ParseResult,
                               ih: str, save_subdir: str | None,
                               priority: int, seed: bool) -> str:
        """本地 .torrent 下载任务：元数据已知，直接 DOWNLOADING。"""
        save_dir = self._task_dir(ih, save_subdir)
        os.makedirs(save_dir, exist_ok=True)
        atp = lt.add_torrent_params()
        atp.ti = lt.torrent_info(source)
        atp.save_path = save_dir
        try:
            handle = self._ses.add_torrent(atp)
        except Exception as e:
            raise ValueError(f"加入会话失败：{e}") from e
        with self._lock:
            rec = TaskRecord(handle=handle, result=result, gen=self._gen,
                             resolving=False, resolve_started=0.0,
                             state=STATE_DOWNLOADING, download=True,
                             seed=seed, priority=int(priority or 0),
                             save_path=save_dir, source=source)
            self._put_record(ih, rec, make_current=self._current_ih is None)
            task = task_from_result(result, state=STATE_DOWNLOADING,
                                    save_path=save_dir,
                                    priority=int(priority or 0),
                                    source=source, seed=seed)
            self._tasks, _ = upsert_task(self._tasks, task)
        self._persist_tasks()
        self._activate_download(rec)
        return ih

    def _convert_to_download(self, rec: TaskRecord, ih: str, source: str,
                             save_subdir: str | None, priority: int,
                             seed: bool) -> str:
        """预览/查看态记录转正为下载任务（D10）。

        沿用既有句柄与落盘目录（.preview/<ih>，已下载分块零额外下载），
        仅解除 upload_mode 并开始按文件优先级下载；调用方须已持锁。
        """
        rec.download = True
        rec.seed = seed
        rec.priority = int(priority or 0)
        rec.source = source
        rec.error = ""
        if not rec.save_path:
            rec.save_path = self._task_dir(ih, save_subdir)
        if rec.result is not None:
            rec.state = STATE_DOWNLOADING
            task = task_from_result(rec.result, state=STATE_DOWNLOADING,
                                    save_path=rec.save_path,
                                    priority=rec.priority,
                                    source=source, seed=seed)
            self._tasks, _ = upsert_task(self._tasks, task)
        else:
            rec.state = STATE_META_FETCH
            if not rec.resolving:
                rec.resolving = True
                rec.resolve_started = time.time()
            task = {"info_hash": ih, "source": source,
                    "name": "(获取元数据中)", "total_size": 0,
                    "files": [], "selected": [],
                    "state": STATE_META_FETCH,
                    "priority": rec.priority,
                    "save_path": rec.save_path, "error": "", "retries": 0,
                    "created_at": time.time(), "finished_at": None,
                    "seed": bool(seed)}
            self._tasks, _ = upsert_task(self._tasks, task)
        self._persist_tasks()
        if rec.result is not None:
            self._activate_download(rec)
        return ih

    def _activate_download(self, rec: TaskRecord) -> None:
        """让下载任务真正开始：解除 upload_mode、按所选文件设优先级、resume。

        预览任务（scheduler.begin）不在此列：它独立 unset auto_managed +
        手动 resume，保证不被 active_downloads 队列饿死（沿用既有做法）。
        """
        if rec.handle is None:
            return
        try:
            rec.handle.unset_flags(lt.torrent_flags.upload_mode)
            rec.handle.set_flags(lt.torrent_flags.auto_managed)
            if rec.result is not None:
                ti = rec.handle.torrent_file()
                if ti is not None:
                    ih = self._hash_key(rec.handle)
                    selected = set(self._tasks.get(ih, {}).get("selected") or [])
                    by_index = {f.index: (4 if (not selected or f.path in selected)
                                          else 0)
                                for f in rec.result.files}
                    prio = [by_index.get(i, 0) for i in range(ti.num_files())]
                    rec.handle.prioritize_files(prio)
            if rec.priority and rec.priority > 0:
                rec.handle.torrent_priority(
                    self._lt_priority(rec.priority))
            rec.handle.resume()
        except Exception as e:
            log_warning("fetcher.activate_download", f"{e}")

    # ---------- 任务操作 API ----------

    @staticmethod
    def _lt_priority(p: int) -> int:
        """任务优先级 0~3 → libtorrent torrent_priority（0~255）。

        0=默认/最低档（1），1/2/3 逐档提升；auto_managed 队列按此排序。
        """
        return {0: 1, 1: 50, 2: 150, 3: 255}.get(int(p), 1)

    def set_priority(self, task_id: str, priority: int) -> bool:
        """设置任务优先级（0~3，映射 torrent_priority，0=默认/最低）。

        QUEUED/META_FETCH/VALIDATE/DOWNLOADING/PAUSED/STOPPED 状态可用；
        返回 False 表示任务不存在、优先级非法或状态不可变。
        """
        try:
            p = int(priority)
        except (TypeError, ValueError):
            return False
        if p < 0 or p > 3:
            return False
        key = (task_id or "").strip().lower()
        with self._lock:
            rec = self._torrents.get(key)
            if rec is None or rec.handle is None:
                return False
            if rec.state not in (STATE_QUEUED, STATE_META_FETCH,
                                 STATE_VALIDATE, STATE_DOWNLOADING,
                                 STATE_PAUSED, STATE_STOPPED):
                return False
            rec.priority = p
            try:
                rec.handle.torrent_priority(self._lt_priority(p))
            except Exception as e:
                log_warning("fetcher.set_priority", f"{e}")
            if key in self._tasks:
                self._tasks[key]["priority"] = p
        self._persist_tasks()
        return True

    def pause_task(self, task_id: str) -> bool:
        """暂停下载任务：pause + 撤 auto_managed（防队列自动续传）。"""
        key = (task_id or "").strip().lower()
        with self._lock:
            rec = self._torrents.get(key)
            if rec is None or rec.handle is None:
                return False
            try:
                rec.handle.pause()
                rec.handle.unset_flags(lt.torrent_flags.auto_managed)
            except Exception as e:
                log_warning("fetcher.pause_task", f"{e}")
            rec.state = STATE_PAUSED
            if key in self._tasks:
                self._tasks[key]["state"] = STATE_PAUSED
                self._tasks[key]["error"] = ""
        self._persist_tasks()
        with self._lock:
            if rec is not None:
                self._request_resume(rec)
        return True

    def resume_task(self, task_id: str) -> bool:
        """恢复下载任务（含失败重试：清除 error、重启元数据看门狗）。"""
        key = (task_id or "").strip().lower()
        with self._lock:
            rec = self._torrents.get(key)
            if rec is None or rec.handle is None:
                return False
            rec.error = ""
            if rec.result is None:
                # 元数据仍未就绪（暂停发生在 META_FETCH）：重启看门狗计时
                rec.state = STATE_META_FETCH
                rec.resolving = True
                rec.resolve_started = time.time()
            else:
                rec.state = STATE_DOWNLOADING
            if key in self._tasks:
                self._tasks[key]["state"] = rec.state
                self._tasks[key]["error"] = ""
        self._persist_tasks()
        with self._lock:
            if rec is not None:
                self._activate_download(rec)
                self._request_resume(rec)
        return True

    def remove_task(self, task_id: str, delete_files: bool = False) -> bool:
        """显式移除任务：断句柄（不删文件）并注销记录。

        唯一允许真正移除句柄的入口；``delete_files=True`` 时删除任务落盘
        目录——只允许删除受管范围（cache_dir 或本会话下载根内）且目录名
        与任务键一致的目录（D9：删文件经守卫，防误删用户数据）。
        """
        key = (task_id or "").strip().lower()
        save_path = None
        with self._lock:
            rec = self._torrents.get(key)
            if rec is not None:
                # 正在预览该句柄：先停预览调度
                if self.scheduler.handle is not None and rec.handle is not None \
                        and self.scheduler.handle == rec.handle:
                    self.scheduler.stop()
                if rec.handle is not None and self._ses is not None:
                    try:
                        # delete_files 时 remove 选项=1（libtorrent 删除文件），
                        # 否则 0（保留磁盘文件，目录由 _delete_task_files 守卫处理）
                        self._ses.remove_torrent(
                            rec.handle, 1 if delete_files else 0)
                    except Exception as e:
                        log_warning("fetcher.remove_task.remove_torrent", f"{e}")
                del self._torrents[key]
                save_path = rec.save_path or None
                if key == self._current_ih:
                    self._current_ih = None
                    self._handle = None
                    self._result = None
                    self._resolving = False
                    self._resolve_started = 0.0
                    self._gen += 1   # 换代：让路中的陈旧后台解析自弃
            task = self._tasks.pop(key, None)
            if rec is None and task is None:
                return False
            if save_path is None:
                save_path = task.get("save_path") or None if task else None
        self._persist_tasks()
        if delete_files and save_path:
            self._delete_task_files(key, save_path)
        return True

    def _delete_task_files(self, key: str, path: str) -> None:
        """删除任务落盘目录（受管范围守卫，详见 remove_task docstring）。"""
        ap = os.path.abspath(path)
        if not os.path.isdir(ap):
            return
        inside = self._is_within(self.cache_dir, ap) \
            or self._is_within(self._download_dir, ap)
        base = os.path.basename(os.path.normpath(ap)).lower()
        if inside and base == key.lower():
            try:
                shutil.rmtree(ap)
            except Exception as e:
                log_warning("fetcher.remove_task.delete", f"{e}")
        else:
            log_warning("fetcher.remove_task.delete",
                        f"拒绝删除非受管任务目录：{ap}")

    def focus_task(self, task_id: str) -> bool:
        """把某下载任务设为「当前」（状态/预览别名指向它）。

        供「打开预览/查看详情」联调用：先停当前预览，再把别名切到目标任务；
        之后可直接 start_preview(f)。
        """
        key = (task_id or "").strip().lower()
        with self._lock:
            rec = self._torrents.get(key)
            if rec is None:
                return False
            self.scheduler.stop()
            self._current_ih = key
            self._handle = rec.handle
            self._result = rec.result
            self._resolving = rec.resolving
            self._resolve_started = rec.resolve_started
            self._gen += 1   # 焦点切换即换代：让路中的陈旧解析自弃
        return True

    def tasks(self) -> list[dict]:
        """全任务快照（下载任务，含运行时派生字段：进度/速度/ETA 不落盘）。

        字段：info_hash/id/source/name/total_size/state/progress(0~1)/
        down_rate/eta/priority/save_subdir/save_path/error/created_at/
        finished_at/selected_files/seed。
        """
        out: list[dict] = []
        with self._lock:
            keys = [k for k in self._tasks]
            for key in keys:
                rec = self._torrents.get(key)
                if rec is None and key not in self._tasks:
                    continue
                t = dict(self._tasks[key])
                t["id"] = key
                t["info_hash"] = key
                t["priority"] = rec.priority if rec is not None \
                    else int(t.get("priority") or 0)
                t["seed"] = bool(rec.seed if rec is not None else t.get("seed"))
                t["save_subdir"] = self._save_subdir_of(
                    t.get("save_path") or (rec.save_path if rec else ""))
                t["selected_files"] = list(t.get("selected") or [])
                total = int(t.get("total_size") or 0)
                done, rate, eta = 0, 0, None
                if rec is not None and rec.handle is not None:
                    try:
                        s = rec.handle.status()
                        done = s.total_done
                        rate = s.download_payload_rate
                        if total > done and rate > 0:
                            eta = (total - done) / rate
                    except Exception:
                        pass
                if rec is not None and rec.error:
                    t["error"] = rec.error
                t["progress"] = (min(1.0, done / total)
                                 if total > 0 else 0.0)
                t["down_rate"] = rate
                t["eta"] = eta
                if t.get("state") == STATE_COMPLETED:
                    t["progress"] = 1.0
                out.append(t)
        return out

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
        st["resolving"] = self._resolving
        st["elapsed"] = time.time() - self._resolve_started if self._resolving else 0.0
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
        """句柄的注册表键：info_hash 十六进制（v1 为 40 位 / v2 为 64 位）。

        元数据未就绪时磁力链的 info_hash 同样有效（取自磁力链 btih 参数，
        libtorrent 在 add_torrent 后立即可用）。
        """
        try:
            ih = str(handle.info_hash())
        except Exception:
            ih = ""
        ih = ih.strip().lower()
        if len(ih) not in (40, 64) or any(c not in "0123456789abcdef"
                                          for c in ih):
            # 兜底：纯 v2 / 无 btih 磁力链（增强期用临时 task_id 匹配，见 t2 规划）
            return f"tmp-{id(handle)}"
        return ih

    @staticmethod
    def _ih_from_params(p) -> str | None:
        """从 parse_magnet_uri 的 add_torrent_params 提取 info_hash 键（未 add 前）。"""
        try:
            ih = str(p.info_hash)
        except Exception:
            return None
        ih = ih.strip().lower()
        if len(ih) not in (40, 64) or any(c not in "0123456789abcdef"
                                          for c in ih):
            return None
        if set(ih) == {"0"}:
            return None   # 全零 = 无有效 info-hash（libtorrent 会拒绝）
        return ih

    def _current_record(self) -> TaskRecord | None:
        """当前任务注册表记录（调用方自行决定是否持锁）。"""
        if self._current_ih is None:
            return None
        return self._torrents.get(self._current_ih)

    def _find_record(self, handle) -> TaskRecord | None:
        """按句柄查注册表（alert 归属校验用）。

        查不到 = 已移除任务的迟到告警，调用方据此丢弃。
        """
        if handle is None:
            return None
        with self._lock:
            rec = self._torrents.get(self._hash_key(handle))
            if rec is not None:
                return rec
            # 兜底：临时键（纯 v2 磁力链）或 Python 包装对象差异时按句柄身份匹配
            for r in self._torrents.values():
                if r.handle is not None and r.handle == handle:
                    return r
            return None

    # ---------- 目录与持久化辅助 ----------

    @staticmethod
    def _is_within(root: str, path: str) -> bool:
        """path 是否位于 root 内（normcase + commonpath 前缀防护）。"""
        root_n = os.path.normcase(os.path.normpath(os.path.abspath(root)))
        path_n = os.path.normcase(os.path.normpath(os.path.abspath(path)))
        try:
            return os.path.commonpath([root_n, path_n]) == root_n
        except ValueError:
            return False

    def _save_subdir_of(self, path: str) -> str:
        """落盘目录 -> tasks()['save_subdir']：cache_dir 内给相对路径，否则绝对。"""
        ap = os.path.abspath(path or "")
        if ap and self._is_within(self.cache_dir, ap):
            return os.path.relpath(ap, self.cache_dir).replace(os.sep, "/")
        return ap

    def _safe_task_save_path(self, ih: str, save_path: str) -> str:
        """磁盘任务记录的 save_path 消毒：逃出受管范围则回退默认目录。"""
        ap = os.path.abspath(str(save_path or ""))
        if ap and (self._is_within(self.cache_dir, ap)
                   or self._is_within(self._download_dir, ap)):
            return ap
        return self._task_dir(ih)

    def _persist_tasks(self) -> None:
        """原子写 .tasks.json（失败仅告警，不阻断任务操作）。"""
        try:
            save_tasks(self.cache_dir, self._tasks)
        except Exception as e:
            log_warning("fetcher.persist_tasks", f"{e}")

    def _read_resume(self, ih: str) -> bytes | None:
        try:
            with open(resume_path(self.cache_dir, ih), "rb") as f:
                return f.read()
        except OSError:
            return None

    def _request_resume(self, rec: TaskRecord) -> None:
        """请求写 fastresume（异步：save_resume_data_alert 落盘）。"""
        if rec is None or rec.handle is None or not rec.download \
                or rec.result is None:
            return
        try:
            rec.handle.save_resume_data(lt.save_resume_flags_t.flush_disk_cache)
        except Exception as e:
            log_warning("fetcher.request_resume", f"{e}")

    def _write_resume_from_alert(self, a) -> None:
        rec = self._find_record(a.handle)
        if rec is None:
            return
        try:
            # 2.1.x：alert.resume_data 为 dict（bytes 键），libtorrent 官方格式
            write_resume(self.cache_dir, self._hash_key(a.handle), a.resume_data)
        except Exception as e:
            log_warning("fetcher.resume.write", f"{e}")

    def _drain_resume_alerts(self, timeout: float = 3.0) -> None:
        """清理阶段直取残留告警，等待全部下载任务 fastresume 落盘（有界）。

        竞态背景：save_resume_data(flush_disk_cache) 是异步请求，alert 在线程
        退出后仍可能晚到；单次 pop 会丢失。此处循环 pop + 检查落盘完成度，
        全部写完或超时才返回（超时仅告警，不阻断退出）。
        """
        if self._ses is None:
            return
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            handled = False
            try:
                for a in self._ses.pop_alerts():
                    try:
                        if isinstance(a, lt.save_resume_data_alert):
                            self._write_resume_from_alert(a)
                            handled = True
                        elif isinstance(a, lt.save_resume_data_failed_alert):
                            log_warning("fetcher.resume.failed",
                                        f"{self._hash_key(a.handle)[:12]}…")
                    except Exception as e:
                        log_exception("fetcher.drain_resume", e)
            except Exception as e:
                log_exception("fetcher.drain_resume.pop", e)
            with self._lock:
                pending = [k for k, r in self._torrents.items()
                           if r.download and r.handle is not None
                           and r.result is not None
                           and not os.path.isfile(resume_path(self.cache_dir, k))]
            if not pending:
                return
            # 仍有未落盘任务：重发请求（幂等），等 flush/告警后下一轮再检查
            for k in pending:
                r = self._torrents.get(k)
                if r is not None and r.handle is not None:
                    try:
                        r.handle.save_resume_data(
                            lt.save_resume_flags_t.flush_disk_cache)
                    except Exception:
                        pass
            time.sleep(0.2)   # 等待 flush 完成/告警到达后重试
        pending = [k[:12] for k in self._torrents if os.path.isfile(
            resume_path(self.cache_dir, k)) is False]
        if pending:
            log_warning("fetcher.drain_resume.timeout",
                        f"fastresume 未在 {timeout}s 内全部落盘：{pending}")

    def _restore_task(self, t: dict) -> bool:
        """启动恢复单个下载任务：resume_data 注入 + 隐式校验，损坏静默全新加入。

        任何失败只标记该任务 FAILED（不抛异常、绝不阻断启动）。
        """
        ih = t.get("info_hash") or ""
        save_path = self._safe_task_save_path(ih, t.get("save_path") or "")
        t["save_path"] = save_path
        os.makedirs(save_path, exist_ok=True)
        source = t.get("source") or ""
        rd = self._read_resume(ih)
        if rd is not None:
            # B3 语义：fastresume 损坏（bencode 解析失败/结构非法）静默降级
            # 为全新加入（丢弃 resume data 从头校验），绝不阻断启动、不误判
            # 任务失败——与模块 docstring「损坏静默降级全新加入」一致。
            try:
                rd = lt.read_resume_data(rd)
            except Exception as e:
                log_warning("fetcher.restore.resume",
                            f"{ih[:12]}… fastresume 损坏，静默全新加入：{e}")
                rd = None
        try:
            if source.startswith("magnet:"):
                p = lt.parse_magnet_uri(source)
                if rd:
                    atp = rd                       # 官方装载：含 info-hash + pieces
                    atp.save_path = save_path
                    atp.url = source
                else:
                    atp = p
                    atp.save_path = save_path
                    if hasattr(atp, "trackers") and not atp.trackers:
                        atp.trackers = BOOTSTRAP_TRACKERS
            elif is_torrent_path(source) and os.path.isfile(source):
                ti = lt.torrent_info(source)
                atp = rd if rd else lt.add_torrent_params()
                atp.ti = ti
                atp.save_path = save_path
            else:
                t["state"] = STATE_FAILED
                t["error"] = "重启恢复失败：来源不可用"
                self._persist_tasks()
                return False
        except Exception as e:
            log_warning("fetcher.restore.source", f"{ih[:12]}… {e}")
            t["state"] = STATE_FAILED
            t["error"] = f"重启恢复失败：{e}"
            self._persist_tasks()
            return False
        try:
            handle = self._ses.add_torrent(atp)
        except Exception as e:
            log_warning("fetcher.restore.add", f"{ih[:12]}… {e}")
            t["state"] = STATE_FAILED
            t["error"] = f"重启恢复失败：{e}"
            self._persist_tasks()
            return False
        st = str(t.get("state") or "").upper()
        if st in (STATE_PAUSED, STATE_STOPPED, STATE_COMPLETED):
            try:
                handle.pause()
                handle.unset_flags(lt.torrent_flags.auto_managed)
            except Exception as e:
                log_warning("fetcher.restore.pause", f"{e}")
        has_meta = handle.torrent_file() is not None
        if not has_meta and st not in (STATE_PAUSED, STATE_STOPPED):
            try:
                handle.resume()
            except Exception as e:
                log_warning("fetcher.restore.resume", f"{e}")
        rec = TaskRecord(
            handle=handle, result=None, gen=self._gen,
            resolving=not has_meta,
            resolve_started=time.time() if not has_meta else 0.0,
            state=(st if st in DOWNLOAD_STATES
                   else (STATE_DOWNLOADING if has_meta else STATE_META_FETCH)),
            timeout=None, download=True,
            seed=bool(t.get("seed")), priority=int(t.get("priority") or 0),
            save_path=save_path, source=source)
        if has_meta:
            try:
                rec.result = self._result_from_torrent_info(
                    handle.torrent_file(), ih)
            except Exception as e:
                log_warning("fetcher.restore.result", f"{ih[:12]}… {e}")
            if rec.state in (STATE_META_FETCH, STATE_QUEUED, STATE_VALIDATE):
                rec.state = STATE_DOWNLOADING
            if rec.result is not None \
                    and st not in (STATE_PAUSED, STATE_STOPPED, STATE_COMPLETED):
                self._activate_download(rec)
        with self._lock:
            self._put_record(ih, rec, make_current=False)
        return True

    # ---------- alert 循环（后台线程） ----------

    def _alert_loop(self):
        while self._running and self._ses is not None:
            try:
                for a in self._ses.pop_alerts():
                    # 单条告警处理失败不得中断整批，否则 metadata_received_alert 会被丢弃
                    try:
                        if isinstance(a, lt.metadata_received_alert):
                            # 归属校验：按 info_hash 查任务注册表；
                            # 查不到 = 已移除任务的迟到告警，丢弃
                            rec = self._find_record(a.handle)
                            if rec is not None:
                                self._on_metadata_received(rec)
                        elif isinstance(a, lt.file_completed_alert):
                            # 同样按任务归属：只服务当前任务（预览）的文件完成事件
                            rec = self._find_record(a.handle)
                            if (rec is not None
                                    and rec is self._current_record()
                                    and self.scheduler.on_file_completed):
                                self.scheduler.on_file_completed(a.index)
                        elif isinstance(a, lt.torrent_finished_alert):
                            rec = self._find_record(a.handle)
                            if rec is not None and rec.download:
                                self._on_download_finished(rec)
                        elif isinstance(a, lt.save_resume_data_alert):
                            self._write_resume_from_alert(a)
                        elif isinstance(a, lt.save_resume_data_failed_alert):
                            log_warning("fetcher.resume.failed",
                                        f"{self._hash_key(a.handle)[:12]}…")
                    except Exception as e:
                        # 此处历史上吞掉过整批告警处理异常，导致元数据永不回调
                        log_exception("fetcher.alert.handle", e)
            except Exception as e:
                log_exception("fetcher.alert.pop", e)
            # 元数据超时看门狗：per-task（替代全局单计时）。
            # 遍历注册表里仍 resolving 的任务，各自对照自己的 resolve_started 与
            # 超时（默认可配：记录级 timeout 覆盖，否则用会话级 _metadata_timeout），
            # 超时任务独立 pause + 任务 FAILED，互不影响；暂停/停止/完成/失败态
            # 任务不看门（用户暂停元数据获取是合法动作）。
            now = time.time()
            expired = []
            with self._lock:
                for rec in self._torrents.values():
                    if not rec.resolving or rec.resolve_started <= 0:
                        continue
                    if rec.state in (STATE_PAUSED, STATE_STOPPED,
                                     STATE_COMPLETED, STATE_FAILED):
                        continue
                    limit = (rec.timeout if rec.timeout is not None
                             else self._metadata_timeout)
                    if now - rec.resolve_started > limit:
                        expired.append(rec)
            for rec in expired:
                with self._lock:
                    if not (rec.resolving and rec.resolve_started > 0):
                        continue   # 已被其他路径处理（如元数据刚到达）
                    rec.resolving = False
                    rec.resolve_started = 0.0
                    rec.state = STATE_FAILED
                    is_current = rec is self._current_record()
                    if is_current:
                        self._resolving = False
                        self._resolve_started = 0.0
                if not rec.download:
                    if is_current:
                        msg = (f"获取元数据超时（>{int(self._metadata_timeout)} 秒）："
                               f"该资源可能已无做种/无在线 Peer")
                    else:
                        key = self._hash_key(rec.handle) \
                            if rec.handle is not None else "?"
                        msg = (f"[{key[:12]}…] 获取元数据超时"
                               f"（>{int(self._metadata_timeout)} 秒）："
                               f"该资源可能已无做种/无在线 Peer")
                    self._emit_error(msg)
                with self._lock:
                    if rec.handle is not None:
                        try:
                            rec.handle.pause()
                        except Exception as e:
                            log_warning("fetcher.timeout.pause", f"{e}")
                if rec.download:
                    with self._lock:
                        if rec.priority and rec.priority > 0:
                            pass
                        rec.error = (f"获取元数据超时"
                                     f"（>{int(self._metadata_timeout)} 秒）："
                                     f"该资源可能已无做种/无在线 Peer")
                        key = self._hash_key(rec.handle) \
                            if rec.handle is not None else ""
                        if key in self._tasks:
                            self._tasks[key]["state"] = STATE_FAILED
                            self._tasks[key]["error"] = rec.error
                    self._persist_tasks()
            # fastresume 60s 周期脏写（仅下载中的任务；libtorrent 建议 ≥1min）
            if now - self._last_resume_sweep >= 60.0:
                self._last_resume_sweep = now
                with self._lock:
                    for rec in self._torrents.values():
                        if rec.download and rec.handle is not None \
                                and rec.state == STATE_DOWNLOADING:
                            self._request_resume(rec)
            time.sleep(0.15)

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
        self._resolving = False
        if self.on_error:
            try:
                self.on_error(msg)
            except Exception as e:
                log_exception("fetcher.emit_error", e)