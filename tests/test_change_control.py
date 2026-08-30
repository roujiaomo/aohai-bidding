import sys
import unittest

sys.path.insert(0, "app")
from change_control import classify


class ChangeControlTests(unittest.TestCase):
    def test_ai_service_is_always_governed(self):
        domains = classify(["services/ai-review/ai_review.py"])
        self.assertTrue({"ai", "classification", "presentation", "governance"}.issubset(domains))

    def test_plain_document_change_does_not_claim_runtime_impact(self):
        self.assertEqual(classify(["docs/说明.md"]), set())

    def test_keyword_change_is_conservative(self):
        self.assertIn("ingestion", classify(["tools/x.py"], "调整抓取入库规则"))
