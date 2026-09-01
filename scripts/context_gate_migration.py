#!/usr/bin/env python3
"""Preview/apply the v10 keyword-context gate without calling DeepSeek."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
from governance import AI_HARNESS_VERSION, RULEBOOK_VERSION
import radar


MANUAL_STATUSES = {"approved_manual", "rejected_manual"}


def stamp() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def backup_file(path: Path, backup_dir: Path) -> Path:
    resolved = path.resolve(strict=True)
    target_dir = backup_dir.resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{resolved.stem}-before-context-v10-{dt.datetime.now():%Y%m%d-%H%M%S}{resolved.suffix}"
    shutil.copy2(resolved, target)
    return target


def preview(radar_db: Path, review_db: Path) -> dict:
    rules = radar.load_config().get("rules") or {}
    rc = sqlite3.connect(radar_db); rc.row_factory = sqlite3.Row
    rows = [dict(row) for row in rc.execute("SELECT * FROM tenders WHERE is_deleted=0 ORDER BY id")]
    rc.close()
    changed, dropped = [], []
    for row in rows:
        new_score, matches = radar.score_item(row, rules)
        if new_score != int(row.get("score") or 0):
            item = {"id": row["id"], "title": row["title"], "old_score": int(row.get("score") or 0),
                    "new_score": new_score, "new_priority": radar.rating_label(new_score, rules.get("opportunity_levels")),
                    "matches": matches}
            changed.append(item)
            if item["old_score"] > 0 and new_score == 0:
                item["rejected_contexts"] = radar.rejected_business_keyword_contexts(row, rules)
                dropped.append(item)
    review_counts = {"affected": 0, "automatic_to_exclude": 0, "already_excluded": 0,
                     "manual_protected": 0, "expired_unchanged": 0}
    if review_db.exists() and dropped:
        ac = sqlite3.connect(review_db); ac.row_factory = sqlite3.Row
        ids = [item["id"] for item in dropped]
        marks = ",".join("?" for _ in ids)
        reviews = [dict(row) for row in ac.execute(
            f"SELECT id,source_tender_id,title,ai_status,bucket,keyword_score FROM reviews WHERE source_tender_id IN ({marks})", ids)]
        ac.close()
        review_counts["affected"] = len(reviews)
        for row in reviews:
            status = row["ai_status"]
            if status in MANUAL_STATUSES:
                review_counts["manual_protected"] += 1
            elif status == "expired":
                review_counts["expired_unchanged"] += 1
            elif status == "exclude":
                review_counts["already_excluded"] += 1
            else:
                review_counts["automatic_to_exclude"] += 1
    return {"mode": "preview_only", "rulebook_version": RULEBOOK_VERSION, "checked": len(rows),
            "score_changed": len(changed), "context_dropped": len(dropped),
            "review_actions": review_counts, "dropped": dropped}


def apply(radar_db: Path, review_db: Path, backup_dir: Path) -> dict:
    report = preview(radar_db, review_db)
    backups = {"radar": str(backup_file(radar_db, backup_dir))}
    if review_db.exists():
        backups["review"] = str(backup_file(review_db, backup_dir))
    rules = radar.load_config().get("rules") or {}
    dropped_ids = {item["id"]: item for item in report["dropped"]}
    rc = sqlite3.connect(radar_db); rc.row_factory = sqlite3.Row
    changed = 0
    # Read a stable snapshot before updating the same table. Mutating through
    # the active SELECT cursor can cause SQLite to skip later rows.
    source_rows = [dict(row) for row in rc.execute(
        "SELECT * FROM tenders WHERE is_deleted=0 ORDER BY id"
    )]
    for item in source_rows:
        new_score, matches = radar.score_item(item, rules)
        priority = radar.rating_label(new_score, rules.get("opportunity_levels"))
        if new_score != int(item.get("score") or 0) or priority != (item.get("priority") or ""):
            rc.execute("UPDATE tenders SET score=?,match_json=?,priority=?,updated_at=? WHERE id=?",
                       (new_score, json.dumps(matches, ensure_ascii=False), priority, stamp(), item["id"]))
            changed += 1
    rc.commit(); rc.close()

    review_updated = manual_protected = 0
    if review_db.exists() and dropped_ids:
        ac = sqlite3.connect(review_db); ac.row_factory = sqlite3.Row
        marks = ",".join("?" for _ in dropped_ids)
        rows = list(ac.execute(f"SELECT * FROM reviews WHERE source_tender_id IN ({marks})", tuple(dropped_ids)))
        for row in rows:
            ac.execute("UPDATE reviews SET keyword_score=0 WHERE id=?", (row["id"],))
            if row["ai_status"] in MANUAL_STATUSES:
                manual_protected += 1
                continue
            # 已经排除或已过期的记录无需改变结论，更不能用迁移兜底理由
            # 覆盖原有、信息量更高的评审结果；这里只同步关键词分数。
            if row["ai_status"] in {"exclude", "expired"}:
                continue
            details = dropped_ids[row["source_tender_id"]]
            rejected = details.get("rejected_contexts") or []
            hit_text = "、".join(sorted({hit for entry in rejected for hit in entry.get("hits", [])})) or "宽泛业务词"
            context_name = "航天或海事卫星" if any(entry.get("context") == "aerospace" for entry in rejected) else "海洋、内河、船舶、航道或港航"
            reason = {"source_objects": [], "product_inferences": [], "reasons": [], "risk_notes": [],
                      "exclude_reason": {"text": f"公告仅命中“{hit_text}”，但未出现可验证的{context_name}语境",
                                         "field": "公告标题", "quote": str(row["title"] or "")[:80]},
                      "context_gate": {"version": RULEBOOK_VERSION, "rejected": rejected}}
            ac.execute("""INSERT INTO review_history(review_id,previous_status,previous_label,previous_fit_score,previous_confidence,previous_reason_json,previous_evidence_json,archived_at,archive_reason)
                          VALUES(?,?,?,?,?,?,?,?,?)""",
                       (row["id"], row["ai_status"], row["ai_label"], row["ai_fit_score"], row["ai_confidence"],
                        row["ai_reason_json"], row["ai_evidence_json"], stamp(), "v10宽泛关键词语境门槛历史修正"))
            previous = row["ai_status"]
            ac.execute("""UPDATE reviews SET ai_status='exclude',ai_label='语境不足',bucket='exclude',project_type='other',supplier_lead=0,
                          ai_fit_score=0,ai_confidence=0.99,ai_reason_json=?,ai_evidence_json=?,policy_version=?,harness_version=?,error='',analyzed_at=? WHERE id=?""",
                       (json.dumps(reason, ensure_ascii=False), json.dumps([{"field": "公告标题", "quote": str(row["title"] or "")[:80]}], ensure_ascii=False),
                        RULEBOOK_VERSION, AI_HARNESS_VERSION, stamp(), row["id"]))
            ac.execute("""INSERT INTO review_events(review_id,event_type,from_status,to_status,policy_version,harness_version,details_json,created_at)
                          VALUES(?,?,?,?,?,?,?,?)""",
                       (row["id"], "context_gate_reconciliation", previous, "exclude", RULEBOOK_VERSION,
                        AI_HARNESS_VERSION, json.dumps({"source_preserved": True, "deepseek_called": False, "rejected_contexts": rejected}, ensure_ascii=False), stamp()))
            review_updated += 1
        ac.commit(); ac.close()
    report.update({"mode": "applied", "backups": backups, "radar_rows_updated": changed,
                   "review_rows_updated": review_updated, "manual_rows_protected": manual_protected,
                   "deepseek_called": False, "raw_rows_deleted": 0})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--radar-db", type=Path, required=True)
    parser.add_argument("--review-db", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = apply(args.radar_db, args.review_db, args.backup_dir or args.radar_db.parent / "backups") if args.apply else preview(args.radar_db, args.review_db)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
