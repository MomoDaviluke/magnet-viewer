#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨语言对拍夹具生成器（唯一真相源 = 桌面 Python 实现）。

用途
----
把桌面 magnet-viewer 里那些**已冻结的纯函数语义**固化成 golden 夹具 JSON
（`android/app/src/test/resources/parity/parity.json`），供安卓（Kotlin）侧
`ParityFixturesTest` 逐条断言一致：参数一改两边同时红，漂移不再靠人眼保证。
夹具里每个 `expect` 都是**真实调用桌面 `core/` 实现**得到的返回值，不是手写
推导——桌面的 Python 行为就是契约。

覆盖（纯函数，只读 core/，一行都不改）：
    core.models.safe_rel_path / human_size
    core.models.contiguous_bytes / ready_until / range_available（构造 PieceMap + have 回调）
    core.scheduler.window_pieces / tail_window_bytes
    core.scheduler.tail_entry_pieces / tail_piece_window（轻量替身文件对象）
    core.states.DOWNLOAD_STATES / BOOTSTRAP_TRACKERS / STATE_* 常量
    core.config.lt_proxy_settings

本脚本自身：不联网、不启动 libtorrent 会话、不写任何既有文件（只写上面那个夹具）。

用法
----
    <repo>/.venv/Scripts/python android/tools/gen_parity_fixtures.py           # 生成/更新夹具
    <repo>/.venv/Scripts/python android/tools/gen_parity_fixtures.py --check   # 只校验（CI 用）

生成：写出 parity.json，打印写入路径与各组用例数，退出码 0。
校验：重新生成到内存并与磁盘文件**逐字节**比较；一致打印 `PARITY OK` 退出 0；
      不一致打印首个差异（组名 + 用例序号 + 期望与实际）退出 1。
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import re
import subprocess
import sys

# ---- 仓库根与 core 导入：路径全部由本文件位置推导，不写死盘符 ----
_THIS = pathlib.Path(__file__).resolve()
REPO_ROOT = _THIS.parents[2]                    # android/tools/x.py → <repo>
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import config as core_config          # noqa: E402  （只 import，不建 QSettings）
from core import models as core_models          # noqa: E402
from core import scheduler as core_scheduler    # noqa: E402  （只 import，不开 libtorrent 会话）
from core import states as core_states          # noqa: E402

FIXTURE_PATH = (_THIS.parents[1]                      # android/
                / "app" / "src" / "test" / "resources" / "parity" / "parity.json")
GENERATED_BY = "android/tools/gen_parity_fixtures.py"

KB = 1024
MB = 1024 * 1024
GB = 1024 * 1024 * 1024
SIZE_SMALL = 5 * MB                 # 小文件 ~5MB
SIZE_TINY = 100                     # 比一个块还小的文件
SIZE_LARGE = 4_100_000_000          # 真机 4.1GB 几何（plan/07 实测样本）


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------

def _git_head_sha() -> str:
    """仓库当前 HEAD 的 40 位 commit 哈希（`git rev-parse HEAD`）。"""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_ROOT),
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:      # git 不可用
        raise SystemExit(f"无法执行 git rev-parse HEAD：{exc}")
    if out.returncode != 0:
        raise SystemExit(f"git rev-parse HEAD 失败（{out.returncode}）：{out.stderr.strip()}")
    sha = out.stdout.strip()
    if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
        raise SystemExit(f"git rev-parse HEAD 输出不是 40 位十六进制哈希：{sha!r}")
    return sha


@dataclasses.dataclass
class _StubFile:
    """torrent 文件替身：只提供 scheduler 纯函数读到的 5 个属性。

    真实类型是 core.models.TorrentFile；这里刻意用轻量替身，避免依赖会话/网络
    （ext 由调用方显式给出，不依赖路径推断）。
    """
    size: int
    offset: int
    start_piece: int
    end_piece: int          # 闭区间
    ext: str


def _stub_file(size: int, piece_length: int, ext: str, offset: int = 0) -> _StubFile:
    """按真实几何造替身：块区间由 offset/size/piece_length 推出（闭区间）。"""
    if piece_length <= 0:
        return _StubFile(size=size, offset=offset, start_piece=0, end_piece=0, ext=ext)
    start = offset // piece_length
    n = -(-size // piece_length) if size > 0 else 0          # ceil，非负
    return _StubFile(size=size, offset=offset, start_piece=start,
                     end_piece=start + max(0, n - 1), ext=ext)


def _file_json(f: _StubFile) -> dict:
    return {"end_piece": f.end_piece, "ext": f.ext, "offset": f.offset,
            "size": f.size, "start_piece": f.start_piece}


def _pm_case(piece_length: int, offset: int, start_piece: int, end_piece: int,
             size: int, have_pieces, **extra) -> dict:
    """一条 PieceMap 用例的 input（have 回调由 have_pieces 白名单表达）。"""
    inp = {"piece_length": piece_length, "offset": offset,
           "start_piece": start_piece, "end_piece": end_piece,
           "size": size, "have_pieces": sorted({int(p) for p in have_pieces})}
    inp.update(extra)                                  # limit / start / end_excl
    return inp


def _pm_of(inp: dict) -> "core_models.PieceMap":
    """由 input 重建 PieceMap：have(p) 当且仅当 p 在 have_pieces 白名单里。"""
    ready = frozenset(inp["have_pieces"])
    return core_models.PieceMap(piece_length=inp["piece_length"], offset=inp["offset"],
                                start_piece=inp["start_piece"], end_piece=inp["end_piece"],
                                size=inp["size"], have=lambda p: p in ready)


def _tail_case(fn, size: int, piece_length: int, ext: str,
               offset: int = 0, file_none: bool = False) -> dict:
    """一条 tail_* 用例：input 里的 file 是替身对象的 JSON 视图。"""
    if file_none:
        return {"input": {"file": None, "piece_length": piece_length},
                "expect": fn(None, piece_length)}
    f = _stub_file(size, piece_length, ext, offset)
    return {"input": {"file": _file_json(f), "piece_length": piece_length},
            "expect": fn(f, piece_length)}


def _json_map(d: dict) -> dict:
    """只保留 JSON 可序列化的键值（键顺序 = 函数返回顺序，写盘时再统一排序）。"""
    out = {}
    for k, v in d.items():
        if not isinstance(v, (str, int, float, bool, type(None))):
            raise SystemExit(f"lt_proxy_settings 返回不可序列化的值：{k}={v!r}")
        out[k] = v
    return out


# --------------------------------------------------------------------------
# 各组用例（expect 一律来自真实调用）
# --------------------------------------------------------------------------

def _build_safe_rel_path() -> list:
    seg_lists = [
        ("..", "..", "Windows", "win.ini"),        # 目录穿越（多级）
        ("..",),                                   # 单个 ..（全被丢弃）
        ("../..", "etc", "passwd"),                # ../.. 嵌在段内
        ("..", "..", "..", "..", "boot.ini"),      # 深层穿越
        ("C:", "Users", "evil.txt"),               # Windows 盘符段
        ("C:\\Users\\evil.txt",),                  # 盘符 + 反斜杠分隔
        ("D:evil.txt",),                           # 盘符无目录分隔符
        ("a<b>c|d?e*f\"g",),                       # Windows 非法字符 < > | ? * "
        ("foo", "", "bar"),                        # 含空段
        ("",),                                     # 空串
        ("", "", ""),                              # 全空段
        ("   ",),                                  # 只有空白（strip 后为空）
        ("\x00name\x00",),                         # 首尾 NUL（strip("\x00") 处理）
        ("a//b///c",),                             # 连续分隔符
        (".", ".", ".."),                          # 纯 . / .. 段
        ("/abs/path",),                            # 绝对路径前缀
        ("..\\..\\evil",),                         # 反斜杠穿越
        (".hidden",),                              # 点开头但不是 . / ..（保留）
        ("dir", "..", "file"),                     # 中段 ..
        ("a:b:c",),                                # 段内冒号（非盘符）→ 下划线
    ]
    return [{"input": list(segs), "expect": core_models.safe_rel_path(*segs)}
            for segs in seg_lists]


def _build_human_size() -> list:
    sizes = [0, 1, 1023, 1024, 1536, 10 * KB, 5 * MB,
             GB, 1610612736,          # 1.5GB
             1024 * GB,               # 1TB
             1024 * 1024 * GB]        # 1PB
    return [{"input": n, "expect": core_models.human_size(n)} for n in sizes]


def _build_contiguous_bytes() -> list:
    raws = [
        _pm_case(4, 0, 0, 9, 40, [0, 1, 2]),                       # 头连续
        _pm_case(4, 0, 0, 9, 40, [0, 1, 3, 4]),                    # 中间缺块 → 停在缺口
        _pm_case(4, 0, 0, 9, 40, []),                              # 一块都没有
        _pm_case(4, 0, 0, 9, 40, list(range(10))),                 # 整文件就绪
        _pm_case(4, 0, 0, 9, 40, [9]),                             # 只有尾块（moov 先落盘）
        _pm_case(4, 0, 0, 9, 40, [9], limit=1000),                 # limit 大于 size
        _pm_case(4, 0, 0, 9, 40, [0, 1, 2], limit=5),              # limit 截断窗口
        _pm_case(4, 2, 0, 2, 10, [0, 1]),                          # 文件从块中间开始（offset）
        _pm_case(4, 0, 0, 9, 0, [0, 1]),                           # size=0
        _pm_case(0, 0, 0, 9, 40, [0, 1]),                          # piece_length=0
        _pm_case(-1, 0, 0, 9, 40, [0, 1]),                         # piece_length 非法
        # 「尾部窗口先落盘 → 总量领先于连续前缀」：16 块的文件只落了尾部 3 块
        _pm_case(262144, 0, 0, 15, 4 * MB, [13, 14, 15]),
        _pm_case(262144, 0, 0, 15, 4 * MB, [0, 1, 14, 15]),        # 头两块 + 尾两块
    ]
    return [{"input": inp, "expect": core_models.contiguous_bytes(_pm_of(inp), inp.get("limit"))}
            for inp in raws]


def _build_ready_until() -> list:
    raws = [
        _pm_case(4, 0, 0, 9, 40, [0, 1, 2], start=0, end_excl=40),   # 部分就绪 → 首个未就绪块边界
        _pm_case(4, 0, 0, 9, 40, list(range(10)), start=0, end_excl=40),  # 区间全就绪
        _pm_case(4, 0, 0, 9, 40, list(range(10)), start=8, end_excl=40),  # 从中间起的全就绪
        _pm_case(4, 0, 0, 9, 40, [0, 1], start=5, end_excl=5),       # start >= end_excl
        _pm_case(4, 0, 0, 9, 40, list(range(10)), start=0, end_excl=41),  # 越界 end_excl > size
        _pm_case(0, 0, 0, 9, 40, [0, 1], start=0, end_excl=40),      # piece_length=0
        _pm_case(4, 0, 0, 9, 40, [0], start=9, end_excl=20),         # start 所在块未就绪 → 回 start
        _pm_case(4, 0, 0, 9, 40, [0], start=2, end_excl=20),         # start 在已就绪块内、后块缺
        _pm_case(4, 2, 0, 2, 10, [0], start=0, end_excl=10),         # offset 几何
        _pm_case(262144, 0, 0, 15, 4 * MB, [0, 1], start=0, end_excl=4 * MB),  # 头两块就绪
    ]
    return [{"input": inp,
             "expect": core_models.ready_until(_pm_of(inp), inp["start"], inp["end_excl"])}
            for inp in raws]


def _build_range_available() -> list:
    raws = [
        _pm_case(4, 0, 0, 9, 40, [0, 1], start=0, end_excl=0),        # 空区间 → True
        _pm_case(4, 0, 0, 9, 40, [0, 1], start=5, end_excl=2),        # start > end_excl → True
        _pm_case(4, 0, 0, 9, 40, [0, 1], start=0, end_excl=-4),       # end_excl <= 0 → True
        _pm_case(4, 0, 0, 9, 40, list(range(10)), start=0, end_excl=41),  # 越界 → False
        _pm_case(0, 0, 0, 9, 40, [0, 1], start=0, end_excl=8),        # piece_length=0 → False
        _pm_case(4, 0, 0, 9, 40, [0, 1], start=0, end_excl=8),        # 区间全就绪 → True
        _pm_case(4, 0, 0, 9, 40, [0], start=0, end_excl=8),           # 区间部分就绪 → False
        _pm_case(4, 0, 0, 9, 40, [0], start=3, end_excl=5),           # 跨块、第二块缺 → False
        _pm_case(4, 0, 0, 9, 40, list(range(10)), start=0, end_excl=40),  # end_excl == size 全覆盖
        _pm_case(4, 2, 0, 2, 10, [0], start=0, end_excl=6),           # offset 几何
        _pm_case(262144, 0, 0, 15, 4 * MB, [0, 1, 14, 15], start=0, end_excl=2 * 262144),
    ]
    return [{"input": inp,
             "expect": core_models.range_available(_pm_of(inp), inp["start"], inp["end_excl"])}
            for inp in raws]


def _build_window_pieces() -> list:
    pls = [16384, 262144, 1048576, 2097152, 4194304, 8388608, 0, -1, 16777216]
    return [{"input": {"piece_length": pl}, "expect": core_scheduler.window_pieces(pl)}
            for pl in pls]


def _build_tail_window_bytes() -> list:
    sizes = [
        0, 1, 100 * KB,
        500 * MB, 1_000_000_000, GB, SIZE_LARGE,
        SIZE_LARGE - 1, SIZE_LARGE + 1,
        2 * MB - 1, 2 * MB, 2 * MB + 1,                # TAIL_BYTES_MIN 边界 ±1
        800 * MB - 1, 800 * MB, 800 * MB + 1,          # int(size*0.0025) 越过 2MB 下限的边界
        16 * MB - 1, 16 * MB, 16 * MB + 1,             # 16MB 一侧的边界 ±1
        6710886400 - 1, 6710886400, 6710886400 + 1,    # 0.25% 恰好撞上 16MB 上限的边界
    ]
    return [{"input": {"size": s}, "expect": core_scheduler.tail_window_bytes(s)} for s in sizes]


def _build_tail_entry_pieces() -> list:
    combos = [(SIZE_SMALL, 262144, ".mp4"), (SIZE_SMALL, 262144, ".mkv"),
              (SIZE_SMALL, 4194304, ".mp4"), (SIZE_SMALL, 4194304, ".mkv"),
              (SIZE_LARGE, 262144, ".mp4"), (SIZE_LARGE, 262144, ".mkv"),
              (SIZE_LARGE, 4194304, ".mp4"), (SIZE_LARGE, 4194304, ".mkv")]
    cases = [_tail_case(core_scheduler.tail_entry_pieces, size, pl, ext)
             for size, pl, ext in combos]
    cases.append(_tail_case(core_scheduler.tail_entry_pieces, 0, 262144, ".mp4", file_none=True))
    cases.append(_tail_case(core_scheduler.tail_entry_pieces, 0, 262144, ".mp4"))     # size=0
    cases.append(_tail_case(core_scheduler.tail_entry_pieces, SIZE_SMALL, 0, ".mp4"))  # pl=0
    cases.append(_tail_case(core_scheduler.tail_entry_pieces, SIZE_SMALL, -1, ".mp4"))  # pl=-1
    cases.append(_tail_case(core_scheduler.tail_entry_pieces, SIZE_TINY, 262144, ".mp4"))  # 比块还小
    cases.append(_tail_case(core_scheduler.tail_entry_pieces, SIZE_SMALL, 4194304, ".mp4",
                            offset=1000))                                             # 非零 offset
    return cases


def _build_tail_piece_window() -> list:
    combos = [(SIZE_SMALL, 262144, ".mp4"), (SIZE_SMALL, 262144, ".mkv"),
              (SIZE_SMALL, 4194304, ".mp4"), (SIZE_SMALL, 4194304, ".mkv"),
              (SIZE_LARGE, 262144, ".mp4"), (SIZE_LARGE, 262144, ".mkv"),
              (SIZE_LARGE, 4194304, ".mp4"), (SIZE_LARGE, 4194304, ".mkv")]
    cases = [_tail_case(core_scheduler.tail_piece_window, size, pl, ext)
             for size, pl, ext in combos]
    cases.append(_tail_case(core_scheduler.tail_piece_window, SIZE_TINY, 262144, ".mp4"))
    cases.append(_tail_case(core_scheduler.tail_piece_window, 100 * KB, 262144, ".mp4"))
    cases.append(_tail_case(core_scheduler.tail_piece_window, SIZE_LARGE, 65536, ".mp4"))   # 命中 TAIL_MAX_PIECES 夹取
    cases.append(_tail_case(core_scheduler.tail_piece_window, SIZE_LARGE, 131072, ".mp4"))
    cases.append(_tail_case(core_scheduler.tail_piece_window, SIZE_LARGE, 4194304, ".mp4",
                            offset=4096))                                                 # 非零 offset
    # 注意：这里**没有** file=None 用例。桌面实现的容错面不对称——
    # tail_entry_pieces 有 `file is None` 守卫（返回 []），tail_piece_window 直接读
    # `file.ext`（传 None 抛 AttributeError）。Kotlin 侧据此把参数定为可空/非空：
    # tailEntryPieces(file: TorrentFile?, pl) 要容 None，tailPieceWindow(file: TorrentFile, pl) 不必。
    cases.append(_tail_case(core_scheduler.tail_piece_window, 0, 262144, ".mp4"))         # size=0
    cases.append(_tail_case(core_scheduler.tail_piece_window, SIZE_SMALL, 0, ".mp4"))    # pl=0
    cases.append(_tail_case(core_scheduler.tail_piece_window, SIZE_SMALL, -1, ".mkv"))   # pl=-1
    return cases


def _build_download_states() -> list:
    return [{"input": {},
             "expect": sorted(core_states.DOWNLOAD_STATES)}]


def _build_bootstrap_trackers() -> list:
    return [{"input": {},
             "expect": sorted(core_states.BOOTSTRAP_TRACKERS)}]


def _build_state_constants() -> list:
    names = sorted(n for n in vars(core_states) if n.startswith("STATE_"))
    return [{"input": {"name": n}, "expect": getattr(core_states, n)} for n in names]


def _build_lt_proxy_settings() -> list:
    inputs = [
        None,                                                       # 整个 proxy 为 None
        {},                                                         # 空配置
        {"type": "none", "host": "proxy.local", "port": 1080,
         "user": "", "pass": "", "peer": True},                     # 未启用
        {"type": "socks5", "host": "", "port": 1080,
         "user": "", "pass": "", "peer": True},                     # 主机为空
        {"type": "socks5", "host": "", "port": 1080,
         "user": "u", "pass": "p", "peer": True},                   # 主机为空（哪怕带账号）
        {"type": "", "host": "proxy.local", "port": 1080,
         "user": "", "pass": "", "peer": True},                     # 类型为空 → 回落直连
        {"type": "socks4", "host": "proxy.local", "port": 1080,
         "user": "", "pass": "", "peer": True},                     # 未知类型 → 回落直连
        {"type": "socks5", "host": "10.0.0.1", "port": 1080,
         "user": "", "pass": "", "peer": True},                     # socks5 无账号
        {"type": "socks5", "host": " 10.0.0.1 ", "port": 1080,
         "user": "", "pass": "", "peer": True},                     # 主机首尾空白 → strip
        {"type": "SOCKS5", "host": "10.0.0.1", "port": 1080,
         "user": "", "pass": "", "peer": True},                     # 大小写不敏感
        {"type": "socks5", "host": "10.0.0.1", "port": 1080,
         "user": "u", "pass": "p", "peer": True},                   # socks5 带账号 → socks5_pw
        {"type": "socks5", "host": "10.0.0.1", "port": 1080,
         "user": "u", "pass": "", "peer": True},                    # 有用户名、密码为空
        {"type": "http", "host": "10.0.0.2", "port": 8080,
         "user": "u", "pass": "p", "peer": True},                   # http 带账号 → http_pw
        {"type": "http", "host": "10.0.0.2", "port": 8080,
         "user": "", "pass": "", "peer": True},                     # http 无账号
        {"type": "http", "host": "10.0.0.2", "port": 8080,
         "user": "", "pass": "", "peer": False},                    # peer 连接不走代理
        {"type": "socks5", "host": "10.0.0.1"},                     # 缺端口 → 默认 1080
        {"type": "socks5", "host": "10.0.0.1", "port": "8080",
         "user": "", "pass": "", "peer": True},                     # 端口是字符串 → int() 归一
    ]
    return [{"input": inp, "expect": _json_map(core_config.lt_proxy_settings(inp))}
            for inp in inputs]


def build_payload() -> dict:
    """构造夹具 JSON 载荷（source_sha 绑定当前 HEAD，便于定位夹具对应提交）。"""
    cases = {
        "safe_rel_path": _build_safe_rel_path(),
        "human_size": _build_human_size(),
        "contiguous_bytes": _build_contiguous_bytes(),
        "ready_until": _build_ready_until(),
        "range_available": _build_range_available(),
        "window_pieces": _build_window_pieces(),
        "tail_window_bytes": _build_tail_window_bytes(),
        "tail_entry_pieces": _build_tail_entry_pieces(),
        "tail_piece_window": _build_tail_piece_window(),
        "download_states": _build_download_states(),
        "bootstrap_trackers": _build_bootstrap_trackers(),
        "state_constants": _build_state_constants(),
        "lt_proxy_settings": _build_lt_proxy_settings(),
    }
    return {"generated_by": GENERATED_BY, "source_sha": _git_head_sha(), "cases": cases}


def render(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# --------------------------------------------------------------------------
# --check：首个差异定位（组名 + 用例序号 + 期望与实际）
# --------------------------------------------------------------------------

def first_difference(gen: dict, disk_text: str) -> list:
    try:
        disk = json.loads(disk_text)
    except ValueError as exc:
        return [f"夹具文件不是合法 JSON：{exc}"]
    if not isinstance(disk, dict):
        return ["夹具文件顶层不是 JSON 对象"]
    if disk.get("generated_by") != gen.get("generated_by"):
        return [f"顶层字段 generated_by 不一致：期望 {gen.get('generated_by')!r} / 实际 {disk.get('generated_by')!r}"]
    # 注意：source_sha **不参与判定**。它绑定的是生成夹具时的提交，只要 HEAD 前进（或 CI 浅克隆
    # 拿不到同一个 HEAD）就会不同——但那不代表"行为漂移"。真正要比的是下面的 cases：
    # 桌面 core/ 的行为一改，用例的 expect 就会变，那时才该红。
    # 溯源信息在 --check 通过时单独打印（见 main），方便对照夹具与提交。
    gen_cases, disk_cases = gen["cases"], disk.get("cases")
    if not isinstance(disk_cases, dict):
        return ["顶层字段 cases 不是 JSON 对象"]
    missing = sorted(set(gen_cases) - set(disk_cases))
    if missing:
        return [f"缺少分组：{', '.join(missing)}"]
    extra = sorted(set(disk_cases) - set(gen_cases))
    if extra:
        return [f"多出分组：{', '.join(extra)}"]
    for group in sorted(gen_cases):
        g, d = gen_cases[group], disk_cases[group]
        if not isinstance(d, list):
            return [f"分组 {group} 不是 JSON 数组"]
        if len(g) != len(d):
            return [f"分组 {group} 用例数不一致：期望 {len(g)} / 实际 {len(d)}"]
        for i, (gc, dc) in enumerate(zip(g, d)):
            if not isinstance(dc, dict):
                return [f"分组 {group} 用例[{i}] 不是 JSON 对象"]
            if gc != dc:
                for field in ("input", "expect"):
                    if gc.get(field) != dc.get(field):
                        return [
                            f"分组 {group} 用例[{i}] 的 {field} 不一致："
                            f"期望(重生成) {json.dumps(gc.get(field), ensure_ascii=False, sort_keys=True)} / "
                            f"实际(磁盘) {json.dumps(dc.get(field), ensure_ascii=False, sort_keys=True)}"]
                return [f"分组 {group} 用例[{i}] 内容不一致："
                        f"期望(重生成) {json.dumps(gc, ensure_ascii=False, sort_keys=True)} / "
                        f"实际(磁盘) {json.dumps(dc, ensure_ascii=False, sort_keys=True)}"]
    return []


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="生成/校验安卓跨语言对拍夹具（唯一真相源 = 桌面 core/ 实现）")
    parser.add_argument("--check", action="store_true",
                        help="只校验：重新生成到内存并与磁盘 parity.json 逐字节比较")
    args = parser.parse_args(argv)

    payload = build_payload()
    text = render(payload)
    cases = payload["cases"]
    total = sum(len(v) for v in cases.values())

    if not args.check:
        FIXTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(FIXTURE_PATH, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        print(f"写入: {FIXTURE_PATH}")
        print(f"source_sha: {payload['source_sha']}")
        print(f"组数: {len(cases)} · 用例总数: {total}")
        for group in sorted(cases):
            print(f"  {group}: {len(cases[group])}")
        return 0

    # ---- --check ----
    if not FIXTURE_PATH.exists():
        print("PARITY MISMATCH")
        print(f"夹具文件不存在：{FIXTURE_PATH}（先跑一次生成模式）")
        return 1
    disk_text = FIXTURE_PATH.read_bytes().decode("utf-8")
    # source_sha 只是溯源信息（绑定生成时的提交），不参与判定：把它归一化后再做逐字节比较，
    # 其余内容仍然要求逐字节一致（夹具是脚本生成的，格式差异本身也是漂移信号）。
    sha_re = re.compile(r'"source_sha": "[0-9a-f]{40}"')
    norm_disk = sha_re.sub('"source_sha": "<ignored>"', disk_text)
    norm_gen = sha_re.sub('"source_sha": "<ignored>"', text)
    if norm_disk == norm_gen:
        disk_sha = (json.loads(disk_text).get("source_sha") or "?")[:12]
        print(f"PARITY OK（source_sha={disk_sha}… 仅作溯源，不参与判定）")
        return 0
    print("PARITY MISMATCH")
    diffs = first_difference(payload, disk_text)
    if diffs:
        print(diffs[0])
    else:
        print("结构与磁盘夹具一致，但字节不一致（空白/换行/键序差异）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
