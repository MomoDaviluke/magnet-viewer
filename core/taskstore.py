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

import json
import os
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

def atomic_write_bytes(path: str, data: bytes) -> str:
    """把字节内容原子写入 path：临时文件 + fsync + os.replace。

    中途失败（写盘/rename 抛错）不破坏已存在的目标文件；
    成功后磁盘上不存在 `.tmp` 残留。
    """
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
    except Exception as e:
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