# magnet-viewer 实施计划 06：迅雷式"边播边缓存"机制

> 生成：2026-09-09 · 基线：分支 `copilot-fixes` @ `445ff9a`
> 读者假设：对本仓库零上下文。所有行号以基线为准，动手前先复现基线（阶段 0）。
> 背景来源：播放缓存机制三线探查报告（写入侧 / 读取侧 / 生命周期），2026-09-09。

## Goal

把预览路径从"临时看得见、关掉就冻结的分段窗口"升级为迅雷式体验：**播放时全文件后台缓存、播放位置插队优先、关预览自动转正为持久下载任务（fastresume + 任务清单 + 配额保护）、磁盘写失败有感知**。

## 关键事实（探查确证，实现者不必再猜）

1. **引擎其实已经在全文件下载**：`scheduler.begin()`（scheduler.py:116-121）把目标文件 `prioritize_files` 置 4、其余置 0、撤 upload_mode 并 resume。file priority>0 的文件会被 libtorrent 完整下载；`set_piece_deadline` 的 60 块 ASAP 窗口（LOOKAHEAD_PIECES，scheduler.py:131-161、212-237）只决定**顺序**，不限制**总量**。→ 所以不需要发明"全量缓存下载策略"，需要的是生命周期转正。阶段 0 仍须用真链路实证此结论（假句柄测不出真实落盘进度）。
2. **"停止预览"冻结一切**：`scheduler.stop()`（scheduler.py:245-263）清 deadline、全文件优先级归 0、`pause()` 句柄、撤 auto_managed。
3. **转正通道已存在（D10）**：`_convert_to_download_locked()`（taskops.py:215-255）沿用 `.preview/<ih>` save_path（:229），已落盘分块零重下；转正后自动获得 persist/fastresume/protected_dirs 全套。
4. **预览记录不写 fastresume**（persist.py:179-187 只认 `rec.download==True`）；转正即补齐。
5. 配额 `enforce_preview_limit`（cache_quota.py:81-125）只在 `_open_preview`（main_window.py:572）执行；画廊翻页路径（main_window.py:621-629）**绕过**配额。
6. `session.handle_alert`（session.py:258-280）**不处理** `storage_failed_alert`/`file_error_alert`——后台满速缓存最容易撞磁盘满，现在无感知，任务卡 DOWNLOADING。

## 架构 / 设计决策（已定，不再讨论）

- **两档不做三档**：新配置 `preview_cache_mode`，`"convert"`（默认，迅雷式：关预览自动转正）| `"hold"`（现状：停预览即暂停冻结）。原设想的"省流量档"**明确不做**——引擎没有廉价的"只要窗口内字节"模式，为省流量牺牲边播稳定性不划算。记入"否决项"。
- **调度器不碰 taskops**：保持零环依赖图（scheduler 是叶子层）。转正编排放 `SessionManager` Facade 层（fetcher.py），依既有模式：scheduler 新增 `stop(release_only: bool = False)` 可选参数——`release_only=True` 时只清 deadline/还原调度锚点，**不 pause、不清文件优先级**；是否转正由宿主在锁外决策。
- **转正复用 D10**，不新增持久化路径；转正后目录天然在 `protected_dirs()`（registry.py:227-235）名单内。
- **生效时机**（遵守"每个配置项显式声明生效时机"惯例）：`preview_cache_mode` = **下次开预览生效**（与 `cache_limit_mb` 同族），设置对话框须带此文案。
- **并发三律**：锁内不碰 libtorrent 慢调用；注册表写路径全走 registry 锁段；转正/停止编排先取无锁快照再出锁干活。

## 工作流硬约束（违背即错误）

1. 全部本地完成，**只 commit 不 push**；阶段串行，禁止并行推进多阶段。
2. 每阶段自检 = 新专项断言（假依赖、秒级）红→绿 + `contract_check.py` 扩项 + `python regression_run.py` 全绿 + 本地 commit + 更新 `.workbuddy/memory/` 当日日志。
3. 测试退出码 0=PASS / 1=FAIL / 2=SKIP；"跳过≠通过"。
4. **git 提交必须用 pathspec**：`git commit -m "msg" -- 文件1 文件2`。工作区有 4 个与本工作无关的已暂存文件（`.github/workflows/ci.yml`、`.github/workflows/release.yml`、`.gitignore`、`magnet-viewer-onefile.spec`），**严禁 `git add -A`、严禁 `git stash`、严禁 `git commit` 无 pathspec**，不得把这些改动混入任何提交。
5. 环境：`C:\Users\Administrator\Desktop\magnet-viewer`，命令一律 `.venv/Scripts/python`（git-bash）。
6. 新功能必须带测试（ARCH-08）；只改任务涉及的区域，不顺手修无关代码。

## 基线（动第一刀前必须复现）

```bash
cd /c/Users/Administrator/Desktop/magnet-viewer
.venv/Scripts/python regression_run.py     # 期望：12 套全绿（qt 允许 SKIP）
.venv/Scripts/python contract_check.py     # 期望：OK ≥126 / FAIL 0
```
### ⚠ 实测偏差（2026-09-09 本会话复核）
`contract_check` 实测 **OK 157 / FAIL 0**（高于文档基线 126，采信实测）。
`regression_run` 实为 **moov_stream_test 稳定失败**（连跑 3 次全挂，非偶发；A/B/C 过、D 段失败，ffmpeg 侧 -10053/End of file）——疑似 `445ff9a` 流服务 keep-alive 改动引入的真回归。
→ **新增阶段 Z：先修复基线，回归全绿前禁止动后续阶段。**

---

## 阶段 Z — 基线修复（第 0 批子代理，前置门禁）

- 根因定位 `moov_stream_test.py` D 段（拖动到未下载位置→期望可解码）在 `445ff9a` 后稳定失败：重点比对 keep-alive 改动（stream_server.py `protocol_version`/连接复用）与 -10053 的关系；对照 qt_stream_open_test（其 D 等效用例过）找差异。
- 最小修复 + 在该测试内补断言防回归；`regression_run.py` 12 套全绿才算收口；独立 commit（pathspec）。
- 若定性为"测试环境敏感（本机防火墙）而非产品缺陷"，须给出证据链（同一二进制/代码在干净条件复跑）并说明处置，不得静默放宽断言。

---

## 阶段 0+A — 前置修复 + 基线确证（第一批子代理）

### A0 真链路确证（不改产品代码）
用 `local_torrent_test.py` 的本地做种手法验证："预览一个大文件后，即使播放窗口停在头部，`file_progress` 是否持续增长超出 LOOKAHEAD 窗口"。结论写入 `.workbuddy/memory/` 日志与 scheduler.begin docstring 补一行注释。**若确证不成立（引擎实际只下窗口），停止后续阶段，回报——设计前提变了。**

### A1 demand_for_path 点播上限（preview.py:107-131）
- `preview.py` 加 `from .scheduler import LOOKAHEAD_PIECES`（方向合法：scheduler 不 import preview，零环保持）。
- `last = min(last, first + LOOKAHEAD_PIECES - 1)`，对齐 scheduler.py:156 语义与注释（点播是临时插队，不限会把剩余整文件刷 ASAP 反而拖慢 seek 目标）。
- 测试：`preview_test.py` 新增段——假句柄记录 `set_piece_deadline` 调用，断言单次 demand 覆盖块数 ≤ LOOKAHEAD_PIECES，且大区间只预约头部 60 块。

### A2 画廊切换触发配额（main_window.py:621-629）
- `_on_gallery_file` 在 `start_preview` 前调 `self._enforce_cache_quota()`（对齐 :572 视频路径）。
- 测试：`gui_feature_test.py` 新增断言——spy `_enforce_cache_quota`，画廊切未下载图片时调用计数 +1。

### A3 流服务协议一致性（stream_server.py）
- 416 响应（:163-168）补 `Content-Length: 0`（对齐 403/503 写法）；删除重复的 `protocol_version`（:76，保留 :67）。
- 测试：在现有流服务测试（moov_stream_test.py / qt_stream_open_test 的非 QT 路径可跑者）里对越界 Range 断言响应头含 `Content-Length: 0`。

### 收口
contract_check 扩项（preview 模块新导入常量引用、scheduler/stop 若本阶段未动则不加）；回归全绿；commit（pathspec）；日志。

---

## 阶段 B — 转正生命周期（第二批子代理）

- **配置**：config.py DEFAULTS 加 `preview_cache_mode`（默认 `"convert"`）；settings_dialog.py 缓存区加 QComboBox（`convert`=「关闭预览后继续缓存」/ `hold`=「关闭预览即暂停」）+ 「下次开启预览时生效」文案。
- **scheduler.stop(release_only=False)**：True 时 `clear_piece_deadlines()` 后**不**清文件优先级、**不** pause、**不**撤 auto_managed；`_play_from`/锚点状态清理照常。默认 False 保持现状（契约兼容：可选参数不破坏既有调用）。
- **Facade 编排**（fetcher.py `stop_preview` / `clear_preview` 路径）：读 mode → `scheduler.stop(release_only=(mode=="convert"))` → convert 档再走 D10 转正（复用 `taskops._convert_to_download_locked`；句柄已是 download 任务则只 release 不重复转正）。锁纪律：快照出锁。
- **状态语义**：转正走既有 persist（任务清单 + `request_resume` 立即写 fastresume）；下载面板应能立刻看到这条任务（回归 download_mgr_test 手法断言清单含之）。
- **测试**：taskops_test / session_test 加假句柄断言：convert 档 `pause()` 零调用 + 转正回调落地 + resume 文件生成；hold 档行为与基线逐字一致（防回归）。
- contract 扩项：`scheduler.stop` 新签名指纹、`SessionManager` 相关公开面若有变同步冻结。
- **阶段 B 审查回炉（2026-09-09）**：Critical-1——转正清单 selected 与实际下载集对齐（关预览转正=继续缓存正在预览的那一个文件，`_convert_to_download_locked` 新增可选参 `selected_files`，stop_preview 在 scheduler 存活期快照预览文件路径透传；resume/重启不再刷成全选）；Minor 批——锁段2 TOCTOU 复核（release 期间被 add_task 转正则跳过）、§I 强化（真 begin() fixture、正向 prioritized 断言、收尾交织序 unset(upload_mode)→set(auto_managed)→resume、selected 回归）、preview_test 消费 stop_release、config 生效时机措辞改「下次关闭预览时生效」。**settings UI 控件/README 文案已按 2026-09-09 用户指令裁至阶段 D。**

## 阶段 C — 配额与跨重启闭环（第三批子代理）

> **挂账（阶段 B 审查 Important-2）**：convert 转正目录仍在 `.preview/`——「立即清理缓存」/退出清理会把活转正任务文件删光（保留名单不含 `.preview` 活任务），阶段 C 必须处理：转正目录改名迁入 `downloads/` 或清理入口复核 `protected_dirs`。修复前已在 `_clear_cache_now` 与 `closeEvent` 清理路径加 interim 注释标注此已知风险（只加注释不改行为）。

- **竞态修复**：`_enforce_cache_quota`（main_window.py:633-656）与 `protected_dirs()` 快照之间窗口——删除前对每个候选目录**复核** keep_dirs（cache_quota 内部二次校验即可，锁纪律不变）。
- **转正后配额语义**：convert 生成的任务在句柄存活期受 protected_dirs 保护；用户删除该任务后目录恢复可被 LRU 回收——补一条回归断言。
- **跨重启测试**：模拟转正→shutdown(drain resume)→restore：同 hash 再预览命中原块（download_mgr_test §5 风格，fake 依赖，不真联网）。
- D9 守卫核对：转正任务目录名即 `<ih>`==任务键，"删除任务和文件"应放行——补断言。

## 阶段 D — 写盘失败感知 + 文案 + 文档（第四批子代理）

- `session.handle_alert` 处理 `storage_failed_alert` / `file_error_alert`：log_warning + 经 registry 锁路径把对应任务标记错误（复用 `_emit_error` 既有通路，R-1 纪律：旁路写入必须持锁）；UI 显示 FAILED 不再静默卡 DOWNLOADING。
- 预览器文案：convert 档播放中状态行显示「后台缓存完整文件：xx%（播放位置优先）」（数据源 `file_progress`/`buffer_progress`）。
- README.md + 设置对话框：三级生效表更新（`preview_cache_mode` 下次开预览生效）；`.workbuddy/memory` 收口日志。
- 全量 contract + 回归收绿；最终 commit。

---

## 验收标准（整体）

1. 双击预览 → 播放流畅性不倒退（moov_stream_test / qt 门控用例全绿）。
2. 关闭预览 → 下载面板出现该任务且继续下载；退出重启 → 任务恢复续传（`.resume` 有该 hash 文件）；转正任务暂停→恢复、重启→恢复后 selected 仍指向预览文件（不全选，审查 Critical-1 补断言）。
3. 设置切 `hold` → 行为与 `445ff9a` 基线逐字一致。
4. 画廊连刷大量图片不再绕过配额；磁盘满场景任务显式 FAILED 而非静默卡死。
5. `regression_run.py` 全绿、`contract_check.py` FAIL 0、无已暂存 4 文件被卷入提交。

## 风险与回滚

- A0 若证伪"全文件已在下载"→ 阶段 B 前必须改 begin 策略，本计划作废重议。
- convert 档默认值改变现有用户行为（关预览不再暂停）——RELEASE NOTE 单列一条；如反馈过激，改默认为 `"hold"` 是一行 config 的事。
- 每阶段独立 commit，任一阶段出问题回滚该 commit 即可，不跨阶段纠缠。
