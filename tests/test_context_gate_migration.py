import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = ["scripts", "services/ai-review", "app"]
import ai_review
import context_gate_migration
import radar


class ContextGateMigrationTests(unittest.TestCase):
    def test_preview_then_apply_hides_only_automatic_context_misses(self):
        original_review_db = ai_review.DB
        try:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                radar_db = root / "radar.db"
                review_db = root / "review.db"
                rc = radar.connect(radar_db); radar.init_db(rc)
                stamp = radar.now()
                rows = [
                    (1, "消防救援总队搜救犬通信系统公开招标公告", "搜救", 10),
                    (2, "职业学院卫星通信实训室采购公告", "卫星通信", 20),
                    (3, "海上搜救船舶应急通信系统采购公告", "搜救", 10),
                    (4, "山区遥感监测数据服务采购公告", "遥感监测", 10),
                ]
                for ident, title, hit, score in rows:
                    rc.execute("""INSERT INTO tenders(id,fingerprint,source_code,source_url,title,buyer,region,content,score,match_json,followup_status,priority,is_deleted,created_at,updated_at)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                               (ident, f"fp-{ident}", "test", f"https://example/{ident}", title, "", "", title,
                                score, f'[{ {"hits": [hit]} }]'.replace("'", '"'), "new", "一般关注", 0, stamp, stamp))
                rc.commit(); rc.close()

                ai_review.DB = review_db
                ac = ai_review.conn()
                ac.executemany("""INSERT INTO reviews(source_tender_id,title,keyword_score,content,ai_status,bucket,synced_at)
                                  VALUES(?,?,?,?,?,?,?)""", [
                    (1, rows[0][1], 10, rows[0][1], "approved", "market_intelligence", stamp),
                    (2, rows[1][1], 20, rows[1][1], "approved_manual", "market_intelligence", stamp),
                    (3, rows[2][1], 10, rows[2][1], "approved", "market_intelligence", stamp),
                    (4, rows[3][1], 10, rows[3][1], "exclude", "exclude", stamp),
                ])
                ac.commit(); ac.close()

                report = context_gate_migration.preview(radar_db, review_db)
                self.assertEqual(report["context_dropped"], 3)
                self.assertEqual(report["review_actions"]["automatic_to_exclude"], 1)
                self.assertEqual(report["review_actions"]["manual_protected"], 1)
                self.assertEqual(report["review_actions"]["already_excluded"], 1)

                result = context_gate_migration.apply(radar_db, review_db, root / "backups")
                self.assertFalse(result["deepseek_called"])
                self.assertEqual(result["raw_rows_deleted"], 0)
                ac = ai_review.conn()
                statuses = {row[0]: row[1] for row in ac.execute("SELECT source_tender_id,ai_status FROM reviews")}
                self.assertEqual(statuses, {1: "exclude", 2: "approved_manual", 3: "approved", 4: "exclude"})
                rc = radar.connect(radar_db)
                scores = {row[0]: row[1] for row in rc.execute("SELECT id,score FROM tenders")}
                self.assertEqual(scores[1], 0)
                self.assertEqual(scores[2], 0)
                self.assertGreater(scores[3], 0)
                self.assertEqual(scores[4], 0)
                rc.close()
                self.assertEqual(ac.execute("SELECT COUNT(*) FROM review_history").fetchone()[0], 1)
                self.assertEqual(ac.execute("SELECT COUNT(*) FROM review_events WHERE event_type='context_gate_reconciliation'").fetchone()[0], 1)
                ac.close()
        finally:
            ai_review.DB = original_review_db


if __name__ == "__main__":
    unittest.main()
