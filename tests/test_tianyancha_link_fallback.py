import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, "app")
import radar


class _Response:
    def __init__(self, final_url, html):
        self.final_url, self.html = final_url, html
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def geturl(self): return self.final_url
    def read(self, _size): return self.html.encode()


class TianyanchaLinkFallbackTests(unittest.TestCase):
    def test_homepage_redirect_is_unavailable(self):
        with patch("radar.urlopen", return_value=_Response("https://caigou.chinatelecom.com.cn/", "首页")):
            self.assertEqual(radar._tianyancha_official_link_status(
                "https://caigou.chinatelecom.com.cn/DeclareDetails?id=1", "卫星通信项目"), "unavailable")

    def test_archive_url_is_saved_with_unavailable_status(self):
        with tempfile.TemporaryDirectory() as directory:
            conn = radar.connect(Path(directory) / "radar.db")
            radar.init_db(conn); radar.seed_sources(conn)
            item = {"source_code": "tianyancha", "source_url": "https://official.example/detail/1",
                    "archive_url": "https://m.tianyancha.com/app/h5/bid/abc", "source_link_status": "unavailable",
                    "title": "岸基AIS系统采购公告", "buyer": "测试单位", "region": "浙江", "content": "采购岸基AIS系统公开招标", "published_at": "2026-09-20"}
            radar.upsert_tender(conn, item, link_ok=0, rules=radar.RULES_DEFAULTS)
            row = conn.execute("SELECT archive_url, source_link_status, link_ok FROM tenders").fetchone()
            self.assertEqual(dict(row), {"archive_url": item["archive_url"], "source_link_status": "unavailable", "link_ok": 0})
            conn.close()
