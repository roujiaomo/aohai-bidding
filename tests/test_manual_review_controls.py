import sys
import unittest

sys.path[:0] = ["services/ai-review"]
from ai_review import resolve_manual_decision, validate_claims


class ManualReviewControlTests(unittest.TestCase):
    def test_excluded_item_requires_display_bucket_when_human_approves(self):
        with self.assertRaisesRegex(ValueError, "必须选择"):
            resolve_manual_decision("exclude", "exclude", "approved")

    def test_excluded_item_can_be_restored_to_direct_opportunity(self):
        status, bucket = resolve_manual_decision("exclude", "exclude", "approved", "direct_opportunity")
        self.assertEqual((status, bucket), ("approved_manual", "direct_opportunity"))

    def test_excluded_item_can_be_restored_to_market_intelligence(self):
        status, bucket = resolve_manual_decision("exclude", "exclude", "approved", "market_intelligence")
        self.assertEqual((status, bucket), ("approved_manual", "market_intelligence"))

    def test_human_rejection_preserves_bucket_for_audit_but_hides_record(self):
        status, bucket = resolve_manual_decision("approved", "direct_opportunity", "rejected")
        self.assertEqual((status, bucket), ("rejected_manual", "direct_opportunity"))

    def test_claim_without_verbatim_quote_is_hidden(self):
        claims = validate_claims([{"text": "采购方业务相关", "field": "采购方", "quote": "不存在的内容"}], "采购单位为航海保障中心")
        self.assertEqual(claims, [])

    def test_source_object_must_be_verbatim_in_its_quote(self):
        corpus = "采购 AIS岸基基站系统1套"
        valid = validate_claims([{"name": "AIS岸基基站系统", "field": "采购需求", "quote": "采购 AIS岸基基站系统1套"}], corpus, "name", require_text_in_quote=True)
        inferred = validate_claims([{"name": "VDES岸基基站", "field": "采购需求", "quote": "采购 AIS岸基基站系统1套"}], corpus, "name", require_text_in_quote=True)
        self.assertEqual(len(valid), 1)
        self.assertEqual(inferred, [])
