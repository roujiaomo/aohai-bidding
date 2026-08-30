import json
import sys
import unittest
from pathlib import Path

sys.path[:0] = ["services/ai-review", "app"]
from ai_review import decide_from_facts
from governance import can_transition, effective_rulebook, validate_extraction_shape
from radar_quality_notify import collect_alerts


def fact(text, quote=None):
    return {"text": text, "field": "公告正文", "quote": quote or text}


class GovernanceHarnessTests(unittest.TestCase):
    def test_state_machine_rejects_unsafe_jump(self):
        self.assertTrue(can_transition("approved", "rejected_manual"))
        self.assertFalse(can_transition("exclude", "approved"))

    def test_rule_snapshot_is_stable(self):
        self.assertEqual(effective_rulebook({"x": [1]})["digest"], effective_rulebook({"x": [1]})["digest"])

    def test_extraction_schema_requires_every_fact_group(self):
        self.assertIn("必须是数组", validate_extraction_shape({"source_objects": []}))

    def test_regression_cases(self):
        samples = json.loads((Path(__file__).parent / "regression_samples.json").read_text(encoding="utf-8"))
        for sample in samples:
            record = sample["record"]
            content = record["content"]
            title = record["title"]
            if "岸基AIS" in content:
                facts = {"source_objects":[{"name":"岸基AIS系统","field":"公告正文","quote":"采购岸基AIS系统"}],"participation":[fact("投标截止", "投标截止2026年09月20日")],"business_scope":[fact("岸基AIS系统", "采购岸基AIS系统")],"project_stage":[fact("公开招标", "公开招标")],"exclusions":[],"risks":[]}
            elif "开关" in title:
                facts = {"source_objects":[],"participation":[],"business_scope":[],"project_stage":[fact("公开招标", "公开招标")],"exclusions":[fact("空气绝缘开关设备")],"risks":[]}
            elif "招聘" in title:
                facts = {"source_objects":[],"participation":[],"business_scope":[],"project_stage":[],"exclusions":[fact("公开招聘")],"risks":[]}
            elif "成交" in title:
                facts = {"source_objects":[{"name":"AIS系统","field":"公告正文","quote":"AIS系统"}],"participation":[],"business_scope":[fact("AIS系统")],"project_stage":[fact("成交结果", "成交结果")],"exclusions":[],"risks":[]}
            else:
                facts = {"source_objects":[],"participation":[],"business_scope":[fact("港航数据平台")],"project_stage":[fact("可行性研究", "可行性研究")],"exclusions":[],"risks":[]}
            with self.subTest(sample=sample["name"]):
                self.assertEqual(decide_from_facts(record, facts)["bucket"], sample["expected"])

    def test_quality_alerts_only_include_actionable_findings(self):
        self.assertEqual(collect_alerts({"sources": {"alerts": []}, "quality": {"deterministic_issues": {}}, "ai": {"mismatch": 0}}), [])
        alerts = collect_alerts({"sources": {"alerts": [{"source": "ccgp", "reason": "最近一次抓取失败"}]}, "quality": {"deterministic_issues": {"招聘/录用公告": 2}}, "ai": {"mismatch": 1}})
        self.assertEqual(len(alerts), 3)
