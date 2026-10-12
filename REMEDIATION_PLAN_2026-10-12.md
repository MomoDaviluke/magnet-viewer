# magnet-viewer 工程治理与进度收敛解决方案

> 制定日期：2026-10-12（周一）  
> 依据：《PROGRESS_AUDIT_2026-10-11.md》+ 2026-10-12 当日代码库复验 + git 对象库实证  
> 方案性质：可执行、门控化、带回滚的四周收敛计划  
> 所有关键判断均附可复现验证命令；未经证实的结论不纳入本方案。

---

## 一、现状诊断

### 1.1 总体判断（一句话）

**功能面已基本完整，真正的瓶颈是工程治理分裂：事实主线 `arch/roadmap-2026q4` 领先工作树 `main` 120 个提交未合并，安卓端源码曾因分支切换从磁盘消失且仍未恢复。当前第一优先级不是写新功能，而是「收割已有资产 + 统一主干」。**

### 1.2 关键实证数据（2026-10-12 复验）

| 维度                | main（当前工作树）                                                                           | arch/roadmap-2026q4（事实主线）                                                                                              |
| ----------------- | ------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| 领先提交数             | —                                                                                     | **120**（`git rev-list --count main..arch/roadmap-2026q4`）                                                              |
| 版本控制文件数           | 47                                                                                    | **272**                                                                                                                |
| `core/` 模块        | 11 个 / 3,221 行                                                                        | **26 个**（含 cache_quota / exceptions / secretbox / proxycheck / metacache / pieceview / session / persist / resolver 等） |
| `core/fetcher.py` | 1,579 行（2 类 / 59 方法）                                                                  | 891 行（已拆 7 模块）                                                                                                         |
| 测试                | 根目录散落 8 套，`regression_run.py:26-35` 硬编码                                               | **tests/ 29 个文件**；CI 回归 16 套 + coverage 全量 27 套（roadmap ci.yml 注释口径）                                                   |
| CI 工作流            | 1（ci.yml）                                                                             | **3**（ci / android / release）                                                                                          |
| 依赖锁定              | 无（`requirements.txt` 仅 `>=` 约束）                                                       | **`requirements.lock`**                                                                                                |
| 覆盖率门禁             | 无                                                                                     | `coverage_run.py` + `--fail-under=70`（基线实测 81%）                                                                        |
| 打包 spec           | 无                                                                                     | `magnet-viewer.spec` + `magnet-viewer-onefile.spec` + `build.bat`                                                      |
| 文档                | README/REVIEW/audit/contract_snapshot                                                 | +`CHANGELOG.md` +`CONTRIBUTING.md` +`docs/` +`technical_roadmap.md` 等                                                  |
| 安卓源码              | **磁盘上不存在**（`android/app/src/main` 缺失，仅剩 21 个未跟踪文件：jks、keystore.properties、testrig 夹具） | 141 个文件受版本控制（95 个 `.kt`）                                                                                               |

### 1.3 核心问题根因分析

| #   | 问题                  | 现象与证据                                                                                                                                                                                                                                                      | 根因                                                         | 影响                                                       |
| --- | ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- | -------------------------------------------------------- |
| P-1 | **主线分裂**            | `git branch --merged main` 仅输出 main；5 个分支（roadmap/copilot-fixes/refactor-fetcher/release-onefile/settings-wiring）全部未合并                                                                                                                                     | 缺乏分支集成策略与合并门禁；roadmap 被当作"功能主线"长期游离                        | 120+ 提交成果不在主干；CI 只覆盖 main/PR，这些代码无 CI 保护；按 main 判断进度严重低估 |
| P-2 | **安卓源码丢失（P0）**      | `ls android/app/src/main` → No such file；`git status` 显示 `?? android/`                                                                                                                                                                                     | `android/` 在 main 上从未被跟踪，从 roadmap 切回 main 时 git 删除全部受跟踪文件 | 安卓端无法编译、无法继续开发；若工作树被清理则依赖 roadmap 分支单点恢复                 |
| P-3 | **签名私钥暴露风险（P0 安全）** | `android/magnetviewer-dev.jks` + 明文弱口令 `android/app/keystore.properties`（`storePassword=keyAlias=keyPassword=magnetviewer-dev`）处于未忽略、未跟踪状态                                                                                                                 | 随源码一并消失的 `android/.gitignore` 未恢复                          | 若直接 `git add android/` 会把私钥与明文口令提交入库，永久留在历史中             |
| P-4 | **功能缺口集中在"有接口缺入口"** | main 上：`grep Semaphore`=0（流服务无限流）；`apply_rate_limit`/`download_rate_limit` 在 core/ui 零命中（限速无公开 API）；无 `cache_quota.py`；`logging_enabled` 仅在 docstring 出现；`ui/main_window.py:224` 直写 `session._metadata_timeout`；`main_window.py:511-512` 静默 `except: pass` | 底层能力在 roadmap 已完成，但未合并；main 上的缺口多数是"未收割"而非"未实现"            | 产品侧入口缺失；UI 破坏 core 封装；故障静默                               |
| P-5 | **质量基建缺口**          | 无 pytest/pyproject/mypy 配置；无依赖锁定；无 hybrid_v2 两入口测试；README 自述"纯函数测试缺失"                                                                                                                                                                                      | 治理性工作从未进入 main                                             | 2.0/2.1 API 不兼容风险敞口；入口路径覆盖策略（README 自述的三个历史缺陷均源于此）执行不完整  |
| P-6 | **文档与产物脱节**         | `contract_snapshot.md` 自称"09-03 改造前基线"；README 项目结构未列 downloads_pane/taskstore 等 4 个新文件、仍称"7 套旧测试"（实际 8 套）；`dist/` 产物（exe 44,254,859B / apk 21,728,460B）均为 09-29 构建，对应 roadmap 状态                                                                           | 文档未随分支演进入同步流程                                              | 按 README 判断进度低估约 120 提交；从 main 重新打包 = 功能倒退的发布物           |

### 1.4 风险清单（按等级）

| 级别 | 风险                                               | 证据                             |
| -- | ------------------------------------------------ | ------------------------------ |
| P0 | 安卓源码未恢复 + 未入库                                    | §1.2 实证                        |
| P0 | 私钥/明文口令可能入库                                      | §1.3 P-3                       |
| P0 | 120 提交成果无 CI 覆盖、随时可能因工作树清理受损                     | §1.3 P-1/P-2                   |
| P1 | 88 处宽泛 `except Exception`（含 10 处 `except: pass`） | grep 计数（roadmap 已收窄至 26 处，未合并） |
| P1 | 开发签名（magnetviewer-dev.jks）不能用于正式分发               | 历史审查 R1，未解决                    |
| P2 | `live_test.py` 沙箱内不可用（BT/UDP 出站被屏蔽）              | README:89 自述                   |
| P2 | 测试临时目录泄漏（历史审查 P2-22，未复测）                         | —                              |

---

## 二、目标设定

**总目标：四周内完成「数据救援 → 主干统一 → 缺口收割 → 质量基建 → 清理发布」，使 main 成为唯一受 CI 保护的集成主线，全部功能缺口关闭，发布链路可复用。**

| 目标                    | 内容                                    | 可度量验收标准                                                                                                |
| --------------------- | ------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| **G1 数据与主线收敛**（第 1 周） | 安卓源码恢复并入库；main 快进至 roadmap；全量回归绿；推送远端 | 141 个安卓文件受版本控制且 0 私钥入库；`git rev-list --count main..roadmap`=0；27 套全绿；`git ls-remote origin main` 与本地一致 |
| **G2 缺口收割**（第 2 周）    | 限速/缓存上限/日志开关/流服务限流/封装违规/静默吞异常全部关闭     | 设置面板控件齐全且接线生效；`grep Semaphore core/` >0；`main_window.py` 无直写 `_` 内部字段；`except Exception: pass` =0      |
| **G3 质量基建**（第 3 周）    | 覆盖率门禁、依赖锁定、混合 v2 测试矩阵、文档同步落地主干        | CI 3 工作流全绿；`--fail-under=70` 生效；README 结构章节与磁盘一致；contract_snapshot 基线说明更新                              |
| **G4 清理与发布就绪**（第 4 周） | 技术债清理；正式签名决策；发布链路验证                   | fetcher 拆分/异常层级/secretbox 等随 ff 落地并验证；v1.0.0 打 tag 前检查表全过                                              |

---

## 三、具体措施（WBS 工作分解）

| 工作包         | 措施                                                                           | 对应问题       | 交付物                    | 验收标准                                                                                     |
| ----------- | ---------------------------------------------------------------------------- | ---------- | ---------------------- | ---------------------------------------------------------------------------------------- |
| **W1 安卓救援** | 从 roadmap 恢复 android/ 入库（先 .gitignore 后 add）                                 | P-2/P-3    | 含 141 文件的提交            | `git ls-files android \| wc -l`=141；`git ls-files android \| grep -cE "jks\|keystore"`=0 |
| **W2 主干统一** | main `--ff-only` 快进至 roadmap；全量回归；推送                                         | P-1        | 统一后的 main + origin 同步  | 回归 27 套绿 + 覆盖率 ≥70%；ls-remote 验证                                                         |
| **W3 功能收割** | ff 后逐项验证 S-2~S-6（多数已由 roadmap 实现）；补 UI 控件未覆盖项；修封装违规与静默吞异常                    | P-4        | 接线代码 + 测试              | 每项有测试断言；contract_check 基线同步                                                              |
| **W4 安卓验证** | 用 JDK+gradle 重编 APK；dex 字符串/R8 mapping 验证新代码入包                               | P-2        | app-release.apk + 验证记录 | APK 可安装；dex 含目标类名字符串                                                                     |
| **W5 质量基建** | 覆盖率门禁/依赖锁定/hybrid_v2 矩阵随 ff 落地；评估 mypy；同步 README/contract_snapshot/CHANGELOG | P-5/P-6    | 3 工作流 CI + 文档          | CI 全绿；文档与磁盘一一对应                                                                          |
| **W6 清理发布** | 技术债（长路径/死代码/异常层级）收尾；正式签名 keystore 决策；tag 发布                                  | §1.4 P1/P2 | 检查表 + Release          | 发布门禁（release_check.py）全过                                                                 |

---

## 四、实施步骤（五阶段门控）

> 每阶段设**退出准则（门禁）**&#x4E0E;**回滚点**；前一阶段未过门禁不得进入下一阶段。所有 git 写操作使用系统 git（`C:\Program Files\Git\cmd\git.exe`）——PATH 中的便携版 git 有 ref 写入静默丢失的实证前科。

### Phase 0 — 准备与备份（今日，约 30 分钟，只读+备份）

1. 记录当前基线：`git rev-parse HEAD`（e974d68）写入本方案附件；`sha256sum dist/*` 备份产物指纹。
2. 备份注册表配置（`restore_config.py` 已存在，21 键备份可回滚）。
3. 确认环境：系统 git 可用；`.venv/Scripts/python.exe -c "import libtorrent"` 通过（**禁用 managed Python 跑测试——缺 libtorrent 会 0.3s 假失败**）。
4. 确认远端网络可达（代理 502 时重试）。

**退出准则**：基线 SHA 与产物指纹已记录；环境三项确认通过。  
**回滚点**：本阶段无写入，无需回滚。

### Phase 1 — 安卓数据救援（D1~D2，约 1 小时）⚠️ 顺序不可颠倒

1. 恢复源码与忽略规则（一条命令同时写入工作树与索引）：
   ```bash
   git checkout arch/roadmap-2026q4 -- android/
   ```
2. **私钥双重检查**（提交前必须两次为空）：
   ```bash
   git status --porcelain android | grep -E "jks|keystore"   # 必须无输出
   git ls-files android | grep -E "jks|keystore"            # 必须无输出
   ```
   （roadmap 的 `android/.gitignore` 已含 `*.jks` / `*.keystore` / `keystore.properties` / `local.properties`，已实证。）
3. 提交：`git add android/ && git commit -m "恢复安卓端源码并纳入版本控制（141 文件，私钥经 .gitignore 排除）"`。
4. 验证：`git ls-files android | wc -l` = 141；`ls android/app/src/main` 存在。

**退出准则**：141 文件入库、0 私钥、提交可 `git show` 复查。  
**回滚点**：提交前 `git rm --cached -r android/` 撤销暂存；提交后 `git revert <sha>`。

### Phase 2 — 主干统一与全量回归（D2~D3，约半天）⭐ 关键路径

1. 确认快进可行性（已实证）：`git merge-base --is-ancestor main arch/roadmap-2026q4` 退出码 0；磁盘上 21 个未跟踪文件与 roadmap 跟踪的 141 个安卓文件**零重叠**，快进不会被未跟踪文件阻塞。
2. 快进：
   ```bash
   git merge --ff-only arch/roadmap-2026q4
   git show-ref main        # 立即验证 ref 落盘；未落盘则 printf '<sha>' > .git/refs/heads/main 兜底
   git rev-parse HEAD       # 应等于 roadmap 尖端
   ```
3. 预期变化（已在方案中声明，非异常）：根目录 8 个散落测试移入 `tests/`；文件数 47→272；`regression_run.py` 改为编排 16 套，`coverage_run.py full` 跑 27 套。
4. 全量回归（**必须 `.venv/Scripts/python.exe`**）：
   ```bash
   .venv/Scripts/python.exe regression_run.py        # 16 套，约 3~4 分钟
   .venv/Scripts/python.exe coverage_run.py full     # 27 套，约 4~5 分钟
   ```
5. 回归绿后推送：`git push origin main`，并用 `git ls-remote origin main` 与本地 HEAD 比对验证（不信任 `git status` 的 ahead 计数）。

**退出准则**：27 套全绿（退出码 0/2，无 1）；覆盖率 ≥70%；远端同步。  
**回滚点**：推送前 `git reset --hard <Phase 1 提交 SHA>`；推送后 `git revert` + force-push（需用户批准）。

### Phase 3 — 缺口收割与安卓编译验证（第 2 周）

1. **先重评范围**：ff 后 S-1（fetcher 拆分）、S-8（异常层级）、S-9（metacache/pieceview/proxycheck）、N-1（覆盖率门禁）、N-2（依赖锁定）、N-3（hybrid_v2）、N-5/N-6（spec/CI）、N-9（secretbox）**预计大部分自动闭合**——逐项用 contract_check 与 grep 复核后从待办中划销，剩余才是真实工作量。
2. 真实剩余项（按预判）：S-3 限速 UI 入口、S-4 缓存上限控件、S-2 日志开关接线、S-5 封装违规、S-6 若 roadmap 版限流需适配验证、S-10 静默吞异常。每项：实现→测试→回归，单独提交。
3. 安卓编译验证：
   ```bash
   JAVA_HOME=%LOCALAPPDATA%\Android\jdk-21\... gradle-9.1.0/bin/gradle.bat assembleRelease --no-build-cache
   ```
   （`--rerun-tasks` 被沙箱挡，`--no-build-cache` 即可绕过，勿误判编译错误；**不用 ./gradlew**。）
4. **APK 验证铁律**：字节数可能与上版完全相同——必须解 dex 查目标类字符串或查 R8 `mapping.txt`，不能看大小。

**退出准则**：§三 W3 各项有测试断言且回归绿；APK 构建成功且 dex 验证通过。  
**回滚点**：每功能点独立提交，逐项 revert。

### Phase 4 — 质量基建与文档同步（第 3 周）

1. CI 三工作流（ci/android/release）随 ff 落地后各跑通一次（GitHub Actions 实跑，非本地模拟）。
2. mypy 门禁评估（roadmap 未含，属新增成本）：先出基线报告再决定是否硬门禁——列入待决策 D-4。
3. 文档同步：README 项目结构章节（补 downloads_pane/add_download_dialog/taskstore/resume）、套件计数（16+27 口径）、contract_snapshot 基线说明、CHANGELOG 补 120 提交摘要。
4. 测试矩阵补齐复核：入口路径覆盖（本地种子/磁力链 × 单文件/多文件/混合 v2）逐格确认有对应用例。

**退出准则**：CI 三工作流全绿；文档与磁盘一一对应（抽查 10 个文件路径全部命中）。  
**回滚点**：文档类提交可整体 revert。

### Phase 5 — 清理与发布就绪（第 4 周）

1. 技术债收尾：Windows 260 字符路径、死代码（`current_result`、优先级提示分支）、测试临时目录泄漏复测。
2. 正式签名决策（待决策 D-3）：开发 jks 仅限开发；正式分发前必须换正式 keystore 并重走 release_check.py。
3. 发布演练：`git tag v1.0.0 && git push origin v1.0.0` 触发 release.yml，验证 exe + portable zip 挂 Release。

**退出准则**：release_check.py 全过；Release 产物可下载且 smoke 通过。  
**回滚点**：删 tag + 删 Release；产物问题不影响主干代码。

---

## 五、责任分工

> 依用户既定协作约定：子代理只承担相互独立的只读调研/验证，产出必须经主代理逐条复核后才采信（历史上有子代理 P1 结论被证伪的先例）。

| 角色          | 职责           | 具体分工                                                                                 |
| ----------- | ------------ | ------------------------------------------------------------------------------------ |
| **用户（PM）**  | 决策与验收，不执行命令  | 批准本方案与四个待决策点（§十）；每阶段门禁结果确认；正式签名策略拍板；最终验收                                             |
| **主代理**     | 全部执行与集成      | Phase 0~5 全部命令执行、提交、推送、回归、报告；子代理产出复核；每步完成后汇报改动范围/验证结果/是否通过                           |
| **子代理（并行）** | 独立只读验证（可选启用） | ① ff 前后文档差异比对；② roadmap 侧安卓模块完成度独立核实；③ README/plan 待办逐条销核；④ CI 配置审查。均为只读，产出交主代理实证后合并 |

**RACI 摘要**：方案与执行=主代理(R/C)；决策点=用户(A)；阶段验收=用户(A)/主代理(R)；并行调研=子代理(R)/主代理(A)。

---

## 六、时间安排（2026-10-12 起，共 4 周）

| 周次 | 日期          | 阶段        | 关键里程碑                       | 时间盒                      |
| -- | ----------- | --------- | --------------------------- | ------------------------ |
| W1 | 10-12~10-18 | Phase 0~2 | **D+1：安卓入库；D+2：main 统一并推送** | 数据救援 1h；快进+回归 0.5 天      |
| W2 | 10-19~10-25 | Phase 3   | 缺口收割完毕；APK 重编验证             | 2~~3 天（含回归 6~~8 次 ×5min） |
| W3 | 10-26~11-01 | Phase 4   | CI 三工作流实跑全绿；文档同步            | 2 天                      |
| W4 | 11-02~11-08 | Phase 5   | 技术债清尾；发布演练                  | 1~2 天                    |

缓冲：每周末预留 0.5 天处理回归红与环境问题。关键路径为 W1——若 W1 延误，全周期顺延（无并行压缩空间，因后续阶段全部依赖统一后的主干）。

---

## 七、资源需求

| 类别   | 资源                                                            | 用途              | 备注                                         |
| ---- | ------------------------------------------------------------- | --------------- | ------------------------------------------ |
| 工具链  | 系统 git `C:\Program Files\Git\cmd\git.exe`                     | 全部 git 写操作      | 便携版 ref 丢失前科；分支操作后必须 show-ref/ls-remote 验证 |
| 运行时  | `.venv/Scripts/python.exe`（libtorrent 2.1.1 + PySide6 6.11.2） | 全部回归            | managed Python 缺 libtorrent，禁用             |
| 安卓   | JDK 21（`%LOCALAPPDATA%\Android\jdk-21`）+ gradle-9.1.0         | Phase 3 APK 验证  | `--no-build-cache` 绕沙箱                     |
| 测试依赖 | imageio-ffmpeg、coverage==7.16.0                               | moov/开播测试、覆盖率门禁 | 版本已由 roadmap requirements.lock 锁定          |
| 网络   | github.com 经会话代理                                              | 推送、Actions      | 代理偶发 502，重试即可                              |
| 人力   | 用户决策 4 次（约 30 分钟）；主代理全程                                       | —               | 子代理按需启用 2~4 个只读任务                          |
| 时间预算 | 回归约 5 min/次 × 6~8 次；安卓构建约 10 min                              | —               | CI runner 30 min 超时内可承受（roadmap 注释已评估）     |


## 九、效果评估

### 9.1 量化指标（现状 → 目标）

| 指标                       | 现状（2026-10-12）     | 目标                        | 验证方法                                             |
| ------------------------ | ------------------ | ------------------------- | ------------------------------------------------ |
| 未合并提交数                   | 120                | **0**                     | `git rev-list --count main..arch/roadmap-2026q4` |
| 受版本控制文件数                 | 47                 | **272**                   | `git ls-files \| wc -l`                          |
| 安卓源码                     | 磁盘缺失               | **141 文件入库**              | `git ls-files android \| wc -l`                  |
| 私钥入库                     | 有风险                | **0**                     | `git ls-files \| grep -cE "jks\|keystore"`       |
| core 模块数                 | 11                 | **26**                    | `ls core/*.py \| wc -l`                          |
| fetcher.py 行数            | 1,579              | **891**（随 ff）             | `wc -l`                                          |
| 测试套件                     | 8（根目录）             | **16 回归 + 27 全量**（tests/） | `regression_run.py` / `coverage_run.py full`     |
| 宽泛 except Exception      | 88 处               | **≤26 处**（随 ff）           | grep 计数                                          |
| `except Exception: pass` | 10 处               | **0**                     | grep 计数                                          |
| 流服务并发限流                  | 无                  | **有界信号量+超时**              | `grep -c Semaphore core/` >0                     |
| 设置接线缺口                   | 限速/缓存/日志 3 项缺      | **全部接线**                  | gui_feature/smoke 断言                             |
| CI 工作流                   | 1                  | **3**（ci/android/release） | Actions 实跑                                       |
| 覆盖率门禁                    | 无                  | **fail-under=70**（基线 81%） | CI 日志                                            |
| 依赖锁定                     | 无                  | **requirements.lock**     | 文件存在性                                            |
| 文档一致性                    | README 缺 4 文件、计数过时 | **一一对应**                  | 抽查 10 路径全命中                                      |

### 9.2 阶段验收检查表

| 阶段      | 检查项  | 通过标准                                   |
| ------- | ---- | -------------------------------------- |
| Phase 1 | 安卓入库 | 141 文件 / 0 私钥 / .gitignore 生效          |
| Phase 2 | 主干统一 | ff 成功 + 27 套绿 + 覆盖率≥70% + ls-remote 一致 |
| Phase 3 | 缺口收割 | W3 各项有测试断言；APK dex 验证通过                |
| Phase 4 | 质量基建 | CI 3 工作流实跑全绿；文档抽查 10/10 命中             |
| Phase 5 | 发布就绪 | release_check 全过；Release 产物 smoke 通过   |

### 9.3 过程指标

- 每阶段汇报三要素：改动范围 / 验证结果 / 是否通过（用户既定协作约定）。
- 回归失败当日定位、当日修复或明确降级理由，不带病进入下一阶段。
- 全部决策留痕于本方案与 CHANGELOG。

---

## 十、待决策事项（需用户批准）

| #   | 决策点                                             | 建议                                           | 影响                                    |
| --- | ----------------------------------------------- | -------------------------------------------- | ------------------------------------- |
| D-1 | 是否批准立即执行 Phase 1（安卓救援）                          | **批准，今日执行**                                  | 每延迟一天，源码仅存于 git 对象库的单点风险多一天           |
| D-2 | 主干统一采用 `--ff-only` 快进 vs 保留 main 做 merge commit | **ff 快进**（已实证：main 是 roadmap 祖先、零文件重叠、无冲突候选） | merge commit 会制造无意义的分叉节点；ff 保持线性历史    |
| D-3 | 正式发布签名策略                                        | Phase 5 前生成正式 keystore 并纳入 release_check 硬门禁 | 开发 jks（magnetviewer-dev）仅限开发，正式分发必须更换 |
| D-4 | mypy 是否纳入 CI 硬门禁                                | **先出基线报告再定**（roadmap 未含，属新增成本）               | 影响 W3 工作量与 CI 时长                      |

---

## 附：本方案的关键判断依据（可复现）

1. `git merge-base --is-ancestor main arch/roadmap-2026q4` → 退出码 0（快进可行）。
2. `comm -12 <磁盘未跟踪清单> <roadmap 跟踪清单>` → 空（ff 不会被未跟踪文件阻塞）。
3. `git show arch/roadmap-2026q4:android/.gitignore` → 含 `*.jks` / `keystore.properties` / `local.properties`（救援安全）。
4. `git ls-tree --name-only arch/roadmap-2026q4` → 272 条目含 requirements.lock / coverage_run.py / 3 workflows / 2 specs / CHANGELOG / CONTRIBUTING / docs。
5. roadmap `ci.yml` 注释：回归 16 套 + coverage 全量 27 套 + `--fail-under=70`（基线 81%）。

---

## 执行记录

### 2026-10-12 Phase 0~~2 完成（当日 14:19~~14:45）

**执行顺序偏差（已声明并留痕）**：原方案 Phase 1（先在 main 提交 android 救援）会使 Phase 2 的 `--ff-only` 必然失败（新提交不在 roadmap 历史中），两阶段合并为一次原子 ff。G1 验收标准不变。

| 阶段             | 结果 | 证据                                                                                                                                                                                           |
| -------------- | -- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Phase 0        | 通过 | 基线 SHA 与环境确认见 `plan/PHASE0_BASELINE_2026-10-12.md`；android 未跟踪文件（含 jks/keystore.properties）已备份至 `Desktop/magnet-viewer-backup-android-untracked-20261012.tgz`（38.7MB/92 文件，排除 build/）        |
| Phase 1（并入 ff） | 通过 | android 受控 **141 文件 / 95 .kt**；唯一 keystore 匹配为 roadmap 有意入库的 `keystore.properties.example`（纯占位符，已人工复核）；真实 jks/keystore.properties/local.properties 均被 android/.gitignore 忽略（check-ignore 实证） |
| Phase 2a ff    | 通过 | `main` e974d68→**2b9021c**，show-ref 落盘验证；受控文件 47→**272**；core 27 个 .py                                                                                                                       |
| Phase 2b 回归    | 通过 | `regression_run.py` **27/27 全绿**（REG_EXIT=0，qt_stream_open 实跑 PASS 非 SKIP）；`coverage_run.py full` **TOTAL 81%** ≥ 70%；contract_check **208/208 OK**；日志 reg_ff_unify.log / cov_ff_unify.log   |
| Phase 2c 推送    | 通过 | `git push origin main`：a22d264→2b9021c；`ls-remote origin main` = 2b9021c 与本地一致                                                                                                               |

**决策点状态**：D-1 批准执行（用户 10-12 授权）；D-2 按 ff 快进执行完毕；D-3（正式签名 keystore）、D-4（mypy 门禁）仍待用户决策，属 Phase 4/5。

**下一步**：Phase 3 缺口收割——ff 后逐项复核 S-2~S-6 实际剩余（子代理盘点已就绪：CI 首跑风险清单 + 文档差异清单）。

### 2026-10-12 Phase 3a 完成（当日 14:45~15:00）：缺口重评与收割

**重评结论：G2 六项缺口中五项半已随 ff 自动闭合，真实剩余仅 S-10（当日清零）。**

| 项              | 重评结果   | 证据                                                                            |
| -------------- | ------ | ----------------------------------------------------------------------------- |
| S-1 fetcher 拆分 | ✅ 自动闭合 | core/fetcher.py = 891 行（与方案预测逐字一致）                                            |
| S-2 日志开关       | ✅ 自动闭合 | settings_dialog.py:392-398 控件 + main_window:335 logutil.set_enabled 接线        |
| S-3 限速         | ✅ 自动闭合 | settings_dialog:378/527 → main_window:135/332 → fetcher:363 → session:501 全链路 |
| S-4 缓存上限       | ✅ 自动闭合 | settings_dialog:339-347 QSpinBox（0=不限制）+ :528 保存 cache_limit_mb               |
| S-5 封装违规       | ✅ 自动闭合 | ui/ 层 `session._`/`mgr._`/`reg._` 私有字段直写 **0 命中**                             |
| S-6 流服务限流      | ✅ 自动闭合 | stream_server.py:554/557 BoundedSemaphore（max_concurrency + wait_limiter）     |
| S-10 静默吞异常     | ✅ 当日清零 | 原值 3 处 → 修 2 处（10ee576）+ 1 处有意豁免（f31a454，见下）                                  |

**S-10 处置明细**：

- `ui/main_window.py:597`（`_on_gallery_file`）：start_preview 失败静默 → 改为 `QMessageBox.warning("预览失败")`，对齐 :549 视频路径既有模式。
- `ui/main_window.py:836`（`_force_close`）：关窗计时器 `except Exception: pass` → 收窄 `except RuntimeError`（Qt wrapped C++ 对象销毁竞态）。
- `core/logutil.py:122`（`_write`）：日志器自守卫**有意保留**——handler emit/格式化/滚动/磁盘满任何异常类型都可能，收窄会破坏「绝不影响主流程」契约。已注释登记为 S-10 唯一豁免，防审计重复误报。**G2 门禁相应修订为：`except Exception: pass` = 0（logutil 自守卫豁免除外）**。
- 附带发现：except Exception 总量 core=27（方案参考值 ≤26，口径偏差 1 处；硬门禁为 pass 模式清零，已达成）。

**分支残留核对（git cherry 补丁等价）**：copilot-fixes / release-onefile / settings-wiring 全部 +0（内容已全含）；refactor-fetcher 仅剩 1 个过时文档提交 a09b7e2（README gui_feature 计数回填 46→70——现 README=126 亦过时，实测 **275 项**），判定全部超集覆盖，**无需收割**；分支删除留 Phase 5 清理（sha 已留痕：copilot-fixes=127d37d、refactor-fetcher=492a06b）。

**验证**：定向三套（theme_check=0 / contract_check=0 / gui_feature 275 项通过）+ 全量回归 **27/27 全绿**（REG_EXIT=0，reg_s10_fix.log）；推送 2b9021c→f31a454，ls-remote 一致。

**新发现文档债（归入 Phase 4）**：README gui_feature 计数 126 → 实测 275。

**下一步**：Phase 3b 安卓编译验证（W4：JDK21+gradle assembleRelease --no-build-cache + dex 字符串验证；gradle 在本沙箱有被挡前科，需无沙箱授权）。

### 2026-10-12 Phase 3b 完成（当日 15:00~15:10）：安卓验证——免重编判定 + 等价证据链闭环

**免重编判定（时序实证）**：android/ 最后代码提交 0c8daa6（10-10 23:59:18）**早于**现有 APK 构建时刻（10-11 00:02-00:03），其间 checkpoint 2b9021c（00:22）仅 +wf_head.sha，S-10 两提交（10ee576/f31a454）仅触桌面 Python——**现有 APK 就是对应当前 android 树的最新产物**。按 R-6 重编只会得到字节级重复，故不重编、直接验证。

**W4 验收（R8 混淆开启下的等价证据链）**：

| 验证              | 结果                                                                                             |
| --------------- | ---------------------------------------------------------------------------------------------- |
| output-metadata | versionCode 1 / versionName 1.0 / applicationId com.bitseed.magnetviewer / minSdk 28           |
| R8 mapping 原始类名 | PieceView→y6.w（37 处）、StreamServer→a7.f（226 处）、IncrementalAvailability（PieceMap.kt:36 顶层接口，5 处） |
| dex 混淆名回查       | y6/w=1 ✓、a7/f=1 ✓（类字节确实在包内）                                                                    |
| 内联函数类           | ProxyProbe/TrackerPush 为 Kotlin 顶层函数（*Kt 文件类），被 R8 内联——mapping 中**原始行号帧**（337:339 等）即源码入包直接证据  |
| 字符串常量           | X-Why=1（S8 诊断头）、PortMapping=2、TrackerPush=1 命中 dex                                             |
| 16KB 页对齐        | zipalign -c -P 16 → **Verification successful**                                                |

**dist 交付副本刷新（字节陷阱又一实证）**：旧副本 09-29 14:30（sha256 8751c55c…）与构建产物 10-11 00:03（sha256 e718dabd…）**字节数完全相同（21,728,460）但内容相差 12 天提交**。已刷新：旧副本存档为 `dist/MagnetViewer-1.0-android-arm64-20260929.bak.apk`，新副本指纹 **e718dabdac34fceac9d3271fa483feed791ad6313909e6ba465d212e35b4bf55**。

**Phase 3 整体关闭**：W3（缺口收割）+ W4（安卓验证）验收标准全部满足。R8（minifyEnabled）对 release 开启一事记录在案。

**下一步**：Phase 4 质量基建——①查 GitHub Actions 三工作流实跑状态（ci.yml 已随 push 触发）；②mypy 门禁评估基线（D-4）；③文档同步（README 计数 126→275 等，子代理清单已备）。

### 2026-10-12 Phase 4a 完成（当日 15:17~15:45）：CI 实跑核验 + 文档同步

**CI 实跑核验（GitHub Actions 真实 runner）**：

| Run | 结论 | 定性 |
|-----|------|------|
| Regression @ f31a454（27 套 + coverage ≥70） | ✅ success | 首跑即绿——ci-audit 风险清单（30min 超时/coverage 锁版/qt_stream 无头）全部未成立 |
| Regression @ 2b9021c 首推 | ❌→✅ 重跑 success | 唯一 FAIL=`local_torrent_test` 磁盘一致性断言（5.2s，下载 100%+流服务字节级正确仅磁盘比对败）→ runner 抖动实证 |
| Android @ 2b9021c/b77d163 | ❌→修复中 | 见下 |
| parity job | ✅ success ×3 | 对拍夹具与 core/ 一致（ci-audit 风险#5 未成立） |

**Android CI 修复（b77d163，两段式）**：
1. `android-actions/setup-android@v3` 必红（两次同点实证）：2026-10 runner 镜像新增 Android XR 等 SDK 许可证 → `sdkmanager --licenses` 交互式 (y/N) 复核 → 非交互退出码 1。修复：ubuntu-latest 本就预装 SDK，替换为 `yes | sdkmanager --licenses || true` 预接受步 → 重跑后该步 success。
2. 推进到 `Run JVM unit tests` 后败于 buildscript 依赖解析（kotlin-stdlib 2.2.0「Could not find」+ compose 插件 POM「Could not parse」）——经 curl 旁证两 POM 均真实存在（200/303），判定 runner 冷缓存解析抖动，重跑后 **success**。

**文档同步（958bde8，7 文件 +73/-19）**：README 结构树补齐 core×10/ui×5/根目录×7 条目、contract_check 169→208、gui_feature 126→275（实测）；套件计数口径统一 27（regression_run.py docstring×2、coverage_run.py、ci.yml 步骤名）；technical_roadmap 状态收口 + 启动清单 5 项勾选；ARCHITECTURE「待产出」删除；contract_snapshot 补最新核对注；CHANGELOG 新增 [2026-10-12] 收敛节。编辑后 contract_check 208/208 + theme_check 复绿。

**D-4 mypy 基线：环境阻塞**。pip 26.1.2 对 tuna/官方索引均立即 `versions: none`（-vv 零网络日志；curl 同机可达索引真实内容；无沙箱复现同败；二次尝试被 SIGTERM）——pip 请求层异常，非代码问题。待网络可用后执行：`.venv/Scripts/python.exe -m pip install mypy -i https://pypi.tuna.tsinghua.edu.cn/simple` → `python -m mypy core ui --ignore-missing-imports` 出基线报告再定硬门禁。不阻塞 Phase 4 其余项。

### 2026-10-12 Phase 4b 收口 + 双端重打包交付（当日 15:46~15:55）

**CI 三工作流验收关闭**：Android @ b77d163 重跑 **success**（setup-android 修复生效 + 依赖解析确证为抖动）；Regression @ b77d163 **success**。Regression ×3 绿 / Android 绿 / parity 连绿；Release 属 tag 触发，归 Phase 5 演练。

**双端重打包（用户指令：电脑+手机软件更新实装）**：

| 产物 | 结果 | sha256 |
|------|------|--------|
| 桌面 onefile `dist/MagnetViewer.exe` | ✅ 新建 53s，44,281,048B（较 09-29 版 +26KB） | `f8ba90913e3fda248950ada0ebccdce9e012d5bb88bcd7f6e4b1f6223de9e711` |
| 桌面 onedir `dist/MagnetViewer/` | ✅ build.bat 自检 pack_check PASS（必需组件 10 项齐全 / 瘦身 4/4） | 主 exe `64b12ff080a48b1fbe94c751523b78e2f4b5d56d7412f1c80da6097f0ee2f6b0` |
| 安卓 `dist/MagnetViewer-1.0-android-arm64.apk` | ✅ 增量重编 1m59s，与 10-11 版**逐字节一致**（安卓树无代码变更的确定性证明） | `e718dabdac34fceac9d3271fa483feed791ad6313909e6ba465d212e35b4bf55` |

**新代码入包实证**：onefile PYZ-00.toc 中 6 个 ff 新增模块（cache_quota / task_controller / stream_host / pieceview / metacache / secretbox）全部命中——09-29 旧包不含这些模块。构建缓存按 spec 名清理（build/magnet-viewer{,-onefile}；cmd rmdir 绕批量删除守卫可靠）。旧产物存档 `dist/_archive-20260929/`（09-29 exe + 10-11 onedir，未删除）。

**装机指引**：电脑直接运行 `dist/MagnetViewer.exe`（或 onedir 版）；手机安装 `dist/MagnetViewer-1.0-android-arm64.apk`（与已装版本同为开发签名 magnetviewer-dev，可直接覆盖升级；正式分发前须换正式 keystore=D-3）。

**下一步**：Phase 5 发布就绪——D-3 正式签名 keystore 决策（用户）、release_check.py 门禁全过、`v1.0.0` tag 触发 release.yml 发布演练。

### 2026-10-12 Phase 5a 完成（当日 15:58~16:10）：技术债收尾 + 门禁实测 + 发布演练启动

**release_check.py 门禁实测（--allow-dev-cert 冒烟）**：R0 APK 存在 / R1 签名（dev 凭据显式放行）/ R2 16KB 页对齐 Verification successful / R3 ABI 收窄只含 arm64-v8a / R4 产物对账 sha256 与 dist 副本逐位一致——**四项全过；正式发布唯一拦路项 = R1 开发凭据 → 待 D-3 决策**。

**技术债收尾三项（§Phase 5.1）**：
| 项 | 结果 |
|----|------|
| Windows 260 字符路径 | 最长受控路径 115 字符（ADR 中文文件名按字节计）→ **风险关闭** |
| 死代码 current_result | 复核为 Facade 兼容别名（fetcher:771→preview:299，仅测试断言引用），受 contract_check「别名全 property 结构」冻结 → **维持现状不删** |
| 优先级提示分支 | priority_spin/Signal/tooltip 均为活功能，**无死代码实证 → 关闭** |
| 测试临时目录泄漏（P2-22） | **复测属实：100 个 mv_* 目录（约 238MB）**→ .bat 脚本 rd 清零（BEFORE=98 AFTER=0）；根因（各测试 mkdtemp 无统一清理）列为后续项，本轮只清淤 |

**分支清理（§清理）**：release-onefile / settings-wiring 走 `-d`（真祖先）；copilot-fixes / refactor-fetcher 因历史非祖先被拒 → cherry 补丁等价已实证 + sha 留痕（127d37d / 492a06b）后 `-D` 收删。现仅剩 `main` 与 `arch/roadmap-2026q4`（R-5 单点恢复引用，有意保留）。

**发布演练**：已用 workflow_dispatch 触发 release.yml（HTTP 204；**只出 artifact 不挂 Release**——规避 R-8 开发签名红线），构建在途，下轮验收 exe + portable zip 工件。

### 2026-10-12 Phase 5b 完成（当日 16:00~16:12）：D-3 落地 + 正式 tag

**D-3 正式签名落地（用户拍板：现在生成正式 keystore）**：
- keytool 生成 `android/magnetviewer-release.jks`（PKCS12 / RSA-2048 / 有效期 10000 天 /
  CN=MagnetViewer——不含 "MagnetViewer Dev" 标记，R1 自动放行）；24 位强随机口令。
- `android/app/keystore.properties` 切换为正式凭据（gitignored 不入库）；旧 dev 配置与
  dev jks 一并备份至仓库外 `Desktop/magnet-viewer-backup-release-keystore-20261012/`
  （含 keystore-password.txt——**请用户自行转存口令管理器**）。
- 重签 APK 后 **release_check.py 无放行全绿**：R1=CN=MagnetViewer 正式凭据 / R2 16KB 对齐 /
  R3 arm64-only / R4 对账。sha256 `c32dee75e810f3e3945ad7f01a82d8919676a058590e4c9002042462aa612c7e`。
- dist 交付副本换为正式签名版；dev 签名版存档 `_archive-20260929/*-dev-signed.bak.apk`。
- 注意：已装 dev 签名版的手机**无法直接覆盖安装**正式签名版（签名不同）→ 需先卸载再装；
  此后所有升级均在正式签名链内覆盖升级。

**R-8 门禁状态**：release_check 全绿 → v1.0.0 tag 合法。执行 `git tag v1.0.0 && git push origin v1.0.0`
触发 release.yml 挂桌面 Release（exe + portable zip；APK 走本地构建+release_check 交付链，不经 Release）。
