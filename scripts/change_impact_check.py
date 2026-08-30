#!/usr/bin/env python3
"""Developer-owned pre-release change-impact check for rulebook A."""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from change_control import classify, summary


def git(args: list[str]) -> str:
    # Prevent Git from quoting Chinese file names; the rulebook path must be
    # compared as an actual path on Windows and Linux alike.
    return subprocess.check_output(["git", "-c", "core.quotepath=false", *args], cwd=ROOT, text=True, encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="与此提交/分支比较；省略时仅检查工作区变更")
    args = parser.parse_args()
    diff_args = ["diff", "--name-only"] + ([args.base, "HEAD"] if args.base else [])
    files = [line.strip() for line in git(diff_args).splitlines() if line.strip()]
    patch_args = ["diff"] + ([args.base, "HEAD"] if args.base else []) + ["--"]
    patch = git(patch_args)
    domains = classify(files, patch)
    rulebook_changed = "docs/整体程序规则.md" in files
    regressions_changed = any(path.startswith("tests/") for path in files)
    print("变更影响：" + ("；".join(summary(domains)) if domains else "未识别到规则行为变化"))
    if domains and not rulebook_changed:
        print("阻断：检测到规则行为相关变更，但规则文件 A 未同步。", file=sys.stderr)
        return 1
    if domains and not regressions_changed:
        print("阻断：检测到规则行为相关变更，但未更新或新增固定回归测试。", file=sys.stderr)
        return 1
    print("变更影响检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
