"""core/registry.py 专项验收（fetcher 重构阶段 3）。

职责边界：TaskRegistry = 单锁归属 + 任务注册表（_torrents/_tasks）+
「当前任务」别名组（handle/result/resolving/resolve_started/gen/current_ih）
+ TaskRecord 行结构。锁策略沿用「一次调用一个锁段」：每个公开方法自持锁，
「须持锁」版本（*_locked）供复合临界区调用，绝不嵌套自锁。

段落：
- §A 纯函数 hash_key：合法 40/64 hex 归一小写；info_hash() 抛 → tmp-<id>；
     非法长度/字符 → tmp-<id>（复刻 fetcher 原语义）
- §B 纯函数 ih_from_params：全零 → None；大写 → 小写；异常 → None
- §C put_record（*_locked）：同 ih 旧句柄让位 remove(handle,1)、句柄比较
     异常按不替换、make_current 同步别名
- §D 焦点与换代：focus_current / bump_gen / current() 快照
- §E find_record：主匹配 + 临时键按句柄身份兜底（复刻 fetcher 双段逻辑）
- §F detach_record：pause + set_flags(upload_mode) + unset_flags(auto_managed)
     各一次；handle=None 安全跳过；句柄炸了不抛
- §G R-1 专项：clear_resolving_safe 自持锁（探针 Lock 计数）；线程并发
     clear_resolving_safe + resolving() 读不崩
- §H preview_dir：空 ih / tmp- 前缀 → cache_dir 平铺；正常 → .preview/<ih>
- §I Facade 接线：SessionManager._registry 存在；_torrents/_tasks/_current_ih/
     _gen 等 property 代理同一本体（防「复制两份数据」的假搬迁）；
     _emit_error 的 _resolving 写真的经过锁（R-1 端到端）

退出码：0=通过，1=失败，2=SKIP（依赖缺失，绝不假装通过）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from importlib import reload

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import libtorrent as lt

    import test_support as ts
    from core import registry
    from core.fetcher import SessionManager
    from core.models import ParseResult
    from core.registry import TaskRecord, TaskRegistry
    from core.states import STATE_META_FETCH, STATE_READY
except Exception as e:      # 依赖缺失：显式 SKIP，绝不假装通过
    print(f"依赖缺失，无法执行 registry 专项验收：{e}")
    sys.exit(2)

IH = "a" * 40
IH2 = "b" * 40


class FakeHandle:
    def __init__(self, ih: str = IH, raise_ih: bool = False,
                 raise_eq: bool = False):
        self.ih = ih
        self.raise_ih = raise_ih
        self.raise_eq = raise_eq
        self.paused = 0
        self.set_flags_calls: list = []
        self.unset_flags_calls: list = []

    def info_hash(self):
        if self.raise_ih:
            raise RuntimeError("句柄已失效")
        return self.ih

    def pause(self):
        self.paused += 1

    def set_flags(self, f):
        self.set_flags_calls.append(f)

    def unset_flags(self, f):
        self.unset_flags_calls.append(f)

    def __eq__(self, other):
        if self.raise_eq or getattr(other, "raise_eq", False):
            raise RuntimeError("句柄比较炸了")
        return self is other

    def __hash__(self):
        return id(self)


class FakeSes:
    def __init__(self):
        self.removed: list = []

    def remove_torrent(self, h, options=0):
        self.removed.append((h, options))


def in_lock(reg) -> bool:
    """探测「当前线程是否持有 reg.lock」：非重入锁 try-acquire 失败即持有。

    不替换锁对象（persist/session 持有同一 lock 引用，替换会造成双锁假象）。
    """
    if reg.lock.acquire(blocking=False):
        reg.lock.release()
        return False
    return True


def watch_resolving_writes(reg):
    """给 reg.resolving 装 spy：记录每次写发生瞬间是否在锁内。返回 (观测列表, 卸载函数)。"""
    obs: list = []
    prop = type(reg).resolving
    orig_set = prop.fset

    def spy(self, v):
        obs.append(in_lock(reg))
        orig_set(self, v)
    type(reg).resolving = property(prop.fget, spy)
    return obs, (lambda: setattr(type(reg), "resolving", prop))


def mk_reg(cache_dir=None, ses=None):
    cache_dir = cache_dir or os.path.join(tempfile.gettempdir(), "mv_reg_x")
    reg = TaskRegistry(cache_dir=cache_dir, ses_get=lambda: ses)
    return reg


# --------------------------------------------------------------------------

def section_hash_key(ck):
    ck.section("§A hash_key")
    ck.check(registry.hash_key(FakeHandle(IH.upper())) == IH,
             "40 位大写 hex → 归一小写")
    ih64 = "c" * 64
    ck.check(registry.hash_key(FakeHandle(ih64)) == ih64, "64 位 v2 hex 原样接受")
    bad = registry.hash_key(FakeHandle("xyz"))
    ck.check(bad.startswith("tmp-"), f"非法 hex → tmp-<id> 兜底（{bad}）")
    ck.check(registry.hash_key(FakeHandle("a" * 39)).startswith("tmp-"),
             "39 位（长度不足）→ tmp 兜底")
    ck.check(registry.hash_key(FakeHandle(raise_ih=True)).startswith("tmp-"),
             "info_hash() 抛异常 → tmp 兜底不冒泡")


def section_ih_params(ck):
    ck.section("§B ih_from_params")

    class P:
        def __init__(self, ih):
            self.info_hash = ih

    ck.check(registry.ih_from_params(P(IH.upper())) == IH, "大写归一小写")
    ck.check(registry.ih_from_params(P("0" * 40)) is None, "全零 → None")
    ck.check(registry.ih_from_params(P("z" * 40)) is None, "非法字符 → None")
    ck.check(registry.ih_from_params(P("a" * 41)) is None, "41 位 → None")

    class Boom:
        @property
        def info_hash(self):
            raise RuntimeError("坏对象")

    ck.check(registry.ih_from_params(Boom()) is None, "属性抛异常 → None")


def section_put_record(ck):
    ck.section("§C put_record（让位与别名）")
    ses = FakeSes()
    reg = mk_reg(ses=ses)
    old_h = FakeHandle(IH)
    new_h = FakeHandle(IH)
    with reg.lock:
        reg.put_record_locked(IH, TaskRecord(handle=old_h))
        reg.put_record_locked(IH, TaskRecord(handle=new_h))
    ck.check(ses.removed == [(old_h, 1)],
             "同 ih 换句柄：旧句柄 remove_torrent(old, 1)（预览让位删数据合法——"
             "review 数据非用户资产）")
    ck.check(reg.torrents[IH].handle is new_h, "新记录覆盖入表")

    # 同对象不移除
    ses2 = FakeSes()
    reg2 = mk_reg(ses=ses2)
    h = FakeHandle(IH)
    with reg2.lock:
        reg2.put_record_locked(IH, TaskRecord(handle=h))
        reg2.put_record_locked(IH, TaskRecord(handle=h))
    ck.check(ses2.removed == [], "同句柄对象重写：不触发 remove")

    # 句柄比较异常 → 按不替换（fetcher:335 原静默分支，现应有日志不改变行为）
    ses3 = FakeSes()
    reg3 = mk_reg(ses=ses3)
    ha = FakeHandle(IH)
    hb = FakeHandle(IH, raise_eq=True)
    try:
        with reg3.lock:
            reg3.put_record_locked(IH, TaskRecord(handle=ha))
            reg3.put_record_locked(IH, TaskRecord(handle=hb))
        ck.check(ses3.removed == [], "句柄比较抛异常：按不替换，不上抛")
    except Exception as e:
        ck.check(False, f"句柄比较异常不得冒泡：{e}")

    # make_current 同步别名
    reg4 = mk_reg()
    r4 = TaskRecord(handle=FakeHandle(IH), result="res", resolving=True,
                    resolve_started=5.0)
    with reg4.lock:
        reg4.put_record_locked(IH, r4, make_current=True)
    ck.check(reg4.current_record() is r4, "make_current=True：current() 返回新记录")
    ck.check((reg4.handle, reg4.result, reg4.resolving,
              reg4.resolve_started, reg4.current_ih)
             == (r4.handle, "res", True, 5.0, IH),
             "make_current 同步全部别名（handle/result/resolving/started/ih）")


def section_focus_gen(ck):
    ck.section("§D 焦点与换代")
    reg = mk_reg()
    g0 = reg.gen
    g1 = reg.bump_gen()
    ck.check(g1 == g0 + 1 and reg.gen == g1, "bump_gen 单调递增且返回新值")
    # focus_current 沿用 _focus_existing_download 语义：只切下载任务
    rec = TaskRecord(handle=FakeHandle(IH), download=True)
    with reg.lock:
        reg.put_record_locked(IH, rec)
    # focus_current / current() 自持锁：必须在锁段外调用（非重入锁纪律）
    ck.check(reg.focus_current(IH) is rec, "focus_current 命中返回记录")
    ck.check(reg.current() is rec and reg.current_ih == IH,
             "focus_current 同步 current_ih 与别名")
    ck.check(reg.focus_current(IH2) is None, "focus_current 未命中 → None")
    # 非下载任务（review 记录）不切焦点——原语义
    rec_rv = TaskRecord(handle=FakeHandle("d" * 40), download=False)
    with reg.lock:
        reg.put_record_locked("d" * 40, rec_rv)
    ck.check(reg.focus_current("d" * 40) is None and reg.current_ih == IH,
             "focus_current 对非下载记录返回 None 且不改焦点")
    # 别名同步：焦点切换把 handle/result 换到新记录
    rec2 = TaskRecord(handle=FakeHandle(IH2), result="r2", download=True)
    with reg.lock:
        reg.put_record_locked(IH2, rec2)
    reg.focus_current(IH2)
    ck.check(reg.result == "r2" and reg.handle is rec2.handle,
             "focus_current 把 handle/result 别名换到目标记录")


def section_find_record(ck):
    ck.section("§E find_record 双重匹配")
    reg = mk_reg()
    h = FakeHandle(IH)
    rec = TaskRecord(handle=h)
    with reg.lock:
        reg.put_record_locked(IH, rec)
    ck.check(reg.find_record(h) is rec, "主匹配：hash_key 直接命中")
    # 临时键记录：键是 tmp-<id(原始包装)>，但包装对象变了（新引用同 ih 不行——
    # 身份兜底要求同 handle 语义）→ 直接构造 tmp 键记录
    h2 = FakeHandle("bad")   # hash_key → tmp-*
    rec2 = TaskRecord(handle=h2)
    tmp_key = registry.hash_key(h2)
    with reg.lock:
        reg.put_record_locked(tmp_key, rec2)
    ck.check(reg.find_record(h2) is rec2, "临时键记录按句柄身份兜底命中")
    ck.check(reg.find_record(None) is None, "handle=None → None")
    ck.check(reg.find_record(FakeHandle("f" * 40)) is None,
             "查无归属（迟到告警句柄）→ None")


def section_detach(ck):
    ck.section("§F detach_record")
    reg = mk_reg()
    h = FakeHandle(IH)
    with reg.lock:
        reg.detach_record_locked(TaskRecord(handle=h))
    ck.check(h.paused == 1, "pause 恰一次")
    ck.check(len(h.set_flags_calls) == 1 and len(h.unset_flags_calls) == 1,
             "set_flags(upload_mode) + unset_flags(auto_managed) 各一次")

    class BoomH(FakeHandle):
        def pause(self):
            raise RuntimeError("失效")

    try:
        with reg.lock:
            reg.detach_record_locked(TaskRecord(handle=BoomH()))
        ck.check(True, "句柄抛异常：吞掉记日志，不上抛")
    except Exception as e:
        ck.check(False, f"detach 不得冒泡：{e}")
    try:
        with reg.lock:
            reg.detach_record_locked(TaskRecord(handle=None))
        ck.check(True, "handle=None 安全跳过")
    except Exception as e:
        ck.check(False, f"handle=None 不得抛：{e}")


def section_r1_lock(ck):
    ck.section("§G R-1：clear_resolving_safe 有锁专项")
    reg = mk_reg()
    reg.set_resolving(True, time.time())   # 置位（测试装置）
    obs, unwatch = watch_resolving_writes(reg)
    try:
        reg.set_resolving(True)            # 装置自身的锁外写，先归零观测
        obs.clear()
        reg.clear_resolving_safe()
    finally:
        unwatch()
    ck.check(obs and all(obs),
             f"clear_resolving_safe 的 resolving 写全部在锁内（观测 {obs}）")
    ck.check(reg.resolving is False and reg.resolve_started == 0.0,
             "clear 后 resolving=False / started=0")

    # 并发韧性：两线程 clear + 两线程读，500 轮不崩不死锁
    reg2 = mk_reg()
    reg2.set_resolving(True, time.time())
    errors = []

    def worker_clear():
        try:
            for _ in range(500):
                reg2.clear_resolving_safe()
        except Exception as e:
            errors.append(e)

    def worker_read():
        try:
            for _ in range(500):
                reg2.resolving_snapshot()
        except Exception as e:
            errors.append(e)

    ths = [threading.Thread(target=worker_clear) for _ in range(2)] \
        + [threading.Thread(target=worker_read) for _ in range(2)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=10)
    ck.check(not errors and not any(t.is_alive() for t in ths),
             "4 线程 ×500 轮 clear+snapshot 交错：无异常、无死锁")


def section_preview_dir(ck):
    ck.section("§H preview_dir")
    cache = "D:\\cache"
    reg = mk_reg(cache_dir=cache)
    ck.check(reg.preview_dir("") == cache, "空 ih → cache_dir 平铺（无 btih 兜底）")
    ck.check(reg.preview_dir("tmp-123") == cache, "tmp- 键 → 平铺")
    got = reg.preview_dir(IH)
    ck.check(got == os.path.join(cache, ".preview", IH),
             f"正常 ih → .preview/<ih>（{got}）")


def section_facade(ck):
    ck.section("§I Facade 接线：SessionManager ↔ TaskRegistry")
    ws = tempfile.mkdtemp(prefix="mv_reg_facade_")
    mgr = SessionManager(os.path.join(ws, "cache"))
    reg = mgr._registry
    ck.check(isinstance(reg, TaskRegistry), "SessionManager 构造出 TaskRegistry")
    ck.check(reg.lock is mgr._lock, "锁单源：mgr._lock 就是 registry 的锁（property 代理）")
    ck.check(mgr._torrents is reg.torrents and mgr._tasks is reg.tasks,
             "数据单源：_torrents/_tasks 代理同一本体（无第二份数据）")
    g = mgr._gen
    mgr._gen += 1
    ck.check(mgr._gen == g + 1 and reg.gen == g + 1,
             "_gen 读写都落 registry（property 可写代理）")
    mgr._current_ih = IH
    ck.check(reg.current_ih == IH, "_current_ih 写落 registry")
    rec = TaskRecord(handle=None)
    mgr._torrents[IH] = rec
    ck.check(reg.torrents.get(IH) is rec, "写入代理本体后 registry 侧可见（单源）")
    mgr._torrents.pop(IH, None)

    # R-1 端到端：_emit_error 的 resolving 写经过锁
    reg = mgr._registry
    obs, unwatch = watch_resolving_writes(reg)
    try:
        reg.set_resolving(True)
        obs.clear()
        got = []
        mgr.on_error = lambda m: got.append(m)
        mgr._emit_error("test-error")
        ck.check(got == ["test-error"], "on_error 回调照常发射")
        ck.check(obs and all(obs),
                 "R-1 端到端：_emit_error 内 resolving 写在锁内（锁段收敛证据）")
    finally:
        unwatch()
        import shutil
        shutil.rmtree(ws, ignore_errors=True)

    # 再导出冻结：fetcher 与测试模块顶层 import 的必须是同一个类对象
    # （fetcher 若自带复制定义，is 立即假）。不做 reload——reload 会产生新类
    # 对象使跨模块 is 必然假红（session_test §I 教训的反向应用）。
    import core.fetcher as fm
    ck.check(fm.TaskRecord is TaskRecord and fm.TaskRegistry is TaskRegistry,
             "fetcher 再导出 TaskRecord/TaskRegistry 同一对象（无复制定义）")
    ck.check(fm.PREVIEW_SUBDIR == ".preview" and fm.DOWNLOADS_SUBDIR == "downloads",
             "子目录常量再导出值不变")


def main() -> int:
    ck = ts.Checker("registry_test（阶段 3 注册表与锁归属专项）")
    ck.section("core/registry.py 专项验收（假句柄，不启会话/不联网）")
    section_hash_key(ck)
    section_ih_params(ck)
    section_put_record(ck)
    section_focus_gen(ck)
    section_find_record(ck)
    section_detach(ck)
    section_r1_lock(ck)
    section_preview_dir(ck)
    section_facade(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
