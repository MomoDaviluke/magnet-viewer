"""任务注册表与单锁归属（fetcher 重构阶段 3 抽出，含 R-1/R-2 修复）。

职责边界：
    ``TaskRegistry`` = **一把锁** + 任务注册表（``torrents``：ih→TaskRecord、
    ``tasks``：.tasks.json 内存镜像）+「当前任务」别名组
    （handle / result / resolving / resolve_started / current_ih / gen）
    + 行结构 :class:`TaskRecord`（R-2：原先寄居 fetcher，persist/session/测试
    全认它，本应属于注册表）。

锁策略（沿用 persist/session 阶段定下的纪律）：
    「一次调用一个锁段」——公开方法自持锁；名字以 ``_locked`` 结尾的版本要求
    调用方**已持锁**（复合临界区用），两者绝不互相嵌套调用（锁不可重入）。
    SessionManager 的 ``_lock`` 是这里的 ``lock`` 的 property 代理：全项目
    只有这一把任务锁（阶段 4/5 的 taskops/resolver/preview 一律借用）。

R-1 修复（本阶段立项时登记的真实缺陷）：
    旧 ``SessionManager._emit_error`` 在告警线程里**裸写** ``self._resolving``
    （不持锁），与主线程锁内读写交错。现统一走 :meth:`clear_resolving_safe`
    （自持锁段），registry_test §G 用探针锁 + property spy 双向断言。

失败策略：注册表方法绝不向调用方抛生命周期异常——句柄操作（pause/flags/
remove_torrent）就地 log_warning，与迁移前逐字节同语义。
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Callable

import libtorrent as lt

from .logutil import log_warning
from .models import ParseResult, safe_rel_path
from .states import STATE_META_FETCH, STATE_READY

DOWNLOADS_SUBDIR = "downloads"
PREVIEW_SUBDIR = ".preview"
METADATA_TIMEOUT = 90.0  # 秒，超时判定为资源无做种（fetcher 再导出，兼容）


@dataclass
class TaskRecord:
    """任务注册表条目：一个 info_hash 唯一对应一个 libtorrent 句柄。

    （R-2：自 fetcher 原样迁入，字段序与默认值不动——持久化与 UI 快照
    按字段名取值。）
    """
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


# ---------------------------------------------------------------- 纯函数层

def hash_key(handle) -> str:
    """句柄的注册表键：info_hash 十六进制（v1 为 40 位 / v2 为 64 位）。

    元数据未就绪时磁力链的 info_hash 同样有效（取自磁力链 btih 参数，
    libtorrent 在 add_torrent 后立即可用）。
    """
    try:
        ih = str(handle.info_hash())
    except Exception as e:
        log_warning("registry.hash_key", f"句柄取 info_hash 失败，转临时键：{e}")
        ih = ""
    ih = ih.strip().lower()
    if len(ih) not in (40, 64) or any(c not in "0123456789abcdef"
                                      for c in ih):
        # 兜底：纯 v2 / 无 btih 磁力链（增强期用临时 task_id 匹配，见 t2 规划）
        return f"tmp-{id(handle)}"
    return ih


def ih_from_params(p) -> str | None:
    """从 parse_magnet_uri 的 add_torrent_params 提取 info_hash 键（未 add 前）。"""
    try:
        ih = str(p.info_hash)
    except Exception as e:
        log_warning("registry.ih_from_params", f"{e}")
        return None
    ih = ih.strip().lower()
    if len(ih) not in (40, 64) or any(c not in "0123456789abcdef"
                                      for c in ih):
        return None
    if set(ih) == {"0"}:
        return None   # 全零 = 无有效 info-hash（libtorrent 会拒绝）
    return ih


# ---------------------------------------------------------------- 注册表

class TaskRegistry:
    """单锁归属的任务注册表 + 「当前任务」别名组（须持锁版本另注 docstring）。

    Args:
        cache_dir: 预览/任务根目录（preview_dir 用；绝对路径由宿主保证）。
        ses_get: 运行时取 libtorrent 会话（可能被置空，必须回调而非快照）。
    """

    def __init__(self, cache_dir: str, ses_get: Callable[[], Any]):
        import threading
        self.lock = threading.Lock()
        self.cache_dir = cache_dir
        self._ses_get = ses_get
        self.torrents: dict[str, TaskRecord] = {}
        self.tasks: dict[str, dict] = {}
        self._current_ih: str | None = None
        self._handle: Any = None
        self._result: ParseResult | None = None
        self._resolving: bool = False
        self._resolve_started: float = 0.0
        self._gen: int = 0
        self._metadata_timeout: float = METADATA_TIMEOUT

    # ---------- 别名（property：读写都不带锁，供调用方在自持锁段内用） ----------

    @property
    def current_ih(self) -> str | None:
        return self._current_ih

    @current_ih.setter
    def current_ih(self, v: str | None) -> None:
        self._current_ih = v

    @property
    def handle(self):
        return self._handle

    @handle.setter
    def handle(self, v) -> None:
        self._handle = v

    @property
    def result(self) -> ParseResult | None:
        return self._result

    @result.setter
    def result(self, v: ParseResult | None) -> None:
        self._result = v

    @property
    def resolving(self) -> bool:
        return self._resolving

    @resolving.setter
    def resolving(self, v: bool) -> None:
        self._resolving = v

    @property
    def resolve_started(self) -> float:
        return self._resolve_started

    @resolve_started.setter
    def resolve_started(self, v: float) -> None:
        self._resolve_started = v

    @property
    def gen(self) -> int:
        return self._gen

    @gen.setter
    def gen(self, v: int) -> None:
        self._gen = v

    @property
    def metadata_timeout(self) -> float:
        return self._metadata_timeout

    @metadata_timeout.setter
    def metadata_timeout(self, v: float) -> None:
        self._metadata_timeout = float(v)

    # ---------- 查询 ----------

    def find_record(self, handle) -> TaskRecord | None:
        """按句柄查注册表（alert 归属校验用；自持锁）。

        查不到 = 已移除任务的迟到告警，调用方据此丢弃。
        """
        if handle is None:
            return None
        with self.lock:
            rec = self.torrents.get(hash_key(handle))
            if rec is not None:
                return rec
            # 兜底：临时键（纯 v2 磁力链）或 Python 包装对象差异时按句柄身份匹配
            for r in self.torrents.values():
                if r.handle is not None and r.handle == handle:
                    return r
            return None

    def find_record_by_ih(self, ih: str) -> TaskRecord | None:
        """按注册表键查记录（自持锁）。"""
        with self.lock:
            return self.torrents.get(ih)

    def current_record(self) -> TaskRecord | None:
        """当前任务记录（**无锁**读，与迁移前的 _current_record 同语义；
        调用方自行决定是否持锁）。"""
        if self._current_ih is None:
            return None
        return self.torrents.get(self._current_ih)

    def current(self) -> TaskRecord | None:
        """当前任务记录（自持锁）。"""
        with self.lock:
            return self.current_record()

    def resolving_snapshot(self) -> tuple[bool, float]:
        """(resolving, elapsed) 一致快照（自持锁）——R-1 家族的锁外读收编点。"""
        with self.lock:
            return (self._resolving,
                    (time.time() - self._resolve_started
                     if self._resolving else 0.0))

    def protected_dirs(self) -> set[str]:
        """所有已注册记录的落盘目录（配额清理保护名单；自持锁）。"""
        with self.lock:
            out: set[str] = set()
            for rec in self.torrents.values():
                sp = getattr(rec, "save_path", "") or ""
                if sp:
                    out.add(sp)
            return out

    # ---------- 写入（*_locked：调用方须已持 self.lock） ----------

    def put_record_locked(self, ih: str, rec: TaskRecord,
                          make_current: bool = False) -> None:
        """写入注册表；同 ih 旧句柄（不同对象）让位移除。

        调用方须已持有 self.lock。make_current=True 时同步「当前」别名。
        """
        old = self.torrents.get(ih)
        ses = self._ses_get()
        try:
            replace = (old is not None and old.handle is not None
                       and rec.handle is not None and ses is not None
                       and old.handle != rec.handle)
        except Exception as e:
            log_warning("registry.put.compare",
                        f"句柄比较异常按不替换处理：{e}")
            replace = False   # 句柄比较异常（失效句柄）按不替换处理
        if replace:
            try:
                ses.remove_torrent(old.handle, 1)
            except Exception as e:
                log_warning("fetcher.register.replace", f"{e}")
        self.torrents[ih] = rec
        if make_current:
            self.apply_current_locked(rec, ih)

    def apply_current_locked(self, rec: TaskRecord, ih: str) -> None:
        """把记录设为「当前」（同步全部别名；须持锁）。"""
        self._current_ih = ih
        self._handle = rec.handle
        self._result = rec.result
        self._resolving = rec.resolving
        self._resolve_started = rec.resolve_started

    def register_current_locked(self, handle, result: ParseResult | None,
                                gen: int) -> TaskRecord:
        """把新解析（review）的句柄登记为「当前任务」并写入注册表（须持锁）。"""
        ih = hash_key(handle)
        rec = TaskRecord(handle=handle, result=result, gen=gen,
                         resolving=result is None,
                         resolve_started=time.time() if result is None else 0.0,
                         state=STATE_META_FETCH if result is None else STATE_READY,
                         save_path=self.preview_dir(ih))
        self.put_record_locked(ih, rec, make_current=True)
        if result is not None:
            self._resolving = False
            self._resolve_started = 0.0
        return rec

    def detach_record_locked(self, rec: TaskRecord) -> None:
        """把任务降级为「仅查看清单」：暂停 + upload_mode，保留在注册表。

        调用方须已持有 self.lock。scheduler.stop() 已撤 deadline/优先级/
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

    def clear_resolving_locked(self) -> None:
        """清「当前解析」别名（须持锁）。"""
        self._resolving = False
        self._resolve_started = 0.0

    def clear_runtime_state_locked(self) -> None:
        """复位全部运行时状态（须持锁，供 shutdown 调用）。"""
        self.tasks.clear()
        self.torrents.clear()
        self._current_ih = None
        self._handle = None
        self._result = None
        self.clear_resolving_locked()

    # ---------- 自持锁版本（跨线程安全入口） ----------

    def focus_current(self, ih: str) -> TaskRecord | None:
        """焦点切到注册表中已有记录（自持锁）。未命中返回 None。

        gen 递增即换代：让路中的陈旧解析自弃（焦点切换语义，fetcher 迁移前
        如此）。返回值供调用方在**锁外**决定是否发射 on_metadata。
        """
        with self.lock:
            rec = self.torrents.get(ih)
            if rec is None or not rec.download:
                return None
            self.apply_current_locked(rec, ih)
            self._gen += 1
            return rec

    def bump_gen(self) -> int:
        """换代（自持锁）：旧解析任务完成后必须自弃。返回新代次。"""
        with self.lock:
            self._gen += 1
            return self._gen

    def clear_resolving_safe(self) -> None:
        """清「当前解析」别名（自持锁；R-1 的修复落点）。"""
        with self.lock:
            self.resolving = False
            self._resolve_started = 0.0

    def set_resolving(self, v: bool, started: float | None = None) -> None:
        """置「当前解析」别名（自持锁；started=None 表示不改开始时刻）。"""
        with self.lock:
            self.resolving = v
            if started is not None:
                self._resolve_started = started

    # ---------- 目录 ----------

    def preview_dir(self, ih: str) -> str:
        """review/预览任务的落盘目录：``cache_dir/.preview/<ih>``（D7）。"""
        if not ih or ih.startswith("tmp-"):
            return self.cache_dir   # 无 btih 磁力链兜底：平铺
        return os.path.join(self.cache_dir,
                            *safe_rel_path(PREVIEW_SUBDIR, ih).split("/"))
