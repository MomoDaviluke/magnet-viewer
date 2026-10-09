# 贡献指南（CONTRIBUTING）

> 适用于 magnet-viewer 桌面端（Python + PySide6）与安卓端（Kotlin + Compose）。
> 架构规则见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)，决策背景见 [docs/adr/](docs/adr/)。

---

## 一、开发环境

### 1.1 桌面端

```powershell
# 1) 创建虚拟环境（Python 3.10+，已在 3.13 验证）
python -m venv .venv

# 2) 安装依赖——发布/回归环境必须用精确锁定文件：
.venv\Scripts\pip install -r requirements.lock
# （日常开发可用 requirements.txt 宽松安装；但注意 libtorrent 2.0 与 2.1
#   会话配置 API 不兼容（2.1 移除 settings_pack），宽松约束可能落到
#   未验证版本——requirements.lock 存在的唯一理由，见文件头注释 P2-24。）

# 3) 运行
.venv\Scripts\python main.py        # 或双击 start.bat（自动建 venv + 装依赖）
```

依赖要点：
- `PySide6_Addons` **不是可选**——内嵌播放器要用的 Qt 多媒体后端插件
  （ffmpegmediaplugin.dll）只在这个包里，缺了它视频能解析能下载但双击
  预览「打不开」（QMediaPlayer ResourceError）。
- 部分流媒体测试（moov_stream / qt_stream_open）需要 ffmpeg 二进制：
  `pip install imageio-ffmpeg`，缺失时对应套件以退出码 2（SKIP）显式跳过。

### 1.2 回归必须用 .venv 的 Python

```powershell
.venv\Scripts\python.exe regression_run.py          # 全量回归（27 套）
.venv\Scripts\python.exe regression_run.py smoke    # 单套（按名字前缀匹配）
```

**不要用系统 Python 跑回归**——系统解释器没装 libtorrent，套件会在导入段
直接失败或被误判 SKIP（2026-10-09 Wave 0 基线回归首跑即因此失败，改用
`.venv\Scripts\python.exe` 后复跑通过）。`build.bat` 同样优先取
`.venv\Scripts\python.exe`，取不到才回落系统 `python`。

### 1.3 安卓端

```powershell
cd android
.\gradlew assembleDebug      # 构建
.\gradlew test               # JVM 单测（150+ 用例）
```

端到端测试靶场（本机做种 + 模拟器 adb 驱动，不依赖公网）：
`android/tools/testrig/README.md`。发布门禁：`android/tools/release_check.py`
（签名 / 16KB / ABI / 产物对账）。

---

## 二、测试规范

- **套件构成**：`regression_run.py` 的 `SUITES` 清单为准，当前 **27 套**
  （第 1 套 `contract_check` 为对外契约自检，秒级，必须最先跑；含旧测试
  兼容契约 8 套、下载管理验收、GUI/主题门禁、并发/性能/入站/代理专项等）。
- **退出码语义**（全项目统一约定，README 与 regression_run.py 头注释同源）：
  - `0` = PASS
  - `1` = FAIL
  - `2` = SKIP（依赖缺失显式跳过）
- **SKIP 不算绿**：单套 SKIP 不计失败、但报告必须区分标注；**全部 SKIP =
  失败**——那意味着环境崩坏或导入段被误吞，绝不能打印「回归全绿」
  （REVIEW-2026-09 P0-3 的教训）。
- 改动 core 公开接口前先跑 `contract_check`（守住 23 个公开接口与协作模块
  签名）；改完后至少跑与所改模块对应的专项套件（persist/session/registry/
  taskops/resolver/preview_test 均为假依赖秒级，可放心全跑）。
- 提交 PR 前：`.venv\Scripts\python.exe regression_run.py` 全量绿 +
  安卓改动则 `gradlew test` 绿。
- 共享测试工具在 `test_support.py`（本机做种端、种子构造、磁盘快照、
  轮询等待），新测试优先复用它。

---

## 三、提交规范

- **Conventional Commits**，按 scope 分类提交：
  `feat(core):` / `fix(android):` / `fix(android,ui):` / `test(ui):` /
  `docs(arch):` / `build(android):` / `chore(release):` …（参照 `git log` 既有风格，
  描述用中文）。
- 一次提交只做一件事；跨端缺陷若根因同源可 `fix(android,ui)` 合并提交，
  否则分 scope 提交。
- 重构类工作小步提交、每批可独立 revert（main_window 拆分「每个搬运批次
  独立 commit」的回滚预案即建立在此约定上）。

---

## 四、代码规范要点

1. **锁纪律**（ADR-0001，并发代码必读）：
   - 全项目唯一任务锁 = `TaskRegistry.lock`；公开方法自持锁、`*_locked`
     后缀要求调用方已持锁，两者绝不嵌套（锁不可重入）；
   - 锁序单向：`reg.lock` → `scheduler._lock` → `PieceView._lock`；scheduler
     是锁序末端，其锁段内绝不取 reg.lock / 落盘 / I/O；
   - 锁内禁 I/O：落盘/fsync/remove_torrent 等阻塞调用一律出锁执行；
     需要「持锁改状态 + 停调度」时锁内记标志、出锁再调（R-10 形态）；
   - 以上不变式由 `concurrency_test.py §F` 静态检查，违规即红。
2. **中文注释要求**：模块文件头必须有职责说明 + 依赖方向 + 锁纪律 +
   失败策略（core/ 24 模块全部如此，新模块对齐）；关键决策处写明背景与
   历史缺陷编号（R-1/P0-1 式引用），面向用户的文案用中文。
3. **宽泛 except 禁令**：业务分支的 `except Exception` 必须收窄为具体类型；
   仅基础设施兜底（HTTP handler、alert 循环、清理路径）允许宽泛捕获，且
   **必须 `log_exception` + 注释说明为何宽泛**——`except Exception: pass`
   在本项目是明令禁止的反模式（logutil.py 头注释登记了它掩盖过的三起
   真实缺陷）。异常层级 `core/exceptions.py` 实施中，新代码优先用其类型。
4. **UI 裸 hex / setStyleSheet 禁令**（theme_check.py 机器门禁强制）：
   `ui/theme.py` 之外任何 `ui/*.py` 不得出现 hex 色值、不得调
   `setStyleSheet`；局部样式用 `setObjectName` + theme.py QSS 规则；
   尺寸走间距令牌 `SP_XS/SM/MD/LG/XL = 4/8/12/16/24`；自绘控件必须在
   绘制时读 `theme.X`（经 `import ui.theme as theme`），不得导入期固化快照，
   否则切主题不跟随。安卓端同理：色走 `LocalPal`、间距走 `Spacing.kt`、
   字号走 `Type.kt`、圆角走 `LocalShapes`。
5. **UI→core 依赖白名单**（Wave 2 收敛目标，ARCHITECTURE.md §2.2）：
   `ui/*.py` 只准 import `core.models` / `core.config` / `core.fetcher` /
   `core.logutil`；新代码不得再增加越层 import。
6. **core 不碰 Qt**：core/ 层禁止 import PySide6（config.py 的 QSettings 是
   登记在案的例外）；后台线程回调经 UI 层 `_Bridge` Signal 转发。

---

## 五、打包验证

桌面端一键打包（清旧产物 → PyInstaller → 产物自检）：

```powershell
build.bat
```

- 产物：`dist\MagnetViewer\MagnetViewer.exe`（onedir，整目录拷走即可运行）；
  spec 为 `magnet-viewer.spec`（含瘦身过滤黑名单），onefile 备用
  `magnet-viewer-onefile.spec`。
- `pack_check.py` 把「砍过头（缺 DLL）」和「没砍动（体积回升）」两类
  打包回归钉在交付之前——退出码沿用 0/1/2 约定（2 = 产物目录不存在，
  还没打包，不假装通过）。**改了依赖、Qt 版本或 spec 过滤名单后必须
  重跑打包 + pack_check**。
- 安卓端发布链路：签名 release + R8（保 JNI 边界类）+ NDK 剥符号 +
  ABI 收窄，CI 见 `.github/workflows/android.yml`；产物对账跑
  `release_check.py`。正式分发前必须换正式签名（当前 R1 仍是开发签名）。
