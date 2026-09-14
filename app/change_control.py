"""Change-impact classification used by release tooling, not by end users.

The developer/Agent owns this check.  It deliberately maps both file paths and
keywords to the business domains documented in rulebook A, so a change cannot
silently bypass the rule-document workflow merely because it lives in a UI or
utility file.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


DOMAINS = {
    "ingestion": "数据抓取、详情补全、入库、去重、时效或回收站",
    "classification": "评分、优先级或业务桶分类",
    "ai": "AI 输入、事实抽取、程序裁决、失败降级或人工确认",
    "presentation": "实时/历史/回收站/通知/帮助展示或推送",
    "governance": "状态机、审计、质量报告、告警或历史重审",
}

PATH_DOMAINS = {
    "app/ingestion_policy.py": {"ingestion"},
    "app/source_parsers.py": {"ingestion"},
    "app/radar.py": {"ingestion", "classification", "presentation", "governance"},
    "app/radar_notify.py": {"presentation"},
    "app/radar_quality_notify.py": {"governance", "presentation"},
    "services/ai-review/ai_review.py": {"ai", "classification", "presentation", "governance"},
    "app/governance.py": {"classification", "ai", "governance"},
    "app/change_control.py": {"governance"},
    "scripts/change_impact_check.py": {"governance"},
}

KEYWORDS = {
    "ingestion": ("fetch", "抓取", "入库", "去重", "deadline", "expired", "回收站"),
    "classification": ("score", "评分", "priority", "bucket", "direct_opportunity", "market_intelligence"),
    "ai": ("deepseek", "prompt", "事实抽取", "analyze", "manual", "人工", "失败降级"),
    "presentation": ("api/tenders", "api/history", "dingtalk", "钉钉", "帮助", "display"),
    "governance": ("state", "状态机", "audit", "质量", "alert", "重审", "rule_versions"),
}


def classify(paths: list[str], content: str = "") -> set[str]:
    """Return all potentially affected business domains.

    This is intentionally conservative: false positives request documentation
    review; false negatives would allow an undocumented behaviour change.
    """
    affected: set[str] = set()
    normalized = [Path(p).as_posix().lstrip("./") for p in paths]
    for path in normalized:
        for prefix, domains in PATH_DOMAINS.items():
            if path == prefix:
                affected.update(domains)
    lower = content.lower()
    for domain, words in KEYWORDS.items():
        if any(word.lower() in lower for word in words):
            affected.add(domain)
    return affected


def summary(domains: set[str]) -> list[str]:
    return [DOMAINS[name] for name in sorted(domains)]


def diff_digest(files: list[str], patch: str) -> str:
    """Bind a review to exactly one diff, including its file inventory."""
    payload = json.dumps({"files": sorted(files), "patch": patch}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_no_rule_change(review: object, files: list[str], patch: str) -> str:
    """Validate an auditable developer assessment, not proof of equivalence."""
    if not isinstance(review, dict):
        return "复核报告必须是 JSON 对象"
    if review.get("decision") != "no_rule_change":
        return "复核结论必须为 no_rule_change"
    if review.get("diff_sha256") != diff_digest(files, patch):
        return "复核报告已过期或不属于当前差异"
    if review.get("files") != sorted(files):
        return "复核文件清单必须与当前差异完全一致且已排序"
    for field in ("reviewer", "reason", "validation"):
        if not isinstance(review.get(field), str) or not review[field].strip():
            return f"复核报告缺少 {field}"
    return ""
