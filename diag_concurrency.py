"""并发护栏的**灵敏度验证**探针（diag_concurrency）。

用途
----
护栏最大的风险是「恒真断言」——测试一直在跑，但缺陷复发时它不会红。
本探针把每条护栏判据**对着旧实现（缺陷形态）与现行实现各跑一遍**，
只在「旧实现必被拦下、现行实现必通过」时才给绿灯：

  C1  R-1 灵敏度：固定临时名 + 无锁的原子写，能否被 §A 的判据抓到
  C2  R-2 灵敏度：把活字典直接交给 save_sasks 的落盘，能否被 §B 的判据抓到

为什么必须做这件事（本探针的由来）
----------------------------------
第 1 批的护栏初版用了**依赖时序**的判据（指望竞态撞出
`dictionary changed size` 再被 `except` 吞掉）。实测：旧实现 5 轮 × 100 次调用
**零命中**——因为一次落盘 3.5ms 里绝大部分是 `fsync`，真正在迭代字典的只有
几十微秒，插入几乎不可能落在那几十微秒内。**时序判据没有牙齿**，
后改为确定性判据（C2 用「传进来的对象是不是活字典本身」的身份检查）。
本探针就是防止这类「看起来在守、其实守不住」的护栏再次混进来。

判据
----
  C1-a 旧实现必须被判不合格（写者异常 > 0 或产物判非 ok）
  C1-b 现行实现必须通过
  C2-a 旧实现必须被判不合格（有「传活字典」的落盘）
  C2-b 现行实现必须通过
退出码：0 = 两条护栏都灵敏且现行实现正确；1 = 出现「旧实现零命中」（护栏无牙）
        或「现行实现被判不合格」（实现有问题）。

用法
----
    .\\.venv\\Scripts\\python.exe diag_concurrency.py        # C1 + C2
    .\\.venv\\Scripts\\python.exe diag_concurrency.py C1     # 只跑一段
零外网，约 5 秒。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import persist as persist_mod      # noqa: E402
from core import taskstore                  # noqa: E402
from core.taskstore import atomic_write_bytes  # noqa: E402

try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass

PAYLOAD = 64 * 1024
THREADS = 5
ROUNDS = 30
CALLS = 100
MUT_MAX = 5000
MUT_SLEEP = 0.001
REPEATS = 5

RESULTS: list = []


def record(tag: str, ok: bool, detail: str) -> None:
    RESULTS.append((tag, ok, detail))
    print(f"  [{'OK  ' if ok else 'FAIL'}] {tag} {detail}")


# ==========================================================================
# C1 —— R-1 灵敏度：原子写的「固定临时名 + 无锁」形态
# ==========================================================================

def _old_atomic_write_bytes(path: str, data: bytes) -> str:
    """缺陷形态：临时名固定为 `<path>.tmp`，全程无锁。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def _payload(writer: int, seq: int) -> bytes:
    head = f"{writer}:{seq}:".encode("ascii")
    return head + bytes([48 + writer]) * (PAYLOAD - len(head))


def _verify(blob: bytes) -> str:
    """产物必须等于**某一次完整写入**；混杂/截断都算不合格。"""
    try:
        i = blob.index(b":", blob.index(b":") + 1)
        writer, _seq = (int(x) for x in blob[:i].split(b":"))
    except ValueError:
        return "头部不可解析"
    body = blob[i + 1:]
    if len(body) != PAYLOAD - i - 1:
        return f"长度异常（{len(body)}）"
    return "ok" if body == bytes([48 + writer]) * len(body) else "内容混杂"


def _one_write_round(impl) -> tuple:
    """返回 (产物判定, 写者异常数, 临时残留数)——与 §A 的三条判据一一对应。"""
    tmp = tempfile.mkdtemp(prefix="mv_diagc_")
    target = os.path.join(tmp, ".tasks.json")
    barrier = threading.Barrier(THREADS)
    errors: list = []

    def writer(idx: int) -> None:
        try:
            try:
                barrier.wait(timeout=30)
            except threading.BrokenBarrierError:
                pass
            for seq in range(ROUNDS):
                impl(target, _payload(idx, seq))
        except Exception as e:
            errors.append(type(e).__name__)

    ths = [threading.Thread(target=writer, args=(i,)) for i in range(THREADS)]
    for t in ths:
        t.start()
    for t in ths:
        t.join(60)
    with open(target, "rb") as f:
        verdict = _verify(f.read())
    left = [f for f in os.listdir(tmp) if f.endswith(".tmp")]
    shutil.rmtree(tmp, ignore_errors=True)
    return verdict, len(errors), len(left)


def c1_atomic_write() -> None:
    print("\n--- C1 R-1 灵敏度：§A 能否抓到「固定临时名 + 无锁」---")
    print("    （§A 判据：写者无异常 + 产物是某一次完整写入 + 无临时残留）")
    old_hit = 0
    for i in range(REPEATS):
        verdict, nerr, left = _one_write_round(_old_atomic_write_bytes)
        bad = verdict != "ok" or nerr or left
        old_hit += 1 if bad else 0
        print(f"      旧实现 #{i + 1}  产物={verdict}  写者异常={nerr}  "
              f"残留={left}  → {'不合格' if bad else '通过'}")
    new_hit = 0
    for i in range(REPEATS):
        verdict, nerr, left = _one_write_round(atomic_write_bytes)
        bad = verdict != "ok" or nerr or left
        new_hit += 1 if bad else 0
        print(f"      现行实现 #{i + 1}  产物={verdict}  写者异常={nerr}  "
              f"残留={left}  → {'不合格' if bad else '通过'}")
    record("C1-a 旧实现被 §A 拦下", old_hit > 0, f"{old_hit}/{REPEATS} 轮命中")
    record("C1-b 现行实现通过 §A", new_hit == 0, f"{new_hit}/{REPEATS} 轮不合格")


# ==========================================================================
# C2 —— R-2 灵敏度：落盘「直接传活字典」形态
# ==========================================================================

LIVE_REF: list = [None]
_SAW: list = []          # 每次落盘记录 (条数, 是否就是活字典本身)


def _old_persist(d, cache_dir) -> None:
    """缺陷形态：活字典不做快照、不持锁。"""
    try:
        persist_mod.save_tasks(cache_dir, d)
    except Exception as e:
        _SAW.append((-1, str(e)[:40]))


def _new_persist(d, cache_dir, lock) -> None:
    """现行形态：锁内快照 → 出锁序列化。"""
    try:
        with lock:
            snap = {k: dict(v) for k, v in d.items()}
        persist_mod.save_tasks(cache_dir, snap)
    except Exception as e:
        _SAW.append((-1, str(e)[:40]))


def _spy(cache_dir, tasks):
    _SAW.append((len(tasks), tasks is LIVE_REF[0]))
    return _REAL_SAVE(cache_dir, tasks)


_REAL_SAVE = None


def _one_persist_round(impl) -> tuple:
    """返回 (落盘次数, 传了活字典的次数)——与 §B 的身份判据对应。"""
    tmp = tempfile.mkdtemp(prefix="mv_diagp_")
    _SAW.clear()
    live: dict = {}
    LIVE_REF[0] = live
    lock = threading.Lock()
    stop = threading.Event()

    def mutator() -> None:
        i = 0
        while not stop.is_set():
            with lock:
                if i < MUT_MAX:
                    live[f"k{i:05d}"] = {"state": "downloading", "error": ""}
            i += 1
            time.sleep(MUT_SLEEP)

    th = threading.Thread(target=mutator)
    th.start()
    if impl == "old":
        for _ in range(CALLS):
            _old_persist(live, tmp)
    else:
        for _ in range(CALLS):
            _new_persist(live, tmp, lock)
    stop.set()
    th.join(5)
    leaked = sum(1 for n, flag in _SAW if n >= 0 and flag)
    shutil.rmtree(tmp, ignore_errors=True)
    return len(_SAW), leaked


def c2_persist_snapshot() -> None:
    global _REAL_SAVE
    print("\n--- C2 R-2 灵敏度：§B 能否抓到「直接传活字典」---")
    print("    （§B 判据：save_tasks 收到的不是活字典本身 + 每次落盘都取过锁）")
    _REAL_SAVE = persist_mod.save_tasks
    persist_mod.save_tasks = _spy
    try:
        old_hit = 0
        for i in range(REPEATS):
            total, leaked = _one_persist_round("old")
            bad = leaked > 0
            old_hit += 1 if bad else 0
            print(f"      旧实现 #{i + 1}  落盘={total}  传活字典={leaked}  "
                  f"→ {'不合格' if bad else '通过'}")
        new_bad = 0
        for i in range(REPEATS):
            total, leaked = _one_persist_round("new")
            bad = leaked > 0 or total != CALLS
            new_bad += 1 if bad else 0
            print(f"      现行实现 #{i + 1}  落盘={total}  传活字典={leaked}  "
                  f"→ {'不合格' if bad else '通过'}")
    finally:
        persist_mod.save_tasks = _REAL_SAVE
    record("C2-a 旧实现被 §B 拦下", old_hit > 0, f"{old_hit}/{REPEATS} 轮命中")
    record("C2-b 现行实现通过 §B", new_bad == 0, f"{new_bad}/{REPEATS} 轮不合格")


# ==========================================================================

SECTIONS = {"C1": c1_atomic_write, "C2": c2_persist_snapshot}


def main(argv: list) -> int:
    wanted = [a for a in argv[1:] if a in SECTIONS] or list(SECTIONS)
    print("=" * 74)
    print(f"并发护栏灵敏度探针：段={','.join(wanted)}；"
          f"每段 {REPEATS} 轮 × {THREADS if wanted[0] == 'C1' else 1} 写者")
    print("=" * 74)
    t0 = time.perf_counter()
    for tag in wanted:
        SECTIONS[tag]()
    bad = [t for t, ok, _ in RESULTS if not ok]
    print("\n" + "=" * 74)
    print(f"汇总：{len(RESULTS)} 项，{len(bad)} 项不合格，用时 {time.perf_counter() - t0:.1f}s")
    for tag in bad:
        print(f"  - {tag}  {next(d for t, _o, d in RESULTS if t == tag)}")
    print("退出码 " + ("1（护栏无牙或实现有问题）" if bad else "0（两条护栏都灵敏）"))
    print("=" * 74)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
