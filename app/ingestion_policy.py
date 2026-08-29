"""Central, deterministic policy for deciding whether an announcement may enter the radar.

The policy is intentionally separate from fetchers and scoring: the same checks are
used when ingesting fresh pages and when auditing older database records.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


NON_OPPORTUNITY_TITLE_RE = re.compile(
    r"招聘|招录|考录|拟聘用|拟录用|录用|录取|面试|报到|"
    r"废标|流标|终止公告|暂停公告|作废公告|合同公告|验收公告|"
    r"环境影响(?:评价)?报告(?:书)?(?:全本)?(?:及.*)?公示|环评(?:报告)?(?:书)?(?:全本)?(?:及.*)?公示"
)
PROCURE_FEATURE_RE = re.compile(
    r"招标|采购|中标|成交|磋商|竞谈|竞价|询价|比选|谈判|单一来源|公告|公示|候选人|结果|出让|转让|拍卖|选聘|征集|预选|招募|挂网"
)
NOISE_TITLE_RE = re.compile(
    r"通知|通告|办法|管控措施|征求意见|解读|救助|搜救|护航|台风|普法|宣贯|讲事故|碰撞|启用|"
    r"考试|考录|面试|录用|复审|许可|资质|评选|考核|监督检查|工作计划|工作总结|执行情况|评估|"
    r"部门预算|工资总额|薪酬|目录|名单|说明$|确认$"
)
PROCURE_STRUCT_RE = re.compile(r"标段|监理|勘察设计|测量|扫测|维保|维修保养|技术服务|设计咨询|工程|项目")
SITE_NAME_RE = re.compile(r"^中华人民共和国|^交通运输部$|^海事局$|^西双版纳海事局$|^云南交通技师学院$")


def non_opportunity_reason(item: dict[str, Any]) -> str:
    """Return a hard-exclusion reason for titles that cannot be opportunities."""
    title = str(item.get("title") or "").strip()
    if not title:
        return "标题为空"
    if re.search(r"招聘|招录|考录|拟聘用|拟录用|录用|录取|面试|报到", title):
        return "招聘/录用公告"
    if re.search(r"废标|流标|终止公告|暂停公告|作废公告|合同公告|验收公告", title):
        return "废标或履约阶段公告"
    if re.search(r"环境影响(?:评价)?报告(?:书)?(?:全本)?(?:及.*)?公示|环评(?:报告)?(?:书)?(?:全本)?(?:及.*)?公示", title):
        return "环评/公众参与公示"
    if re.search(r"招租|房屋出租|商铺出租|资产出租|场地出租|经营权(?:出让|出租)", title):
        return "资产招租/经营权信息"
    # “北斗路、北斗镇”等地名不是卫星导航采购；有明确卫星/定位设备语义时才放行。
    if re.search(r"北斗(?:路|街|乡|镇|村|社区)", title) and not re.search(r"卫星|导航|定位|终端|授时|通信", title):
        return "地名北斗误匹配"
    return ""


def title_gate_reason(item: dict[str, Any], rules: dict[str, Any]) -> str:
    """Return a reason when a title lacks enough procurement signal to be ingested."""
    title = str(item.get("title") or "").strip()
    if PROCURE_FEATURE_RE.search(title):
        return ""
    if NOISE_TITLE_RE.search(title):
        return "新闻或政务通告标题"
    if SITE_NAME_RE.search(title):
        return "网站导航或机构名称"
    if title.endswith(("...", "…")) or PROCURE_STRUCT_RE.search(title):
        return ""
    lowered = title.lower()
    for category in rules.get("business_categories", []):
        for keyword in category.get("keywords", []):
            if keyword and str(keyword).lower() in lowered:
                return ""
    return "标题缺少招采和业务特征"


def source_scope_reason(item: dict[str, Any]) -> str:
    """Reject records whose URL is outside a source's explicitly crawled column."""
    codes = {part.strip() for part in str(item.get("source_code") or "").split(",") if part.strip()}
    path = urlparse(str(item.get("source_url") or "")).path
    if "hn_msa" in codes and path and not path.startswith("/xxgk_4_6/"):
        return "海南海事局非项目招标栏目"
    return ""


def ingestion_issue_reason(item: dict[str, Any], rules: dict[str, Any], score: int) -> str:
    """Return the single canonical reason an item must be excluded from the radar."""
    return (
        non_opportunity_reason(item)
        or source_scope_reason(item)
        or ("无业务关键词评分" if score <= 0 else "")
        or title_gate_reason(item, rules)
    )
