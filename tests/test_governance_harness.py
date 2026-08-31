import json
import sys
import unittest
from pathlib import Path

sys.path[:0] = ["services/ai-review", "app"]
from ai_review import decide_from_facts, audit_legacy_contradictions, repair_legacy_contradictions, extraction_prompt_for
import ai_review
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
            elif "LED" in title:
                facts = {"source_objects":[{"name":"LED灯器","field":"公告正文","quote":"LED灯器采购项目"}],"participation":[],"business_scope":[],"project_stage":[fact("中标结果", "中标结果公告")],"exclusions":[fact("普通 LED 灯器", "LED灯器采购项目")],"risks":[]}
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
        alerts = collect_alerts({"sources": {"alerts": [{"source": "ccgp", "reason": "最近一次抓取失败"}]}, "quality": {"deterministic_issues": {"招聘/录用公告": 2}}, "ai": {"mismatch": 1, "failure_rate": 0.1, "failed": 2}})
        self.assertEqual(len(alerts), 4)

    def test_legacy_explicit_exclusion_is_audited_and_repaired(self):
        import tempfile
        from pathlib import Path
        original_db = ai_review.DB
        try:
            with tempfile.TemporaryDirectory() as temp:
                ai_review.DB = Path(temp) / "review.db"
                c = ai_review.conn()
                reason = {"exclude_reason": {"text": "普通 LED 灯器，与遨海能力不匹配", "field": "公告正文", "quote": "LED灯器采购项目"}}
                c.execute("""INSERT INTO reviews(source_tender_id,title,keyword_score,content,ai_status,bucket,prompt_version,ai_reason_json,synced_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""", (341, "广州航标处LED灯器采购项目中标结果公告", 42, "LED灯器采购项目中标结果公告", "approved", "market_intelligence", "aohai-review-v5-evidence", json.dumps(reason, ensure_ascii=False), ai_review.now()))
                c.commit(); c.close()
                self.assertEqual(audit_legacy_contradictions()["count"], 1)
                self.assertEqual(repair_legacy_contradictions()["repaired"], 1)
                c = ai_review.conn()
                row = c.execute("SELECT ai_status,bucket,policy_version FROM reviews WHERE source_tender_id=341").fetchone()
                self.assertEqual((row["ai_status"], row["bucket"]), ("exclude", "exclude"))
                self.assertTrue(row["policy_version"])
                self.assertEqual(c.execute("SELECT COUNT(*) FROM review_history").fetchone()[0], 1)
                c.close()
        finally:
            ai_review.DB = original_db

    def test_generic_purchase_object_is_not_maritime_scope(self):
        record = {"title": "海事局政府集中采购情况", "buyer": "海事局", "content": "集中采购公务用车和打印机", "deadline_at": ""}
        facts = {"source_objects": [{"name": "公务用车", "field": "公告正文", "quote": "集中采购公务用车和打印机"}],
                 "participation": [], "business_scope": [], "project_stage": [], "exclusions": [], "risks": []}
        self.assertEqual(decide_from_facts(record, facts)["bucket"], "exclude")

    def test_contract_performance_and_delivery_acceptance_do_not_exclude_open_ais_tender(self):
        record = {"title": "岸基AIS系统补点工程公开招标公告", "buyer": "航海保障中心", "content": "采购岸基AIS系统。合同履行期限为签订后十个月，完成安装调试并通过验收。投标截止2026年09月20日", "deadline_at": "2026-09-20"}
        facts = {"source_objects": [{"name": "岸基AIS系统", "field": "采购需求", "quote": "采购岸基AIS系统"}],
                 "participation": [fact("投标截止", "投标截止2026年09月20日")],
                 "business_scope": [fact("岸基AIS系统", "采购岸基AIS系统")],
                 "project_stage": [fact("公开招标", "公开招标公告")], "exclusions": [], "risks": []}
        result = decide_from_facts(record, facts)
        self.assertEqual(result["bucket"], "direct_opportunity")
        self.assertFalse(result["exclude_reason"])

    def test_true_contract_and_acceptance_notices_have_specific_reasons(self):
        empty_facts = {"source_objects": [], "participation": [], "business_scope": [], "project_stage": [], "exclusions": [], "risks": []}
        contract = decide_from_facts({"title": "AIS设备采购合同公告", "buyer": "海事局", "content": "", "deadline_at": ""}, empty_facts)
        acceptance = decide_from_facts({"title": "AIS设备项目验收结果公告", "buyer": "海事局", "content": "", "deadline_at": ""}, empty_facts)
        self.assertEqual(contract["exclude_reason"]["rule_code"], "contract_notice")
        self.assertIn("合同阶段", contract["exclude_reason"]["text"])
        self.assertEqual(acceptance["exclude_reason"]["rule_code"], "acceptance_notice")
        self.assertNotIn("确定性排除条件", acceptance["exclude_reason"]["text"])

    def test_generic_smart_waterway_name_is_market_intelligence_not_direct(self):
        record = {"title": "六片区智慧航道项目询比公告", "buyer": "通信公司", "content": "现进行公开询比，具体内容详见技术需求书", "published_at": "2026-08-25", "deadline_at": ""}
        facts = {"source_objects": [], "participation": [fact("公开询比", "现进行公开询比")],
                 "business_scope": [fact("智慧航道项目", "六片区智慧航道项目")],
                 "project_stage": [fact("询比公告", "询比公告")], "exclusions": [], "risks": []}
        self.assertEqual(decide_from_facts(record, facts)["bucket"], "market_intelligence")

    def test_extraction_prompt_limits_fact_count_to_prevent_json_truncation(self):
        prompt = extraction_prompt_for({"title": "测试", "buyer": "", "region": "", "budget": "", "published_at": "", "deadline_at": "", "content": "正文"}, {"content_limit": 3000})
        self.assertIn("source_objects 最多2项", prompt)
        self.assertIn("最长60字", prompt)

    def test_full_dual_run_covers_every_persisted_review_without_changing_conclusions(self):
        import tempfile
        original_db = ai_review.DB
        try:
            with tempfile.TemporaryDirectory() as temp:
                ai_review.DB = Path(temp) / "review.db"
                c = ai_review.conn()
                for idx, status in enumerate(("approved", "exclude", "expired", "approved_manual"), start=1):
                    c.execute("""INSERT INTO reviews(source_tender_id,title,keyword_score,content,ai_status,bucket,synced_at)
                        VALUES(?,?,?,?,?,?,?)""", (idx, f"公告{idx}", 10, "采购岸基AIS系统", status,
                                                       "direct_opportunity" if status != "exclude" else "exclude", ai_review.now()))
                c.commit(); c.close()
                result = ai_review.dual_run(None, live=False)
                self.assertEqual((result["scope"], result["selected"], result["processed"], result["failed"]),
                                 ("all_persisted_reviews", 4, 4, 0))
                c = ai_review.conn()
                self.assertEqual(c.execute("SELECT COUNT(*) FROM review_evaluations WHERE run_id=?", (result["run_id"],)).fetchone()[0], 4)
                self.assertEqual(c.execute("SELECT COUNT(*) FROM reviews WHERE ai_status='approved_manual'").fetchone()[0], 1)
                c.close()
        finally:
            ai_review.DB = original_db
