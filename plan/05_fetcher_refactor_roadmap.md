# magnet-viewer 后续开发计划：fetcher 重构阶段 3~6 + 重构后路线

> 生成：2026-09-07 · 基线：分支 `refactor-fetcher` @ `836272a`（阶段 0/1/2 已完成，未推送）
> 读者假设：对本仓库零上下文。所有行号以基线 commit 为准，动手前先 `git log -1` 确认一致。

## Goal

把 1308 行的 `core/fetcher.py` 巨类按既定七阶段路线拆完（阶段 3 registry → 4 taskops → 5 resolver+preview → 6 收口），全程契约冻结 + 专项测试护航、串行推进、本地提交不推送；重构收口后清偿 push 欠账与 P2 性能/工程欠账。

## Current context / assumptions

- **工作流硬约束（用户定的，违背即错误）**：
  1. 全部工作本地完成，七阶段全绿后才 push；
  2. 每阶段自检模式 = 新专项测试（假依赖、秒级）+ `contract_check.py` 扩项 + 全量回归全绿 + 本地 commit + 更新 `.workbuddy/memory/` 日志；
  3. 测试退出码约定 0=PASS / 1=FAIL / 2=SKIP；「跳过≠通过」。
- **既有模式（照抄，不要发明新写法）**：阶段 1 的 `core/persist.py`（`PersistDeps` dataclass 注入 getter/setter）与阶段 2 的 `core/session.py`（`SessionDeps` + `SessionCore`）。新模块一律：不 import `core.fetcher`、宿主成员经 `*_get`/`*_set` 回调取、失败就地 `log_warning`/`log_exception`、纯函数提到模块级。
- **回归基线（动第一刀前必须复现）**：`python regression_run.py` → 11 套全绿（qt SKIP）约 2m45s；`python contract_check.py` → `OK 103 / FAIL 0`。
- 环境：`C:\Users\Administrator\Desktop\magnet-viewer`，venv 在 `.venv`，命令一律用 `.venv/Scripts/python`（git-bash）。
- 上轮结构体检结论（已验证，直接采信）：依赖图零环；`TaskRecord` 被 persist（`record_cls` 注入）与 persist_test/session_test 直接 import；UI 破封装只剩 `main_window.py:240` 一处直写 `_metadata_timeout`；R-1 锁缺陷在 `fetcher.py` `_emit_error`（约 1303 行）。

## Architecture / proposed approach

延续「Facade + 依赖注入 + 单锁」：阶段 3 抽出 `core/registry.py`——**锁归 registry 所有**（现状 fetcher 内 31 处 `with self._lock` 逐步收敛），注册表数据（`_torrents`、`_current_ih`、当前别名组 `_handle/_result/_resolving/_resolve_started/_gen`）与 `TaskRecord` 一并迁入；persist/session 的注入回调全部改指 registry。阶段 4 抽任务 CRUD（`taskops.py`，允许 import registry/persist——方向定死永不反向），阶段 5 抽解析与预览桥（`resolver.py` + `preview.py` 同 commit），阶段 6 收口文档与 R-4。每阶段 TDD：**先写专项测试（红）→ 搬迁（绿）→ contract 扩项 → 全量回归 → commit**。

模块依赖终态（阶段 5 完成后）：

```
states ← TaskRecord(registry) ← registry ← persist/session/taskops/resolver/preview ← fetcher(Facade ≤350行)
ui/* → 只经 fetcher 公开面（+models/config 等纯模块）
```

---

## 阶段 3 — `core/registry.py`（含 R-1、R-2 修复）

**搬什么**（fetcher 现状行号 → registry）：
| 成员/方法 | fetcher 行号 | 去向 |
|---|---|---|
| `TaskRecord` dataclass | 58-73 | `core/registry.py`（R-2），fetcher 薄再导出 |
| `_hash_key` / `_ih_from_params` | 1057-1088 | 模块级纯函数 `hash_key()` / `ih_from_params()`（去 staticmethod） |
| `_torrents` `_current_ih` `_handle` `_result` `_resolving` `_resolve_started` `_gen` + `_lock` 本体 | __init__ 107-124 | `TaskRegistry` 实例字段 |
| `_current_record` / `_find_record` / `_put_record` / `_register_current` / `_detach_record` / `_preview_dir` / `protected_dirs` | 1090-1113, 302-384, 213-225 | `TaskRegistry` 方法（持锁版） |
| `_begin_resolve` / `_focus_existing_download` 的注册表部分 | 264-300, 365-384 | registry 提供原语，编排留 fetcher |
| `_clear_resolving` / `_clear_runtime_state` | 234-248 | registry 方法 |
| `_emit_error` 的裸写 `_resolving`（R-1） | 1303 | `registry.clear_resolving_safe()`（内部 `with self._lock`） |
| 常量 `DOWNLOADS_SUBDIR` / `PREVIEW_SUBDIR` | 54-55 | registry 模块常量，fetcher 再导出 |

**设计决策（实现者不必再猜）**：
- registry 暴露「受控访问原语」而非裸字典：`reg.locked()`（contextmanager 持可重入?——**不**，保持 `threading.Lock` 非可重入，用「一次调用一个锁段」风格：每个公开方法自带锁段，需要复合读写的公开方法内部自持锁，绝不嵌套调用另一个持锁方法——沿用 persist review 第 2 条「锁内快照、出锁干活」教训）。
- **锁字段改名 `_lock` → `lock`（registry 公开属性）**，persist/session 的 `lock=d.lock` 接线在 fetcher 组装处一行改完。理由：锁归 registry 后别的模块持它是常态，不再是家丑。
- 「当前别名组」提供 `reg.current()`（返回快照元组）与 `reg.set_current(ih)`、`reg.bump_gen()`；`_handle` 兼容属性仍留在 fetcher（contract [3] 冻结了它，UI/测试直访）——fetcher 的 `ses_set` 同款思路：别名 getter/setter 代理到 registry。

### 任务 3.1 复现基线（1 分钟）
```bash
cd /c/Users/Administrator/Desktop/magnet-viewer
.venv/Scripts/python regression_run.py
# 期望末行: === 回归全绿（契约未破）=== （qt SKIP 允许）
```
不一致就停下，先查环境再动刀。

### 任务 3.2 写 `registry_test.py` 失败骨架（TDD 红）
新建 `registry_test.py`，仿 `persist_test.py` 头部（try-import → `sys.exit(2)`、`ts.Checker`、`IH="a"*40`）。段落与关键断言（**此时 import core.registry 直接 ModuleNotFoundError = 预期的红**）：
- §A `hash_key`：合法 40/64 hex 小写归一；非法句柄（`info_hash()` 抛）→ `tmp-<id>`；大写 hex → 小写。
- §B `ih_from_params`：全零 40 hex → None（复刻 fetcher:1080 语义）。
- §C `put_record` 让位：同 ih 旧句柄不同对象 + ses 非空 → 调 `remove_torrent(old, 1)`；句柄比较抛异常 → 按不替换（复刻 fetcher:335 静默分支，**测试里给它补日志断言**：改用 `capsys` 不便，直接断言行为不变即可，日志在 3.5 补）。
- §D 焦点：`set_current` 后 `current()` 返回记录；`bump_gen` 单调。
- §E `find_record`：主匹配 + 临时键兜底按句柄身份匹配（复刻 fetcher:1101 双段逻辑）。
- §F `detach_record`：FakeHandle 断言 `pause()` 1 次、`set_flags(upload_mode)`、`unset_flags(auto_managed)` 各 1 次；handle None 安全跳过。
- §G **R-1 专项**：起 2 线程各调 1000 次 `clear_resolving_safe()` + `resolve_started()` 读——断言不崩且 `_resolving` 写全部经锁（用 `threading.Lock` 探针：替换 lock 为记账 Lock 子类，断言 write 发生在 acquired 区间内）。
- §H 委托接线：`SessionManager._registry` 存在且 `mgr._put_record` 等薄委托打到 spy（仿 persist_test §F 换 spy 手法）。

### 任务 3.3 实现 `core/registry.py`（绿）
新建文件，结构照 `core/session.py` 的文档头风格。要点代码：
```python
@dataclass
class TaskRecord:            # 从 fetcher 58-73 原样搬，字段序不动（contract 冻结）
    handle: lt.torrent_handle | None = None
    ...                      # 其余字段照抄

def hash_key(handle) -> str: ...
def ih_from_params(p) -> str | None: ...

class TaskRegistry:
    def __init__(self, cache_dir: str, download_dir: str,
                 ses_get, record_cls=TaskRecord):
        self.lock = threading.Lock()
        self.torrents: dict[str, TaskRecord] = {}
        self.current_ih: str | None = None
        self.gen = 0
        self.metadata_timeout = METADATA_TIMEOUT   # 见 3.4 注
        ...
    def put_record(self, ih, rec, make_current=False): ...   # 自持锁段
    def find_record(self, handle): ...
    def current(self): ...          # 锁内返回 (rec, handle, result, resolving, started, gen) 快照
    def set_current(self, ih): ...
    def bump_gen(self) -> int: ...
    def detach_record(self, rec): ...
    def register_current(self, handle, result, gen): ...
    def clear_resolving(self): ...        # 持锁（R-1 根治点）
    def clear_runtime_state(self): ...
    def protected_dirs(self) -> set[str]: ...
    def preview_dir(self, ih) -> str: ... # tmp-/空 ih 平铺兜底，照抄 fetcher:317-322
```
搬移纪律：**函数体逐行照搬，只改 `self._xxx` → registry 字段名，不改任何逻辑、不顺手修 bug**（行为差异只允许 R-1 一处，它是本阶段立项目标）。

### 任务 3.4 fetcher 改宿主 + 再导出
- `__init__`：删 7 个注册表成员，构造 `self._registry = TaskRegistry(...)`；`_metadata_timeout`/`_handle` 等 contract 冻结的兼容成员用 property 代理到 registry（`_metadata_timeout` 必须**可写**——`main_window.py:240` 直写它，property 配 setter，setter 加锁）。
- persist/session 组装：`lock=self._registry.lock`、`torrents_get=...`、`find_record=self._registry.find_record`、`hash_key=registry.hash_key`、`current_record=...`、`clear_resolving=self._registry.clear_resolving`、`clear_runtime_state=self._registry.clear_runtime_state`。
- 旧的 12 个方法降级薄委托（同 persist 惯例，docstring 写「实现见 core.registry」）；`_emit_error` 删裸写行改调 `self._registry.clear_resolving()`——注意 `_emit_error` 自身**不得**再持锁（调用点有的已在锁外，如 session 看门狗；registry 内部短锁段无冲突）。
- 再导出：`from .registry import TaskRecord, TaskRegistry, hash_key, ...`；`DOWNLOADS_SUBDIR/PREVIEW_SUBDIR` 再导出。
- **`TaskRecord` 注解里 `lt.torrent_handle`**：registry 自己 `import libtorrent as lt`，不再蹭 fetcher。
验证：`.venv/Scripts/python registry_test.py; echo $?` → `EXIT=0`，OK 数 ≥ 45。

### 任务 3.5 静默分支补日志（体检发现的 fetcher:335/1066/1080 三处，registry 内落地）
`hash_key`/`ih_from_params` 的 `except Exception` 补 `log_warning("registry.hash_key", ...)`（低频：仅失效句柄触发，不刷屏）。行为不变，registry_test §A/§B 重跑确认仍绿。

### 任务 3.6 扩 contract + 回归 + commit
- `contract_check.py`：`sig_check("registry", registry, {...})` 纯函数 + `TaskRegistry` 方法签名 + `check(hasattr(fetcher_mod,"TaskRecord"))` 再导出冻结。目标：OK 103 → **≥115**。
- `regression_run.py` SUITES 第 4 位插 `"registry_test"`（11 → 12 套）。
- `.venv/Scripts/python regression_run.py` → 12 套全绿（download_mgr 的 §6 重启续传是本阶段主判据之一）。
- 更新 `.workbuddy/memory/2026-09-07.md`（阶段 3 段：行数变化 fetcher 1308→约 1050、contract 数、踩坑）。
- `git add` 全部 → commit `重构 fetcher 阶段 3：抽 core/registry.py（锁与注册表归属）+ R-1/R-2`。

## 阶段 4 — `core/taskops.py`（任务 CRUD，约 450 行）

**搬什么**：`add_task`(498)/`_add_magnet_task`(548)/`_add_torrent_file_task`(585)/`_convert_to_download`(614)/`_activate_download`(655)/`set_priority`(693)/`pause_task`(724)/`resume_task`(746)/`remove_task`(771)/`_delete_task_files`(814)/`focus_task`(831)/`tasks()`(851)/`_lt_priority`(686)/`_task_dir`(540)。**`connect_peer` 不搬**（它属解析域，留阶段 5）。

**设计**：`TaskOps` 构造函数收 `Registry`、`TaskPersistence`、`ses_get`、`scheduler_get`、`emit` 回调（这次**直接 import registry/persist**——它们是无环下游，SessionDeps 那套全回调风格只对「会被重绑的宿主状态」有意义，服务对象引用一次注入即可）。`_tasks`（任务清单镜像）归属定死：**registry 兼管**（它和 `_torrents` 在同 20 个锁段里成对读写，拆开必死锁）——阶段 3 的 TaskRegistry 预留 `tasks` 字段 + `tasks_get/set`，本阶段直接用。
- **R-3 落地**：`tasks()` 改「锁内快照 `[(key, dict(t), rec_snapshot_fields)]` → 出锁循环 `handle.status()` 派生 progress/eta」。字段名与顺序一个不许变（downloads_pane 按 dict 键渲染）。

TDD：先写 `taskops_test.py`（假句柄+假注册）覆盖：重复 ih 添加拒绝、`add_task` 磁力/torrent 两入口的 atp 字段、pause 撤 auto_managed（P1-9 语义）、remove 的 delete_files 0/1 两分支、`_convert_to_download` 后清单 upsert、`tasks()` 派生字段计算（total=0 → progress 0.0；rate>0 且有剩余 → eta）。红→搬→绿→contract 扩项（`TaskOps` 12 方法）→ 12 套回归（**download_mgr_test 80 项就是这层的端到端验收，全绿是硬标准**）→ 更新记忆日志 → commit。

## 阶段 5 — `core/resolver.py` + `core/preview.py`（同一 commit）

**resolver** 搬：`resolve`(250)/`_begin_resolve` 编排壳/`_resolve_torrent_file`(386)/`_resolve_magnet`(464)/`connect_peer`(428)/`_result_from_torrent_info`(1262)/`_on_metadata_received`(1185)/`_on_download_finished`(1160)。
**preview** 搬：`start_preview`(898)/`stop_preview`(906)/`_find_record_for_path`(911)/`piece_map_for_path`(931)/`demand_for_path`(953)/`have_piece`(986)/`piece_length`(983)/`status`(1012)/`current_result`(1051)。
**合并理由（已定案勿再拆）**：`_on_metadata_received` 同时写注册表（经 registry 原语）与刷新预览映射，拆开要做两次桥接。
gen 代次防竞态语义原样保留（`gen != reg.gen → 自弃`）；`_result_from_torrent_info` 提为模块级纯函数（persist 的 `result_from_torrent_info` 回调改指它，删 persist 对 fetcher 的这最后一根注入线）。
TDD：`resolver_test.py`（假 ses add_torrent 抛 → emit_error 恰好一条且 gen 自弃分支不发射；重复解析快路径：`torrent_file()` 非 None → 直接 `_on_metadata_received`）+ `preview_test.py`（`_find_record_for_path` 前缀匹配含反斜杠归一、无 rec → None「绝不降级全量」）。回归判据：local_magnet/local_torrent/single_file/moov/gui_feature 五套全绿。contract：resolver/preview 签名 + `_result_from_torrent_info` 再导出冻结。commit。

## 阶段 6 — 收口

1. fetcher ≤350 行纯 Facade：只剩 `__init__` 组装 + 薄委托 + 兼容 property。
2. **R-4**：SessionManager 加 `apply_metadata_timeout(sec: float)`（锁内写 registry），`ui/main_window.py:240` 改调它；contract [3] 里摘掉 `_metadata_timeout` 直访豁免（**只摘这一项**，其余 8 项兼容属性保留——决策不变）。
3. 文档同步：README 项目结构（新 6 模块）、README:123「纯函数测试缺失」段改为现状（5 个专项已存在）、REVIEW.md §五 静默 except 42 → 实测值（体检数据：搬完预计全项目 <10）、`contract_snapshot.md` 第 9 节标「已兑现」。
4. 全量回归 + commit。

## 阶段 6.5 — 重构后第一批（顺序固定）

1. **push `refactor-fetcher` + 开 PR**（R13 欠账；先 `git remote -v` 确认 HTTPS——SSH 在这台机挂死）。push 后跑一次 GitHub Actions 确认 CI 绿（CI 无 GUI 会话，qt SKIP 属预期）。
2. **P2-1 批量位图**：`core/scheduler.py` 与 `fetcher.status()` 的 `have_piece` 循环改 `handle.status().pieces` 位图一次取回；验收 = 新专项断言位图路径 + moov/qt 流测不回归。
3. `requirements.lock`（pip-compile，锁 libtorrent/PySide6 精确版本）+ coverage.py 接入 `regression_run.py`（`--cov=core`，先只报告不设门禁）。

## Tests / validation（总览）

- 每阶段 TDD 循环：专项测试红 → 搬迁绿 → contract 扩项 → `regression_run.py` 全绿 → 记忆日志 → commit。跳过任何一步视为阶段未完成。
- 全程不许改 25 个公开方法签名与 3 个 property（contract 会当场红）；行为差异只允许 R-1/R-3/R-4 三处已立项的。
- 每阶段完成定义里都含 `download_mgr_test`（118s，端到端主判据）与 `persist_test`/`session_test`（注入线改接口的第一时间受害者）。

## Risks, tradeoffs, and open questions

| 风险 | 缓解 |
|---|---|
| 阶段 3 把锁搬走后，persist/session 注入线大改，回归出现时序类假绿/假红 | 先只改接线的最小 commit 跑两遍全量回归；registry_test §G 双线程探针专门盯 R-1 改动 |
| `tasks()` 出锁快照（R-3）引入状态不一致（快照后清单被并发改） | 快照拷贝 dict 即可（现有代码本就 `dict(self._tasks[key])` 拷贝）；download_mgr §8 双任务并发是哨兵 |
| `_metadata_timeout` property 化后 `main_window:240` 直写行为变化（property setter vs 裸赋值） | 阶段 6 才收编为方法；阶段 3 property 带 setter，行为逐字节等价，session_test §B1（start 后宿主值生效）护航 |
| 每阶段 2~3 分钟回归 × 4 阶段 + 专项编写，单人总投入约 2~3 天 | 刀口按现成分段（无一个方法被劈开搬）；阶段 4/5 彼此独立，卡壳可互换顺序 |
| Open question：`_tasks` 归 registry 是偏离原 7 步计划的微调（原计划 registry 只管 `_torrents`） | 依据是实测 20 个成对锁段；若实现中发现别扭，降级方案 = registry 持锁、`_tasks` 留 fetcher 经回调——阶段 3.3 动手前定案即可 |
