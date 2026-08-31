"""Machine-readable governance contract for the bidding radar.

This module is deliberately dependency-free so ingestion, AI review, command
line tools and tests can all use the same contract.  It does not persist data
or call external services.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


RULEBOOK_VERSION = "2026.08.31-governance-v6"
AI_HARNESS_VERSION = "two-stage-v5-contextual-exclusions"

# These are the only display states a source tender may take in the formal
# review workflow.  Transitions are enforced in the review service and logged
# by callers; the original tender record is never deleted by this state change.
STATE_TRANSITIONS = {
    "pending": {"approved", "exclude", "failed", "expired"},
    "failed": {"pending", "approved", "exclude", "expired"},
    "approved": {"approved_manual", "rejected_manual", "expired"},
    "exclude": {"approved_manual", "rejected_manual", "expired"},
    "approved_manual": {"expired"},
    "rejected_manual": {"expired"},
    "expired": {"pending"},
}

DISPLAY_BUCKETS = frozenset({"direct_opportunity", "market_intelligence"})
AI_BUCKETS = DISPLAY_BUCKETS | {"exclude"}
PROJECT_TYPES = frozenset({"direct_product", "integration_project", "early_stage", "other"})
EVIDENCE_FIELDS = frozenset({
    "公告标题", "公告正文", "项目名称", "采购项目名称", "采购需求", "技术参数", "采购方式",
    "采购单位", "获取文件时间", "响应截止时间", "开标时间", "资格要求", "联合体要求", "预算金额", "公告期限",
})

AI_EXTRACTION_SCHEMA = {
    "type": "object",
    "required": ["source_objects", "participation", "business_scope", "project_stage", "exclusions", "risks"],
    "properties": {
        "source_objects": "array[{name,field,quote}]",          # 原文明确采购对象
        "participation": "array[{text,field,quote}]",           # 报名/投标/截止等参与证据
        "business_scope": "array[{text,field,quote}]",          # 通信导航、通航安全等业务范围
        "project_stage": "array[{text,field,quote}]",           # 招标、结果、可研等阶段事实
        "exclusions": "array[{text,field,quote}]",              # 仅原文明确的排除事实
        "risks": "array[{text,field,quote}]",                   # 资格、清单不足等待核实事实
    },
}


def effective_rulebook(rules: dict[str, Any]) -> dict[str, Any]:
    """Return the auditable effective rules with a stable content digest."""
    body = {"version": RULEBOOK_VERSION, "rules": deepcopy(rules)}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body["digest"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return body


def can_transition(source: str, target: str) -> bool:
    """Return whether a formal review state change is permitted."""
    return target in STATE_TRANSITIONS.get(source, set())


def transition_error(source: str, target: str) -> str:
    if can_transition(source, target):
        return ""
    return f"不允许评审状态从 {source or '未知'} 变为 {target or '未知'}"


def validate_extraction_shape(payload: object) -> str:
    """Validate the model's first-stage envelope before any decision is made."""
    if not isinstance(payload, dict):
        return "第一阶段输出必须是对象"
    for key in AI_EXTRACTION_SCHEMA["required"]:
        if not isinstance(payload.get(key), list):
            return f"第一阶段字段 {key} 必须是数组"
    return ""
