"""下载任务 CRUD（fetcher 重构阶段 4 抽出）。

职责边界：
    ``TaskOps`` = 下载任务的全部生命周期操作——添加（磁力链 / 本地 .torrent
    两条入口）、review 记录转正（D10）、启动激活（解除 upload_mode + 文件
    优先级 + resume）、暂停/恢复/优先级/焦点/移除（含受管守卫删文件）、
    任务清单快照 ``tasks()``。

与前几阶段的关系：
    - 锁与注册表数据在 ``core.registry.TaskRegistry``（单锁归属）；
    - 清单落盘/fastresume 在 ``core.persist.TaskPersistence``；
    - 本模块**直接 import registry/persist**（无环下游）——全回调注入
      只对「会被重绑的宿主状态」有意义，服务对象引用一次注入即可；
    - 会话句柄 ``ses_get`` 例外仍用回调：shutdown 会把它置空。

依赖方向定死：taskops → registry / persist / taskstore / parser / states，
永不反向；fetcher 只组装与委托。

锁纪律沿用 registry 的约定：「一次调用一个锁段」；``with reg.lock`` 段内
只调 ``*_locked``。R-3（review 修订项）：``tasks()`` 锁内只做快照拷贝，
``handle.status()`` 等 libtorrent 绑定调用移出锁外派生——锁持有时间从
N×句柄往返降为一次字典拷贝。
"""
from __future__ import annotations

import os
import shutil
import time
from typing import Any, Callable

import libtorrent as lt

from .logutil import log_warning
from .models import ParseResult
from .parser import is_torrent_path, parse_torrent_file
from .persist import TaskPersistence, is_within, save_subdir_of, task_dir
from .registry import (TaskRecord, TaskRegistry, hash_key, ih_from_params)
from .states import (BOOTSTRAP_TRACKERS, STATE_COMPLETED, STATE_DOWNLOADING,
                     STATE_META_FETCH, STATE_PAUSED, STATE_QUEUED,
                     STATE_STOPPED, STATE_VALIDATE)
from .taskstore import task_from_result, upsert_task


def lt_priority(p: int) -> int:
    """任务优先级 0~3 → libtorrent torrent_priority（0~255）。

    0=默认/最低档（1），1/2/3 逐档提升；auto_managed 队列按此排序。
    """
    return {0: 1, 1: 50, 2: 150, 3: 255}.get(int(p), 1)


class TaskOps:
    """下载任务 CRUD（挂在 SessionManager 上，经其薄委托对外）。

    Args:
        reg: 任务注册表（锁与数据本体所在）。
        persist: 持久化服务（清单落盘 / resume 请求）。
        ses_get: 运行时取 libtorrent 会话（shutdown 置空，必须回调）。
        scheduler_get: 运行时取预览调度器（remove/focus 时停预览）。
        download_dir: 下载根目录（构造后不变，直接持值）。
    """

    def __init__(self, reg: TaskRegistry, persist: TaskPersistence,
                 ses_get: Callable[[], Any],
                 scheduler_get: Callable[[], Any],
                 download_dir: str):
        self.reg = reg
        self.persist = persist
        self._ses_get = ses_get
        self._scheduler_get = scheduler_get
        self.download_dir = download_dir

    # ---------- 添加 ----------

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
        # D4 收编：会话引用一次取用（旧代码检查与使用之间隔着 parse 调用，
        # 竞态窗口内 _ses 被 shutdown 置空会裸 AttributeError）
        ses = self._ses_get()
        if ses is None:
            raise RuntimeError("会话未启动")
        if is_torrent_path(source):
            result = parse_torrent_file(source)   # 同步解析：返回 id 需要 info_hash
            ih = result.info_hash
            reg = self.reg
            converted = None
            with reg.lock:
                if ih in reg.tasks:
                    return ih                     # 去重：已是下载任务
                rec = reg.torrents.get(ih)
                if rec is not None and not rec.download:
                    ih = self._convert_to_download_locked(
                        rec, ih, source, save_subdir, priority, seed)
                    converted = rec               # I/O 收尾出锁执行（P1-4）
            if converted is not None:
                self.persist.persist_tasks()
                if converted.result is not None:
                    self.activate_download(converted)
            else:
                return self._add_torrent_file_task(ses, source, result, ih,
                                                   save_subdir, priority, seed)
            return ih
        # 磁力链
        try:
            p = lt.parse_magnet_uri(source)
        except Exception as e:
            raise ValueError(f"磁力链接无效：{e}") from e
        ih = ih_from_params(p)
        reg = self.reg
        converted = None
        with reg.lock:
            if ih and ih in reg.tasks:
                return ih
            if ih and ih in reg.torrents and not reg.torrents[ih].download:
                rec_conv = reg.torrents[ih]
                ih = self._convert_to_download_locked(
                    rec_conv, ih, source, save_subdir, priority, seed)
                converted = rec_conv              # I/O 收尾出锁执行（P1-4）
        if converted is not None:
            self.persist.persist_tasks()
            if converted.result is not None:
                self.activate_download(converted)
            return ih
        return self._add_magnet_task(ses, source, p, ih, save_subdir,
                                     priority, seed)

    def task_dir(self, ih: str, save_subdir: str | None = None) -> str:
        """下载任务落盘目录：``<下载根>/<子目录>``（默认子目录 = info_hash）。

        save_subdir 只允许单层干净相对段（防穿越）；不在 cache_dir 内时
        由 tasks() 以绝对路径暴露。实现见 core.persist.task_dir。
        """
        return task_dir(self.download_dir, ih, save_subdir)

    def _add_magnet_task(self, ses, source: str, p, ih: str | None,
                         save_subdir: str | None, priority: int,
                         seed: bool) -> str:
        """磁力链下载任务：以 META_FETCH 入表，元数据到达后转 DOWNLOADING。"""
        key = ih or f"tmp-{id(p)}"
        save_dir = self.task_dir(key, save_subdir)
        os.makedirs(save_dir, exist_ok=True)
        p.save_path = save_dir
        if hasattr(p, "trackers") and not p.trackers:
            p.trackers = BOOTSTRAP_TRACKERS
        try:
            handle = ses.add_torrent(p)
        except Exception as e:
            raise ValueError(f"加入 DHT 会话失败：{e}") from e
        reg = self.reg
        with reg.lock:
            rec = TaskRecord(handle=handle, result=None, gen=reg.gen,
                             resolving=True, resolve_started=time.time(),
                             state=STATE_META_FETCH, download=True,
                             seed=seed, priority=int(priority or 0),
                             save_path=save_dir, source=source)
            reg.put_record_locked(key, rec,
                                  make_current=reg.current_ih is None)
            if ih:
                task = {"info_hash": ih, "source": source,
                        "name": "(获取元数据中)", "total_size": 0,
                        "files": [], "selected": [],
                        "state": STATE_META_FETCH,
                        "priority": int(priority or 0),
                        "save_path": save_dir, "error": "", "retries": 0,
                        "created_at": time.time(), "finished_at": None,
                        "seed": bool(seed)}
                reg.tasks, _ = upsert_task(reg.tasks, task)
        if ih:
            self.persist.persist_tasks()
        handle.resume()
        return key

    def _add_torrent_file_task(self, ses, source: str, result: ParseResult,
                               ih: str, save_subdir: str | None,
                               priority: int, seed: bool) -> str:
        """本地 .torrent 下载任务：元数据已知，直接 DOWNLOADING。"""
        save_dir = self.task_dir(ih, save_subdir)
        os.makedirs(save_dir, exist_ok=True)
        atp = lt.add_torrent_params()
        atp.ti = lt.torrent_info(source)
        atp.save_path = save_dir
        try:
            handle = ses.add_torrent(atp)
        except Exception as e:
            raise ValueError(f"加入会话失败：{e}") from e
        reg = self.reg
        with reg.lock:
            rec = TaskRecord(handle=handle, result=result, gen=reg.gen,
                             resolving=False, resolve_started=0.0,
                             state=STATE_DOWNLOADING, download=True,
                             seed=seed, priority=int(priority or 0),
                             save_path=save_dir, source=source)
            reg.put_record_locked(ih, rec, make_current=reg.current_ih is None)
            task = task_from_result(result, state=STATE_DOWNLOADING,
                                    save_path=save_dir,
                                    priority=int(priority or 0),
                                    source=source, seed=seed)
            reg.tasks, _ = upsert_task(reg.tasks, task)
        self.persist.persist_tasks()
        self.activate_download(rec)
        return ih

    def _convert_to_download_locked(self, rec: TaskRecord, ih: str,
                                    source: str, save_subdir: str | None,
                                    priority: int, seed: bool) -> str:
        """预览/查看态记录转正为下载任务（D10）。

        沿用既有句柄与落盘目录（.preview/<ih>，已下载分块零额外下载），
        仅解除 upload_mode 并开始按文件优先级下载；调用方须已持锁。
        """
        reg = self.reg
        rec.download = True
        rec.seed = seed
        rec.priority = int(priority or 0)
        rec.source = source
        rec.error = ""
        if not rec.save_path:
            rec.save_path = self.task_dir(ih, save_subdir)
        if rec.result is not None:
            rec.state = STATE_DOWNLOADING
            task = task_from_result(rec.result, state=STATE_DOWNLOADING,
                                    save_path=rec.save_path,
                                    priority=rec.priority,
                                    source=source, seed=seed)
            reg.tasks, _ = upsert_task(reg.tasks, task)
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
            reg.tasks, _ = upsert_task(reg.tasks, task)
        # P1-4（REVIEW-2026-09）：persist_tasks（同步文件写）与 activate_download
        # （libtorrent 会话调用）不再在 reg.lock 内执行——由调用方出锁收尾，
        # 失败语义不变（两者内部均仅告警）。
        return ih

    def activate_download(self, rec: TaskRecord,
                          preserve_files: bool = False) -> None:
        """让下载任务真正开始：解除 upload_mode、resume（可选重设文件优先级）。

        预览任务（scheduler.begin）不在此列：它独立 unset auto_managed +
        手动 resume，保证不被 active_downloads 队列饿死（沿用既有做法）。

        ``preserve_files=True``（阶段 B convert 转正）：跳过按任务清单
        "selected" 重设文件优先级——预览态 begin() 已把目标文件置 4、其余
        置 0，转正要延续的正是在下那些块。且 libtorrent 对 upload_mode
        句柄的 prioritize_files 会被丢弃，这里必须**先解除 upload_mode
        再置 auto_managed 最后 resume**，优先级才真正保留。
        """
        if rec.handle is None:
            return
        try:
            rec.handle.unset_flags(lt.torrent_flags.upload_mode)
            rec.handle.set_flags(lt.torrent_flags.auto_managed)
            if rec.result is not None and not preserve_files:
                ti = rec.handle.torrent_file()
                if ti is not None:
                    ih = hash_key(rec.handle)
                    selected = set(self.reg.tasks.get(ih, {}).get("selected")
                                   or [])
                    by_index = {f.index: (4 if (not selected
                                                or f.path in selected)
                                          else 0)
                                for f in rec.result.files}
                    prio = [by_index.get(i, 0) for i in range(ti.num_files())]
                    rec.handle.prioritize_files(prio)
            if rec.priority and rec.priority > 0:
                rec.handle.torrent_priority(lt_priority(rec.priority))
            rec.handle.resume()
        except Exception as e:
            log_warning("fetcher.activate_download", f"{e}")

    # ---------- 任务操作 ----------

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
        reg = self.reg
        with reg.lock:
            rec = reg.torrents.get(key)
            if rec is None or rec.handle is None:
                return False
            if rec.state not in (STATE_QUEUED, STATE_META_FETCH,
                                 STATE_VALIDATE, STATE_DOWNLOADING,
                                 STATE_PAUSED, STATE_STOPPED):
                return False
            rec.priority = p
            try:
                rec.handle.torrent_priority(lt_priority(p))
            except Exception as e:
                log_warning("fetcher.set_priority", f"{e}")
            if key in reg.tasks:
                reg.tasks[key]["priority"] = p
        self.persist.persist_tasks()
        return True

    def pause_task(self, task_id: str) -> bool:
        """暂停下载任务：pause + 撤 auto_managed（防队列自动续传）。"""
        key = (task_id or "").strip().lower()
        reg = self.reg
        with reg.lock:
            rec = reg.torrents.get(key)
            if rec is None or rec.handle is None:
                return False
            try:
                rec.handle.pause()
                rec.handle.unset_flags(lt.torrent_flags.auto_managed)
            except Exception as e:
                log_warning("fetcher.pause_task", f"{e}")
            rec.state = STATE_PAUSED
            if key in reg.tasks:
                reg.tasks[key]["state"] = STATE_PAUSED
                reg.tasks[key]["error"] = ""
        self.persist.persist_tasks()
        # P1-4：fastresume 请求（libtorrent 异步入队）出锁执行；rec 若已被
        # 并发删除，handle 失效由 request_resume 内部 except 告警兜住
        self.persist.request_resume(rec)
        return True

    def resume_task(self, task_id: str) -> bool:
        """恢复下载任务（含失败重试：清除 error、重启元数据看门狗）。"""
        key = (task_id or "").strip().lower()
        reg = self.reg
        with reg.lock:
            rec = reg.torrents.get(key)
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
            if key in reg.tasks:
                reg.tasks[key]["state"] = rec.state
                reg.tasks[key]["error"] = ""
        self.persist.persist_tasks()
        # P1-4：激活与 fastresume 请求出锁执行（与既有 best-effort 容错一致：
        # rec 被并发删除时 handle 失效由内部 except 告警兜住）
        self.activate_download(rec)
        self.persist.request_resume(rec)
        return True

    def remove_task(self, task_id: str, delete_files: bool = False) -> bool:
        """显式移除任务：断句柄（不删文件）并注销记录。

        唯一允许真正移除句柄的入口；``delete_files=True`` 时删除任务落盘
        目录——只允许删除受管范围（cache_dir 或本会话下载根内）且目录名
        与任务键一致的目录（D9：删文件经守卫，防误删用户数据）。
        """
        key = (task_id or "").strip().lower()
        save_path = None
        reg = self.reg
        ses = self._ses_get()
        sched = self._scheduler_get()
        with reg.lock:
            rec = reg.torrents.get(key)
            if rec is not None:
                # 正在预览该句柄：先停预览调度
                if sched.handle is not None and rec.handle is not None \
                        and sched.handle == rec.handle:
                    sched.stop()
                if rec.handle is not None and ses is not None:
                    try:
                        # delete_files 时 remove 选项=1（libtorrent 删除文件），
                        # 否则 0（保留磁盘文件，目录由 delete_task_files 守卫处理）
                        ses.remove_torrent(
                            rec.handle, 1 if delete_files else 0)
                    except Exception as e:
                        log_warning("fetcher.remove_task.remove_torrent",
                                    f"{e}")
                del reg.torrents[key]
                save_path = rec.save_path or None
                if key == reg.current_ih:
                    reg.current_ih = None
                    reg.handle = None
                    reg.result = None
                    reg.resolving = False
                    reg.resolve_started = 0.0
                    reg.gen += 1   # 换代：让路中的陈旧后台解析自弃
            task = reg.tasks.pop(key, None)
            if rec is None and task is None:
                return False
            if save_path is None:
                save_path = task.get("save_path") or None if task else None
        self.persist.persist_tasks()
        if delete_files and save_path:
            self.delete_task_files(key, save_path)
        return True

    def delete_task_files(self, key: str, path: str) -> None:
        """删除任务落盘目录（受管范围守卫，详见 remove_task docstring）。"""
        ap = os.path.abspath(path)
        if not os.path.isdir(ap):
            return
        inside = (is_within(self.reg.cache_dir, ap)
                  or is_within(self.download_dir, ap))
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
        reg = self.reg
        with reg.lock:
            rec = reg.torrents.get(key)
            if rec is None:
                return False
            self._scheduler_get().stop()
            reg.apply_current_locked(rec, key)
            reg.gen += 1   # 焦点切换即换代：让路中的陈旧解析自弃
        return True

    # ---------- 快照 ----------

    def tasks(self) -> list[dict]:
        """全任务快照（下载任务，含运行时派生字段：进度/速度/ETA 不落盘）。

        字段：info_hash/id/source/name/total_size/state/progress(0~1)/
        down_rate/eta/priority/save_subdir/save_path/error/created_at/
        finished_at/selected_files/seed。

        R-3：锁内只做一次性快照拷贝（键序即清单序），``handle.status()``
        等 libtorrent 绑定调用全部出锁派生——UI 每 700ms 轮询不再把任务锁
        按住 N 次句柄往返。句柄本身线程安全（libtorrent 值语义）；快照后
        任务被并发移除时按已拷贝数据出快照，与旧「整段持锁」观感一致。
        """
        reg = self.reg
        with reg.lock:
            snap = []
            for key, t in reg.tasks.items():
                rec = reg.torrents.get(key)
                snap.append((key, dict(t),
                             rec,
                             None if rec is None else (
                                 rec.priority, rec.seed, rec.save_path,
                                 rec.error)))
        out: list[dict] = []
        for key, t, rec, rec_f in snap:
            t["id"] = key
            t["info_hash"] = key
            if rec is not None:
                priority, seed, save_path_rec, error = rec_f
                t["priority"] = priority
                t["seed"] = bool(seed)
            else:
                t["priority"] = int(t.get("priority") or 0)
                t["seed"] = bool(t.get("seed"))
                save_path_rec = ""
                error = ""
            t["save_subdir"] = save_subdir_of(
                reg.cache_dir, t.get("save_path") or save_path_rec)
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
            if rec is not None and error:
                t["error"] = error
            t["progress"] = (min(1.0, done / total)
                             if total > 0 else 0.0)
            t["down_rate"] = rate
            t["eta"] = eta
            if t.get("state") == STATE_COMPLETED:
                t["progress"] = 1.0
            out.append(t)
        return out
