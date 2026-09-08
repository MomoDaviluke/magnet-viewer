"""磁力链/种子解析与元数据就绪编排（fetcher 重构阶段 5 抽出）。

职责边界：
    ``ResolverCore`` = 解析入口（resolve：本地 .torrent 后台线程 / 磁力链
    同步 add）、新解析换代（begin_resolve：旧「当前任务」降级/让位四分支）、
    Peer 直连（connect_peer）、两条 alert 处理链（on_metadata_received：
    per-task 就绪 → 清单 upsert → 转正激活；on_download_finished：D3 完成
    自动停止/做种）。模块级纯函数 ``result_from_torrent_info``：
    libtorrent ``torrent_info`` → ParseResult（单/多文件路径构造与
    safe_rel_path 兜底，历史上 P0-1/P0-2 两缺陷的现场）。

与既有模块的关系（依赖方向定死，永不反向）：
    resolver → registry（锁+注册表原语）、persist/taskops（无环下游）、
    宿主注入 ses/scheduler/emit 回调（会被重绑或改值）。
    fetcher 只组装与薄委托；session 的两条 alert 回调自本阶段起
    直接指向 ResolverCore 绑定方法。

锁纪律沿用 registry 约定：「一次调用一个锁段」，with 段内只调 *_locked。
gen 代次语义原样保留：换代后旧解析必须自弃（P1-10/P1-11 竞态修复的机制）。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

import libtorrent as lt

from .logutil import log_exception, log_warning
from .models import ParseResult, TorrentFile, safe_rel_path
from .parser import is_torrent_path, parse_torrent_file
from .persist import save_subdir_of
from .registry import TaskRecord, TaskRegistry, hash_key, ih_from_params
from .states import (BOOTSTRAP_TRACKERS, STATE_COMPLETED, STATE_DOWNLOADING,
                     STATE_FAILED, STATE_PAUSED, STATE_READY, STATE_SEEDING)
from .taskops import TaskOps


def result_from_torrent_info(ti, info_hash: str) -> ParseResult:
    """libtorrent torrent_info → ParseResult（历史上 P0-1/P0-2 缺陷现场）。

    - 单文件种子：path 不套前缀（libtorrent 存 save_path/root）；
    - 多文件：剥根目录段；safe_rel_path 兜底恶意路径不逃缓存目录；
    - total_size 排除 .pad（BEP-47）。
    """
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


class ResolverCore:
    """解析与元数据编排（挂在 SessionManager 上，经其薄委托对外）。"""

    def __init__(self, reg: TaskRegistry, ops: TaskOps,
                 cache_dir: str,
                 ses_get: Callable[[], Any],
                 scheduler_get: Callable[[], Any],
                 persist_tasks: Callable[[], None],
                 request_resume: Callable[[TaskRecord], None],
                 emit_metadata: Callable[[ParseResult], None],
                 emit_error: Callable[[str], None]):
        self.reg = reg
        self.ops = ops
        self.cache_dir = cache_dir
        self._ses_get = ses_get
        self._scheduler_get = scheduler_get
        self._persist_tasks = persist_tasks
        self._request_resume = request_resume
        self._emit_metadata = emit_metadata
        self._emit_error = emit_error

    # ---------- 解析入口（预览/查看清单，落盘 .preview/<ih>） ----------

    def resolve(self, source: str):
        """解析磁力链或 .torrent 文件（仅查看清单，不下载）。

        本地 .torrent 的 bencode 解析可能耗时（大文件），同样放到后台线程，
        避免卡死 UI 线程；磁力链本身由 libtorrent 后台完成。
        """
        self.begin_resolve()
        reg = self.reg
        with reg.lock:
            gen = reg.gen
        if is_torrent_path(source):
            threading.Thread(target=self._resolve_torrent_file,
                             args=(source, gen), daemon=True).start()
        else:
            self._resolve_magnet(source)

    def begin_resolve(self):
        """新解析前的换代：旧「当前任务」降级/让位，gen 递增防竞态。

        与旧 _reset_current 的差异：**不再无差别 remove 旧句柄**——
        - 下载任务（含 META_FETCH 等待中）：保留运行、继续下载，仅更换「当前」；
        - 已就绪（仅查看清单/预览）的 review 任务：保留在注册表
          （pause + upload_mode，供「转下载」复用分块），仅从「当前」降级；
        - 仍在解析（无结果）的 review 任务：等同旧行为移除（未成任务，不算删除）；
        - 真正移除句柄只发生在显式 remove_task()。
        """
        reg = self.reg
        ses = self._ses_get()
        with reg.lock:
            self._scheduler_get().stop()
            rec = reg.current_record()
            if rec is not None and rec.download:
                pass  # 下载任务：保留运行（含 META_FETCH 等待元数据）
            elif rec is not None and rec.handle is not None and rec.result is not None:
                reg.detach_record_locked(rec)   # 仅查看清单：保留在 map
            else:
                if rec is not None and rec.handle is not None and ses is not None:
                    try:
                        ses.remove_torrent(rec.handle, 1)
                    except Exception as e:
                        log_warning("fetcher.begin_resolve.remove_torrent",
                                    f"{e}")
                if rec is not None and reg.current_ih is not None:
                    reg.torrents.pop(reg.current_ih, None)
                # 游离句柄兜底（无注册表记录，不应出现）
                if reg.handle is not None and ses is not None:
                    try:
                        ses.remove_torrent(reg.handle, 1)
                    except Exception as e:
                        log_warning("fetcher.begin_resolve.remove_torrent",
                                    f"{e}")
            reg.handle = None
            reg.result = None
            reg.resolving = True
            reg.gen += 1          # 换代：旧解析任务完成后必须自弃
            reg.resolve_started = time.time()
            reg.current_ih = None

    def _focus_existing_download(self, ih: str) -> TaskRecord | None:
        """resolve() 命中了已是下载任务的 ih：不重复添加句柄，焦点切到该任务。

        旧 UI 语义保持：有结果则把文件树/预览指向它（重新发射 on_metadata）；
        无结果（仍在 META_FETCH）则仅切换焦点。返回记录（未命中返回 None）。
        """
        rec = self.reg.focus_current(ih)
        if rec is not None and rec.result is not None:
            self._emit_metadata(rec.result)
        return rec

    def _resolve_torrent_file(self, path: str, gen: int):
        reg = self.reg
        try:
            result = parse_torrent_file(path)
        except Exception as e:
            if gen == reg.gen:   # 旧代次的失败不打扰新会话
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
        result.save_subdir = save_subdir_of(
            self.cache_dir, reg.preview_dir(result.info_hash))
        if gen != reg.gen:
            return   # 期间用户已发起新解析：放弃本次结果
        ses = self._ses_get()
        atp = lt.add_torrent_params()
        atp.ti = lt.torrent_info(path)
        atp.save_path = reg.preview_dir(result.info_hash)
        atp.flags |= lt.torrent_flags.upload_mode  # 只解析不下载
        try:
            handle = ses.add_torrent(atp)
            handle.pause()
        except Exception as e:
            if gen == reg.gen:
                log_exception("fetcher.resolve_torrent.add", e)
                self._emit_error(f"加入会话失败：{e}")
            return
        with reg.lock:
            if gen != reg.gen:
                # 竞态兜底：换代后不再占用会话，句柄让位
                try:
                    ses.remove_torrent(handle, 1)
                except Exception as e:
                    log_warning("fetcher.resolve_torrent.genconflict", f"{e}")
                return
            reg.register_current_locked(handle, result, gen)
        self._emit_metadata(result)

    def _resolve_magnet(self, uri: str):
        reg = self.reg
        try:
            p = lt.parse_magnet_uri(uri)
        except Exception as e:
            log_exception("fetcher.resolve_magnet.parse", e)
            self._emit_error(f"磁力链接无效：{e}")
            return
        ih_known = ih_from_params(p)
        if ih_known is not None \
                and self._focus_existing_download(ih_known) is not None:
            return   # 已是下载任务：切焦点复用句柄，不重复添加
        save_dir = reg.preview_dir(ih_known or "")
        p.save_path = save_dir
        p.flags |= lt.torrent_flags.upload_mode  # 只取元数据，不下载资源
        if hasattr(p, "trackers") and not p.trackers:
            p.trackers = BOOTSTRAP_TRACKERS
        ses = self._ses_get()
        try:
            handle = ses.add_torrent(p)
        except Exception as e:
            log_exception("fetcher.resolve_magnet.add", e)
            self._emit_error(f"加入 DHT 会话失败：{e}")
            return
        with reg.lock:
            rec = reg.register_current_locked(handle, None, reg.gen)
        # 重复解析同一磁力链：libtorrent 返回既有句柄，元数据可能已就绪，
        # 不会再发 metadata_received_alert——此处直接走「元数据到达」快路径
        try:
            if rec.handle is not None and rec.handle.torrent_file() is not None:
                self.on_metadata_received(rec)
        except Exception as e:
            log_warning("fetcher.resolve_magnet.ready", f"{e}")

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
        语义等待当前任务别名 handle）。
        """
        reg = self.reg
        key = (task_id or "").strip().lower() if task_id else None
        deadline = time.time() + max(0.0, wait_handle)
        while True:
            with reg.lock:
                if key is not None:
                    rec = reg.torrents.get(key)
                    handle = rec.handle if rec is not None else None
                else:
                    handle = reg.handle
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

    # ---------- alert 处理链（session 循环回调进来） ----------

    def on_download_finished(self, rec: TaskRecord):
        """torrent_finished_alert：任务完成；默认自动停止（D3），seed 则做种。"""
        reg = self.reg
        key = hash_key(rec.handle) if rec.handle is not None else ""
        with reg.lock:
            rec.resolving = False
            rec.resolve_started = 0.0
            if rec.seed:
                rec.state = STATE_SEEDING
            else:
                rec.state = STATE_COMPLETED
            if key in reg.tasks:
                reg.tasks[key]["state"] = rec.state
                reg.tasks[key]["finished_at"] = time.time()
        try:
            if rec.seed:
                rec.handle.resume()
            else:
                rec.handle.pause()
                rec.handle.unset_flags(lt.torrent_flags.auto_managed)
        except Exception as e:
            log_warning("fetcher.finished", f"{e}")
        self._persist_tasks()
        with reg.lock:
            self._request_resume(rec)

    def on_metadata_received(self, rec: TaskRecord):
        """metadata_received_alert 处理：per-task 就绪。

        - review 记录：pause（拿到元数据即停，不下载），当前任务对外回调；
        - 下载任务：转入 DOWNLOADING（解除 upload_mode + 按所选文件优先级 +
          resume），仅当它是当前任务时才发全局 on_metadata（UI 展示）；
          状态机 META_FETCH → DOWNLOADING，暂停态保持暂停。
        """
        reg = self.reg
        with reg.lock:
            rec.resolving = False
            rec.resolve_started = 0.0
            is_current = rec is reg.current_record()
            if is_current:
                reg.resolving = False
                reg.resolve_started = 0.0
            if rec.result is not None:
                return   # 已就绪（重复告警）：幂等跳过
            handle = rec.handle
            key = hash_key(handle) if handle is not None else ""
            was_paused = reg.tasks.get(key, {}).get("state") == STATE_PAUSED
        if handle is None:
            return
        try:
            ti = handle.torrent_file()
            result = result_from_torrent_info(ti, str(handle.info_hash()))
        except Exception as e:
            log_exception("fetcher.metadata_received", e)
            with reg.lock:
                rec.state = STATE_FAILED
                cur = rec is reg.current_record()
                if rec.download:
                    rec.error = f"元数据处理失败：{e}"
                    if key in reg.tasks:
                        reg.tasks[key]["state"] = STATE_FAILED
                        reg.tasks[key]["error"] = rec.error
            if cur and not rec.download:
                self._emit_error(f"元数据处理失败：{e}")
            if rec.download:
                self._persist_tasks()
            return
        result.cache_dir = self.cache_dir
        result.save_subdir = save_subdir_of(self.cache_dir, rec.save_path)
        with reg.lock:
            rec.result = result
            if rec.download:
                if was_paused:
                    rec.state = STATE_PAUSED
                else:
                    rec.state = STATE_DOWNLOADING
                if key in reg.tasks:
                    reg.tasks[key].update({
                        "name": result.name,
                        "total_size": int(result.total_size),
                        "files": list(result.files),
                        "selected": [f.path for f in result.view_files],
                        "state": rec.state,
                        "error": "",
                    })
                    if rec.state == STATE_PAUSED:
                        reg.tasks[key]["state"] = STATE_PAUSED
            else:
                rec.state = STATE_READY
            cur = rec is reg.current_record()
            if cur:
                reg.result = result
                reg.resolving = False
                reg.resolve_started = 0.0
            if rec.download:
                self._request_resume(rec)
        if rec.download:
            self._persist_tasks()
            if not was_paused:
                self.ops.activate_download(rec)
        if cur:
            self._emit_metadata(result)
