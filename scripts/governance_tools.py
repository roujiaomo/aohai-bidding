#!/usr/bin/env python3
"""Read-only governance tools: release gate, rule impact, and reprocess preview."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import radar
from governance import RULEBOOK_VERSION, effective_rulebook


def candidate_rules(path: Path | None) -> dict:
    if not path:
        return radar.load_config().get("rules") or {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("rules", data) if isinstance(data, dict) else {}


def impact(args) -> int:
    current = radar.load_config().get("rules") or {}
    target = candidate_rules(args.rules_file)
    errors = radar.validate_rules(target)
    if errors:
        print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
        return 2
    conn = radar.connect(args.db); radar.init_db(conn)
    changes, summary = [], {"checked": 0, "score_changed": 0, "priority_changed": 0, "ingestion_changed": 0}
    for row in conn.execute("SELECT * FROM tenders WHERE is_deleted=0 ORDER BY id").fetchall():
        item = dict(row); summary["checked"] += 1
        old_score, _ = radar.score_item(item, current); new_score, _ = radar.score_item(item, target)
        old_priority = radar.rating_label(old_score, current.get("opportunity_levels")); new_priority = radar.rating_label(new_score, target.get("opportunity_levels"))
        old_issue = radar.quality_issue_reason(item, current); new_issue = radar.quality_issue_reason(item, target)
        if old_score != new_score: summary["score_changed"] += 1
        if old_priority != new_priority: summary["priority_changed"] += 1
        if old_issue != new_issue: summary["ingestion_changed"] += 1
        if old_score != new_score or old_priority != new_priority or old_issue != new_issue:
            changes.append({"id": item["id"], "title": item["title"], "score": [old_score, new_score],
                            "priority": [old_priority, new_priority], "quality": [old_issue, new_issue]})
    conn.close()
    result = {"ok": True, "mode": "preview_only", "rulebook": effective_rulebook(target), "summary": summary, "changes": changes[:args.limit], "truncated": max(0, len(changes) - args.limit)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def release_gate(args) -> int:
    rules = radar.load_config().get("rules") or {}
    errors = radar.validate_rules(rules)
    tests = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], cwd=ROOT, text=True, capture_output=True)
    document = subprocess.run([sys.executable, "scripts/verify_rule_document.py"], cwd=ROOT, text=True, capture_output=True)
    result = {"rulebook": effective_rulebook(rules), "rulebook_version": RULEBOOK_VERSION,
              "config_errors": errors, "tests_ok": tests.returncode == 0, "rule_document_ok": document.returncode == 0,
              "tests_tail": (tests.stdout + tests.stderr)[-3000:], "rule_document": (document.stdout + document.stderr).strip(),
              "release_ready": not errors and tests.returncode == 0 and document.returncode == 0}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["release_ready"] else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=ROOT / "data" / "radar.db")
    sub = ap.add_subparsers(required=True)
    p = sub.add_parser("impact-report", help="只读预览候选规则对存量数据的影响")
    p.add_argument("--rules-file", type=Path)
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=impact)
    p = sub.add_parser("reprocess-preview", help="历史重审前的只读差异预览")
    p.add_argument("--rules-file", type=Path)
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=impact)
    p = sub.add_parser("release-gate", help="配置与固定回归样本发布门禁")
    p.set_defaults(func=release_gate)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
