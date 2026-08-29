#!/usr/bin/env python3
"""Migrate active Tianyancha records into the current radar database.

The import deliberately calls the live radar upsert path, so current ingestion
rules, score thresholds and duplicate merging are applied instead of copying
old rows wholesale.  It never writes to the legacy database.
"""
import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path


def as_date(value):
    text = str(value or "").strip().replace("年", "-").replace("月", "-").replace("日", "")
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def is_current(row, today, window_days):
    deadline = as_date(row["deadline_at"])
    if deadline and deadline < today:
        return False, "past_deadline"
    published = as_date(row["published_at"])
    if published and published + dt.timedelta(days=window_days) < today:
        return False, "past_window"
    return True, ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-db", type=Path, required=True)
    parser.add_argument("--current-db", type=Path, required=True)
    parser.add_argument("--app-dir", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.app_dir))
    import radar

    rules = radar.load_config().get("rules") or {}
    window_days = int(radar.load_config().get("auto_expire_days", 30))
    today = radar.cn_today().date()
    legacy = sqlite3.connect(f"file:{args.legacy_db}?mode=ro", uri=True)
    legacy.row_factory = sqlite3.Row
    current = radar.connect(args.current_db)
    radar.init_db(current)
    radar.seed_sources(current)
    source_rows = legacy.execute(
        """SELECT source_url,title,buyer,region,budget,published_at,deadline_at,content
           FROM tenders WHERE is_deleted=0 AND source_code LIKE ? ORDER BY id""",
        ("%tianyancha%",),
    ).fetchall()
    counts = {"source_active": len(source_rows), "past_deadline": 0, "past_window": 0,
              "created": 0, "merged": 0, "rejected_by_current_rules": 0}
    for row in source_rows:
        ok, reason = is_current(row, today, window_days)
        if not ok:
            counts[reason] += 1
            continue
        item = dict(row)
        item["source_code"] = "tianyancha"
        created, score = radar.upsert_tender(current, item, link_ok=1, rules=rules)
        if created:
            counts["created"] += 1
        elif score:
            counts["merged"] += 1
        else:
            counts["rejected_by_current_rules"] += 1
    counts["deduplicated"] = radar.sweep_duplicates(current)
    current.commit()
    current.close()
    legacy.close()
    print("migration_summary=" + ";".join(f"{key}={value}" for key, value in counts.items()))


if __name__ == "__main__":
    main()
