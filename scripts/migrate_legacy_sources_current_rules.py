#!/usr/bin/env python3
"""Migrate non-Tianyancha legacy records through the current radar rules."""
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy-db", type=Path, required=True)
    ap.add_argument("--current-db", type=Path, required=True)
    ap.add_argument("--app-dir", type=Path, required=True)
    args = ap.parse_args()
    sys.path.insert(0, str(args.app_dir))
    import radar

    config = radar.load_config()
    rules = config.get("rules") or {}
    window_days = int(config.get("auto_expire_days", 30))
    today = radar.cn_today().date()
    legacy = sqlite3.connect(f"file:{args.legacy_db}?mode=ro", uri=True)
    legacy.row_factory = sqlite3.Row
    current = radar.connect(args.current_db)
    radar.init_db(current)
    radar.seed_sources(current)
    known_sources = {row[0] for row in current.execute("SELECT code FROM sources")}
    source_rows = legacy.execute(
        """SELECT source_code,source_url,title,buyer,region,budget,published_at,deadline_at,content
           FROM tenders WHERE is_deleted=0 AND source_code NOT LIKE ? ORDER BY id""",
        ("%tianyancha%",),
    ).fetchall()
    counts = {"source_active": len(source_rows), "past_deadline": 0, "past_window": 0,
              "created": 0, "merged": 0, "rejected_by_current_rules": 0, "unknown_source": 0}
    for row in source_rows:
        deadline, published = as_date(row["deadline_at"]), as_date(row["published_at"])
        if deadline and deadline < today:
            counts["past_deadline"] += 1
            continue
        if published and published + dt.timedelta(days=window_days) < today:
            counts["past_window"] += 1
            continue
        item = dict(row)
        # Historical dedupe may have recorded multiple comma-separated sources.
        # Choose the first still-configured source for a valid new provenance key.
        source = next((x.strip() for x in str(row["source_code"] or "").split(",") if x.strip() in known_sources), "")
        if not source:
            counts["unknown_source"] += 1
            continue
        item["source_code"] = source
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
