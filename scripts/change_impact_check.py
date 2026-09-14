#!/usr/bin/env python3
"""Developer-owned pre-release change-impact check for rulebook A."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from change_control import classify, summary, diff_digest, validate_no_rule_change


def git(args: list[str]) -> str:
    # Prevent Git from quoting Chinese file names; the rulebook path must be
    # compared as an actual path on Windows and Linux alike.
    return subprocess.check_output(["git", "-c", "core.quotepath=false", *args], cwd=ROOT, text=True, encoding="utf-8", errors="replace")


def collect_changes(base: str | None) -> tuple[list[str], str]:
    # Default covers staged + unstaged + untracked changes. --base is a
    # committed release comparison and deliberately excludes the worktree.
    revisions = [base, "HEAD"] if base else ["HEAD"]
    files = git(["diff", "--name-only", "-z", *revisions]).split("\0")
    files = [path for path in files if path]
    patch = git(["diff", "--no-ext-diff", "--binary", *revisions, "--"])
    if not base:
        for name in git(["ls-files", "--others", "--exclude-standard", "-z"]).split("\0"):
            if name:
                files.append(name)
                # Hex preserves binary and newline differences in the digest.
                data = (ROOT / name).read_bytes()
                patch += f"\nUNTRACKED {name}\n{data.hex()}\n{data.decode('utf-8', errors='replace')}\n"
    return sorted(set(files)), patch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", help="与此提交/分支比较；省略时仅检查工作区变更")
    parser.add_argument("--review-report", type=Path, help="差异绑定的无规则变化 JSON 复核报告；随交付留档")
    args = parser.parse_args(argv)
    files, patch = collect_changes(args.base)
    domains = classify(files, patch)
    rulebook_changed = "docs/整体程序规则.md" in files
    regressions_changed = any(path.startswith("tests/") for path in files)
    print("变更影响：" + ("；".join(summary(domains)) if domains else "未识别到规则行为变化"))
    print(json.dumps({"files": files, "diff_sha256": diff_digest(files, patch)}, ensure_ascii=False))
    if args.review_report:
        try:
            review = json.loads(args.review_report.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"阻断：无法读取复核报告：{exc}", file=sys.stderr)
            return 1
        error = validate_no_rule_change(review, files, patch)
        if error:
            print("阻断：" + error, file=sys.stderr)
            return 1
        print("开发者无规则变化复核（不替代自测或 release-gate）：" + json.dumps(review, ensure_ascii=False))
        return 0
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
