"""下载续传数据（fastresume）路径约定与编解码（纯函数模块）。

约定（plan/t2 §2 B3）：``<cache_dir>/.resume/<ih>.fastresume``，
内容为 libtorrent resume data 的 bencode（顶层 dict、key 全为 bytes）。
编解码复用 ``core.parser`` 的自研 bencode（零 libtorrent 会话依赖、带 DoS
防御、确定性）——不用 ``lt.bencode``：2.1.x python 绑定对 int 值编码会触发
DeprecationWarning、且不支持 int 键（resume data 的量/位图恰是 int/bytes 混合），
自研编解码已在 smoke_test [2d] 与 ``lt.bdecode`` 做交叉互通验证。

- 解码失败 / 顶层非 dict = 数据损坏 → 返回 None，调用方静默重建
  （损坏 fastresume 绝不阻断启动）；
- info_hash 文件名强制 40/64 hex（复用 taskstore.normalize_info_hash），
  防路径注入（ih 可能来自不可信来源的拼接路径）；
- 写盘复用 taskstore.atomic_write_bytes（原子替换，不产生半截文件）。
"""
from __future__ import annotations

import os

from .parser import bdecode as _bdecode
from .parser import bencode as _bencode
from .taskstore import atomic_write_bytes, normalize_info_hash

RESUME_SUBDIR = ".resume"


def resume_dir(cache_dir: str) -> str:
    """fastresume 目录：``<cache_dir>/.resume``（由调用方保证 cache_dir 合法）。"""
    return os.path.join(cache_dir, RESUME_SUBDIR)


def resume_path(cache_dir: str, info_hash: str) -> str:
    """单个任务的 fastresume 文件路径：``<cache_dir>/.resume/<ih>.fastresume``。

    info_hash 非法（非 40/64 hex）抛 ValueError——文件名由外部拼入，必须防穿越。
    """
    ih = normalize_info_hash(info_hash, raise_invalid=True)
    return os.path.join(resume_dir(cache_dir), f"{ih}.fastresume")


def normalized_resume(data: dict) -> dict:
    """把 libtorrent 2.1 的 resume data 归一为「自研 bencode 认得」的结构。

    坑（性能专项 P-1b 引入 info 段后暴露）：开启 ``save_info_dict`` 后，
    resume data 的 ``info`` 键是**已编码好的 bencode 原始字节**，python 绑定
    把它暴露成 **有符号 int 元组**（不是 bytes，也不是 dict）—— 自研
    ``bencode`` 不认 tuple，直接编码抛 TypeError，整份 resume data 写盘
    静默失败（表现：任务重启后没有续传数据、退回重新索取元数据）。

    还原方式：元组 → 字节 → 走自研 ``bdecode`` 变回嵌套 dict。bencode 字典
    要求键有序且本就按序存储，重新编码与原字节**逐字节一致**，info-hash
    不受影响（这是能否正确恢复的前提）。首字节不是 ``d``/``l`` 的元组按普通
    字节串处理，绝不臆测。
    """
    def _norm(obj):
        if isinstance(obj, tuple):
            raw = bytes(int(x) & 0xFF for x in obj)
            if raw[:1] in (b"d", b"l"):
                try:
                    return _norm(_bdecode(raw))
                except (TypeError, ValueError):
                    # ValueError：自研 bdecode 的全部失败面（index 未找到/整数
                    # 非法/深度与长度上限）；TypeError：元组元素非数值时 int()
                    # 失败。解不开按普通字节串原样保留
                    return raw
            return raw
        if isinstance(obj, dict):
            return {k: _norm(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_norm(v) for v in obj]
        return obj
    return _norm(data)


def encode_resume(data: dict) -> bytes:
    """把 resume data dict（bytes key）编码为 bencode 字节。

    键必须是 bytes/str（libtorrent resume 格式规范），不合格抛 ValueError。
    编码前先过 :func:`normalized_resume`（info 段的 int 元组还原，见其说明）。
    """
    try:
        return _bencode(normalized_resume(data))
    except (TypeError, ValueError) as e:
        # 自研 bencode 只抛 TypeError（bool/不支持类型/键不可排序），
        # normalized_resume 的元组还原只抛 ValueError（int() 非法）
        raise ValueError(f"fastresume 编码失败：{e}") from e


def decode_resume(data: bytes) -> dict | None:
    """解码 fastresume 字节 → dict；损坏（含顶层非 dict）返回 None。"""
    try:
        out = _bdecode(data)
    except ValueError:
        # 自研 bdecode 的全部失败面都是 ValueError（损坏数据按「解码失败」处理）
        return None
    return out if isinstance(out, dict) else None


def write_resume(cache_dir: str, info_hash: str, data: dict) -> str:
    """把 resume data 原子写入磁盘（时机由调用方决定：暂停/退出/60s 周期脏）。

    返回写出的文件路径。
    """
    return atomic_write_bytes(resume_path(cache_dir, info_hash),
                              encode_resume(data))