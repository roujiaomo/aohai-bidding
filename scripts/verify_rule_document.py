#!/usr/bin/env python3
"""Block a release when the business rule document is not synchronized."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from governance import RULEBOOK_VERSION

DOCUMENT = ROOT / "docs" / "整体程序规则.md"
REQUIRED_HEADINGS = ("数据处理总流程", "抓取与原始入库", "AI 评审规则", "页面展示规则", "通知、质量与审计", "规则变更与发布")


def main() -> int:
    if not DOCUMENT.is_file():
        print(f"缺少规则文件 A：{DOCUMENT}", file=sys.stderr)
        return 1
    text = DOCUMENT.read_text(encoding="utf-8")
    marker = re.search(r"机器规则版本：`([^`]+)`", text)
    if not marker or marker.group(1) != RULEBOOK_VERSION:
        print(f"规则文件 A 版本不同步：文档={marker.group(1) if marker else '缺失'}，代码={RULEBOOK_VERSION}", file=sys.stderr)
        return 1
    missing = [x for x in REQUIRED_HEADINGS if x not in text]
    if missing:
        print("规则文件 A 缺少章节：" + "、".join(missing), file=sys.stderr)
        return 1
    print(f"规则文件 A 已同步：{RULEBOOK_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
