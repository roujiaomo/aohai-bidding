import sys
import unittest

sys.path[:0] = ["services/ai-review", "app"]
from ai_review import has_core_maritime_product, has_open_participation_evidence, tender_has_passed_deadline


class AiPolicyTests(unittest.TestCase):
    def test_open_ais_tender_uses_latest_participation_date(self):
        row = {"title": "岸基AIS系统补点工程公开招标公告", "buyer": "航海保障中心", "deadline_at": "", "content": "获取招标文件至2026年08月17日，开标时间2026年08月31日"}
        self.assertFalse(tender_has_passed_deadline(row))
        self.assertTrue(has_open_participation_evidence(row, [{"text": "开标时间", "field": "开标时间", "quote": "开标时间2026年08月31日"}]))
        self.assertTrue(has_core_maritime_product([{"name": "岸基AIS系统", "field": "采购需求", "quote": "采购岸基AIS系统"}]))

    def test_date_before_submit_response_phrase_is_expired(self):
        row = {"title": "AIS采购竞争性谈判公告", "buyer": "航标处", "deadline_at": "", "content": "并于2026年08月12日15点30分前提交响应文件"}
        self.assertTrue(tender_has_passed_deadline(row))

    def test_power_ais_is_not_maritime_product(self):
        row = {"title": "AIS开关柜采购", "buyer": "供电公司", "deadline_at": "", "content": "空气绝缘开关设备"}
        self.assertFalse(has_core_maritime_product([{"name": "空气绝缘开关设备", "field": "采购需求", "quote": "采购空气绝缘开关设备"}]))

    def test_result_announcement_is_not_open(self):
        row = {"title": "AIS系统采购中标结果公告", "buyer": "海事局", "deadline_at": "", "content": "中标结果公告"}
        self.assertFalse(has_open_participation_evidence(row, []))
