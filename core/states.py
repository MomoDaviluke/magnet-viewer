"""任务生命周期常量层（叶子模块，不 import core 内任何东西）。

搬迁的背景：``SessionManager`` 拆分后，状态取值与 bootstrap tracker 列表同时
被 ``fetcher``（Facade）、``persist``（阶段 1）、以及后续的 ``registry`` /
``taskops`` 使用。若继续留在 fetcher，会出现 ``fetcher → persist → fetcher``
的循环导入，因此下沉到本模块。

兼容保证：``core.fetcher`` 以 ``from .states import ...`` 再导出，既有写法
``from core.fetcher import STATE_DOWNLOADING``（download_mgr_test 在用）
不受影响。
"""
from __future__ import annotations

# 任务生命周期状态机（对齐 plan/t1 §3 与 downloads_pane 的 STATE_META 命名）：
# QUEUED → META_FETCH → VALIDATE → DOWNLOADING ⇄ PAUSED → COMPLETED → STOPPED；
# FAILED / DELETED 为终态；默认完成后自动停止（不做种，决策 D3）。
STATE_QUEUED = "QUEUED"
STATE_META_FETCH = "META_FETCH"
STATE_VALIDATE = "VALIDATE"
STATE_DOWNLOADING = "DOWNLOADING"
STATE_PAUSED = "PAUSED"
STATE_COMPLETED = "COMPLETED"
STATE_STOPPED = "STOPPED"
STATE_FAILED = "FAILED"
STATE_SEEDING = "SEEDING"
STATE_DELETED = "DELETED"
STATE_READY = "READY"        # 内部态：仅查看清单/预览（review 记录专用，
                             # 不持久化、不进入 tasks() 快照）

DOWNLOAD_STATES = {STATE_QUEUED, STATE_META_FETCH, STATE_VALIDATE,
                   STATE_DOWNLOADING, STATE_PAUSED, STATE_COMPLETED,
                   STATE_STOPPED, STATE_FAILED, STATE_SEEDING, STATE_DELETED}

# 公共 tracker，提升冷门磁力链的 peer 发现率
BOOTSTRAP_TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.torrent.eu.org:451/announce",
]
