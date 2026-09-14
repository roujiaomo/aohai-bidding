import sys
import unittest

sys.path.insert(0, "app")
from change_control import classify, diff_digest, validate_no_rule_change


class ChangeControlTests(unittest.TestCase):
    def test_ai_service_is_always_governed(self):
        domains = classify(["services/ai-review/ai_review.py"])
        self.assertTrue({"ai", "classification", "presentation", "governance"}.issubset(domains))

    def test_plain_document_change_does_not_claim_runtime_impact(self):
        self.assertEqual(classify(["docs/说明.md"]), set())

    def test_keyword_change_is_conservative(self):
        self.assertIn("ingestion", classify(["tools/x.py"], "调整抓取入库规则"))

    def test_unrelated_app_file_is_not_radar(self):
        self.assertEqual(classify(["app/text_format.py"]), set())
        self.assertEqual(classify(["app/change_control.py"]), {"governance"})

    def test_review_is_bound_to_content_and_inventory(self):
        files, patch = ["app/radar.py"], "comment-only diff"
        review = {"decision": "no_rule_change", "files": files,
                  "diff_sha256": diff_digest(files, patch), "reviewer": "developer",
                  "reason": "Only a comment was corrected", "validation": "Existing tests passed"}
        self.assertEqual(validate_no_rule_change(review, files, patch), "")
        self.assertTrue(validate_no_rule_change(review, files, patch + " runtime change"))
        self.assertTrue(validate_no_rule_change(review, files + ["app/governance.py"], patch))
        for field in ("reviewer", "reason", "validation"):
            self.assertTrue(validate_no_rule_change({**review, field: " "}, files, patch))
        self.assertTrue(validate_no_rule_change([], files, patch))
        self.assertTrue(validate_no_rule_change({**review, "decision": "rule_change"}, files, patch))
