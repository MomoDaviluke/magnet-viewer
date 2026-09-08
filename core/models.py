"""数据模型：种子文件条目与解析结果。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".flv", ".wmv", ".webm", ".ts", ".m4v", ".rmvb", ".mpg", ".mpeg"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} PB"


@dataclass
class TorrentFile:
    """种子中的一个文件。

    path 为包含种子根名的完整相对路径（'/' 分隔），
    可直接映射到磁盘缓存目录 cache_dir / path。
    """
    index: int
    path: str          # 根名/子目录/文件名
    size: int
    offset: int        # 在种子数据流中的字节偏移
    start_piece: int
    end_piece: int     # 闭区间

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def ext(self) -> str:
        return os.path.splitext(self.name)[1].lower()

    @property
    def is_video(self) -> bool:
        return self.ext in VIDEO_EXTS

    @property
    def is_image(self) -> bool:
        return self.ext in IMAGE_EXTS

    @property
    def is_pad(self) -> bool:
        """libtorrent 生成的 piece 对齐填充文件，展示时应过滤。"""
        return "/.pad/" in f"/{self.path}"

    @property
    def is_previewable(self) -> bool:
        return (self.is_video or self.is_image) and not self.is_pad


@dataclass
class ParseResult:
    """一次元数据解析的完整结果。"""
    info_hash: str
    name: str                     # 种子根名（磁盘子目录名）
    total_size: int
    piece_size: int
    num_pieces: int
    files: list = field(default_factory=list)   # list[TorrentFile]
    trackers: list = field(default_factory=list)
    comment: str = ""
    created_by: str = ""
    source: str = ""              # "torrent" | "magnet"
    cache_dir: str = ""           # 磁盘缓存根目录（由会话管理器注入）
    save_subdir: str = ""         # 落盘子目录（D7 隔离：".preview/<ih>" 或
                                  # "downloads/<ih>"；空 = 平铺 cache_dir）。
                                  # 语义：相对 cache_dir 的子目录；download_dir
                                  # 配置在 cache_dir 之外时为绝对路径。由会话
                                  # 管理器注入，main_window 据此拼磁盘路径键。

    @property
    def view_files(self) -> list:
        """界面展示用文件列表（过滤 .pad 对齐填充文件）。"""
        return [f for f in self.files if not f.is_pad]

    @property
    def images(self) -> list:
        return [f for f in self.files if f.is_image and not f.is_pad]

    @property
    def videos(self) -> list:
        return [f for f in self.files if f.is_video and not f.is_pad]


def safe_rel_path(*segments: str) -> str:
    """把种子里的路径段净化成安全相对路径（防御目录穿越）。

    恶意种子可声明 ``path: ["..", "..", "Windows", "win.ini"]`` 或绝对路径，
    若原样拼接会逃出缓存目录。此处逐级丢弃 ``.`` / ``..`` / 空段，剥离盘符与
    根前缀，并把段内的分隔符与 Windows 非法字符替换为下划线。
    """
    out: list[str] = []
    for seg in segments:
        for part in str(seg).replace("\\", "/").split("/"):
            part = part.strip().strip("\x00")
            if part in ("", ".", ".."):
                continue
            # 剥离盘符（C:）与设备前缀
            if len(part) > 1 and part[1] == ":":
                part = part.split(":", 1)[1]
            part = part.replace(":", "_")
            for ch in '<>"|?*':
                part = part.replace(ch, "_")
            if part and part not in (".", ".."):
                out.append(part)
    return "/".join(out) or "unnamed"


def disk_root(cache_dir: str, save_subdir: str = "") -> str:
    """任务落盘根目录：``save_subdir`` 相对时拼到 cache_dir，绝对路径时直接使用。

    ``ParseResult.save_subdir`` 语义：相对 cache_dir 的子目录（如 ``.preview/<ih>``、
    ``downloads/<ih>``）；当 download_dir 配置在 cache_dir 之外时为**绝对路径**
    （见 fetcher._save_subdir_of）。用 ``os.path.join`` 把绝对路径拼到别的目录下
    会退化成盘符相对路径（``C:\\cache`` + ``D:\\dl\\ih`` → ``D:dl\\ih``），
    必须在此统一处理——main_window 的磁盘键、画廊、流服务相对路径都经此函数。
    """
    sub = str(save_subdir or "").strip()
    if not sub:
        return str(cache_dir or "")
    if os.path.isabs(sub):
        return sub
    return os.path.join(str(cache_dir or ""),
                        *[s for s in sub.replace("\\", "/").split("/") if s])


def file_disk_path(cache_dir: str, f: TorrentFile) -> str:
    """文件在磁盘缓存中的完整路径（经 safe_rel_path 净化，不会逃出 cache_dir）。

    注意：带任务隔离布局时请用 ``file_disk_path(disk_root(cache_dir, save_subdir), f)``
    （gallery 曾因漏拼 save_subdir 而永远找不到已下载图片）。
    """
    return os.path.join(cache_dir, *safe_rel_path(f.path).split("/"))


@dataclass
class PieceMap:
    """文件 → 种子分块的映射视图，供流服务按“已下载分块”判定可读区间。

    have(piece_index) 返回该种子分块是否已完整落盘
    （libtorrent 只有整块落盘才算 have，未下载区间是稀疏零数据，不可读取）。
    """
    piece_length: int
    offset: int                 # 文件首字节在种子数据流中的偏移
    start_piece: int
    end_piece: int              # 闭区间
    size: int
    have: Callable[[int], bool]


def contiguous_bytes(pm: PieceMap, limit: int | None = None) -> int:
    """文件头部连续可读字节数（从 0 开始、中途无缺失块的连续区间）。

    边下边播场景下“已下载前缀”不再等于 file_progress 总量——尾部(moov)窗口
    先落盘时，总量会领先于连续前缀，而播放器只能消费连续前缀。
    """
    size = pm.size if limit is None else min(pm.size, limit)
    if size <= 0 or pm.piece_length <= 0:
        return 0
    p = pm.start_piece
    while p <= pm.end_piece and pm.have(p):
        p += 1
    contig = p * pm.piece_length - pm.offset
    return max(0, min(size, contig))


def ready_until(pm: PieceMap, start: int, end_excl: int) -> int:
    """从 start 起向后延伸，返回第一个**未就绪**字节的位置（开区间末端）。

    用于流服务的「渐进服务」：拖动进度条时 FFmpeg 发 `bytes=X-`（覆盖到
    文件尾），要求整个区间就绪必然超时——改为就绪多少发多少，FFmpeg 读尽
    Content-Length 后会自动续发下一段 Range（与头部顺序播放同一机制）。
    返回 start 表示 start 处一个就绪字节都没有。
    """
    if start >= end_excl:
        return start
    if pm.piece_length <= 0 or end_excl > pm.size:
        return start
    p0 = (pm.offset + start) // pm.piece_length
    p1 = (pm.offset + end_excl - 1) // pm.piece_length
    p = p0
    while p <= p1 and pm.have(p):
        p += 1
    until = p * pm.piece_length - pm.offset
    return max(start, min(end_excl, until))


def range_available(pm: PieceMap, start: int, end_excl: int) -> bool:
    """[start, end_excl) 区间内的每个字节是否都属于已落盘分块。"""
    if start >= end_excl or end_excl <= 0:
        return True
    if end_excl > pm.size:
        return False
    if pm.piece_length <= 0:
        return False
    p0 = (pm.offset + start) // pm.piece_length
    p1 = (pm.offset + end_excl - 1) // pm.piece_length
    for p in range(p0, p1 + 1):
        if not pm.have(p):
            return False
    return True
