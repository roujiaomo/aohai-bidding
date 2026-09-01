import sys
import unittest
import datetime as dt

sys.path[:0] = ["services/ai-review", "app"]
from ai_review import has_core_maritime_product, has_open_participation_evidence, review_stats, tender_has_passed_deadline


class AiPolicyTests(unittest.TestCase):
    def test_review_headline_equals_visible_tab_counters(self):
        import sqlite3

        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        c.execute("CREATE TABLE reviews(ai_status TEXT, deadline_at TEXT, content TEXT)")
        future = (dt.date.today() + dt.timedelta(days=10)).isoformat()
        past = (dt.date.today() - dt.timedelta(days=10)).isoformat()
        c.executemany("INSERT INTO reviews VALUES(?,?,?)", [
            ("approved", future, "公开招标"),
            ("approved_manual", future, "公开招标"),
            ("exclude", future, "无关采购"),
            ("exclude", past, "无关采购"),
            ("rejected_manual", future, "人工不通过"),
        ])
        stats = review_stats(c)
        self.assertEqual(stats["total"], stats["approved"] + stats["approved_manual"] + stats["exclude"])
        self.assertEqual(stats["total"], 4)
        self.assertEqual(stats["rejected"], 1)

    def test_open_ais_tender_uses_latest_participation_date(self):
        file_date = (dt.date.today() + dt.timedelta(days=5)).isoformat()
        open_date = (dt.date.today() + dt.timedelta(days=10)).isoformat()
        row = {"title": "岸基AIS系统补点工程公开招标公告", "buyer": "航海保障中心", "deadline_at": "", "content": f"获取招标文件至{file_date}，开标时间{open_date}"}
        self.assertFalse(tender_has_passed_deadline(row))
        self.assertTrue(has_open_participation_evidence(row, [{"text": "开标时间", "field": "开标时间", "quote": f"开标时间{open_date}"}]))
        self.assertTrue(has_core_maritime_product([{"name": "岸基AIS系统", "field": "采购需求", "quote": "采购岸基AIS系统"}]))

    def test_date_before_submit_response_phrase_is_expired(self):
        past_date = (dt.date.today() - dt.timedelta(days=10)).isoformat()
        row = {"title": "AIS采购竞争性谈判公告", "buyer": "航标处", "deadline_at": "", "content": f"并于{past_date}15点30分前提交响应文件"}
        self.assertTrue(tender_has_passed_deadline(row))

    def test_power_ais_is_not_maritime_product(self):
        row = {"title": "AIS开关柜采购", "buyer": "供电公司", "deadline_at": "", "content": "空气绝缘开关设备"}
        self.assertFalse(has_core_maritime_product([{"name": "空气绝缘开关设备", "field": "采购需求", "quote": "采购空气绝缘开关设备"}]))

    def test_result_announcement_is_not_open(self):
        row = {"title": "AIS系统采购中标结果公告", "buyer": "海事局", "deadline_at": "", "content": "中标结果公告"}
        self.assertFalse(has_open_participation_evidence(row, []))
