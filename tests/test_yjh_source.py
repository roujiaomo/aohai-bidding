import unittest
from unittest.mock import patch

from app import radar


class YjhSourceTests(unittest.TestCase):
    def test_split_year_in_cms_text_still_yields_deadline(self):
        text = "".join(map(chr, (0x6295, 0x6807, 0x622A, 0x6B62, 0x65F6, 0x95F4, 0xFF1A))) + "202 6 " + chr(0x5E74) + " 8 " + chr(0x6708) + " 27 " + chr(0x65E5) + " 10 " + chr(0x65F6)
        self.assertEqual("2026-08-27", radar._extract_deadline(text, "yjh_water"))

    def test_purchase_article_is_emitted_from_static_listing(self):
        listing = '''<a href="/yjh/xxgk/cggg/art/2026/art_abc.html">高港船闸AIS岸基基站设备采购采购公告（二次）</a>'''
        detail = "发布时间：2026年8月24日 投标截止时间：2026年8月27日"
        with patch.object(radar, "load_config", return_value={"yjh_purchase_seed_urls": []}), \
             patch.object(radar, "_http_get", side_effect=[listing, listing, detail]), \
             patch.object(radar, "_fetch_detail_full", return_value=("2026-08-27", detail)):
            items = radar.fetch_yjh_water()
        self.assertEqual(1, len(items))
        self.assertIn("AIS岸基基站", items[0]["title"])
        self.assertEqual("2026-08-27", items[0]["deadline_at"])
        self.assertEqual("江苏省泰州引江河管理处", items[0]["buyer"])


if __name__ == "__main__":
    unittest.main()
