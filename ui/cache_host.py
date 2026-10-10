"""缓存治理协作者（Wave 2 拆分自 ui/main_window.py，见 ARCHITECTURE.md §2.3）。

为什么单独成模块：缓存目录清理（手动/退出共用执行段）、活任务保护名单、
LRU 配额触发、状态栏「缓存占用」刷新是同一内聚簇——它们只经 SessionManager
facade（§2.2 白名单：clear_cache_contents/guard_ok_for_cleanup/
enforce_preview_cache_limit/preview_downloaded_bytes/protected_dirs）与
cfg/status_panel/cache_dir 交互，不碰任何视图组装逻辑。留在 MainWindow 里
只是「上帝对象」症状的一部分；抽出来后清理/配额语义（D8/D9/C1/D0 决策）
集中一处，回归测试的白盒断言也有了唯一归属点。

宿主耦合约定（行为零变化）：本类**不快照** cache_dir/cfg/session——一律经
``self._win`` 晚绑定读取，因为白盒测试（gui_feature [4b-3]/[4b-4]）会在
运行中重绑 ``w.cache_dir`` 验证清理/配额路径；快照会把测试变成假通过。
``clear_now`` 调 ``win._stop_preview``（晚绑定）同理：测试临时替换该槽位。
"""
from __future__ import annotations

import os

# §2.2 UI 依赖规则：只准 core.models / core.config / core.fetcher（facade）/
# core.logutil。清理守卫与内容清空经 facade 再导出取用，不越层直连
# core.cache_guard / core.cache_quota。
from core.fetcher import (PREVIEW_CACHE_CONVERT, clear_cache_contents,
                          guard_ok_for_cleanup)
from core.logutil import log_warning
from core.models import human_size


def _clear_preview_cache(cache_dir: str, keep_dirs=set()) -> int:
    """只清预览缓存内容，保留 downloads/ 与任务持久化文件。

    统一走 core.cache_guard.clear_cache_contents 的保留名单（决策 D8/D9）；
    返回删除的条目数。注意：设置对话框「立即清理」曾在此之后再无名单清空
    整个目录、误删用户下载数据（P0-1）——所有清理入口都必须经此函数。
    ``keep_dirs``（阶段 C C1 + plan/11 R-7）：活任务落盘目录的**静态快照**
    （退出清理路径）或**回调**（手动清理路径：每删一个顶层条目前重取名单，
    TOCTOU 逐条复核），连同内容整体跳过——convert 转正任务的目录仍在
    .preview/<ih> 下。
    """
    return clear_cache_contents(cache_dir, keep_dirs=keep_dirs)


def background_cache_text(mode: str, preview_file,
                          file_progress: list) -> str | None:
    """convert 档播放中的后台缓存进度文案（阶段 D D3）；其余场景 None。

    引擎在预览期就按 file-priority 全量下载整个文件（A0 实证），
    「缓冲 xx%」只反映播放位置前的连续窗口——用户看不到整文件其实
    在攒。补一行整文件进度注记；数据源用 status() 现成的
    file_progress 数组（零新增查询路径）。hold 档关预览即冻结、
    播放期同样在全下但转正语义不存在，文案维持基线不变。
    """
    if (mode != PREVIEW_CACHE_CONVERT or preview_file is None
            or not file_progress or preview_file.size <= 0):
        return None
    try:
        done = int(file_progress[preview_file.index])
    except (IndexError, TypeError, ValueError):
        return None
    pct = max(0, min(100.0, done * 100.0 / preview_file.size))
    return f"后台缓存完整文件：{pct:.1f}%（播放位置优先）"


def cache_usage_text(downloaded: int, limit_bytes: int) -> str:
    """状态栏「缓存占用」文案（纯函数，plan/07 阶段 3）。

    口径 = **已下载字节**（``downloaded_bytes(file_progress)``），不再是目录
    预分配尺寸：真机 4.1GB 稀疏文件才下 59MB 却显示「缓存 4.1 GB / 2.0 GB」，
    既误导又像爆缓存。``limit_bytes`` 为预览缓存上限（0/负 = 不限制）：
    不限制时仅在确有下载量时返回文案（避免常驻噪音），否则返回空串
    （调用方隐藏标签）；有上限时恒显示「已下载 / 上限」，0 字节也显示——
    让用户看到配额在生效。

    注意：**仅用于显示**——配额判定仍按目录占用（``dir_size_bytes``，
    管磁盘占用、需保守），两者口径不同。
    """
    if limit_bytes <= 0:
        return f"缓存 {human_size(downloaded)}" if downloaded > 0 else ""
    return f"缓存 {human_size(downloaded)} / {human_size(limit_bytes)}"


class CacheHost:
    """缓存治理协作者：清理入口 + LRU 配额 + 占用显示（宿主晚绑定）。"""

    def __init__(self, win):
        self._win = win

    # ---------- 清理入口（手动 / 退出共用） ----------

    def clear_now(self):
        """设置对话框「立即清理缓存」：先停预览，再只清预览缓存内容。

        决策 D8：downloads/（用户下载数据）与任务持久化文件（.tasks.json /
        .resume）不在清理范围；守卫校验不变（受管标记）。

        阶段 C C1（Important-2 收口）：convert 转正任务目录仍在 .preview/<ih>
        （沿用已下分块零重下）——清理前复核 session.protected_dirs()，活任务
        目录连同内容整体跳过，杜绝「清完引擎对着空目录重下」的互噬永动机。

        停机窗口守卫（I2）在 MainWindow._clear_cache_now 入口槽——本方法
        是它的执行体，不重复守卫（close_lag §G 的 AST 清点认入口槽）。

        清理走 ``win._clear_preview_cache_now``（宿主槽位晚绑定）而非本类
        方法直调：与拆分前调用图逐字一致——close_lag §I2 / gui_feature
        以**实例属性替换**该槽位拦截清理副作用，绕开槽位会让 spy 失明。
        """
        win = self._win
        win._stop_preview()
        # plan/11 R-7：传**回调**而非快照——删除期间活任务可能新增/转正，
        # cache_guard 会在每删一个顶层条目前重取名单（TOCTOU 逐条复核）。
        return win._clear_preview_cache_now(
            keep_dirs=win._live_cache_dirs,
            log_key="main.clear_cache_now")

    def live_dirs(self) -> set[str]:
        """活任务落盘目录快照（protected_dirs()）——**手动/退出清理入口**的
        C1 复核名单。

        会话异常（未启动/已停机竞态）兜底空集：清理退回基线行为，绝不
        因取名单失败而阻断清理主流程（旁路纪律，fail-open 有意为之）。
        注意：LRU 配额（enforce_quota）不走本方法——那条路必须
        fail-closed（名单故障→零删除），见 D0（审查 Important-1）。
        """
        try:
            return set(self._win.session.protected_dirs())
        except Exception as e:
            log_warning("main.live_cache_dirs", f"取活任务名单失败：{e}")
            return set()

    def clear_preview_cache_now(self, keep_dirs=set(),
                                log_key: str = "main.clear_cache") -> int:
        """守卫 + 清理的统一执行段（手动清理/退出清理共用，D8/D9/C1）。

        ``keep_dirs``：活任务目录快照（静态集合，退出清理路径在用）**或**
        回调（返回最新名单，plan/11 R-7 的 TOCTOU 逐条复核形态——手动清理
        路径在用）。返回删除条目数；守卫拒绝或清理异常返回 -1（只告警）。
        """
        cache_dir = self._win.cache_dir
        if not guard_ok_for_cleanup(cache_dir):
            log_warning(log_key, f"拒绝清理非受管缓存目录：{cache_dir}")
            return -1
        try:
            return _clear_preview_cache(cache_dir, keep_dirs=keep_dirs)
        except Exception as e:
            log_warning(log_key, f"清理失败：{e}")
            return -1

    # ---------- 预览缓存配额（P2-2） ----------

    def enforce_quota(self):
        """切换预览前执行缓存配额：超限按 LRU 清最旧的预览目录。

        只动 <cache>/.preview/<ih>/；保护名单来自 session.protected_dirs()
        （活跃句柄的落盘目录），downloads/ 与任务持久化文件天然不在
        扫描范围。limit<=0 表示用户关闭了配额。

        阶段 C C2：keep_dirs 传**零参回调**而非静态快照——cache_quota 在
        每个候选目录 rmtree 前重新取名单复核，堵住「取名单 → 磁盘扫描
        （出锁干活，耗时）→ 执行删除」窗口内刚登记的新 review 被陈旧
        快照漏保误删的竞态（回调内部自持 registry 锁，锁内零 libtorrent，
        符合并发三律；锁的持有粒度不变）。

        阶段 D D0（审查 Important-1）：回调**裸调** session.protected_dirs
        ——异常上抛给 cache_quota._norm_keep 的 None 分支→本轮保守不删
        （fail-closed 作为最后防线；protected_dirs 锁内纯读，实际不抛）。
        不再经 live_dirs 的空集兜底（fail-open）：那个兜底只服务
        「清理失败不阻断主流程」的手动清理入口。主要故障模式是空注册表
        竞态（会话未起/已停机）：该场景返回空集（cache 根仍受保），维持
        现状语义，不把空集当可疑。
        """
        win = self._win
        limit = int(win.cfg.get("cache_limit_mb") or 0)
        if limit <= 0:
            return
        preview_root = win.session.preview_root()
        if not os.path.isdir(preview_root):
            return
        cache_root = os.path.normcase(win.cache_dir)

        def _keep() -> set[str]:
            # D0 fail-closed：protected_dirs 裸调——异常透传给 cache_quota
            # 属最后防线（该方法锁内纯读，实际不抛；主要故障模式=空注册表
            # 竞态，返回空集、维持现状语义，见 D5-B）；每次解析现取（含
            # cache 根兜底保护，语义与原静态快照一致）。
            return ({os.path.normcase(p)
                     for p in win.session.protected_dirs()} | {cache_root})
        try:
            _total, _freed = win.session.enforce_preview_cache_limit(
                limit, _keep,
                warn=lambda m: log_warning("main.cache_quota", m))
        except Exception as e:
            log_warning("main.cache_quota", f"配额清理异常（已忽略）：{e}")
            return
        # 清理后刷新显示：口径 = 已下载字节（阶段 3），不再用返回的目录占用
        self.refresh_usage()

    # ---------- 缓存占用显示（plan/07 阶段 3） ----------

    def refresh_usage(self):
        """刷新状态栏缓存占用显示（低频：30s 定时器 + 配额清理后）。

        口径 = **已下载字节**（``status().file_progress`` 汇总，plan/07
        阶段 3）——显示「真实下了多少」而非稀疏预分配尺寸。配额**判定**仍
        走目录占用（``enforce_quota`` → ``enforce_preview_limit``），
        两者解耦：显示跟着进度走，上限管磁盘占用（保守）。
        """
        self._update_usage(self._downloaded_bytes())

    def _downloaded_bytes(self) -> int:
        """当前任务已下载字节（facade 口径；无会话/异常 → 0）。"""
        try:
            return self._win.session.preview_downloaded_bytes()
        except Exception as e:
            log_warning("main.cache_usage", f"{e}")
            return 0

    def _update_usage(self, downloaded: int):
        """渲染缓存占用文案（纯函数 ``cache_usage_text``：已下载 / 上限）。"""
        win = self._win
        limit = int(win.cfg.get("cache_limit_mb") or 0)
        win.status_panel.set_cache_usage(
            cache_usage_text(downloaded, limit * 1024 * 1024))
