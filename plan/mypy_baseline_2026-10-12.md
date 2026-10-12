# mypy 基线报告（D-4 决策依据）

> 日期：2026-10-12
> 工具：mypy 2.4.0（compiled；经 curl 手动拉 wheel 离线安装——本机 pip 索引通道异常，见 REMEDIATION_PLAN Phase 4a）
> 命令：`.venv/Scripts/python.exe -m mypy core ui --ignore-missing-imports`
> 范围：core + ui 共 43 个源文件（tests 不入基线）

## 基线结果

**88 errors in 18 files**（退出码 1）；另有 20 处 `annotation-unchecked` note（未注解函数体不检查，非错误）。

### 错误码分布

| 错误码 | 数量 | 判读 |
|--------|------|------|
| attr-defined | 66 | 大头。PySide6 动态属性 / libtorrent SWIG 对象的既有stub缺口，非真实缺陷居多 |
| assignment | 9 | 真实类型缺口 |
| union-attr | 5 | Optional 未判空 |
| arg-type | 5 | 真实类型缺口 |
| call-overload | 2 | 重载匹配 |
| misc | 1 | — |

### 文件分布（Top 10，共 63/88 集中在前 6 文件）

| 文件 | 数 |
|------|----|
| ui/style.py | 14 |
| ui/settings_dialog.py | 13 |
| ui/downloads_pane.py | 10 |
| core/stream_server.py | 10 |
| ui/file_tree.py | 8 |
| core/persist.py | 8 |
| ui/add_download_dialog.py | 4 |
| core/taskops.py | 4 |
| ui/task_controller.py | 3 |
| ui/gallery.py | 3 |

## D-4 门禁建议（供拍板）

基线 88 直接上 `--fail-under` 硬门禁不现实（会常年红）。建议三步爬坡：

1. **现在**：CI 增加非门禁 mypy 报告步骤（每次跑出 artifact，不拦绿）——让增量劣化可见。
2. **短期**：治理 attr-defined 大头（优先 ui/style.py、settings_dialog.py——多为 PySide6 stub 语义，
   部分可用 `# type: ignore[attr-defined]` 精确压制），目标 <20。
3. **达标后**：`--fail-under` 或错误数上限硬门禁入 CI。

**现状建议：暂不设硬门禁**（避免常年假红）；报告步骤是否先入 CI 由 PM 决定（成本低、收益是防劣化）。

## 复现

```bash
.venv/Scripts/python.exe -m mypy core ui --ignore-missing-imports
```

环境注：本机 pip 索引通道异常，wheel 离线安装法（curl 拉 pypi JSON→wheel→--no-index --find-links）
已在 `Desktop` 外留有可复用 wheel 缓存（%TEMP%/mypywheels）。
