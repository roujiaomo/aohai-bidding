import json
import sys
import unittest
from pathlib import Path

sys.path[:0] = ["services/ai-review", "app"]
from ai_review import (decide_from_facts, audit_legacy_contradictions,
                       repair_legacy_contradictions, extraction_prompt_for,
                       _fact_groups, active_participation_evidence)
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
            if sample.get("facts"):
                facts = sample["facts"]
            elif "岸基AIS" in content:
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

    def test_raw_body_keywords_cannot_create_a_core_product(self):
        record = {"title": "办公设备采购公告", "buyer": "海事局", "content": "采购打印机。网页背景资料介绍AIS船舶监管系统。投标截止2026年09月20日", "published_at": "2026-08-31", "deadline_at": "2026-09-20"}
        facts = {"source_objects": [{"name": "打印机", "field": "采购需求", "quote": "采购打印机"}],
                 "participation": [fact("投标截止", "投标截止2026年09月20日")],
                 "business_scope": [], "project_stage": [fact("采购公告", "采购公告")], "exclusions": [], "risks": []}
        self.assertEqual(decide_from_facts(record, facts)["bucket"], "exclude")

    def test_raw_body_led_reference_cannot_exclude_verified_ais_object(self):
        record = {"title": "岸基AIS系统采购公告", "buyer": "航标处", "content": "采购岸基AIS系统。安装应避开LED灯具线路。投标截止2026年09月20日", "published_at": "2026-08-31", "deadline_at": "2026-09-20"}
        facts = {"source_objects": [{"name": "岸基AIS系统", "field": "采购需求", "quote": "采购岸基AIS系统"}],
                 "participation": [fact("投标截止", "投标截止2026年09月20日")],
                 "business_scope": [fact("海事通信", "岸基AIS系统")], "project_stage": [fact("采购公告", "采购公告")], "exclusions": [], "risks": []}
        self.assertEqual(decide_from_facts(record, facts)["bucket"], "direct_opportunity")

    def test_single_ais_word_is_not_a_core_product(self):
        record = {"title": "数据分析服务采购公告", "buyer": "研究院", "content": "报告参考AIS数据。公开询比。", "published_at": "2026-08-31", "deadline_at": ""}
        facts = {"source_objects": [{"name": "AIS", "field": "公告正文", "quote": "报告参考AIS数据"}],
                 "participation": [fact("公开询比", "公开询比")], "business_scope": [],
                 "project_stage": [fact("采购公告", "采购公告")], "exclusions": [], "risks": []}
        self.assertEqual(decide_from_facts(record, facts)["bucket"], "exclude")

    def test_procurement_title_completes_only_a_full_core_product_phrase(self):
        record = {"title": "高港船闸AIS岸基基站设备采购公告（二次）", "buyer": "船闸管理处",
                  "region": "江苏", "content": "投标截止时间：2026年09月20日", "deadline_at": "2026-09-20"}
        corpus = " ".join(str(record.get(k) or "") for k in ("title", "buyer", "region", "content"))
        payload = {"source_objects": [], "participation": [fact("投标截止", "投标截止时间：2026年09月20日")],
                   "business_scope": [], "project_stage": [], "exclusions": [], "risks": []}
        facts = _fact_groups(payload, corpus, record)
        self.assertEqual(facts["source_objects"][0]["name"], "AIS岸基基站")
        self.assertEqual(decide_from_facts(record, facts)["bucket"], "direct_opportunity")

    def test_title_completion_never_promotes_standalone_ais_or_background_text(self):
        for record in (
            {"title": "AIS相关数据研究", "buyer": "研究院", "region": "", "content": "AIS", "deadline_at": ""},
            {"title": "办公设备采购公告", "buyer": "海事局", "region": "", "content": "背景资料介绍岸基AIS系统", "deadline_at": ""},
        ):
            corpus = " ".join(str(record.get(k) or "") for k in ("title", "buyer", "region", "content"))
            facts = _fact_groups({"source_objects": [], "participation": [], "business_scope": [], "project_stage": [], "exclusions": [], "risks": []}, corpus, record)
            self.assertEqual(facts["source_objects"], [])

    def test_semantic_participation_fact_accepts_a_verified_date_only_quote(self):
        record = {"title": "集成型插卡式AIS竞争性谈判公告", "published_at": "2026-08-31", "deadline_at": "2026-09-11"}
        item = {"text": "响应截止", "field": "响应截止时间", "quote": "2026年09月11日 09点00分"}
        self.assertEqual(active_participation_evidence(record, [item]), item)

    def test_closed_stage_fact_overrides_old_participation_sentence(self):
        record = {"title": "岸基AIS系统采购成交结果公告", "buyer": "海事局", "content": "原招标文件写投标截止2026年09月20日，现发布成交结果", "published_at": "2026-08-31", "deadline_at": "2026-09-20"}
        facts = {"source_objects": [{"name": "岸基AIS系统", "field": "采购项目名称", "quote": "岸基AIS系统采购"}],
                 "participation": [fact("原投标截止", "投标截止2026年09月20日")],
                 "business_scope": [fact("海事通信", "岸基AIS系统采购")],
                 "project_stage": [fact("成交结果", "现发布成交结果")], "exclusions": [], "risks": []}
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

    def test_history_reanalysis_preserves_manual_decisions_and_old_snapshots(self):
        import tempfile
        original_db = ai_review.DB
        original_deepseek = ai_review.deepseek
        try:
            with tempfile.TemporaryDirectory() as temp:
                ai_review.DB = Path(temp) / "review.db"
                c = ai_review.conn()
                for idx, status in enumerate(("approved", "expired", "approved_manual"), start=1):
                    c.execute("""INSERT INTO reviews(source_tender_id,title,keyword_score,content,ai_status,bucket,synced_at)
                        VALUES(?,?,?,?,?,?,?)""", (idx, f"公告{idx}", 80, "采购岸基AIS系统", status, "market_intelligence", ai_review.now()))
                c.commit(); c.close()
                candidate = {"bucket":"direct_opportunity","project_type":"direct_product","supplier_lead":False,
                             "fit_score":80,"confidence":0.9,"source_objects":[],"product_inferences":[],
                             "reasons":[],"risk_notes":[],"exclude_reason":{},"evidence":[]}
                ai_review.deepseek = lambda row, conf: (candidate, {"input":1,"output":1,"hit":0,"cost":0.0})
                result = ai_review.reanalyze_history_records()
                self.assertEqual((result["selected"], result["processed"], result["failed"]), (2, 2, 0))
                c = ai_review.conn()
                self.assertEqual(c.execute("SELECT COUNT(*) FROM reviews WHERE ai_status='approved_manual'").fetchone()[0], 1)
                self.assertEqual(c.execute("SELECT COUNT(*) FROM reviews WHERE ai_status='approved'").fetchone()[0], 2)
                self.assertEqual(c.execute("SELECT COUNT(*) FROM review_history").fetchone()[0], 2)
                c.close()
        finally:
            ai_review.DB = original_db
            ai_review.deepseek = original_deepseek
