"""Machine-readable governance contract for the bidding radar.

This module is deliberately dependency-free so ingestion, AI review, command
line tools and tests can all use the same contract.  It does not persist data
or call external services.
"""
from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any


RULEBOOK_VERSION = "2026.08.31-governance-v7"
AI_HARNESS_VERSION = "two-stage-v6-fact-object-decision"

# One shared, structured policy is consumed by ingestion, AI review, tests and
# the rule/help views.  Raw announcement content is deliberately absent from
# these functions: it may validate a quote, but it may never classify a row.
TITLE_HARD_EXCLUSION_RULES = (
    ("recruitment", r"公开招聘|招聘.{0,12}(?:人员|工作人员|岗位|人才)|招录.{0,8}(?:人员|公务员)|考录|拟聘用|拟录用|录用.{0,8}(?:名单|公示|公告|公务员)|面试.{0,8}(?:公告|名单|安排)|报到公告", "公告标题明确为招聘或录用信息，不属于采购商机"),
    ("rental", r"(?:房屋|商铺|资产|场地).{0,8}(?:招租|出租)|招租(?:公告)?$|租赁权|经营权(?:出租|出让)", "公告标题明确为招租或经营权出让，不属于采购商机"),
    ("cancelled", r"废标|流标|终止公告|暂停公告|作废公告", "公告标题明确为废标、流标或终止项目，已无有效参与入口"),
    ("contract_notice", r"合同公告|采购合同(?:公告|公示)|合同签订(?:公告|公示)", "公告标题明确为合同阶段公告，已无有效参与入口"),
    ("acceptance_notice", r"验收公告|验收结果|项目验收(?:公告|公示|报告)", "公告标题明确为验收阶段公告，已无有效参与入口"),
    ("environmental_notice", r"环境影响(?:评价)?报告(?:书)?(?:全本)?(?:及.*)?公示|环评(?:报告)?(?:书)?(?:全本)?(?:及.*)?公示", "公告标题明确为环评或公众参与公示，不属于采购商机"),
    ("power_ais", r"空气绝缘(?:开关|设备)|AIS\s*(?:开关柜|开关设备)|变电(?:站|设备).{0,12}(?:采购|招标)|输配电设备", "公告标题明确为电力设备采购，其中 AIS 不代表船舶自动识别系统"),
)

NON_CAPABILITY_OBJECT_RULES = (
    ("ordinary_lighting", r"普通\s*led|led\s*(?:灯|灯器|照明)|普通\s*照明|一般\s*照明", "公告明确采购普通照明或 LED 灯器，超出遨海可供货范围"),
    ("power_ais", r"空气绝缘|AIS\s*(?:开关柜|开关设备)|开关柜|变电设备|输配电设备", "公告明确采购电力设备，其中 AIS 不代表船舶自动识别系统"),
)

CORE_PRODUCT_RE = re.compile(
    r"岸基\s*AIS|船载\s*AIS|插卡(?:式)?\s*AIS|AIS\s*(?:系统|基站|岸基站|船载终端|终端|设备|实体航标|配备|核心网|数据)|"
    r"VDES\s*(?:系统|基站|岸基站|船载终端|终端|设备|核心网|卫星载荷)|"
    r"船站|船载终端|岸基站|船岸(?:通信|无线)|航标遥测|航标遥控|ECDIS|电子海图|INS\s*综合导航|AIS\s*大数据",
    re.I,
)
CONCRETE_SCOPE_RE = re.compile(
    r"海事通信|通信导航|船舶信息|船舶定位|通航安全|航标遥测|航标遥控|船岸通信|"
    r"电子海图|综合导航|港航监管|海事监管|调度监管|船舶态势|轨迹回放|电子围栏|"
    r"AIS\s*数据|港航数据平台|海洋数据平台|监管数据平台|VTS\s*(?:系统|建设|改造)|船舶交通管理系统",
    re.I,
)
INTEGRATION_SCOPE_RE = re.compile(r"智慧航道|智慧船闸|智慧港口|智慧海洋|航海保障|港航监管|VTS\s*(?:系统|建设|改造)|船舶交通管理系统", re.I)


def _fact_value(item: dict[str, Any]) -> str:
    """Return the model's validated semantic value, never its evidence quote.

    Quotes prove that a fact exists in the corpus.  They intentionally do not
    drive a rule because nearby words can belong to background or boilerplate.
    """
    return " ".join(str(item.get(key) or "") for key in ("name", "text"))


def title_hard_exclusion_fact(title: str) -> dict[str, str] | None:
    """Return a hard exclusion based on the structured title phase only."""
    for code, expression, reason in TITLE_HARD_EXCLUSION_RULES:
        if re.search(expression, title or "", re.I):
            return {"rule_code": code, "text": reason, "field": "公告标题", "quote": (title or "")[:80]}
    return None


def object_hard_exclusion_fact(source_objects: list[dict[str, Any]]) -> dict[str, str] | None:
    """Return an exclusion only when a verified procurement object proves it."""
    for item in source_objects:
        value = _fact_value(item)
        for code, expression, reason in NON_CAPABILITY_OBJECT_RULES:
            if re.search(expression, value, re.I):
                return {"rule_code": code, "text": reason, "field": str(item.get("field") or "采购对象"), "quote": str(item.get("quote") or "")[:80]}
    return None


def core_product_fact(source_objects: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find a concrete core product exclusively in verified source objects."""
    for item in source_objects:
        value = _fact_value(item)
        if any(re.search(rule[1], value, re.I) for rule in NON_CAPABILITY_OBJECT_RULES):
            continue
        if CORE_PRODUCT_RE.search(value):
            return item
    return None


def concrete_business_scope_fact(business_scope: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find concrete business scope; generic smart-project names do not count."""
    return next((item for item in business_scope if CONCRETE_SCOPE_RE.search(_fact_value(item))), None)


def integration_scope_fact(business_scope: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((item for item in business_scope if INTEGRATION_SCOPE_RE.search(_fact_value(item))), None)

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
