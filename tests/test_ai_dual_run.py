import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path[:0] = ["services/ai-review", "app"]
import ai_review
from ai_review import dual_run, facts_from_saved_review


class AiDualRunTests(unittest.TestCase):
    def test_saved_review_replay_never_invents_source_object(self):
        review = {"title": "岸基 AIS 采购公告", "buyer": "海事局", "region": "", "content": "采购岸基AIS系统。",
                  "ai_reason_json": json.dumps({"source_objects": [{"name": "VDES岸基站", "field": "公告正文", "quote": "采购岸基AIS系统"}]}, ensure_ascii=False)}
        self.assertEqual(facts_from_saved_review(review)["source_objects"], [])

    def test_dual_run_writes_only_evaluation_ledger(self):
        original_db = ai_review.DB
        with tempfile.TemporaryDirectory() as temp:
            ai_review.DB = Path(temp) / "review.db"
            try:
                c = ai_review.conn()
                c.execute("""INSERT INTO reviews(source_tender_id,title,keyword_score,content,ai_status,bucket,synced_at,ai_reason_json)
                    VALUES(?,?,?,?,?,?,?,?)""", (1, "岸基AIS采购公告", 80, "采购岸基AIS系统，投标截止2026年12月31日。", "approved", "direct_opportunity", ai_review.now(), json.dumps({"source_objects":[{"name":"岸基AIS系统","field":"公告正文","quote":"采购岸基AIS系统"}]}, ensure_ascii=False)))
                c.commit(); c.close()
                result = dual_run(1)
                self.assertEqual(result["failed"], 0, result)
                self.assertEqual(result["processed"], 1, result)
                c = ai_review.conn()
                self.assertEqual(c.execute("SELECT ai_status FROM reviews WHERE source_tender_id=1").fetchone()[0], "approved")
                self.assertEqual(c.execute("SELECT COUNT(*) FROM review_evaluations").fetchone()[0], 1)
                c.close()
            finally:
                ai_review.DB = original_db
