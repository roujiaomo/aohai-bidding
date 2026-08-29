import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from ingestion_policy import ingestion_issue_reason, non_opportunity_reason
from source_parsers import parse_li_list


RULES = {"business_categories": [{"keywords": ["AIS", "海事", "航道"]}]}


class IngestionPolicyTests(unittest.TestCase):
    def test_recruitment_is_hard_excluded(self):
        item = {"title": "海南海事局2026年度录用公务员报到公告"}
        self.assertEqual(non_opportunity_reason(item), "招聘/录用公告")

    def test_property_rental_and_place_name_beidou_are_rejected(self):
        self.assertEqual(non_opportunity_reason({"title": "重庆市涪陵区北斗路14号2栋招租"}), "资产招租/经营权信息")
        self.assertEqual(non_opportunity_reason({"title": "北斗镇卫生院建设项目监理"}), "地名北斗误匹配")

    def test_hainan_non_tender_column_is_rejected(self):
        item = {
            "title": "某海事系统采购公告",
            "source_code": "hn_msa",
            "source_url": "https://www.hn.msa.gov.cn/xxgk_4_4/46619.jhtml",
        }
        self.assertEqual(ingestion_issue_reason(item, RULES, 50), "海南海事局非项目招标栏目")

    def test_scoped_tender_is_allowed(self):
        item = {
            "title": "AIS岸基站采购公告",
            "source_code": "hn_msa",
            "source_url": "https://www.hn.msa.gov.cn/xxgk_4_6/46619.jhtml",
        }
        self.assertEqual(ingestion_issue_reason(item, RULES, 50), "")

    def test_parser_does_not_accept_another_hainan_column(self):
        html = (
            '<a href="/xxgk_4_4/46619.jhtml">录用公务员公告</a>'
            '<a href="/xxgk_4_6/46620.jhtml">AIS岸基站采购公告</a>'
        )
        rows = parse_li_list(html, "https://www.hn.msa.gov.cn/xxgk_4_6/index.jhtml", "海南海事", r"/xxgk_4_6/\d+\.jhtml\b")
        self.assertEqual([row["title"] for row in rows], ["AIS岸基站采购公告"])
