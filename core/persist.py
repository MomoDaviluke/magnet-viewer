"""任务持久化：任务清单原子写、fastresume 请求/落盘/清理、启动恢复。

职责边界（fetcher 重构阶段 1 抽出）：
    本模块只负责**磁盘上的任务状态**——``.tasks.json`` 的保存、
    ``.resume/<ih>.fastresume`` 的请求/落盘/残留清理，以及启动时的逐任务恢复。
    会话生命周期、任务注册表增删改、下载激活、torrent_info 解析都不归它管：
    这些能力由调用方**显式注入**（见 :class:`PersistDeps`），因此本模块
    **不反向依赖 fetcher**，杜绝循环导入。

失败策略：一切异常就地 ``log_warning``，绝不向上抛——持久化失败绝不允许
阻断启动、任务操作或退出。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import libtorrent as lt

from .logutil import log_exception, log_warning
from .models import safe_rel_path
from .parser import is_torrent_path
from .resume import resume_path, write_resume
from .states import (BOOTSTRAP_TRACKERS, DOWNLOAD_STATES, STATE_COMPLETED,
                     STATE_DOWNLOADING, STATE_FAILED, STATE_META_FETCH,
                     STATE_PAUSED, STATE_QUEUED, STATE_STOPPED, STATE_VALIDATE)
from .taskstore import normalize_info_hash, save_tasks


# ---------------------------------------------------------------- 纯函数层
# 不依赖会话/注册表，任何模块都可安全调用（后续 registry / taskops 复用）。

def is_resume_key(key: str) -> bool:
    """该注册表键能否作为 fastresume 文件名（40/64 位 hex）。

    临时键（``tmp-<id>``：纯 v2 / 无 btih 磁力链在元数据到达前的占位主键）
    不是合法 info_hash，交给 ``resume_path`` 会 ValueError——凡是拼 resume
    路径的地方都要先过这一关，否则一个临时键能掀翻整个退出清理。
    """
    return bool(normalize_info_hash(key, raise_invalid=False))


def is_within(root: str, path: str) -> bool:
    """path 是否位于 root 内（normcase + commonpath 前缀防护）。

    注意：``startswith`` 式前缀判断会被同前缀兄弟目录绕过
    （``cacheT`` vs ``cacheT_evil``），必须走 commonpath。
    """
    root_n = os.path.normcase(os.path.normpath(os.path.abspath(root)))
    path_n = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    try:
        return os.path.commonpath([root_n, path_n]) == root_n
    except ValueError:
        return False


def task_dir(download_dir: str, ih: str, save_subdir: str | None = None) -> str:
    """下载任务落盘目录：``<下载根>/<子目录>``（默认子目录 = info_hash）。

    save_subdir 只允许单层干净相对段（防穿越）。
    """
    sub = (save_subdir or ih or "").strip().strip("/\\")
    sub = safe_rel_path(sub) if sub else (ih or "")
    if not sub:
        return download_dir
    # safe_rel_path 以正斜杠拼接（保证跨平台一致），此处归一到系统分隔符，
    # 让返回值可直接与 os.path.join 的结果做字符串比较（省得各处再 normpath）。
    return os.path.normpath(os.path.join(download_dir, sub))


def save_subdir_of(cache_dir: str, path: str) -> str:
    """落盘目录 -> tasks()['save_subdir']：cache_dir 内给相对路径，否则绝对。"""
    ap = os.path.abspath(path or "")
    if ap and is_within(cache_dir, ap):
        return os.path.relpath(ap, cache_dir).replace(os.sep, "/")
    return ap


def safe_task_save_path(cache_dir: str, download_dir: str, ih: str,
                        save_path: str) -> str:
    """磁盘任务记录的 save_path 消毒：逃出受管范围则回退默认目录。"""
    ap = os.path.abspath(str(save_path or ""))
    if ap and (is_within(cache_dir, ap) or is_within(download_dir, ap)):
        return ap
    return task_dir(download_dir, ih)


def read_resume(cache_dir: str, ih: str) -> bytes | None:
    """读单任务 fastresume；不存在/读失败返回 None（损坏语义由上层判定）。"""
    try:
        with open(resume_path(cache_dir, ih), "rb") as f:
            return f.read()
    except OSError:
        return None


def missing_resume_keys(cache_dir: str, torrents: dict) -> list:
    """注册表中「键合法 + fastresume 尚未落盘」的任务键（逾时告警用）。"""
    out = []
    for k in (torrents or {}):
        if not is_resume_key(k):
            continue
        try:
            if os.path.isfile(resume_path(cache_dir, k)):
                continue
        except Exception:
            continue
        out.append(k)
    return out


def pending_resume_keys(cache_dir: str, torrents: dict) -> list:
    """待落盘且``确实该有`` fastresume 的任务键（下载 + 句柄 + 结果皆就绪）。

    排除预览/纯解析记录：它们本就不写 fastresume，不该被当成「未落盘」。
    """
    torrents = torrents or {}
    out = []
    for k in missing_resume_keys(cache_dir, torrents):
        r = torrents.get(k)
        if r is not None and r.download and r.handle is not None \
                and r.result is not None:
            out.append(k)
    return out


# ---------------------------------------------------------------- 依赖注入

@dataclass
class PersistDeps:
    """``TaskPersistence`` 所需的宿主能力，由 SessionManager 注入。

    之所以用回调/getter 而不是直接持有 SessionManager：一是保证本模块可独立
    测试，二是宿主部分字段（``_tasks`` / ``_torrents`` / ``_ses``）会被整体
    重绑定或置空，只有运行时取值才拿得到最新对象。
    """
    cache_dir: str
    download_dir: str
    lock: Any                             # threading.Lock（阶段 3 后归 registry）
    tasks_get: Callable[[], dict]         # -> {ih: task dict}
    torrents_get: Callable[[], dict]      # -> {ih: TaskRecord}
    gen_get: Callable[[], int]            # -> 当前解析代次
    ses_get: Callable[[], Any]            # -> lt.session | None
    hash_key: Callable[[Any], str]        # torrent_handle -> info_hash
    find_record: Callable[[Any], Any]     # torrent_handle -> TaskRecord | None
    put_record: Callable[..., None]       # (ih, rec, make_current=False) -> None
    record_cls: type                      # TaskRecord 构造器（duck typing 免环）
    result_from_torrent_info: Callable[[Any, str], Any]
    activate_download: Callable[[Any], None]


class TaskPersistence:
    """面向单个 SessionManager 实例的持久化服务。"""

    def __init__(self, deps: PersistDeps):
        self.deps = deps

    # ---------- 任务清单 ----------

    def persist_tasks(self) -> None:
        """原子写 .tasks.json（失败仅告警，不阻断任务操作）。"""
        d = self.deps
        try:
            save_tasks(d.cache_dir, d.tasks_get())
        except Exception as e:
            log_warning("fetcher.persist_tasks", f"{e}")

    # ---------- fastresume ----------

    def read_resume(self, ih: str) -> bytes | None:
        # 非法键（空串 / 临时键 tmp-<id>）没有 fastresume 语义，直接按「没有」
        # 处理——拼路径会 ValueError，而本类的约定是失败不向上抛。
        if not is_resume_key(ih):
            return None
        return read_resume(self.deps.cache_dir, ih)

    def request_resume(self, rec) -> None:
        """请求写 fastresume（异步：save_resume_data_alert 落盘）。"""
        if rec is None or rec.handle is None or not rec.download \
                or rec.result is None:
            return
        try:
            rec.handle.save_resume_data(lt.save_resume_flags_t.flush_disk_cache)
        except Exception as e:
            log_warning("fetcher.request_resume", f"{e}")

    def write_resume_from_alert(self, a) -> None:
        d = self.deps
        try:
            rec = d.find_record(a.handle)      # 畸形告警（无 handle）也在此兜住
        except Exception as e:
            log_warning("fetcher.resume.find", f"{e}")
            return
        if rec is None:
            return
        try:
            # 2.1.x：alert.resume_data 为 dict（bytes 键），libtorrent 官方格式
            write_resume(d.cache_dir, d.hash_key(a.handle), a.resume_data)
        except Exception as e:
            log_warning("fetcher.resume.write", f"{e}")

    def drain_resume_alerts(self, timeout: float = 3.0) -> None:
        """清理阶段直取残留告警，等待全部下载任务 fastresume 落盘（有界）。

        竞态背景：save_resume_data(flush_disk_cache) 是异步请求，alert 在线程
        退出后仍可能晚到；单次 pop 会丢失。此处循环 pop + 检查落盘完成度，
        全部写完或超时才返回（超时仅告警，不阻断退出）。
        """
        d = self.deps
        ses = d.ses_get()
        if ses is None:
            return
        deadline = time.time() + max(0.0, timeout)
        while time.time() < deadline:
            try:
                for a in ses.pop_alerts():
                    try:
                        if isinstance(a, lt.save_resume_data_alert):
                            self.write_resume_from_alert(a)
                        elif isinstance(a, lt.save_resume_data_failed_alert):
                            log_warning("fetcher.resume.failed",
                                        f"{d.hash_key(a.handle)[:12]}…")
                    except Exception as e:
                        log_exception("fetcher.drain_resume", e)
            except Exception as e:
                log_exception("fetcher.drain_resume.pop", e)
            with d.lock:
                torrents = dict(d.torrents_get())
            pending = pending_resume_keys(d.cache_dir, torrents)
            if not pending:
                return
            # 仍有未落盘任务：重发请求（幂等），等 flush/告警后下一轮再检查
            for k in pending:
                r = torrents.get(k)
                if r is not None and r.handle is not None:
                    try:
                        r.handle.save_resume_data(
                            lt.save_resume_flags_t.flush_disk_cache)
                    except Exception:
                        pass
            time.sleep(0.2)   # 等待 flush 完成/告警到达后重试
        # 超时兜底：只告警，绝不阻断退出（下次启动按损坏/缺失重建）
        pending = [k[:12] for k in pending_resume_keys(
            d.cache_dir, d.torrents_get())]
        if pending:
            log_warning("fetcher.drain_resume.timeout",
                        f"fastresume 未在 {timeout}s 内全部落盘：{pending}")

    def restore_task(self, t: dict) -> bool:
        """启动恢复单个下载任务：resume_data 注入 + 隐式校验，损坏静默全新加入。

        任何失败只标记该任务 FAILED（不抛异常、绝不阻断启动）。
        """
        d = self.deps
        ih = t.get("info_hash") or ""
        if not is_resume_key(ih):
            # 清单被外部改坏 / 键丢失：留可见状态比让异常冒到调用方更好
            t["state"] = STATE_FAILED
            t["error"] = "重启恢复失败：任务缺少合法 info_hash"
            self.persist_tasks()
            return False
        # 落盘目录创建失败（同名文件占位 / 权限 / 非法盘符）必须按
        # 「该任务 FAILED」处理而非向上抛：否则会打破本方法「任何失败都不
        # 阻断启动、且一定留下可见状态」的契约（调用方虽有兜底 except，
        # 但那样任务既不会 FAILED、下次启动还会重试同一个坏路径）。
        try:
            save_path = safe_task_save_path(self.deps.cache_dir,
                                            self.deps.download_dir, ih,
                                            t.get("save_path") or "")
            os.makedirs(save_path, exist_ok=True)
        except Exception as e:
            log_warning("fetcher.restore.save_path", f"{ih[:12]}… {e}")
            t["state"] = STATE_FAILED
            t["error"] = f"重启恢复失败：{e}"
            self.persist_tasks()
            return False
        t["save_path"] = save_path
        source = t.get("source") or ""
        rd = self.read_resume(ih)
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
                self.persist_tasks()
                return False
        except Exception as e:
            log_warning("fetcher.restore.source", f"{ih[:12]}… {e}")
            t["state"] = STATE_FAILED
            t["error"] = f"重启恢复失败：{e}"
            self.persist_tasks()
            return False
        ses = d.ses_get()
        try:
            handle = ses.add_torrent(atp)
        except Exception as e:
            log_warning("fetcher.restore.add", f"{ih[:12]}… {e}")
            t["state"] = STATE_FAILED
            t["error"] = f"重启恢复失败：{e}"
            self.persist_tasks()
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
        rec = d.record_cls(
            handle=handle, result=None, gen=d.gen_get(),
            resolving=not has_meta,
            resolve_started=time.time() if not has_meta else 0.0,
            state=(st if st in DOWNLOAD_STATES
                   else (STATE_DOWNLOADING if has_meta else STATE_META_FETCH)),
            timeout=None, download=True,
            seed=bool(t.get("seed")), priority=int(t.get("priority") or 0),
            save_path=save_path, source=source)
        if has_meta:
            try:
                rec.result = d.result_from_torrent_info(
                    handle.torrent_file(), ih)
            except Exception as e:
                log_warning("fetcher.restore.result", f"{ih[:12]}… {e}")
            if rec.state in (STATE_META_FETCH, STATE_QUEUED, STATE_VALIDATE):
                rec.state = STATE_DOWNLOADING
            if rec.result is not None \
                    and st not in (STATE_PAUSED, STATE_STOPPED, STATE_COMPLETED):
                d.activate_download(rec)
        with d.lock:
            d.put_record(ih, rec, make_current=False)
        return True
