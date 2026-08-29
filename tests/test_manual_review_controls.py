import sys
import unittest

sys.path[:0] = ["services/ai-review"]
from ai_review import resolve_manual_decision


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

