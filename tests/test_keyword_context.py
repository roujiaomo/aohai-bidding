import sys
import unittest

sys.path.insert(0, "app")
from radar import RULES_DEFAULTS, score_item


class KeywordContextTests(unittest.TestCase):
    def score(self, title, buyer="", content=""):
        return score_item({"title": title, "buyer": buyer, "region": "", "content": content}, RULES_DEFAULTS)[0]

    def test_search_and_rescue_cannot_score_without_water_context(self):
        self.assertEqual(self.score("消防救援总队搜救犬通信系统公开招标公告"), 0)

    def test_maritime_search_and_rescue_keeps_recall(self):
        self.assertGreater(self.score("海上搜救船舶应急通信系统采购公告"), 0)

    def test_generic_satellite_communications_cannot_score_alone(self):
        self.assertEqual(self.score("职业学院卫星通信实训室采购公告"), 0)

    def test_satellite_engineering_object_supplies_aerospace_context(self):
        self.assertGreater(self.score("卫星通信终端阵列天线测试系统采购公告"), 0)

    def test_satellite_maintenance_service_supplies_aerospace_context(self):
        self.assertGreater(self.score("卫星通信维保服务采购公告"), 0)

    def test_aerospace_satellite_context_keeps_recall(self):
        self.assertGreater(self.score("低轨卫星通信载荷测试系统采购公告"), 0)

    def test_maritime_satellite_application_keeps_recall(self):
        self.assertGreater(self.score("船舶卫星通信终端采购公告"), 0)

    def test_generic_beidou_cannot_score_without_water_context(self):
        self.assertEqual(self.score("公路车辆北斗高精度定位终端采购公告"), 0)

    def test_explicit_ais_product_still_scores(self):
        self.assertGreater(self.score("岸基AIS系统补点工程公开招标公告"), 0)

    def test_inland_waterway_term_carries_its_own_context(self):
        self.assertGreater(self.score("内河航道整治工程招标公告"), 0)


if __name__ == "__main__":
    unittest.main()
