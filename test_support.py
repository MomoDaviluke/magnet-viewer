"""下载管理模块验收测试共享基建（test_support）。

下载管理模块的验收测试（download_mgr_test.py）与后续回归共用的基础设施：

- 本机做种端（seed_mode，127.0.0.1 闭环，无需外网）——复用 local_magnet_test.py
  的验证底盘（做种端 :52，resolve+connect_peer :86-87）；
- 载荷/种子构造（多文件目录 + .torrent）；
- 磁盘字节快照（暂停/恢复断言：5s 内字节不变 / 续增）；
- 结果收集器（OK/FAIL + 退出码 0/1/2 约定，同 README.md:90）；
- 轮询等待辅助函数。

退出码约定（全项目一致）：0=通过，1=失败，2=SKIP（依赖缺失时显式跳过，
绝不假装通过）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import libtorrent as lt  # noqa: E402


# --------------------------------------------------------------------------
# 载荷与种子构造
# --------------------------------------------------------------------------

def build_payload(root: str, files: dict[str, int] | None = None) -> str:
    """构造一个含随机字节文件的目录（默认视频/图片/文本混排），返回目录路径。

    files: {"相对路径": 字节数}；默认 4 文件（与 local_magnet_test 相同构图）。
    """
    if files is None:
        files = {
            "movie/demo.mp4": 900 * 1024,
            "pics/a.jpg": 120 * 1024,
            "pics/b.png": 60 * 1024,
            "readme.txt": 512,
        }
    for rel, n in files.items():
        p = os.path.join(root, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(os.urandom(n))
    return root


def make_torrent(payload: str, piece_size: int = 16 * 1024) -> lt.torrent_info:
    """为 payload 目录生成种子（libtorrent 2.x 默认 v1+v2 混合），返回 torrent_info。"""
    fs = lt.file_storage()
    lt.add_files(fs, payload)
    ct = lt.create_torrent(fs, piece_size)
    lt.set_piece_hashes(ct, os.path.dirname(payload))
    return lt.torrent_info(ct.generate())


def start_seeder(ti: lt.torrent_info, payload_parent: str,
                 port: int) -> tuple[lt.session, lt.torrent_handle]:
    """以 seed_mode 启动本机做种端（数据已完整，跳过校验）。"""
    ses = lt.session({
        "listen_interfaces": f"0.0.0.0:{port}",
        "enable_dht": False, "enable_lsd": False, "enable_upnp": False,
        "enable_natpmp": False,
    })
    atp = lt.add_torrent_params()
    atp.ti = ti
    atp.save_path = payload_parent
    atp.flags |= lt.torrent_flags.seed_mode
    h = ses.add_torrent(atp)
    h.resume()
    return ses, h


def stop_seeder(ses: lt.session | None) -> None:
    """停做种端（drop 引用即析构会话）。"""
    if ses is not None:
        ses = None


def magnet_uri(info_hash: str, dn: str = "demo-payload") -> str:
    return f"magnet:?xt=urn:btih:{info_hash}&dn={dn}"


# --------------------------------------------------------------------------
# 磁盘字节快照（暂停/恢复/删除断言）
# --------------------------------------------------------------------------

def dir_byte_snapshot(root: str) -> int:
    """目录下全部文件字节总数（含子目录）；目录不存在时返回 0。"""
    if not os.path.isdir(root):
        return 0
    total = 0
    for d, _, ns in os.walk(root):
        for n in ns:
            try:
                total += os.path.getsize(os.path.join(d, n))
            except OSError:
                pass
    return total


# --------------------------------------------------------------------------
# 轮询等待
# --------------------------------------------------------------------------

def wait_until(pred, timeout: float, interval: float = 0.25,
               desc: str = "条件") -> bool:
    """轮询等待 pred() 为真，超时返回 False。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------
# 结果收集器（0/1/2 退出码约定）
# --------------------------------------------------------------------------

class Checker:
    """累积 OK/FAIL/SKIP 并计算退出码。

    - 任何 FAIL → 退出码 1；
    - 无 FAIL 但有 SKIP（依赖缺失/能力未就绪）→ 退出码 2（绝不假装通过）；
    - 全部实跑且无 FAIL → 退出码 0。
    """

    def __init__(self, title: str = ""):
        self.title = title
        self.ok: list[str] = []
        self.fail: list[str] = []
        self.skips: list[str] = []

    def check(self, cond: bool, msg: str) -> bool:
        (self.ok if cond else self.fail).append(msg)
        print(("  [OK]   " if cond else "  [FAIL] ") + msg)
        return cond

    def skip(self, msg: str) -> None:
        self.skips.append(msg)
        print("  [SKIP] " + msg)

    def section(self, name: str) -> None:
        print(f"\n--- {name} ---")

    @property
    def ran_any(self) -> bool:
        return bool(self.ok or self.fail)

    def exit_code(self) -> int:
        if self.fail:
            return 1
        if self.skips:
            return 2  # 存在未验证用例：显式 SKIP，不假装通过
        return 0

    def report(self) -> int:
        print(f"\n{self.title}：OK {len(self.ok)} 项，FAIL {len(self.fail)} 项"
              + (f"，SKIP {len(self.skips)} 项" if self.skips else ""))
        for m in self.fail:
            print("  X " + m)
        for m in self.skips:
            print("  - " + m)
        rc = self.exit_code()
        print("=== 结果：" + ("通过" if rc == 0 else
                             "失败" if rc == 1 else "SKIP（依赖缺失）") + " ===")
        return rc


# --------------------------------------------------------------------------
# 临时工作区
# --------------------------------------------------------------------------

class WorkSpace:
    """自动清理的临时工作区：默认 layout：

    tmp/
      payload/            # 做种端数据
      seed_cache/         # 做种端缓存（可选）
      cache/              # SessionManager 缓存目录
    """

    def __init__(self, prefix: str = "mv_mgr_"):
        self.root = tempfile.mkdtemp(prefix=prefix)
        self.payload = os.path.join(self.root, "payload")
        self.cache = os.path.join(self.root, "cache")

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "WorkSpace":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()


def sweep_stale_temp_dirs(prefix: str = "mv_", max_age_hours: int = 24) -> int:
    """清扫历史泄漏的测试临时目录（P2-22 根因收口，2026-10-12）。

    背景：约 20 个套件用裸 tempfile.mkdtemp(prefix="mv_*") 建临时目录，
    清理只写在各文件主流程尾部——FAIL/SKIP 提前退出的路径来不及清，
    实测一次全量回归可净增十余个（曾累积 100 个 / 238MB）。

    修法取舍：逐散点补 try/finally 侵入面太大且易漏，故在运行器入口做
    守卫式清扫——只删「目录名以 prefix 开头且 mtime 早于 max_age_hours」
    的目录，绝不触碰在跑套件（其目录都是刚建的）。返回删除数；
    单项失败忽略不阻断（清扫是卫生措施，不是测试前置条件）。
    """
    import time

    root = tempfile.gettempdir()
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    try:
        entries = os.listdir(root)
    except OSError:
        return 0
    for name in entries:
        if not name.startswith(prefix):
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        try:
            if os.path.getmtime(path) > cutoff:
                continue
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                removed += 1
        except OSError:
            continue
    return removed