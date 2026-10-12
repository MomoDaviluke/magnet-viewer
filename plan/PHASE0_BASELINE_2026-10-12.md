# Phase 0 基线记录（2026-10-12 14:27 GMT+8）

> 依据《REMEDIATION_PLAN_2026-10-12.md》Phase 0 退出准则生成。本文件为只读快照，不作他用。

## git 基线

| 项 | 值 |
|----|-----|
| main（执行前 HEAD） | `e974d68a812d992efdb2cc76aba57703f7d5a8ce` |
| arch/roadmap-2026q4 尖端 | `2b9021cb9731fe51bccf8c7c2cc487af1429f89f` |
| origin/main（ls-remote 实测） | `a22d264db5a1c9725f7f8d0b80e93f2205bbe2c5`（本地祖先，推送=远端快进） |
| ff 可行性 | `merge-base --is-ancestor main roadmap` 退出码 0 |
| 未跟踪文件与 roadmap 跟踪清单重叠 | **0**（roadmap 272 文件） |
| 回滚点 | `git reset --hard e974d68`；android 内容可从 `arch/roadmap-2026q4` 引用恢复 |

## dist 产物指纹（执行前）

| 文件 | sha256 |
|------|--------|
| dist/MagnetViewer.exe | `f9be02f54af4e0221f6cdecd88ccc5bb2794ce6d13ad2aa43cda03ea3aebf164` |

## 环境确认

| 项 | 结果 |
|----|------|
| 系统 git | `C:/Program Files/Git/cmd/git.exe` 可用（全部写操作使用之） |
| .venv 解释器 | Python 3.13.14 + libtorrent 2.1.1.0 导入通过 |
| 远端可达 | ls-remote 成功（无 502） |
| android 未跟踪文件备份 | `Desktop/magnet-viewer-backup-android-untracked-20261012.tgz`（38.7MB / 92 文件，含 magnetviewer-dev.jks 与 app/keystore.properties，已排除 build/） |

## 与方案的执行顺序偏差（已声明）

方案 Phase 1（先在 main 提交 android 救援）会使 Phase 2 的 `--ff-only` 失败——新提交不在 roadmap 历史中。
故合并为一次原子 ff：android/ 141 文件随 roadmap 树直接入库（roadmap 从未跟踪 jks/keystore.properties，
android/.gitignore 同时生效），G1 验收标准不变。
