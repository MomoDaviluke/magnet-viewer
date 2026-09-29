"""磁力链元数据缓存：``<cache_dir>/.meta/<ih>.torrent``。

背景（性能专项 P-1：本次优化的最大瓶颈）
---------------------------------------
磁力链只含 info_hash，文件清单（metadata）必须向 DHT / peer 索取：一次完整的
DHT 查找 + ut_metadata 握手在现网通常是 **数秒到数十秒**，冷门资源直接撞
``metadata_timeout``（默认 90s）超时。而改造前本程序对元数据**零缓存**：

- ``ResolverCore._resolve_magnet`` 每次只带 info_hash 调 ``add_torrent``，
  重复解析同一条磁力链 = 完整重跑一遍网络往返；
- 下载任务的 fastresume 只传 ``flush_disk_cache``、缺 ``save_info_dict``，
  落盘的 resume data **不含 info 段** —— 连重启恢复都要退回 META_FETCH，
  重新向网络索取一次；
- 本地 ``.torrent`` 的解析结果也不落盘，同一个 info_hash 的磁力链无法复用。

本模块把「已拿到的 metadata」按 info_hash 落盘成标准 .torrent（info 段 +
tracker 列表），解析 / 恢复 / 添加三条链路先查缓存，
命中即经 ``add_torrent_params.ti`` 注入 —— 网络往返直接从关键路径上摘掉。

设计约束
--------
- **只缓存 info 段与 tracker 列表**（几 KB ~ 几 MB），不缓存任何用户数据；
  文件本体、fastresume、任务清单仍分属 .preview / .resume / .tasks.json；
- 文件名走 ``taskstore.normalize_info_hash`` 强校验（40/64 位 hex），
  非法键一律按「未命中」处理，绝不拼路径（防穿越）；
- 写盘复用 ``taskstore.atomic_write_bytes``（临时文件 + fsync + os.replace），
  崩溃不留半截文件；读到的损坏文件直接删除并计一次未命中（自愈）；
- **失败绝不向上抛**：缓存是旁路加速设施，任何异常只记 STATS，
  主流程该走网络还走网络（与 ``core.persist`` / ``core.cache_quota`` 同纪律）；
- 编解码复用 ``core.parser`` 自研 bencode —— 与 ``core.resume`` 同理：
  ``lt.bencode`` 会把 ``bytes`` 值当**裸字符串**编码，``{"info": <bytes>}``
  会被写成 ``4:info588:d9:file tree...``，libtorrent 反解报
  ``missing or invalid 'info' section``（本环境 2.1.1 实测）。自研 bencode
  按嵌套结构编码，``lt.torrent_info(blob)`` 往返 info_hash 完全一致。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from .logutil import log_warning
from .parser import bencode, bdecode
from .taskstore import atomic_write_bytes, normalize_info_hash

META_SUBDIR = ".meta"
META_SUFFIX = ".torrent"
META_MAX_FILES = 1000               # 缓存条目上限（LRU 淘汰，防无限膨胀）
META_MAX_BYTES = 32 * 1024 * 1024   # 单条元数据上限（恶意超长文件清单兜底）
META_CREATED_BY = "magnet-viewer"
META_COMMENT = "magnet-viewer metadata cache"

# 写路径剪枝的节流状态（见 maybe_prune）：{normcase(dir): (条目数, 下次重扫时刻)}
_PRUNE_LOCK = threading.Lock()
_PRUNE_STATE: dict[str, tuple[int, float]] = {}
_PRUNE_RESYNC = 60.0                # 秒：最长多久强制重扫一次目录计数

# 命中率统计（进程内，线程安全）：供 UI/测试读「缓存命中率」改善指标
_STATS_LOCK = threading.Lock()
STATS: dict[str, int] = {
    "hits": 0,
    "misses": 0,
    "writes": 0,
    "corrupt": 0,
    "mismatch": 0,      # 内容与文件名声称的 info_hash 不符（见 _matches_key）
    "pruned": 0,
}


# ---------------------------------------------------------------- 路径层

def meta_dir(cache_dir: str) -> str:
    """元数据缓存目录：``<cache_dir>/.meta``。"""
    return os.path.join(cache_dir, META_SUBDIR)


def meta_path(cache_dir: str, info_hash: str) -> str:
    """单条元数据文件路径：``<cache_dir>/.meta/<ih>.torrent``。

    info_hash 非法（非 40/64 hex）抛 ValueError —— 文件名由不可信来源拼入，
    必须防穿越（与 ``core.resume.resume_path`` 同纪律）。
    """
    ih = normalize_info_hash(info_hash, raise_invalid=True)
    return os.path.join(meta_dir(cache_dir), f"{ih}{META_SUFFIX}")


def safe_meta_path(cache_dir: str, info_hash: str) -> str | None:
    """非法 info_hash 返回 None（调用方按「未命中」处理），不抛异常。"""
    ih = normalize_info_hash(info_hash)
    if ih is None:
        return None
    return os.path.join(meta_dir(cache_dir), f"{ih}{META_SUFFIX}")


def has_meta(cache_dir: str, info_hash: str) -> bool:
    """缓存中是否已有该 info_hash 的元数据（非法键返回 False）。"""
    p = safe_meta_path(cache_dir, info_hash)
    return bool(p) and os.path.isfile(p)


def invalidate_meta(cache_dir: str, info_hash: str) -> bool:
    """**显式失效**单条元数据（返回是否真的删掉了文件）。

    失效策略的第三只手（另两只：损坏/错配时的命中自愈删除、超量时的 LRU
    剪枝）。适用场景：用户清缓存、任务重建、修改 tracker 后强制重取
    metadata。刻意不做成「按 ih 反查内容再删」——失效必须便宜且幂等，
    调用方已经知道要删哪一条。
    """
    p = safe_meta_path(cache_dir, info_hash)
    if not p:
        return False
    try:
        os.remove(p)
    except OSError:
        return False
    with _PRUNE_LOCK:
        # 条目数减一；未知（-1）保持未知，下次写入自动重扫
        key = os.path.normcase(meta_dir(cache_dir))
        cnt, next_scan = _PRUNE_STATE.get(key, (-1, 0.0))
        if cnt > 0:
            _PRUNE_STATE[key] = (cnt - 1, next_scan)
    return True


def read_meta(cache_dir: str, info_hash: str) -> bytes | None:
    """读元数据字节；不存在 / 读失败返回 None（非法键同样 None）。"""
    p = safe_meta_path(cache_dir, info_hash)
    if not p:
        return None
    try:
        with open(p, "rb") as f:
            return f.read()
    except OSError:
        return None


def write_meta(cache_dir: str, info_hash: str, blob: bytes) -> str | None:
    """原子写元数据；返回写出的路径，非法键 / 超上限 / 写失败返回 None。"""
    p = safe_meta_path(cache_dir, info_hash)
    if not p or not blob:
        return None
    if len(blob) > META_MAX_BYTES:
        return None
    try:
        return atomic_write_bytes(p, blob)
    except OSError:
        return None


# ---------------------------------------------------------------- 编解码层

def encode_meta(ti, comment: str = META_COMMENT,
                created_by: str = META_CREATED_BY) -> bytes | None:
    """``lt.torrent_info`` → 标准 .torrent 字节（info 段 + tracker 列表）。

    带上 tracker 列表是**有意的**：重启恢复时 ``atp.ti`` 直接携带 announce，
    省掉一次 tracker 发现过程（元数据缓存的收益不止「省 metadata」）。
    info 段原样搬运（v1/v2/混合通吃），绝不重新计算 piece hash。
    """
    if ti is None:
        return None
    try:
        sec = ti.info_section()
    except Exception:
        return None
    try:
        info = bdecode(bytes(sec)) if isinstance(sec, (bytes, bytearray)) else sec
        if not isinstance(info, dict):
            return None
        meta: dict[bytes, Any] = {
            b"info": info,
            b"created by": created_by,
            b"creation date": int(time.time()),
        }
        if comment:
            meta[b"comment"] = comment
        urls: list[bytes] = []
        try:
            for e in ti.trackers():
                u = getattr(e, "url", None)
                if isinstance(u, str) and u:
                    urls.append(u.encode("utf-8"))
                elif isinstance(u, (bytes, bytearray)) and u:
                    urls.append(bytes(u))
        except Exception:
            urls = []
        if urls:
            meta[b"announce"] = urls[0]
            meta[b"announce-list"] = [[u] for u in urls[:32]]
        return bencode(meta)
    except Exception:
        return None


def decode_meta(blob: bytes | None):
    """元数据字节 → ``lt.torrent_info``；空 / 损坏返回 None（不抛）。"""
    if not blob:
        return None
    import libtorrent as lt            # 局部导入：纯路径层无需 libtorrent
    try:
        return lt.torrent_info(bytes(blob))
    except Exception:
        return None


def info_hash_of(ti) -> tuple[str, str]:
    """``torrent_info`` 自带 ``(v1, v2)`` 十六进制 info-hash（缺失 = 空串）。

    全零哈希视为「不存在」：v1-only 种子的 ``info_hashes().v2`` 是全零 sha256，
    直接 ``str()`` 会得到 64 个 ``0``，不剔除会被误判成「这是 v2 种子」。
    判别口径与 ``registry.ih_from_params`` 的全零守卫一致。
    """
    v1 = v2 = ""
    try:
        h = ti.info_hashes()
    except Exception:
        return v1, v2
    for slot in ("v1", "v2"):
        try:
            s = str(getattr(h, slot)).strip().lower()
        except Exception:
            continue
        if len(s) not in (40, 64) or set(s) == {"0"}:
            continue
        if slot == "v1":
            v1 = s
        else:
            v2 = s
    return v1, v2


def alias_keys(ti) -> list[str]:
    """该元数据**全部可查键**（去重、保序，首个 = ``registry.hash_key`` 口径）。

    为什么需要别名（2026-09-17 实测发现，直接影响命中率）：
    本环境 libtorrent 2.1.1 对 **v1+v2 混合种子** 的 ``torrent_info.info_hash()``
    返回的**不是 v1**，而是 v2 哈希截断到 20 字节——实测同一份种子
    ``info_hash() = 5caea46a…9b82``（= ``info_hashes().v2`` 的前 40 hex），
    而真正的 ``info_hashes().v1 = 8cc92095…93fc``，两者完全不同。
    ``registry.hash_key(handle)`` 取的是前者，所以 `.meta` 文件按前者命名；
    而用户手上的磁力链 btih 很可能是真正的 **v1**（多数站点/客户端发的就是 v1）
    → ``load_torrent_info`` 按 v1 查文件名必然**未命中**，混合种子白白丢掉一次
    缓存命中，退回完整 DHT + ut_metadata 往返（数秒到数十秒）。

    修法：写入时把同一份元数据**同时**落在 v1 / v2 两个别名键下（元数据只有
    KB 级，多写 1~2 个文件代价可忽略），任意一种 btih 形态的磁力链都能命中。
    校验时同样按这个集合判定，不再假设「40 位一定是 v1」。
    """
    out: list[str] = []
    try:
        best = str(ti.info_hash()).strip().lower()
    except Exception:
        best = ""
    if len(best) in (40, 64) and set(best) != {"0"}:
        out.append(best)
    for h in info_hash_of(ti):
        if h and h not in out:
            out.append(h)
    return out


def _matches_key(ti, key: str) -> bool:
    """``key`` 是否是该元数据的合法可查键（见 ``alias_keys``）。

    **为什么要校验**（2026-09-17，探针 D3-a 实证为真实缺陷）：
    ``.meta/<ih>.torrent`` 的**文件名是我们按 info_hash 拼的键**，内容却来自
    （可能不可信的）网络数据或本地文件。旧实现只验证「bencode 能被 libtorrent
    反解」，**不验证内容里的 info 段是否就是文件名声称的那个种子**。于是任何
    缓存错配——同键写了别的内容、磁盘位翻转、人为替换——都会被当作**命中**
    返回：UI 按错误元数据建文件树、算文件偏移与分块区间，流服务随后按错误
    几何判定「哪段可读」，下游是一片难以定位的错乱（画面错位、seek 无效、
    404）。这既是正确性缺陷，也是一条缓存投毒面（本地低权限进程往 .meta 写
    一个文件即可让应用按攻击者选定的种子几何行事）。

    命中即校验、不符即删（自愈），与损坏文件的处理同一纪律：最坏情况只是
    回退到一次网络元数据获取，绝不拿错误元数据继续跑。
    """
    k = str(key or "").strip().lower()
    if len(k) not in (40, 64) or set(k) == {"0"}:
        return False
    return k in alias_keys(ti)


def params_from_ti(ti, save_path: str, *, upload_mode: bool = False,
                   trackers=None):
    """用缓存的 ``torrent_info`` 构造 ``add_torrent_params``（缓存注入的唯一入口）。

    **为什么必须新建 params，而不能沿用 ``parse_magnet_uri`` 那个再塞 ``ti``**：
    混合 v1+v2 种子的 ``ti`` 同时携带 v1 与 v2 两个 info-hash，而磁力链只有
    v1，libtorrent 校验两者不等即抛 ``mismatching info-hash``（本环境 2.1.1
    实测；纯 v1 种子不触发，所以这个坑只在混合种子上暴露）。新建的 params
    只带 ``ti``、info_hash 留空，由 libtorrent 自行推导，v1 / v2 / 混合三种
    形态都成立。

    ``upload_mode``：预览/只看清单场景置 True（与解析路径既有语义一致）；
    ``trackers``：磁力链上显式携带的 ``&tr=``（ti 自带的 announce 由
    libtorrent 自动读取，无需重复传入）。

    **不回灌磁力链 url**：磁力链的 btih 只有 v1，混合 v1+v2 种子的 ti 带两个
    info-hash，libtorrent 解析 url 后判定两者不等即抛 ``mismatching info-hash``
    （本环境 2.1.1 实测）。来源字符串本就存在任务清单里，无需再塞进句柄。
    """
    import libtorrent as lt            # 局部导入：纯路径层无需 libtorrent
    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = save_path
    if upload_mode:
        atp.flags |= lt.torrent_flags.upload_mode
    if trackers:
        try:
            atp.trackers = [str(t) for t in trackers]
        except Exception:
            pass
    return atp


# ---------------------------------------------------------------- 主入口

def load_torrent_info(cache_dir: str, info_hash: str):
    """查缓存取 ``lt.torrent_info``；未命中 / 损坏 / **错配**返回 None 并计数。

    命中会**刷新 mtime**（LRU 语义：常看的资源不会被淘汰），并计一次 hit；
    文件存在但反解失败计 corrupt 后**删除**该文件（自愈，下次重写）；
    反解成功但内容 info_hash 与文件名不符计 mismatch 后同样删除——见
    ``_matches_key`` 的论证（缓存错配会按错误元数据建文件树）。
    """
    p = safe_meta_path(cache_dir, info_hash)
    if not p:
        _bump("misses")
        return None
    blob = None
    try:
        with open(p, "rb") as f:
            blob = f.read()
    except OSError:
        _bump("misses")
        return None
    ti = decode_meta(blob)
    if ti is None:
        _bump("misses")
        _bump("corrupt")
        try:
            os.remove(p)
        except OSError:
            pass
        return None
    if not _matches_key(ti, info_hash):
        # 内容可用但与键不符：按未命中处理并删除（自愈 + 防投毒）
        _bump("misses")
        _bump("mismatch")
        log_warning("metacache.mismatch",
                    f"{os.path.basename(p)}：内容 info_hash 与文件名不符，"
                    f"已删除该缓存条目")
        try:
            os.remove(p)
        except OSError:
            pass
        return None
    _bump("hits")
    try:
        os.utime(p, None)           # 命中即续命（LRU）
    except OSError:
        pass
    return ti


def save_torrent_info(cache_dir: str, info_hash: str, ti,
                      aliases: bool = True):
    """落盘元数据（幂等，失败返回 None），随后按 LRU 淘汰超量条目。

    ``aliases=True`` 时把同一份字节**同时**落在 ``alias_keys(ti)`` 的全部键下
    （通常 1~3 个文件）——混合 v1+v2 种子的磁力链 btih 可能是真 v1、也可能是
    绑定层 ``info_hash()`` 给的 v2 截断值，两个键都落盘才能都命中（论证见
    ``alias_keys``）。调用方显式给了别的 ``info_hash`` 时仍以它为首键写入，
    别名只是**额外**的查得到，不改既有查询面。
    """
    blob = encode_meta(ti)
    if not blob:
        return None
    out = write_meta(cache_dir, info_hash, blob)
    if out is None:
        return None
    written = 1
    if aliases:
        for k in alias_keys(ti):
            if k == str(info_hash or "").strip().lower():
                continue
            if write_meta(cache_dir, k, blob) is not None:
                written += 1
    _bump("writes")
    maybe_prune(cache_dir, written=written)
    return out


def _count_entries(d: str) -> int:
    """目录内文件条目数（只 scandir 不 stat；失败按 0）。"""
    try:
        return sum(1 for e in os.scandir(d) if e.is_file())
    except OSError:
        return 0


def maybe_prune(cache_dir: str, max_files: int = META_MAX_FILES,
                written: int = 1) -> int:
    """写路径上的**节流** LRU 剪枝：只在「计数可能超限」或「到期重扫」时扫盘。

    为什么要节流（2026-09-17，探针 D3-b 实测 30 次写入触发 30 次全目录扫描）：
    旧实现在**每次**写入后都调 ``prune_meta``，而后者要 ``scandir`` + 逐条
    ``stat`` + 排序整个 ``.meta``（1000 条 = 1000 次 stat）。元数据写入发生在
    「元数据到达 → 界面出文件树」这条用户最敏感的链路上（``on_metadata_received``
    与 ``_resolve_torrent_file`` 都同步调用），把与本次写入无关的全目录统计
    挂在这条链上是纯浪费。

    实现：进程内维护 `{目录: (已知条目数, 下次强制重扫时点)}`。首次（或距上次
    重扫超过 ``PRUNE_RESYNC``）扫一次计数，此后按 ``written`` 递增；只有计数
    越过上限才真正剪枝。``PRUNE_RESYNC`` 兜住「绕过本模块直接 ``write_meta``
    / 外部程序往 .meta 丢文件」导致的计数漂移。

    ``max_files <= 0`` 视为不限制；任何异常都按「本轮不剪」处理——剪枝是旁路
    设施，绝不阻断元数据写入。
    """
    if max_files <= 0:
        return 0
    d = meta_dir(cache_dir)
    key = os.path.normcase(d)
    now = time.monotonic()
    with _PRUNE_LOCK:
        cnt, next_scan = _PRUNE_STATE.get(key, (-1, 0.0))
        rescan = cnt < 0 or now >= next_scan
    # 审计 2026-09-29（P3）：rescan 的 _count_entries 是毫秒级 scandir I/O，
    # 原先在 _PRUNE_LOCK 内做——元数据到达→界面出文件树的热链每条都要过
    # 这把锁，短暂放大锁竞争。移到锁外扫描；本设施本就容忍计数偏差
    # （「部分清理尽力而为」语义），两段锁之间状态漂移一两个条目无影响。
    if rescan:
        cnt = _count_entries(d)
    else:
        cnt += max(1, int(written))     # 刚才那几次写入自己
    with _PRUNE_LOCK:
        if cnt <= max_files:
            _PRUNE_STATE[key] = (cnt,
                                 (now + _PRUNE_RESYNC) if rescan else next_scan)
            return 0
    removed = prune_meta(cache_dir, max_files)
    with _PRUNE_LOCK:
        _PRUNE_STATE[key] = (_count_entries(d), time.monotonic() + _PRUNE_RESYNC)
    return removed


def prune_meta(cache_dir: str, max_files: int = META_MAX_FILES) -> int:
    """LRU 淘汰：条目数超 ``max_files`` 时删最旧的（按 mtime），返回删除数。

    只在超限时才扫目录（每次写入一次 scandir，元数据写入频率极低）；
    ``max_files <= 0`` 视为不限制。绝不抛异常——清理是旁路设施。
    """
    if max_files <= 0:
        return 0
    d = meta_dir(cache_dir)
    try:
        entries = list(os.scandir(d))
    except OSError:
        return 0
    items = []
    for e in entries:
        try:
            if not e.is_file():
                continue
            st = e.stat()
            items.append((st.st_mtime, e.path))
        except OSError:
            continue
    if len(items) <= max_files:
        return 0
    items.sort()                       # 同刻按路径排序，确定性淘汰
    removed = 0
    for _mtime, path in items[:len(items) - max_files]:
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    if removed:
        _bump("pruned", removed)
    return removed


# ---------------------------------------------------------------- 统计

def _bump(key: str, n: int = 1) -> None:
    with _STATS_LOCK:
        STATS[key] = STATS.get(key, 0) + int(n)


def stats() -> dict[str, int]:
    """命中统计快照（hits/misses/writes/corrupt/mismatch/pruned）。"""
    with _STATS_LOCK:
        return dict(STATS)


def hit_rate() -> float:
    """缓存命中率 0~1（无查询时返回 0.0，避免 0/0）。"""
    s = stats()
    total = s["hits"] + s["misses"]
    return (s["hits"] / total) if total else 0.0


def reset_stats() -> None:
    """清零统计与剪枝节流状态（测试用）。"""
    with _STATS_LOCK:
        for k in STATS:
            STATS[k] = 0
    with _PRUNE_LOCK:
        _PRUNE_STATE.clear()
