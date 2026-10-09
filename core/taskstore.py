"""下载任务清单 JSON 持久化（纯函数模块，无 Qt / libtorrent 会话依赖）。

载体：``<cache_dir>/.tasks.json``（决策点 D6：否决 QSettings——避免注册表膨胀
与测试污染；存 cache_dir 保证「换机器/重装不残留、测试可隔离」）。
写时机由调用方决定（任务增删、状态切换、closeEvent）；写入采用
「临时文件 + ``os.replace`` 原子替换」——崩溃/断电不留下半截 JSON，
旧文件保持完整可读。

损坏容错（B3 语义，绝不阻断启动）：
- 文件缺失 / 非 JSON / 截断 / 顶层结构错误 / 版本超前 → 静默返回空任务表；
- 单条记录非法（info_hash 缺失或格式不符）→ 只丢弃该条，保留其余合法记录，
  并可通过 ``warn`` 回调上报（UI 可展示，不阻断）。

任务记录字段（对齐 plan/t1 §2 DownloadTask，速度/进度/ETA 不持久化——
重启后由 handle.status() 派生）：
``info_hash``（=id，40/64 hex，唯一键）/ ``source`` / ``name`` / ``total_size`` /
``files``（TorrentFile 序列化，含 .pad，供 file_progress 索引对齐）/
``selected``（勾选文件路径集，MVP 默认全选）/ ``state`` / ``priority`` /
``save_path`` / ``error`` / ``retries`` / ``created_at`` / ``finished_at``。
状态取值与 core.states 的 STATE_* 常量保持一致（resolving/ready/downloading/
paused/completed/stopped/failed/seeding/deleted），此处不导入 fetcher/states
以避免把 libtorrent 拉进纯函数模块（常量本体已在 core.states，改名须同步）。
"""
from __future__ import annotations

import itertools
import json
import os
import threading
import time
from typing import Callable, Mapping

from .models import TorrentFile

TASK_FILE_NAME = ".tasks.json"
SCHEMA_VERSION = 1


def task_file_path(cache_dir: str) -> str:
    """任务清单文件完整路径。"""
    return os.path.join(cache_dir, TASK_FILE_NAME)


def normalize_info_hash(ih, raise_invalid: bool = False) -> str | None:
    """info_hash 归一化：小写 40（v1/SHA-1）或 64（v2/SHA-256）位 hex。

    非法值返回 None（或 raise_invalid=True 时抛 ValueError）。
    统一格式是持久化键与 fastresume 文件名安全的前提（防路径注入）。
    """
    if ih is None:
        s = ""
    else:
        s = str(ih).strip().lower()
    if len(s) in (40, 64) and all(c in "0123456789abcdef" for c in s):
        return s
    if raise_invalid:
        raise ValueError(f"非法 info_hash：{ih!r}（须为 40/64 位 hex）")
    return None


# ---------------- 记录构造 ----------------

def encode_file(f: TorrentFile) -> dict:
    """TorrentFile → JSON 字典（序列化）。"""
    return {"index": f.index, "path": f.path, "size": f.size,
            "offset": f.offset, "start_piece": f.start_piece,
            "end_piece": f.end_piece}


def decode_file(raw) -> TorrentFile | None:
    """JSON 字典 → TorrentFile；字段缺失/类型错误返回 None（单条容错）。"""
    try:
        return TorrentFile(int(raw["index"]), str(raw["path"]),
                           int(raw["size"]), int(raw["offset"]),
                           int(raw["start_piece"]), int(raw["end_piece"]))
    except (TypeError, ValueError, KeyError):
        return None


def task_from_result(result, *, state: str = "ready", save_path: str = "",
                     priority: int = 0, retries: int = 0, source: str | None = None,
                     error: str = "", created_at: float | None = None,
                     finished_at: float | None = None,
                     selected: list | None = None, seed: bool = False) -> dict:
    """由一次解析结果构造任务记录（派生字段不落盘，见模块 docstring）。

    ``files`` 持久化全量文件（含 .pad，索引必须与 libtorrent 一致），
    ``selected`` 缺省 = 全部可见文件（MVP 种子级任务，文件级勾选为增强）。
    ``seed`` 记录完成后是否做种（决策 D3 默认不做种）。
    """
    return {
        "info_hash": normalize_info_hash(result.info_hash, raise_invalid=True),
        "source": source or getattr(result, "source", "unknown"),
        "name": result.name,
        "total_size": int(result.total_size),
        "files": list(result.files),
        "selected": ([str(s) for s in selected]
                     if selected is not None
                     else [f.path for f in result.view_files]),
        "state": str(state),
        "priority": int(priority),
        "save_path": str(save_path),
        "error": str(error),
        "retries": int(retries),
        "created_at": (float(created_at) if created_at is not None
                       else time.time()),
        "finished_at": (float(finished_at) if finished_at is not None
                        else None),
        "seed": bool(seed),
    }


def normalize_task(raw) -> dict | None:
    """校验/归一化一条磁盘记录；非法（含恶意的穿越/超长路径）返回 None。

    已知键做类型钳制，未知键丢弃（结构由本模块掌控）；``files``/``selected``
    逐条容错。绝不信任磁盘上的原始类型（磁盘可被手工编辑/损坏）。
    """
    if not isinstance(raw, dict):
        return None
    ih = normalize_info_hash(raw.get("info_hash") or raw.get("id"))
    if ih is None:
        return None

    def _int(k: str, dflt: int = 0) -> int:
        v = raw.get(k, dflt)
        try:
            return int(v)
        except (TypeError, ValueError):
            return dflt

    def _float(k: str, dflt=None):
        v = raw.get(k, dflt)
        if v is None or isinstance(v, bool):
            return dflt
        try:
            return float(v)
        except (TypeError, ValueError):
            return dflt

    files = raw.get("files")
    entries = [decode_file(x) for x in files] if isinstance(files, list) else []
    sel = raw.get("selected")
    seed_raw = raw.get("seed")
    return {
        "info_hash": ih,
        "source": str(raw.get("source") or "unknown"),
        "name": str(raw.get("name") or ih),
        "total_size": _int("total_size"),
        "files": [f for f in entries if f is not None],
        "selected": [str(s) for s in sel] if isinstance(sel, list) else [],
        "state": str(raw.get("state") or "unknown"),
        "priority": _int("priority"),
        "save_path": str(raw.get("save_path") or ""),
        "error": str(raw.get("error") or ""),
        "retries": _int("retries"),
        "created_at": _float("created_at"),
        "finished_at": _float("finished_at"),
        "seed": seed_raw in (True, 1, "1", "true", "True", "yes"),
    }


# ---------------- 读写（原子写） ----------------

_TMP_SEQ = itertools.count()      # 进程内单调序号：临时名唯一性的第三重保险
_WRITE_SHARDS = 64                # 分片数：同路径必落同一片（互斥），内存有界
_WRITE_LOCKS = [threading.Lock() for _ in range(_WRITE_SHARDS)]


def _tmp_path(path: str) -> str:
    """目标路径 → **唯一**临时路径（同目录，保证 os.replace 是同卷原子替换）。

    唯一性三重来源：pid（跨进程）+ 线程 id（跨线程）+ 进程内单调序号
    （防同线程重入或 tid 快速复用）。三者叠加后并发写者不可能撞名。
    """
    return f"{path}.{os.getpid()}.{threading.get_ident()}.{next(_TMP_SEQ)}.tmp"


def _write_lock_for(path: str):
    """目标路径 → 写锁（按路径哈希分片，同路径恒得同一把）。

    为什么用分片数组而不是「按路径建锁字典」（安卓 `TaskStore.kt` 的做法）：
    安卓侧目标数受任务数约束，桌面侧还有 ``.meta/<ih>.torrent`` 这种随
    「看过多少种子」单调增长的路径，字典会无界增长；固定分片换取**内存有界**。
    代价是不同路径偶发共用一把锁（轻微串行化），对「写盘频率极低」的本场景
    无影响。
    """
    key = os.path.normcase(os.path.abspath(path))
    return _WRITE_LOCKS[hash(key) % _WRITE_SHARDS]


def _replace_with_retry(tmp: str, path: str, attempts: int = 3) -> None:
    """``os.replace`` 的**有界重试**（plan/11 R-15）。

    Windows 上**目标**文件被读句柄打开（``open(path, "rb")`` 不共享删除）时，
    ``MoveFileEx`` 抛 ``PermissionError [WinError 32/5]``——读方通常是 UI 轮询
    读 ``.tasks.json`` 或 ``load_torrent_info`` 读 ``.meta``，窗口很短。
    只重试权限冲突这一类（其它异常照原样立刻上抛），退避 2/10/30ms；
    重试发生在 ``_write_lock_for`` 锁内，但该锁是**本模块私有**的（不是
    registry 锁），不会与 §E 的「落盘不得持 registry 锁」红线冲突。
    仍失败则照原样上抛——绝不吞。
    """
    delay = 0.002
    for attempt in range(max(1, attempts)):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 5.0


def atomic_write_bytes(path: str, data: bytes) -> str:
    """把字节内容原子写入 path：唯一临时文件 + fsync + os.replace。

    **并发安全（plan/11 R-1）。** 历史缺陷：临时名固定为 ``f"{path}.tmp"``
    且全程无锁——两个线程在同一 ``open→fsync→replace`` 窗口写同一路径时会
    **共用同一个临时文件**。

    失败形态（**实测，非推断**）：
      - **Windows 主导形态**：两个写者同时持有同一临时文件时，先完成者的
        ``os.replace`` 抛 ``PermissionError [WinError 32]``——源文件仍被另一个
        线程打开，``MoveFileEx`` 搬不动它。负向对照实测（5 写者 × 30 轮 × 5 次）：
        **147–149 次异常 / 150 次写入**，即并发写几乎**每一次**都以异常收场，
        而不是「偶尔产出坏文件」。安卓侧早已实测到同一现象并修掉，
        见 ``data/TaskStore.kt:188-189`` 对 ``rename 失败：….tasks.json.tmp``
        的记载——此类缺陷在本项目不是假想。
      - POSIX 语义下则是两个 fd 各自从偏移 0 覆写 ⇒ **内容交错**，全程无异常。

    后果：异常从本函数抛出后，调用方 ``persist_tasks`` 的 ``except`` 把它吞成
    一行告警 ⇒ 并发落盘**静默失败**。最重的是 ``.tasks.json``：读端 ``load_tasks``
    对损坏/缺失的策略是「返回空表」（绝不阻断启动）⇒ 用户下次启动**整个下载
    任务列表为空**。``.meta`` / ``.fastresume`` / ``.dht_state`` 同受影响
    （``.meta`` 有内容校验自愈，表现为缓存未命中 + 重取元数据）。

    三道措施（与安卓 ``data/TaskStore.kt:192-218`` 同款，仅互斥实现不同，
    见 ``_write_lock_for``）：

    1. 临时名唯一（``_tmp_path``）——杜绝多写者共用临时文件；
    2. 同目标路径互斥（``_write_lock_for``）；
    3. 任何失败路径都清掉临时文件，不留 ``.tmp`` 残骸。

    失败语义不变：中途失败不破坏已存在的目标文件，异常**照原样上抛**
    （``save_tasks`` 的调用方依赖它判断「这次没写成」）。
    成功后磁盘上不存在临时文件残留。
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = _tmp_path(path)
    with _write_lock_for(path):
        try:
            with open(tmp, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            _replace_with_retry(tmp, path)      # plan/11 R-15：读-写撞 WinError 32 的有界重试
        except BaseException:
            # 清理必须覆盖 BaseException：残骸留在缓存目录会被后续 scandir
            # 当成条目统计（.meta 剪枝按条目数算），也会让「无 .tmp 残留」
            # 的护栏失效
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
    return path


def _dump_task(t: dict) -> dict:
    """内存态任务 → 可 JSON 序列化字典（files 的 TorrentFile 重新编码）。

    内存态（upsert/load 后的任务）里 ``files`` 是 TorrentFile 对象，
    落盘时转回普通字典；兼容已是 dict 的条目（防御性直传）。
    """
    out = dict(t)
    files = t.get("files") or []
    out["files"] = [encode_file(f) if isinstance(f, TorrentFile) else f
                    for f in files]
    return out


def save_tasks(cache_dir: str, tasks: Mapping[str, dict]) -> str:
    """原子写任务清单；返回写出的文件路径。失败向上抛 OSError（调用方展示）。"""
    payload = {"version": SCHEMA_VERSION,
               "tasks": {k: _dump_task(v) for k, v in tasks.items()}}
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    return atomic_write_bytes(task_file_path(cache_dir), text.encode("utf-8"))


def load_tasks(cache_dir: str,
               warn: Callable[[str], None] | None = None) -> dict[str, dict]:
    """读取任务清单 → ``dict[info_hash, task]``。

    任何损坏形态都返回空表（绝不抛异常、绝不阻断启动）；
    部分损坏只丢弃非法记录，``warn`` 回调逐条上报。
    """
    path = task_file_path(cache_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        # OSError：读文件失败；ValueError：json.JSONDecodeError 与
        # UnicodeDecodeError 都是其子类。契约：任何损坏形态返回空表不抛
        if warn is not None:
            warn(f"任务清单损坏，已按空表处理：{e}")
        return {}
    if not isinstance(raw, dict) or raw.get("version") != SCHEMA_VERSION:
        return {}                       # 结构错误或版本不兼容（拒读，静默重建）
    tasks_raw = raw.get("tasks")
    if not isinstance(tasks_raw, dict):
        return {}
    out: dict[str, dict] = {}
    for key, entry in tasks_raw.items():
        t = normalize_task(entry)
        if t is None or t["info_hash"] != str(key).strip().lower():
            if warn is not None:
                warn(f"任务记录非法，已丢弃：{key!r}")
            continue
        out[t["info_hash"]] = t
    return out


# ---------------- 变更操作（纯函数，返回新 dict） ----------------

def upsert_task(tasks: Mapping[str, dict], task: dict
                ) -> tuple[dict[str, dict], bool]:
    """按 info_hash 去重写入（后写覆盖）；返回 (新表, 是否已存在)。

    非法记录直接抛 ValueError——调用方传入的是内存数据，不同于磁盘读入
    （后者走 load_tasks 的容错路径）。
    """
    t = normalize_task(task)
    if t is None:
        raise ValueError("任务记录非法：info_hash 缺失或格式不符")
    out = dict(tasks)
    replaced = t["info_hash"] in out
    out[t["info_hash"]] = t
    return out, replaced


def remove_task(tasks: Mapping[str, dict], info_hash: str
                ) -> tuple[dict[str, dict], bool]:
    """按 info_hash 删除任务；ih 非法或不存在时原样返回 False。"""
    ih = normalize_info_hash(info_hash)
    if ih is None:
        return dict(tasks), False
    out = dict(tasks)
    removed = out.pop(ih, None) is not None
    return out, removed