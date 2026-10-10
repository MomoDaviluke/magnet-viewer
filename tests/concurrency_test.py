"""并发安全专项护栏（concurrency_test）。

对应 `plan/11_concurrency_risk_analysis.md` 的第 1 批修复（R-1 / R-2 / R-5 / R-6b）。
本套件只守「**并发下必须成立的性质**」，不做吞吐计时（那属 `throughput_test`）。

分四段：
  §A R-1 原子写并发安全   —— 同一路径多写者不得产出混杂内容/残留临时文件
  §B R-2 落盘自持快照     —— 并发修改任务表时 persist_tasks 不得丢落盘
  §C R-5 句柄失效不抛     —— 句柄被并发摘除后 tick/seek/rearm 静默返回
  §D R-6b 关闭不在锁内落盘 —— shutdown 调 persist_tasks 时不得持锁（否则自锁死）

为什么 §A 用「内容自洽」而不是「不抛异常」当判据：
    历史缺陷（固定临时名 ``f"{path}.tmp"`` + 无锁）的症状是**两者都要防**：
      - Windows 实测主导形态是 **`PermissionError [WinError 32]`**——两个写者
        同时持有同一临时文件时，先完成者的 ``os.replace`` 搬不动「仍被另一个
        线程打开」的源文件。负向对照（5 写者 × 30 轮 × 5 次）实测旧实现
        **147–149 次异常 / 150 次写入**，几乎每次并发写都以异常收场；
      - POSIX 语义下的形态是两个 fd 各自从偏移 0 覆写 ⇒ **内容交错**，全程无
        异常，产物是「前半段 A 的、后半段 B 的」混杂文件。
    所以判据是三条并列：**写者无异常 + 产物等于某一次完整写入 + 无临时残留**。
    只断言「没抛异常」会漏掉混杂形态；只断言「内容自洽」会漏掉 Windows 的
    异常形态（实测正是后者先发生）。

退出码：0=通过 1=失败 2=SKIP（依赖缺失，绝不假装通过）
用法：python tests/concurrency_test.py
"""
from __future__ import annotations

import ast
import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import test_support as ts  # noqa: E402
    from core import persist as persist_mod  # noqa: E402
    from core import scheduler as sched_mod  # noqa: E402
    from core import session as session_mod  # noqa: E402
    from core import taskstore  # noqa: E402
    from core.registry import TaskRecord  # noqa: E402
    from core.scheduler import PreviewScheduler  # noqa: E402
except Exception as e:                      # 依赖缺失：显式 SKIP
    print(f"依赖缺失，无法执行并发护栏：{e}")
    sys.exit(2)

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

PAYLOAD = 64 * 1024          # 单次写入字节数：够大才让「写窗口」可观测
A_THREADS = 5
A_ROUNDS = 30
B_CALLS = 100                # §B 落盘次数
MUT_MAX = 5000               # §B 变异线程的插入上限（防止自旋撑爆对象/序列化）
MUT_SLEEP = 0.001            # §B 变异插入间隔：让增长跨越整轮而非几毫秒内饱和


# --------------------------------------------------------------------------
# §A R-1 原子写并发安全
# --------------------------------------------------------------------------

def _payload(writer: int, seq: int, size: int = PAYLOAD) -> bytes:
    """可自证内容的载荷：`"<writer>:<seq>:"` + writer 专属字节填满。"""
    head = f"{writer}:{seq}:".encode("ascii")
    return head + bytes([48 + writer]) * (size - len(head))


def _verify_payload(blob: bytes) -> str:
    """返回 "ok" 或失败原因（用于定位混杂内容）。"""
    idx = blob.find(b":", blob.find(b":") + 1)
    if idx < 0:
        return f"头部结构损坏（前 32 字节 {blob[:32]!r}）"
    try:
        writer, seq = (int(x) for x in blob[:idx].split(b":"))
    except ValueError:
        return f"头部不可解析（{blob[:24]!r}）"
    body = blob[idx + 1:]
    want = bytes([48 + writer])
    if body != want * len(body):
        bad = next((i for i, b in enumerate(body) if b != want), None)
        return (f"内容混杂：writer={writer} seq={seq} 第 {bad} 字节起不是"
                f"{want!r}（长度 {len(body)}）")
    return "ok"


def _no_tmp(directory: str) -> list:
    return [f for f in os.listdir(directory) if f.endswith(".tmp")]


def section_atomic_write(ck: ts.Checker) -> None:
    ck.section("§A R-1 原子写并发安全（同路径多写者）")
    tmp = tempfile.mkdtemp(prefix="mv_conc_a_")
    try:
        target = os.path.join(tmp, ".tasks.json")
        barrier = threading.Barrier(A_THREADS)
        errors: list = []

        def _gate() -> None:
            """起跑栅栏**只用于最大化写窗口重叠，不是正确性依赖**。

            慢机器/CI 上 `Barrier.wait(timeout=)` 可能超时抛
            `threading.BrokenBarrierError`——那不该判失败，否则护栏自己成了
            flaky 源（2026-09-17 复审指出）。超时就各自起跑，重叠概率低一些，
            但判据仍然成立（判据是「写者无异常 + 产物完整」，与重叠程度无关）。
            """
            try:
                barrier.wait(timeout=30)
            except threading.BrokenBarrierError:
                pass

        def writer(idx: int) -> None:
            try:
                _gate()
                for seq in range(A_ROUNDS):
                    taskstore.atomic_write_bytes(target, _payload(idx, seq))
            except Exception as e:
                errors.append(f"writer{idx}: {e!r}")

        ths = [threading.Thread(target=writer, args=(i,))
               for i in range(A_THREADS)]
        t0 = time.perf_counter()
        for t in ths:
            t.start()
        for t in ths:
            t.join(60)
        dt = time.perf_counter() - t0

        ck.check(not errors, f"{A_THREADS} 写者 × {A_ROUNDS} 轮无异常"
                            f"（{errors[:2] or '无'}）")
        ck.check(all(not t.is_alive() for t in ths), "全部写者正常结束（无卡死）")

        with open(target, "rb") as f:
            blob = f.read()
        verdict = _verify_payload(blob)
        ck.check(verdict == "ok",
                 f"最终文件是**某一次完整写入**而非混杂内容（{verdict}；"
                 f"用时 {dt:.2f}s）")
        left = _no_tmp(tmp)
        ck.check(not left, f"无临时文件残留（{left or '无'}）")

        # 临时名唯一性（固定名缺陷的直接对立面）
        p1, p2 = taskstore._tmp_path(target), taskstore._tmp_path(target)
        ck.check(p1 != p2 and p1.endswith(".tmp") and p2.endswith(".tmp"),
                 f"临时名唯一（{os.path.basename(p1)} vs {os.path.basename(p2)}）")
        # 分片锁：同路径必得同一把（否则互斥形同虚设）
        ck.check(taskstore._write_lock_for(target)
                 is taskstore._write_lock_for(target),
                 "同一路径恒得同一把写锁（分片一致）")
        ck.check(len(taskstore._WRITE_LOCKS) == taskstore._WRITE_SHARDS,
                 f"写锁内存有界（{taskstore._WRITE_SHARDS} 片，不随目标数增长）")

        # 失败路径：replace 抛错 → 异常照原样上抛 + 旧文件完好 + 无残留
        good = _payload(9, 999)
        taskstore.atomic_write_bytes(target, good)
        real_replace = taskstore.os.replace
        taskstore.os.replace = lambda s, d: (_ for _ in ()).throw(
            OSError("simulated replace failure"))
        raised = None
        try:
            taskstore.atomic_write_bytes(target, _payload(1, 1))
        except OSError as e:
            raised = e
        finally:
            taskstore.os.replace = real_replace
        ck.check(raised is not None, "替换失败时异常照原样上抛（调用方据此判失败）")
        with open(target, "rb") as f:
            ck.check(f.read() == good, "替换失败不得破坏已存在的目标文件")
        ck.check(not _no_tmp(tmp), "失败路径也清掉临时文件（不留残骸）")

        # 不同路径并发互不干扰
        errs2: list = []

        def writer2(idx: int) -> None:
            try:
                for seq in range(20):
                    taskstore.atomic_write_bytes(
                        os.path.join(tmp, f"m{idx}.json"), _payload(idx, seq))
            except Exception as e:
                errs2.append(f"w{idx}: {e!r}")

        ths2 = [threading.Thread(target=writer2, args=(i,)) for i in range(5)]
        for t in ths2:
            t.start()
        for t in ths2:
            t.join(60)
        ok2 = True
        for i in range(5):
            with open(os.path.join(tmp, f"m{i}.json"), "rb") as f:
                v = _verify_payload(f.read())
            if v != "ok":
                ok2 = False
                errs2.append(f"m{i}.json: {v}")
        ck.check(not errs2 and ok2,
                 f"5 路不同目标并发写各自完整（{errs2[:2] or '全部通过'}）")

        # 真实调用链：save_tasks/load_tasks 并发 roundtrip 可解析
        tdir = os.path.join(tmp, "cache")
        task = {"info_hash": "a" * 40, "source": "magnet:?xt=urn:btih:" + "a" * 40,
                "name": "n", "total_size": 1, "files": [], "selected": [],
                "state": "downloading", "priority": 0, "save_path": tdir,
                "error": "", "retries": 0, "created_at": 0.0, "finished_at": None,
                "seed": False}
        errs3: list = []

        def writer3(idx: int) -> None:
            try:
                for i in range(10):
                    # 键必须与记录自身的 info_hash **一致**，否则 load_tasks 按
                    # 「记录非法」丢弃（core/taskstore.py 的
                    # `t["info_hash"] != str(key).strip().lower()`）。
                    # 首版夹具用固定 info_hash 配不同键 → 读出 0 条，是夹具缺陷。
                    key = f"{idx}{i:03d}" + "b" * 36
                    entry = dict(task)
                    entry["info_hash"] = key
                    taskstore.save_tasks(tdir, {key: entry})
            except Exception as e:
                errs3.append(repr(e))

        ths3 = [threading.Thread(target=writer3, args=(i,)) for i in range(4)]
        for t in ths3:
            t.start()
        for t in ths3:
            t.join(60)
        loaded = taskstore.load_tasks(tdir)
        ck.check(not errs3, f"并发 save_tasks 无异常（{errs3[:2] or '无'}）")
        ck.check(isinstance(loaded, dict) and len(loaded) > 0,
                 f"并发落盘后仍可解析出任务（{len(loaded)} 条，"
                 f"应等于某一写的完整集合）")
        ck.check(not _no_tmp(tdir), "并发 save_tasks 后无临时文件残留")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# §B R-2 落盘自持快照
# --------------------------------------------------------------------------

def _persist_deps(cache_dir: str, lock, tasks_get, persisted: list):
    return persist_mod.PersistDeps(
        cache_dir=cache_dir, download_dir=os.path.join(cache_dir, "downloads"),
        lock=lock, tasks_get=tasks_get, torrents_get=lambda: {},
        gen_get=lambda: 0, ses_get=lambda: None,
        hash_key=lambda h: "", find_record=lambda h: None,
        put_record=lambda *a, **k: None, record_cls=TaskRecord,
        result_from_torrent_info=lambda ti, c: None,
        activate_download=lambda rec: None)


def section_persist_snapshot(ck: ts.Checker) -> None:
    """守 R-2 引入的性质：`persist_tasks` 的落盘必须基于**锁内快照**。

    **判据为什么是「身份 + 取锁」而不是「让竞态去撞 RuntimeError」**（2026-09-17
    实测修正）：初版判据是「实落次数 == 调用次数」，指望旧实现（把活字典直接交给
    `save_tasks`）在迭代期撞上 `dictionary changed size` 被 `except` 吞掉。
    负向对照实测 **旧实现 5 轮 × 100 次调用，0 次命中**——原因是**窗口太窄**：
    一次落盘 ~3.5ms 里绝大部分是 `fsync`，真正在迭代字典的只有几十微秒，
    变异的插入几乎不可能正好落在那几十微秒内。**依赖时序的判据没有牙齿。**

    改为两条**确定性**判据（与实现语义一一对应，不依赖调度）：
      1. `save_tasks` 收到的对象**不是**活字典本身（身份检查，`is not live`）；
      2. 每次落盘都**取过一次** registry 锁（`_ProbeLock.acquires` 递增）。
    旧实现在 (1) 上必失败（它传的就是活字典），新实现在两条上都必通过。
    """
    ck.section("§B R-2 落盘自持快照（并发修改任务表时不丢落盘）")
    tmp = tempfile.mkdtemp(prefix="mv_conc_b_")
    warns: list = []
    real_warn = persist_mod.log_warning
    try:
        live: dict = {}
        lock = _ProbeLock()          # 可观测锁：同时用于「是否取过锁」的判据
        persisted: list = []
        p = persist_mod.TaskPersistence(
            _persist_deps(tmp, lock, lambda: live, persisted))
        real_save = persist_mod.save_tasks

        def spy(cache_dir, tasks):
            # 记录 (条数, 是否是活字典本身)——后者是确定性判据的核心
            persisted.append((len(tasks), tasks is live))
            return real_save(cache_dir, tasks)

        persist_mod.save_tasks = spy
        persist_mod.log_warning = lambda tag, msg=None: warns.append(
            f"{tag} {msg}")

        stop = threading.Event()

        def mutator() -> None:
            """**持锁原地插入**新键：模拟「按锁纪律插入、但对象正被迭代」的最坏形态。

            两条自我约束（初版没有，实测把本用例跑成「无输出被 SIGTERM 杀掉」
            ——本项目对死锁/活锁的典型症状）：
              1. 插入量**封顶** `MUT_MAX`：不封顶时自旋插入可达百万键，主线程每次
                 `json.dumps` 都要序列化整表 ⇒ 测试从秒级变成不可完成、内存暴涨。
              2. 插入**限速**（`time.sleep(MUT_SLEEP)`）而不是全速饱和：全速时
                 几毫秒就插满，之后对象不再变化，反而让「是否与迭代重叠」的
                 概率掉到零（这也是负向对照从「有命中」变成「零命中」的原因）。
                 限速后增长跨越整轮，稳定制造重叠。
            判据本身不依赖插入速率，故封顶/限速都不影响拦截力。
            """
            i = 0
            while not stop.is_set():
                with lock:
                    if i < MUT_MAX:
                        live[f"k{i:05d}"] = {"state": "downloading",
                                             "error": ""}
                i += 1
                time.sleep(MUT_SLEEP)

        th = threading.Thread(target=mutator)
        th.start()
        calls = B_CALLS
        t0 = time.perf_counter()
        for _ in range(calls):
            p.persist_tasks()
        dt = time.perf_counter() - t0
        stop.set()
        th.join(5)

        # --- 判据 1：确定性「身份检查」---
        leaked = [n for n, is_live in persisted if is_live]
        ck.check(not leaked,
                 f"save_tasks 收到的**不是活字典本身**（泄漏 {len(leaked)} 次；"
                 f"旧实现在此必失败）")
        # --- 判据 2：确定性「每次落盘都取锁」---
        ck.check(lock.acquires >= calls,
                 f"每次落盘都取过一次 registry 锁"
                 f"（{lock.acquires} ≥ {calls}；不取锁就无法与写者互斥）")
        # --- 辅助判据：次数与静默降级 ---
        ck.check(len(persisted) == calls,
                 f"{calls} 次 persist_tasks 全部真正落盘（实落 {len(persisted)} 次）")
        ck.check(not warns, f"无「静默降级」告警（{warns[:2] or '无'}）")
        with open(os.path.join(tmp, ".tasks.json"), "r", encoding="utf-8") as f:
            raw = json.load(f)
        ck.check(isinstance(raw, dict) and raw.get("version") == 1,
                 f"并发落盘产物可解析（version={raw.get('version')}）")
        # 一致性判据：键必须是 k00000 起的**连续前缀**。插入单调 + 快照一次成型
        # ⇒ 任何一次落盘看到的都是「前 m 条」，不可能出现空洞。
        keys = sorted(raw["tasks"])
        want = [f"k{i:05d}" for i in range(len(keys))]
        ck.check(keys == want,
                 f"落盘键是连续前缀（{len(keys)} 条；非连续说明快照被撕裂）")
        ck.check(len(keys) > 0,
                 f"落盘非空（{len(keys)} 条；{calls} 次落盘用时 {dt:.2f}s）")
    finally:
        persist_mod.log_warning = real_warn
        if 'real_save' in dir():
            persist_mod.save_tasks = real_save
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# §C R-5 句柄失效不抛
# --------------------------------------------------------------------------

class _DeadHandle:
    """失效句柄替身：任何绑定调用都抛（模拟 remove_torrent 之后）。"""

    def torrent_file(self):
        raise RuntimeError("invalid torrent handle")

    def set_piece_deadline(self, p, ms):
        raise RuntimeError("invalid torrent handle")

    def clear_piece_deadlines(self):
        raise RuntimeError("invalid torrent handle")


class _File:
    ext = ".mp4"
    path = "video/movie.mp4"
    index = 0
    size = 4 * 1024 * 1024
    offset = 0
    start_piece = 0
    end_piece = 3


def section_dead_handle(ck: ts.Checker) -> None:
    ck.section("§C R-5 句柄失效/被摘除后不抛异常")
    s = PreviewScheduler()
    s.handle = _DeadHandle()
    s.file = _File()
    s._head_n = 4
    s._play_from = 0
    s._tail_pieces = [0, 1, 2, 3]
    s._entry_pieces = [3]
    for name, call in (("tick", s.tick),
                       ("seek_to_byte", lambda: s.seek_to_byte(0)),
                       ("rearm", s.rearm),
                       ("request_range", lambda: s.request_range(0, 1024)),
                       ("append", s.buffer_progress)):
        try:
            call()
            ck.check(True, f"{name}() 在失效句柄下未抛异常")
        except Exception as e:
            ck.check(False, f"{name}() 在失效句柄下抛了 {e!r}")

    ck.check(s._torrent_file_or_none() is None,
             "_torrent_file_or_none() 失效句柄返回 None（不抛）")
    s2 = PreviewScheduler()
    ck.check(s2._torrent_file_or_none() is None,
             "_torrent_file_or_none() 未绑句柄返回 None")
    # 句柄被置 None（stop 的语义）后同样不得抛
    s.handle = None
    for name, call in (("tick", s.tick), ("rearm", s.rearm),
                       ("seek_to_byte", lambda: s.seek_to_byte(0)),
                       ("request_range", lambda: s.request_range(0, 1))):
        try:
            call()
            ck.check(True, f"{name}() 在 handle=None 下未抛异常")
        except Exception as e:
            ck.check(False, f"{name}() 在 handle=None 下抛了 {e!r}")


# --------------------------------------------------------------------------
# §D R-6b 关闭不在锁内落盘
# --------------------------------------------------------------------------

class _ProbeLock:
    """可观测锁：记录「持有时长」与「某时刻是否被持有」。"""

    def __init__(self):
        self._l = threading.Lock()
        self.held = 0
        self.acquires = 0

    def __enter__(self):
        self._l.acquire()
        self.held += 1
        self.acquires += 1
        return self

    def __exit__(self, *a):
        self.held -= 1
        self._l.release()
        return False


class _Sched:
    def __init__(self):
        self.stopped = 0
        self.handle = None

    def stop(self):
        self.stopped += 1


class _Ses:
    """会话替身：shutdown 的 save_resume_data / remove_torrent 两条分支都要走到。

    首版夹具让 ``ses_get`` 返回 None，于是「请求 fastresume」与「摘句柄」两段
    被 `if ses is not None` 整段跳过，`save_resume_data 仍被调用` 恒失败——
    **夹具缺陷**，不是产品缺陷。
    """

    def __init__(self):
        self.removed = 0

    def remove_torrent(self, handle, options=0):
        self.removed += 1


_SES = _Ses()


class _Handle:
    def __init__(self, sink):
        self._sink = sink

    def save_resume_data(self, flags):
        self._sink.append(("save_resume_data", flags))


def section_shutdown_off_lock(ck: ts.Checker) -> None:
    ck.section("§D R-6b 关闭路径：同步 I/O 不在锁内")
    lock = _ProbeLock()
    sched = _Sched()
    sink: list = []
    observed: list = []
    rec = TaskRecord(handle=_Handle(sink), download=True)
    rec.result = object()          # 仅需非 None（快照条件）
    tasks = {"a" * 40: {"state": "downloading"}}
    state = {"tasks": tasks, "synced": False}

    def persist_tasks():
        # 关键观测点：落盘被调用时锁必须**未被持有**（否则 Lock 不可重入 → 自锁死）
        observed.append(lock.held)

    deps = session_mod.SessionDeps(
        listen_port=0, active_downloads=3, lock=lock,
        ses_get=lambda: _SES, ses_set=lambda v: None,
        running_get=lambda: True, running_set=lambda v: None,
        thread_get=lambda: None, thread_set=lambda v: None,
        last_sweep_get=lambda: 0.0, last_sweep_set=lambda v: None,
        metadata_timeout_get=lambda: 90.0, metadata_timeout_set=lambda v: None,
        torrents_get=lambda: {"a" * 40: rec}, tasks_get=lambda: state["tasks"],
        tasks_set=lambda v: state.update(tasks=v),
        hash_key=lambda h: "a" * 40, find_record=lambda h: rec,
        current_record=lambda: rec, tasks_loader=lambda: {},
        restore_task=lambda t: True,
        thread_factory=lambda target: None, scheduler_get=lambda: sched,
        persist_tasks=persist_tasks,
        write_resume_from_alert=lambda a: None,
        request_resume=lambda r: None, drain_resume_alerts=lambda: None,
        on_metadata_received=lambda a: None,
        on_download_finished=lambda r: None, emit_error=lambda m: None,
        clear_resolving=lambda: None, clear_runtime_state=lambda: None,
        dht_state_path="")
    core = session_mod.SessionCore(deps)
    # 局限（复审指出，必须写明）：本段证明的是「`session.shutdown` **这一个**调用点
    # 不在锁内」，**不等于**「`persist_tasks` 全局不会重入」——后者由 §E 的 AST
    # 静态红线覆盖（它扫描全部模块的全部调用点）。
    try:
        core.shutdown()
        ck.check(observed == [0],
                 f"persist_tasks 执行时锁未被持有（观测 {observed}，"
                 f"持锁调用会因 Lock 不可重入而自锁死）")
        ck.check(sched.stopped == 1, "shutdown 先停调度器")
        ck.check(len(sink) == 1, f"save_resume_data 仍被调用（{len(sink)} 次）")
        ck.check(lock.held == 0, f"shutdown 结束时锁已释放（held={lock.held}）")
        ck.check(lock.acquires >= 2,
                 f"锁仍按「取快照」粒度获取（{lock.acquires} 次），"
                 f"未把整段关闭串成一段")
    except Exception as e:
        ck.check(False, f"shutdown() 抛异常：{e!r}")


# --------------------------------------------------------------------------
# §E 静态红线：落盘调用不得出现在锁段内（AST）
# --------------------------------------------------------------------------

_SENSITIVE = ("persist_tasks", "save_tasks", "atomic_write_bytes")
# 只把 registry 那把全局锁（`lock` / `_lock`）算作「会构成自锁死的锁」：
# 三个目标函数里**只有 `persist_tasks` 自持锁**（取 `d.lock`），故只有
# 「已持 d.lock 再调 persist_tasks」才构成重入。`metacache._PRUNE_LOCK` /
# `_STATS_LOCK` 不在这条链上（`atomic_write_bytes` 从不取它们），把它们纳入
# 名单反而会在剪枝路径上**误报**——复审曾建议纳入，此处按语义拒绝并留档。
_LOCK_NAMES = {"lock", "_lock"}


def _corner_name(node) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _scan_locked_calls(path: str) -> list:
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    hits: list = []

    def walk(node, locks: list) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.With):
                inner = locks + [_corner_name(it.context_expr)
                                 for it in child.items]
                walk(child, [n for n in inner if n in _LOCK_NAMES])
                continue
            if isinstance(child, ast.Call):
                fn = child.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (
                    fn.id if isinstance(fn, ast.Name) else "")
                if name in _SENSITIVE:
                    hits.append((child.lineno, name, list(locks)))
            walk(child, locks)

    walk(tree, [])
    return hits


def section_lock_redline(ck: ts.Checker) -> None:
    """守 R-2 引入的新约束：`persist_tasks` 现在自持 lock（锁内取快照）。

    `threading.Lock` 不可重入 ⇒ 任何「已持锁再调落盘」的调用点都会**自锁死**
    （症状是「测试超时且输出为空」，不是报错）。人眼核对 30+ 个调用点会漏，
    故用 AST 机械检查。

    **局限（必须写明）**：只检查**词法外层**的 `with <锁>`，看不到「本函数被
    另一个持锁函数调用」这种间接情形。间接情形靠本仓库既有的 `*_locked`
    命名纪律兜：`*_locked` 方法假定已持锁、**绝不**落盘；公开方法自己取锁、
    出锁落盘（`core/taskops.py` 全篇如此）。本段只作第一道机械网。
    """
    ck.section("§E 静态红线：落盘调用不得出现在锁段内（AST）")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    scanned, violators = 0, []
    # **按目录发现**而非写死文件名：写死列表会漏掉将来新增的 core/ui 模块，
    # 让「零违规」随模块数增长而悄悄失真（2026-09-17 复审指出）。
    targets: list = []
    for sub in ("core", "ui"):
        d = os.path.join(root, sub)
        if os.path.isdir(d):
            targets += [os.path.join(d, f) for f in sorted(os.listdir(d))
                        if f.endswith(".py")]
    targets += [os.path.join(root, f) for f in sorted(os.listdir(root))
                if f.endswith(".py")]
    for path in targets:
        try:
            hits = _scan_locked_calls(path)
        except SyntaxError as e:
            ck.check(False, f"{os.path.basename(path)} 解析失败：{e}")
            continue
        scanned += 1
        for lineno, fname, locks in hits:
            if locks:
                violators.append(f"{os.path.basename(path)}:{lineno} {fname}")
    ck.check(scanned >= 25,
             f"扫描到 {scanned} 个模块文件（core/ + ui/ + 根目录全量发现）")
    names = {os.path.basename(p) for p in targets}
    ck.check({"taskstore.py", "persist.py", "session.py",
              "main_window.py"} <= names,
             f"发现逻辑覆盖关键模块（{len(names)} 个文件名）——"
             f"发现失败不会退化成「零违规」的假绿")
    ck.check(not violators,
             f"核心模块内无「持锁落盘」调用点（违规 {violators or '无'}）")
    # 反向自检：扫描器本身要能识别出锁段（否则「零违规」恒真、护栏是空的）
    probe = os.path.join(tempfile.gettempdir(), "mv_ast_probe.py")
    with open(probe, "w", encoding="utf-8") as f:
        f.write("def a(lock, x, y):\n"
                "    with lock:\n"
                "        x.persist_tasks()\n"
                "    y.persist_tasks()\n")
    try:
        hits = _scan_locked_calls(probe)
        locked = [h for h in hits if h[2]]
        free = [h for h in hits if not h[2]]
        ck.check(len(locked) == 1 and len(free) == 1,
                 f"扫描器灵敏度自检：锁内 {len(locked)} 处 / 锁外 {len(free)} 处"
                 f"（应为 1 / 1）")
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


# --------------------------------------------------------------------------
# §F R-3 调度器互斥（静态不变式 + 三线程压力）
# --------------------------------------------------------------------------

class _LiveTI:
    """可用 torrent_info 替身（begin 需要 num_files / piece_length）。"""

    def __init__(self, num_files=4, piece_length=512 * 1024):
        self._n = num_files
        self._pl = piece_length

    def num_files(self):
        return self._n

    def piece_length(self):
        return self._pl


class _LiveStatus:
    def __init__(self, pieces):
        self.pieces = pieces
        self.state = "downloading"
        self.num_peers = 1
        self.download_payload_rate = 0
        self.total_done = 0


class _LiveHandle:
    """可用句柄替身：记录 set_piece_deadline / 各种 flag 调用（供 §F 断言）。"""

    def __init__(self, num_pieces=400):
        self.deadlines: list = []
        self.cleared = 0
        self.priorities: list = []
        self.paused = 0
        self.resumed = 0
        self._np = num_pieces

    def torrent_file(self):
        return _LiveTI()

    def status(self, flags=None):
        return _LiveStatus([False] * self._np)

    def have_piece(self, p):
        return False

    def set_piece_deadline(self, p, ms):
        self.deadlines.append((p, ms))

    def clear_piece_deadlines(self):
        self.cleared += 1

    def prioritize_files(self, prio):
        self.priorities.append(list(prio))

    def pause(self):
        self.paused += 1

    def resume(self):
        self.resumed += 1

    def set_flags(self, flags):
        pass

    def unset_flags(self, flags):
        pass


def _scan_locked_calls_in_methods(path: str, methods: frozenset) -> list:
    """找「在锁段内调用 ``methods`` 里任一方法」的位置。

    锁段有两种形态，**两种都要认**（首版只认第二种，于是被 4 个装饰器入口
    完全绕过 —— 复审指出后补上，这一条正是「护栏在它要守的新机制处失效」）：

      1. ``@_exclusive(...)`` 装饰的方法：**整个函数体**都在锁内
         （tick / rearm / request_range / stop 走这条）；
      2. 方法体内显式 ``with self._lock:`` 语句（begin / seek_to_byte 走这条）。

    只做词法判定，**看不到间接调用**（锁内调 A、A 再调取锁方法）——该局限对
    §E 与 §F 同样成立，当前代码里经人工核对不存在这种路径。
    用手写递归而不是 ``ast.NodeVisitor``：``visit_With`` 里子节点遍历与
    ``orelse`` 这类细节容易写错（首版踩过：3.13 的 ``With`` 没有 ``orelse``）。
    """
    with open(path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    hits: list = []

    def _is_self_lock(node) -> bool:
        return (isinstance(node, ast.Attribute) and node.attr == "_lock"
                and isinstance(node.value, ast.Name)
                and node.value.id == "self")

    def _is_exclusive_deco(node) -> bool:
        """``@_exclusive(...)`` 或 ``@_exclusive`` 都算。"""
        fn = node.func if isinstance(node, ast.Call) else node
        name = fn.attr if isinstance(fn, ast.Attribute) else (
            fn.id if isinstance(fn, ast.Name) else "")
        return name == "_exclusive"

    def walk(node, in_lock: int) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                deco_locked = any(_is_exclusive_deco(d)
                                  for d in child.decorator_list)
                walk(child, in_lock + (1 if deco_locked else 0))
                continue
            if isinstance(child, ast.With):
                is_lock = any(_is_self_lock(it.context_expr)
                              for it in child.items)
                walk(child, in_lock + (1 if is_lock else 0))
                continue
            if in_lock and isinstance(child, ast.Call):
                fn = child.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (
                    fn.id if isinstance(fn, ast.Name) else "")
                if name in methods:
                    hits.append((child.lineno, name))
            walk(child, in_lock)

    walk(tree, 0)
    return hits


def section_scheduler_lock(ck: ts.Checker) -> None:
    ck.section("§F R-3 调度器互斥（静态不变式 + 三线程压力）")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spath = os.path.join(root, "core", "scheduler.py")

    # --- F1 静态：锁段内不得调用会取锁的方法（Lock 不可重入） ---
    taking = set(getattr(sched_mod, "_LOCK_TAKING", ()))
    ck.check(bool(taking), f"_LOCK_TAKING 已声明（{len(taking)} 个入口）")
    try:
        hits = _scan_locked_calls_in_methods(spath, taking)
    except SyntaxError as e:
        hits, ck_err = [], f"scheduler.py 解析失败：{e}"
        ck.check(False, ck_err)
    ck.check(not hits,
             f"`with self._lock` 段内无「取锁方法」调用（违规 {hits or '无'}）")
    # 灵敏度自检：扫描器必须同时认「装饰器锁」与「显式 with 锁」两种形态
    probe = os.path.join(tempfile.gettempdir(), "mv_lock_probe.py")
    with open(probe, "w", encoding="utf-8") as f:
        f.write("class A:\n"
                "    @_exclusive()\n"
                "    def deco_locked(self):\n"
                "        self.tick()\n"
                "        self.helper()\n"
                "\n"
                "    def with_locked(self):\n"
                "        with self._lock:\n"
                "            self.tick()\n"
                "        self.tick()\n"
                "\n"
                "    def free(self):\n"
                "        self.tick()\n")
    try:
        got = _scan_locked_calls_in_methods(probe, {"tick"})
        ck.check(len(got) == 2,
                 f"扫描器灵敏度自检：命中 {len(got)} 处（应为 2 = "
                 f"装饰器锁 1 处 + 显式 with 锁 1 处；`free` 里的不算）——"
                 f"首版只认 with 形态，4 个装饰器入口被完全绕过")
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass

    # --- F2 行为：begin 的**首次预约必须真的发生** ---
    # 这是本批最大的回归风险：若把 begin 整个方法体包进锁，它尾部的 self.tick()
    # 会因**非阻塞取锁失败**而静默跳过 ⇒ 首窗永不预约 ⇒ 播放永不开。
    s = sched_mod.PreviewScheduler()
    h, f = _LiveHandle(), _File()
    s.begin(h, f)
    ck.check(bool(h.deadlines),
             f"begin() 后句柄确实收到预约（{len(h.deadlines)} 处 deadline）——"
             f"「持锁调 tick 导致首窗丢失」的直接护栏")
    ck.check(s._scheduled_to >= f.start_piece,
             f"begin() 后 _scheduled_to 已推进（{s._scheduled_to} ≥ "
             f"{f.start_piece}）")
    s.tick()                      # 周期拍不得抛
    s.request_range(0, 1 << 20)   # 点播不得抛
    ck.check(True, "tick()/request_range() 在活动状态下不抛")

    # --- F2b 反例自检：持锁调 tick() 必然被跳过（确定性实证，不靠时序）---
    # 这正是 begin/seek_to_byte 只包「状态赋值区间」、必须在 tick 前放锁的理由。
    s0 = sched_mod.PreviewScheduler()
    h0 = _LiveHandle()
    s0.handle, s0.file = h0, _File()
    s0._head_n, s0._play_from, s0._scheduled_to = 4, 0, -1
    s0._lock.acquire()
    try:
        s0.tick()                 # 非阻塞取锁失败 → 静默跳过（无异常！）
        skipped = (s0._scheduled_to == -1 and not h0.deadlines)
    finally:
        s0._lock.release()
    s0.tick()                     # 放锁后同一拍立刻能预约
    ck.check(skipped and bool(h0.deadlines),
             "反例自检：锁内 tick() 被静默跳过（首窗丢失、且**不报错**）、"
             f"放锁后立即生效（{len(h0.deadlines)} 处 deadline）")

    # --- F3 三线程压力：GUI 拍 + HTTP 点播 + 生命周期换代 ---
    # 线程设计守两条纪律（第 1 批实测过：自旋变异线程会把用例跑成
    # 「无输出被 SIGTERM 杀掉」）：**限时** + 每轮 `sleep(0)` 让出 GIL。
    hs, fs = _LiveHandle(), _File()
    s2 = sched_mod.PreviewScheduler()
    stop = threading.Event()
    errors: list = []
    counts = {"tick": 0, "req": 0, "cycle": 0}

    def _guard(tag: str, fn) -> None:
        try:
            fn()
            counts[tag] += 1
        except Exception as e:                 # 任何异常逃逸都要现形
            errors.append(f"{tag}: {type(e).__name__}: {e}")

    def t_tick() -> None:
        while not stop.is_set():
            _guard("tick", s2.tick)
            time.sleep(0)

    def t_req() -> None:
        while not stop.is_set():
            _guard("req", lambda: s2.request_range(0, 4 << 20))
            time.sleep(0)

    def t_cycle() -> None:
        while not stop.is_set():
            _guard("cycle", lambda: s2.begin(hs, fs))
            _guard("cycle", s2.stop)
            time.sleep(0)

    ths = [threading.Thread(target=t) for t in (t_tick, t_req, t_cycle)]
    for t in ths:
        t.start()
    time.sleep(1.2)
    stop.set()
    for t in ths:
        t.join(15)
    alive = [t.name for t in ths if t.is_alive()]
    ck.check(not alive,
             f"三线程压力 1.2s 后全部退出（未退出 {alive or '无'}）——"
             f"卡住即视为死锁/活锁")
    ck.check(not errors,
             f"压力期间无异常逃逸（{errors[:2] or '无'}；"
             f"tick {counts['tick']} / req {counts['req']} / "
             f"cycle {counts['cycle']} 次）")
    ck.check(counts["cycle"] > 0 and counts["tick"] > 0,
             f"三线程都真的跑了（{counts}）")
    # 收尾态：stop 之后不得再有活动，且各入口仍不抛
    s2.stop()
    ck.check(not s2.active, "压力后 stop() 生效（active is False）")
    for name, call in (("tick", s2.tick), ("request_range",
                                           lambda: s2.request_range(0, 1)),
                       ("seek_to_byte", lambda: s2.seek_to_byte(0)),
                       ("rearm", s2.rearm), ("stop", s2.stop)):
        try:
            call()
            ck.check(True, f"停止后 {name}() 不抛")
        except Exception as e:
            ck.check(False, f"停止后 {name}() 抛了 {e!r}")


# --------------------------------------------------------------------------
# §G 第 3 批：流服务停止事件 / 清理名单回调 / 孤儿清扫 / replace 重试
# --------------------------------------------------------------------------

def section_batch3(ck: ts.Checker) -> None:
    ck.section("§G 第 3 批：R-6B 停止事件 / R-7 名单回调 / R-14 清扫 / R-15 重试")
    import tempfile as _tf

    # ---- G1 R-14 孤儿清扫（只删过期 .tmp，保留新鲜条目与其余文件）----
    cache = _tf.mkdtemp(prefix="mv_g1_")
    try:
        from core import cache_guard
        # 必须先写受管标记：sweep 走 guard_ok_for_cleanup（只清「本程序声明过」
        # 的缓存目录）——首版夹具没写标记，守卫按设计拒绝，断言全 FAIL。
        cache_guard.ensure_cache_dir(cache)
        for sub in ("", ".resume", ".meta"):
            d = os.path.join(cache, sub)
            os.makedirs(d, exist_ok=True)
            stale = os.path.join(d, "a" * 40 + ".0001.0001.1.tmp")
            fresh = os.path.join(d, "b" * 40 + ".0001.0001.2.tmp")
            keep = os.path.join(d, "c.bin")
            for p, age in ((stale, 3600.0), (fresh, 0.0), (keep, 3600.0)):
                with open(p, "wb") as fh:
                    fh.write(b"x")
                if age:
                    old = time.time() - age
                    os.utime(p, (old, old))
        n = cache_guard.sweep_tmp_files(cache)
        left = [f for sub in ("", ".resume", ".meta")
                for f in os.listdir(os.path.join(cache, sub))]
        ck.check(n == 3 and all("0001.0001.1.tmp" not in f for f in left),
                 f"过期孤儿被清（{n} 个，含三层目录）")
        ck.check(all("0001.0001.2.tmp" in f for f in left
                     if f.endswith(".tmp")),
                 f"新鲜 .tmp 保留（防与并发写者赛跑）")
        ck.check("c.bin" in left, "非 .tmp 文件不动")
    finally:
        shutil.rmtree(cache, ignore_errors=True)

    # ---- G2 R-7 名单回调：清理期间新出现的活任务目录必须被逐条复核保住 ----
    cache = _tf.mkdtemp(prefix="mv_g2_")
    try:
        d1 = os.path.join(cache, ".preview", "t1")
        d2 = os.path.join(cache, ".preview", "t2")
        os.makedirs(d1)
        os.makedirs(d2)
        with open(os.path.join(d1, "a.mp4"), "wb") as fh:
            fh.write(b"x")
        with open(os.path.join(d2, "b.mp4"), "wb") as fh:
            fh.write(b"x")
        with open(os.path.join(cache, "junk.bin"), "wb") as fh:
            fh.write(b"x")
        state = {"calls": 0, "keep": {"t1"}}

        def keep_fn():
            state["calls"] += 1
            # 模拟「清理中途新增活任务 t2」：首次取名单只认得 t1，
            # 之后（逐条复核阶段）t2 也已是活任务。**键名必须与磁盘目录名
            # 一致**——首版夹具用 "d1"/"d2" 而磁盘叫 t1/t2，保护名单指向
            # 不存在的目录，partial 清理按设计把 t1/t2 删光（夹具缺陷）。
            if state["calls"] > 1:
                state["keep"].add("t2")
            return {os.path.join(cache, ".preview", k) for k in state["keep"]}

        from core.cache_guard import clear_cache_contents
        removed = clear_cache_contents(cache, keep_dirs=keep_fn)
        ck.check(os.path.isdir(d1), "初始活任务 t1 保留")
        ck.check(os.path.isdir(d2),
                 f"清理中途新增的活任务 t2 被逐条复核保住"
                 f"（名单取用 {state['calls']} 次）——R-7 的 TOCTOU 直接护栏")
        ck.check(not os.path.isfile(os.path.join(cache, "junk.bin")),
                 f"无保条目照常清理（removed={removed}）")
        # 反例自检：名单源故障（返回 None）必须**保守中止**——产品行为是抛
        # RuntimeError（调用方 try→-1 兜底），绝不把 None 当成「空名单」继续删。
        aborted = False
        try:
            clear_cache_contents(cache, keep_dirs=lambda: None)
        except RuntimeError:
            aborted = True
        ck.check(aborted and os.path.isdir(d1),
                 "名单取用失败 → 抛 RuntimeError 保守中止（d1 未被误删）")
    finally:
        shutil.rmtree(cache, ignore_errors=True)

    # ---- G3 R-15 replace 有界重试（确定性：monkeypatch 计数）----
    tmp = _tf.mkdtemp(prefix="mv_g3_")
    try:
        target = os.path.join(tmp, ".tasks.json")
        with open(target, "wb") as fh:
            fh.write(b"old")
        real_replace = taskstore.os.replace
        state = {"n": 0}

        def flaky(src, dst):
            state["n"] += 1
            if state["n"] <= 2:      # 前两次模拟 WinError 32
                raise PermissionError(32, "另一个程序正在使用此文件")
            return real_replace(src, dst)

        taskstore.os.replace = flaky
        try:
            taskstore.atomic_write_bytes(target, b"new")
            ok_write = True
        except Exception as e:
            ok_write = False
            ck.check(False, f"重试后仍失败：{e!r}")
        finally:
            taskstore.os.replace = real_replace
        with open(target, "rb") as fh:
            ck.check(ok_write and fh.read() == b"new",
                     f"PermissionError 在 {state['n']} 次 os.replace 内被重试吸收")
        # 仍失败路径：三次都权限冲突 → 照原样上抛，旧文件完好
        taskstore.os.replace = flaky
        state["n"] = -10           # 让它永远抛 PermissionError
        raised = None
        try:
            taskstore.atomic_write_bytes(target, b"again")
        except PermissionError as e:
            raised = e
        finally:
            taskstore.os.replace = real_replace
        with open(target, "rb") as fh:
            ck.check(raised is not None and fh.read() == b"new",
                     "重试耗尽后照原样上抛，且不破坏旧文件")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- G4 R-6B 流服务停止事件：shutdown 必须让在途 _wait_ready 立即返回 ----
    cache = _tf.mkdtemp(prefix="mv_g4_")
    try:
        from core.models import PieceMap
        from core.stream_server import StreamServer
        blob = os.path.join(cache, "v.mp4")
        with open(blob, "wb") as fh:
            fh.write(b"\0" * (1 << 20))
        never = lambda _p: PieceMap(piece_length=1 << 16, offset=0,
                                    start_piece=0, end_piece=15,
                                    size=1 << 20, have=lambda _p: False)
        srv = StreamServer(cache, pieces_cb=never, wait_timeout=20.0)
        srv.start()
        done = threading.Event()

        def fetch():
            try:
                _tf_urlopen(srv.url_for("v.mp4"), timeout=30)
            except Exception:
                pass
            finally:
                done.set()

        th = threading.Thread(target=fetch, daemon=True)
        th.start()
        time.sleep(0.3)               # 让请求进入 _wait_ready 轮询
        t0 = time.perf_counter()
        srv.shutdown()
        ck.check(done.wait(6.0),
                 "shutdown() 置停止事件后，卡在 `_wait_ready` 的在途请求立即返回"
                 f"（{time.perf_counter() - t0:.2f}s；旧实现要等 20s 上限）")
        ck.check(not th.is_alive(), "在途请求线程已结束")
    finally:
        shutil.rmtree(cache, ignore_errors=True)


def _tf_urlopen(url: str, timeout: float) -> None:
    import urllib.request
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        resp.read(64)


# --------------------------------------------------------------------------

def main() -> int:
    ck = ts.Checker("concurrency_test（并发安全专项：R-1/R-2/R-3/R-5/R-6b 护栏）")
    print("\n--- 并发安全护栏（2026-09-17，plan/11 第 1+2+3 批）---")
    section_atomic_write(ck)
    section_persist_snapshot(ck)
    section_dead_handle(ck)
    section_shutdown_off_lock(ck)
    section_lock_redline(ck)
    section_scheduler_lock(ck)
    section_batch3(ck)
    return ck.report()


if __name__ == "__main__":
    sys.exit(main())
