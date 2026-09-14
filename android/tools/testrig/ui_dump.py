"""把 uiautomator dump 出来的 ui.xml 解析成「文本 → 可点中心坐标」清单。

用法：
    ./.venv/Scripts/python android/tools/testrig/ui_dump.py [ui.xml 路径]
默认路径：%LOCALAPPDATA%\\Temp\\ui.xml
"""
from __future__ import annotations

import os
import re
import sys
import xml.etree.ElementTree as ET

path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.environ.get("LOCALAPPDATA", "/tmp"), "Temp", "ui.xml")
root = ET.parse(path).getroot()
for node in root.iter("node"):
    text = (node.get("text") or "").strip()
    if not text:
        continue
    nums = [int(x) for x in re.findall(r"\d+", node.get("bounds") or "")]
    if len(nums) != 4:
        continue
    cx, cy = (nums[0] + nums[2]) // 2, (nums[1] + nums[3]) // 2
    print(f"{text[:44]:46s} center=({cx:4d},{cy:4d}) clickable={node.get('clickable')}")
