import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import radar_notify


class RadarNotifyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.radar_db = base / "radar.db"
        self.review_db = base / "review.db"
        self.config = base / "config.json"
        self.config.write_text(json.dumps({"filter_expired": False}), encoding="utf-8")
        radar = sqlite3.connect(self.radar_db)
        radar.execute("""CREATE TABLE tenders (
            id INTEGER PRIMARY KEY, title TEXT, priority TEXT, buyer TEXT, region TEXT,
            deadline_at TEXT, published_at TEXT, source_url TEXT, score INTEGER,
            is_deleted INTEGER, followup_status TEXT)""")
        radar.executemany(
            "INSERT INTO tenders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (1, "已通过", "重点关注", "甲", "上海", "", "2026-08-30", "https://one", 90, 0, ""),
                (2, "已排除", "重点关注", "乙", "上海", "", "2026-08-30", "https://two", 90, 0, ""),
                (3, "待评审", "重点关注", "丙", "上海", "", "2026-08-30", "https://three", 90, 0, ""),
            ],
        )
        radar.commit(); radar.close()
        review = sqlite3.connect(self.review_db)
        review.execute("CREATE TABLE reviews (source_tender_id INTEGER, ai_status TEXT, bucket TEXT)")
        review.executemany("INSERT INTO reviews VALUES (?,?,?)", [
            (1, "approved", "direct_opportunity"),
            (2, "exclude", ""),
            (3, "pending", ""),
        ])
        review.commit(); review.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_only_formally_approved_rows_are_selected(self):
        rows = radar_notify.current_rows(
            self.radar_db, radar_notify.load_config(self.config), ("重点关注",), self.review_db
        )
        self.assertEqual([row["title"] for row in rows], ["已通过"])

    def test_dry_run_uses_new_dashboard_and_never_sends(self):
        with patch("sys.argv", ["radar_notify.py", "--db", str(self.radar_db), "--config", str(self.config), "--review-db", str(self.review_db), "--slot", "key", "--dry-run"]), patch.object(radar_notify, "send_markdown") as send:
            self.assertEqual(radar_notify.main(), 0)
        send.assert_not_called()

