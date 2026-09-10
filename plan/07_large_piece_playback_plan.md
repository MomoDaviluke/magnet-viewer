# magnet-viewer 实施计划 07：大块种子的播放缓存体验优化

> 生成：2026-09-10 · 基线：分支 `copilot-fixes` @ `609287c`（plan/06 已收口并推送）
> 触发：用户真机实测 4.1GB / 4MB 块种子 → 缓冲 0.0% 恒不变、30s 不见画面、UI 卡顿。

## 现象与实测证据（本计划的事实基础）

真机现场（用户截图 + 日志 + 磁盘）：
- 单文件 mp4 4.1GB，`缓存 4.1 GB / 2.0 GB`，`速度 2.4 MB/s`，已下 1.4%（59MB），**缓冲 0.0%**，状态文案恒为"等待数据与索引块就绪"。
- `.preview/<ih>/` 下 mp4 已按全尺寸预分配；用户配置 `download_rate_limit=0`（无客户端限速）。

复现实验（本机真链路，300MB / 4MB 块，做种端限速模拟慢链路）：
| 实验 | 观察 |
|---|---|
| 现状策略 A | 块分布**散乱**（t=4s 时已下块为 3/10/22/31/32/34/66，头 3 块一个未下）；头连续 ≥1 块耗时 **14.0s** |
| 无 deadline 对照 | 同样散乱、同样在 25MB 附近停滞 → 病根不是 deadline 机制本身 |
| 关闭客户端限速 | 300MB 4 秒下完（26–44MB/s）→ 客户端 `local_download_rate_limit` 与大块交互会硬停滞（libtorrent 层面，另记） |
| 修复方向 B（窗口按字节 16MB + deadline 递增保序 + 尾窗排后） | 头 1 块就绪 **9.0s**（-36%），t=13s 已连续 8MB |

## 根因

1. **窗口按"块数"定，与块尺寸脱钩**：`LOOKAHEAD_PIECES=60` 在 16KB 块下 = 960KB（合理），在 4MB 块下 = **240MB 全 ASAP**（300MB 文件的 80%），libtorrent 在大量同级"紧急"块里按可用性散抓 → 文件头/播放位置**迟迟凑不出连续前缀**。
2. **尾部索引窗口过大且与播放窗口同优先级**：`tail_bytes = min(size, max(4MB, 1%·size), 64MB)`，4.1GB 文件 → 44MB（11 块）必须**整窗就绪**才开门控，与头部窗口抢带宽。
3. **门控条件对慢链路过于苛刻**：需"头连续 ≥ max(1MB, 1 块) 且 整尾窗齐"；在 2.4MB/s 下仅尾窗就 ≈18s。
4. 附带观感问题：状态栏"缓存 4.1GB/2.0GB"按**预分配尺寸**统计（稠密稀疏文件恒等于文件大小），既不反映真实下载量，也让人误以为缓存爆了。

## 设计决策（已定）

- **窗口按字节定**：新增 `LOOKAHEAD_BYTES = 16MB`，块数 = `clamp(ceil(LOOKAHEAD_BYTES / piece_length), 4, 64)`；`LOOKAHEAD_PIECES` 保留为上限常量（契约冻结项仍在）。
- **保序**：窗口内 `set_piece_deadline(p, i * DEADLINE_STEP_MS)` 递增（默认 400ms）——**从播放位置起顺序**取块，保证连续前缀先到齐；`request_range` 点播保持"临时插队"语义但同样按序递增，不再全 0。
- **尾窗只下"够用"的**：先用小探针块（最末 2MB）定位 moov，再只预约 moov 覆盖的块；探不到（非尾部 moov/未知格式）才回退到现有按比例窗口。尾窗 deadline 一律**排在播放窗口之后**。
- **门控放宽**：`contig ≥ min(1MB, size)` **且** moov 覆盖块就绪即可开播；门控文案区分"等数据"与"等索引"。
- **统计口径**：缓存占用显示改用**已下载字节**（`file_progress` 汇总），不再用目录分配尺寸。

## 硬约束（沿用）

1. 串行阶段、TDD 红→绿、每阶段 `contract_check` 扩项 + `regression_run` 全绿 + pathspec commit（工作区 4 个已暂存发布文件严禁卷入）+ `.workbuddy/memory/` 日志。
2. 真链路验收：本计划的验收器 = `probe_ab_diag.py`（A/B 对比），必须把 B 组指标固化成常驻断言。
3. 只动任务区域；core 零 Qt；并发三律。

---

## 阶段 1 — 调度窗口与保序（核心）
- `core/scheduler.py`：`begin()`/`tick()`/`request_range()`/`seek_to_byte()` 全部改为"按字节换算块数 + 递增 deadline"；点播保持上限但同样递增。
- 验收：`probe_ab_diag.py A|B` 的 B 组指标（头 1 块 ≤ 10s @1MB/s 慢链路）固化为回归断言（新建 `playback_window_test.py`，真链路 + 假句柄双覆盖）。
- contract：`scheduler` 新常量与签名指纹。

## 阶段 2 — moov 定位与尾窗收敛
- 新增 moov 探针（读最末 N KB 解析 atom 头，定位 `moov` 偏移与长度）→ 只预约覆盖块；搜不到则回退现有比例窗口。
- 尾窗 deadline 排在播放窗口之后；门控改为"moov 块就绪"。
- 验收：moov 尾部/非尾部两种构造（moov_stream_test 已有 fixture）全绿；4MB 块场景尾窗预约块数 ≤ moov 覆盖块数 + 1。

## 阶段 3 — 统计口径与文案
- 缓存占用显示改用已下载字节；门控文案区分"等数据"/"等索引"。
- 验收：gui_feature_test 断言口径函数；真机观感由用户确认。

## 阶段 4 — 收口
- README（大块种子行为说明）、`contract_check`、全量回归、`.workbuddy/memory` 收口日志、本地 commit（不推送，等用户真机确认后统一推）。

## 另记（不在本计划，避免范围蔓延）
- **客户端 `local_download_rate_limit` + 大块 → 硬停滞**：实测 8MB/s 限速下 300MB 停在下 25MB（无告警、双方静默）；无限速则 4s 下完。疑似 libtorrent 限速器与本地通道/大块交互缺陷。用户当前未设限速，故不作为本次目标，但要写进 README 已知问题并复现记录（`probe_alerts_diag.py` 可复现）。
- 种子级"多文件只缓存播放文件"已有语义（plan/06 selected 钉死），不改。
