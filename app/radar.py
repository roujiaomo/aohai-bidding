#!/usr/bin/env python3
"""遨海商机雷达：低配单机、本地优先的公告发现与跟进工具。"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import html as html_mod
import unicodedata
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

AUTH_MODULE_DIR = Path(os.getenv("AOHAI_AUTH_MODULE_DIR", str(Path(__file__).resolve().parents[1] / "ai_auth")))
if str(AUTH_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(AUTH_MODULE_DIR))
from shared_auth import (LOGIN_HTML, auth_enabled, clear_cookies, csrf_valid, current_user, init_auth,
                         login as auth_login, logout as auth_logout, session_cookies)
from ingestion_policy import ingestion_issue_reason, non_opportunity_reason, title_gate_reason
from source_parsers import parse_li_list

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "radar.db"
AI_REVIEW_DB = Path(os.getenv("AI_REVIEW_DB", "/opt/bidding-ai-review/data/ai_review.db"))
DEFAULT_CONFIG = ROOT / "config.json"

# 业务时区：北京时间。服务器可能跑在 UTC，所有“近/满 N 天”判断必须用同一时区，
# 否则入库过滤与页面过滤会差 8 小时，出现两边都看不到或两边都看到的记录。
CN_TZ = timezone(timedelta(hours=8))


def cn_today() -> datetime:
    """当前北京时间（带时区）。"""
    return datetime.now(timezone.utc).astimezone(CN_TZ)

CONFIG_DEFAULTS = {
    "page_size": 15,
    "filter_expired": True,
    "auto_expire_days": 30,
    "verify_links": True,
    "verify_link_timeout": 8,
    "retention_days": 365,
}

RULES_DEFAULTS = {
    "business_categories": [
        {"name": "AIS/VDES 与海事通信", "weight": 35, "keywords": ["ais", "vdes", "船舶自动识别", "岸基站", "船岸通信", "甚高频", "vhf", "航标遥测", "航标遥控", "海事卫星", "船载通信", "海上通信"]},
        {"name": "通航安全与智慧航道", "weight": 28, "keywords": ["vts", "通航安全", "船舶交通管理", "智慧航道", "数字航道", "电子航道图", "航道整治", "航标管理", "海事监管", "航道维护", "航道疏浚", "航道工程", "航标工程", "航标维保"]},
        {"name": "卫星互联网与航天软件", "weight": 20, "keywords": ["卫星互联网", "低轨卫星", "卫星通信", "卫星物联网", "星地融合", "卫星地面站", "卫星运营"]},
        {"name": "北斗与时空服务", "weight": 16, "keywords": ["北斗", "船舶定位", "时空大数据", "遥感监测", "高精度定位"]},
        {"name": "海洋资源与海岸工程", "weight": 18, "keywords": ["海洋观测", "海洋牧场", "海上风电", "海岸工程", "海洋测绘", "海底电缆", "海洋平台", "海洋装备", "海洋环境", "海洋调查", "海域使用", "海岛保护", "海洋生态", "海洋监测", "海洋数据", "智慧海洋", "蓝色经济"]},
        {"name": "相关延展场景", "weight": 10, "keywords": ["智慧港口", "港航一体化", "水上应急", "搜救", "河湖监管", "水运工程", "港口工程", "码头工程", "船舶管理", "航运管理"]},
    ],
    # 天眼查检索词（界面：数据规则 → 天眼查检索词配置）。每个词每次抓取消耗 1 次额度；
    # 默认 12 词 × 每天 6 次抓取 ≈ 72 次/天，低于账号日限额 100。可用 TYC_KEYWORDS 环境变量覆盖。
    "tyc_search_keywords": [
        "航道", "航标", "海事", "AIS", "VDES", "船舶", "甚高频",
        "海上通信", "卫星", "海洋风电", "智慧海洋", "智慧港口",
    ],
    "priority_regions": {
        "一级": ["辽宁", "山东", "北京", "上海", "江苏", "安徽", "江西", "湖北", "湖南", "重庆", "四川", "广东", "广西"],
        "二级": ["天津", "河北", "浙江", "福建", "海南"],
    },
    "priority_cities": ["武汉", "宜昌", "岳阳", "九江", "芜湖", "南京", "镇江", "扬州", "杭州", "嘉兴", "湖州", "广州", "佛山", "肇庆", "南宁", "梧州", "泸州", "宜宾"],
    "client_terms": ["海事局", "航道局", "交通运输", "港口", "港务", "应急管理", "水利", "中国移动", "中国电信", "中国联通", "国家电网", "南方电网", "中国石油", "中国石化", "中国海油", "国家能源", "华润", "国铁", "集团"],
    # 商机级别阈值（界面：数据规则 → 商机分类）：入库时按评分初始化优先级，可动态调整。
    # 分数 ≥ key_threshold → 重点关注；≥ follow_threshold → 值得跟进；其余 → 一般关注。仅影响入库初始值，不覆盖人工调整。
    "opportunity_levels": {"key_threshold": 80, "follow_threshold": 50},
    # 商机种类打标关键词（界面：数据规则 → 商机分类）：按列表顺序匹配标题，先命中者生效，都不命中不打标。
    "opportunity_categories": [
        {"name": "前期线索", "color": "橙", "keywords": ["勘察设计", "方案设计", "初步设计", "施工图设计", "可研", "可行性研究", "前期咨询", "设计咨询", "全过程咨询", "规划设计"]},
        {"name": "直接产品", "color": "绿", "keywords": ["ais", "vdes", "岸基站", "甚高频", "vhf", "航标遥测", "航标遥控", "船岸通信", "水上通信", "海事卫星", "船载通信", "北斗", "中高频", "navtex", "航行警告", "搜救雷达应答器", "epirb"]},
        {"name": "集成项目", "color": "蓝", "keywords": ["航道整治", "智慧航道", "数字航道", "电子航道图", "智慧港口", "港航一体化", "水运工程", "港口工程", "码头工程", "航道工程", "航标工程", "疏浚", "通航安全", "船舶交通管理", "vts", "海事监管", "智慧海事", "海洋观测", "智慧海洋", "水上应急", "航运管理", "航道养护", "航道测绘"]},
    ],
}

def load_config(path: Path = DEFAULT_CONFIG) -> dict:
    """加载配置文件，缺失字段用默认值补齐。"""
    cfg = dict(CONFIG_DEFAULTS)
    cfg["rules"] = _deep_copy(RULES_DEFAULTS)
    if path.is_file():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
            cfg.update(user)
            # 深度合并 rules
            if "rules" in user:
                for k, v in RULES_DEFAULTS.items():
                    if k not in cfg["rules"]:
                        cfg["rules"][k] = _deep_copy(v)
        except Exception:
            pass
    return cfg

def _deep_copy(obj):
    if isinstance(obj, dict): return {k: _deep_copy(v) for k, v in obj.items()}
    if isinstance(obj, list): return [_deep_copy(v) for v in obj]
    return obj

def save_config(cfg: dict, path: Path = DEFAULT_CONFIG):
    """持久化配置到 JSON 文件。"""
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

SOURCES = [
    # ---- 已接入（已实现适配器，可稳定获取公告列表）----
    ("cjhdj", "长江航道局", "https://www.cjhdj.com.cn/", "public_http", "connected", "招标公告列表页为静态 HTML，适配器已实现。"),
    ("csg", "南方电网供应链平台", "https://www.bidding.csg.cn/", "public_http", "connected", "采购公告列表页为静态 HTML，适配器已实现。投标另需企业认证。"),
    ("crc", "华润守正电子招标", "https://szecp.crc.com.cn/", "public_http", "connected", "招标公告列表页为静态 HTML（需 SSL legacy 兼容），适配器已实现。"),
    # ---- 待接入（公开但列表页为 JS 动态渲染，标准库无法解析）----
    ("ln_ggzy", "辽宁省公共资源交易平台", "https://ggzy.ln.gov.cn/", "public_http", "covered", "已由全国公共资源交易平台（ggzy）按省拉取覆盖，无需单独适配。"),
    ("sd_ggzy", "山东省公共资源交易中心", "https://ggzyjy.shandong.gov.cn/", "public_http", "covered", "已由全国公共资源交易平台（ggzy）按省拉取覆盖，无需单独适配。"),
    ("bj_ggzy", "北京公共资源交易服务平台", "https://ggzyfw.beijing.gov.cn/", "public_http", "covered", "已由全国公共资源交易平台（ggzy）按省拉取覆盖，无需单独适配。"),
    ("msa", "交通运输部海事局", "https://www.msa.gov.cn/", "public_http", "connected", "招标信息栏目（含部机关及直属海事系统采购/中标公告）静态列表，适配器已实现。"),
    ("cjhy", "长江航务管理局", "https://cjhy.mot.gov.cn/", "public_http", "connected", "长江水系聚合源：长航局公告 + 重大建设项目招标公告，TRS 静态列表，适配器已实现。"),
    ("hb_msa", "河北海事局", "https://www.hb.msa.gov.cn/", "public_http", "connected", "海事项目招标公告 + 中标（成交）结果栏目，静态列表，适配器已实现。"),
    ("cjkhd", "长江口航道管理局", "https://www.cjkhd.com/", "public_http", "connected", "信息公开栏目（招标公告/中标候选人公示），TRS 静态列表，适配器已实现。"),
    ("ln_msa", "辽宁海事局", "https://www.ln.msa.gov.cn/", "public_http", "connected", "招标信息/中标信息/政府集中采购三栏目，静态列表，适配器已实现。"),
    ("gd_msa", "广东海事局", "http://gd.msa.gov.cn/", "public_http", "connected", "招标信息栏目（采购量大），静态列表，适配器已实现。"),
    ("cj_msa", "长江海事局", "http://cj.msa.gov.cn/", "public_http", "connected", "海事项目招标公告 + 中标公告栏目，TRS 静态列表，适配器已实现。与长航局公告部分重合，去重合并。"),
    ("sd_msa", "山东海事局", "https://www.sd.msa.gov.cn/", "public_http", "connected", "大汉 CMS：首页提取文章链接 + 详情页面包屑识别采购栏目，适配器已实现。"),
    ("js_msa", "江苏海事局", "http://www.js.msa.gov.cn/", "public_http", "connected", "大汉 CMS 通知公告栏目（含招标信息），适配器已实现，由评分管线筛选。"),
    ("zj_msa", "浙江海事局", "https://www.zj.msa.gov.cn/", "public_http", "connected", "采购信息栏目（/ZJ/ 子站），TRS 静态列表，适配器已实现。"),
    ("hn_msa", "海南海事局", "https://www.hn.msa.gov.cn/", "public_http", "connected", "项目招标栏目（JEECMS，需 https），适配器已实现。"),
    ("cq_jtw", "重庆市交通运输委", "https://jtysw.cq.gov.cn/", "public_http", "connected", "招标公告栏目（含港航海事中心采购），TRS 静态列表，适配器已实现。"),
    ("sc_jtt", "四川省交通运输厅", "https://jtt.sc.gov.cn/", "public_http", "connected", "招投标信息专栏 + 采购公告栏目，适配器已实现。"),
    ("henan_jt", "河南省交通运输厅", "https://jtyst.henan.gov.cn/", "public_http", "connected", "政府采购 + 通知公告栏目（含水运工程招标/中标公示），适配器已实现。"),
    ("yn_hw", "云南省航务管理局", "https://www.ynshwglj.org.cn/", "public_http", "connected", "通知公告栏目（澜沧江/金沙江航运），适配器已实现，由评分管线筛选。"),
    ("hlj_jt", "黑龙江省交通运输厅", "https://jt.hlj.gov.cn/", "public_http", "connected", "通知公告栏目，适配器已实现，由评分管线筛选。"),
    ("hlj_msa", "黑龙江海事局", "https://hlj.msa.gov.cn/", "public_http", "connected", "栏目页 JS 渲染，改用首页提取文章链接 + 面包屑识别采购栏目，适配器已实现。"),
    ("fj_msa", "福建海事局", "https://www.fj.msa.gov.cn/", "public_http", "connected", "无独立采购栏目入口，首页文章链接粗筛后交评分管线，适配器已实现。"),
    ("ah_msa", "安徽省地方海事中心", "http://www.msa.ah.cn/", "public_http", "not_automated", "JSP 站内无招标/采购栏目（仅财务预决算），采购公告由政采网检索兜底。"),
    ("jx_gh", "江西省高等级航道中心", "http://jxgh.jt.jiangxi.gov.cn/", "public_http", "not_automated", "站点内容 JS 渲染且无招标栏目入口，采购公告由政采网检索兜底。"),
    ("zjhy", "珠江航务管理局", "https://zjhy.mot.gov.cn/", "public_http", "not_automated", "信息公开栏目条目 JS 动态加载，未找到可用接口，采购公告由政采网检索兜底。"),
    ("sd_port", "山东港口阳光慧采", "https://yghc.sd-port.com/", "public_http", "connected", "招标公告/采购寻源/采购成交三栏目静态列表，适配器已实现。"),
    ("tj_port", "天津港集团", "https://www.ptacn.com/", "public_http", "connected", "集团公告频道静态列表，适配器已实现，由评分管线筛选。"),
    ("ln_port", "辽港集团", "http://www.liaoningport.com/", "public_http", "connected", "通知公告栏目静态列表，适配器已实现，由评分管线筛选。"),
    ("bbw_port", "北部湾港招采平台", "https://zc.bbwport.com/", "public_http", "pending_structure", "列表页 Vue 前端渲染，数据接口待确认；采购/澄清/结果公告分类清晰。"),
    ("zj_hg", "浙江海港电子招采", "https://hgdzzb.nbport.com.cn/", "public_http", "pending_structure", "列表 JS 渲染，purchaseDown 页待摸清数据接口。"),
    ("gz_sun", "广州阳光采购", "https://www.gzsun.com.cn/", "public_http", "pending_structure", "采购大厅 JS 渲染，数据接口待确认。"),
    ("js_port", "江苏港口集团", "http://www.portjs.cn/", "public_http", "pending_structure", "通知公告栏目首页无静态条目，列表渲染方式待确认。"),
    ("sbhw", "苏北航务管理处", "http://www.sbhw.cn/", "public_http", "unreachable", "连接被重置，多次重试失败，采购公告由政采网检索兜底。"),
    ("hb_port", "河北港口集团", "http://www.porthebei.com/", "public_http", "unreachable", "连接超时，多次重试失败。"),
    ("fj_port", "福建港口集团", "http://www.fjpg.cn/", "public_http", "unreachable", "连接超时，多次重试失败。"),
    ("sh_port", "上港集团", "https://www.portshanghai.com.cn/", "protected", "not_automated", "HTTP 521（CDN 回源拦截），标准库无法访问。"),
    ("gd_hy", "广东港航集团", "https://www.hangyun.com.cn/", "public_http", "not_automated", "企业官网无招标栏目入口，采购公告由政采网检索兜底。"),
    ("tj_msa", "天津海事局", "https://www.tj.msa.gov.cn/", "protected", "not_automated", "HTTP 412 反爬拦截，暂不自动化；其采购公告由政采网检索兜底。"),
    ("sz_msa", "深圳海事局", "http://www.sz.msa.gov.cn/", "protected", "not_automated", "列表页瑞数动态防护（202 + $_ts），标准库无法绕过；采购公告由政采网检索兜底。"),
    ("lyg_msa", "连云港海事局", "http://www.lyg.msa.gov.cn/", "protected", "not_automated", "HTTP 412 反爬拦截，暂不自动化。"),
    ("cmcc", "中国移动采购与招标网", "https://b2b.10086.cn/", "public_http", "pending_js", "SPA 单页应用（首屏仅 855 字节），需 JS 渲染。"),
    ("ceb", "中国招标投标公共服务平台", "https://www.cebpubservice.com/", "public_http", "connected", "发改委指定法定媒介。bulletin.cebpubservice.com 招标公告列表页静态 HTML，适配器已实现（第1页，后续页需验证码）。"),
    ("ccgp", "中国政府采购网", "https://www.ccgp.gov.cn/", "public_http", "connected", "中央/地方公开招标公告列表页为静态 HTML，适配器已实现（前3页）。"),
    ("ccgp_search", "中国政府采购网·检索", "http://search.ccgp.gov.cn/", "public_http", "connected", "bxsearch 按行业检索词搜全国（含地方）采购公告，服务端渲染静态 HTML。有频控：独立定时每天 2 轮、词间限速，不进常规抓取。"),
    ("hn_ggzy", "湖南省公共资源交易平台", "https://www.hnsggzy.com/", "public_api", "connected", "tradeApi 接口：listByFile 按关键词搜全省工程招标公告（汇聚 14 市州），详情接口返回完整公告正文，无反爬。"),
    ("ggzy", "全国公共资源交易平台", "https://www.ggzy.gov.cn/", "public_api", "connected", "发改委牵头，汇聚全国各省交易数据。SPA 页面但后端 JSON API 可直接 POST 调用，适配器已实现。"),
    ("chnenergy", "国家能源集团国能e招", "https://www.chnenergybidding.com.cn/", "public_http", "pending_js", "首屏仅 151 字节 JS 跳转页，无实际内容。"),
    # ---- 需账号授权 ----
    ("caizhao", "采招网", "https://www.bidcenter.com.cn/", "account_required", "awaiting_authorization", "会员账号补盲；后续按账户权限、条款和允许方式接入。"),
    ("tianyancha", "天眼查", "https://www.tianyancha.com/", "official_api", "connected", "已通过天眼AI官方授权（tyc CLI OAuth）接入：fetch 按业务关键词搜索全国招标公告，enrich 对高分采购单位做画像。按账号额度调用。"),
    # ---- 反爬防护，暂不自动化 ----
    ("sgcc", "国家电网 ECP", "https://ecp.sgcc.com.cn/", "protected", "not_automated", "SPA hash 路由（#/），首屏 9KB 壳；一期使用公开同步公告或人工核验。"),
    ("unicom", "中国联通电子招投标", "https://www.chinaunicombidding.cn/", "protected", "not_automated", "HTTP 412 Precondition Failed（反爬拦截）；详情可能需登录。"),
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  code TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
  access_mode TEXT NOT NULL, status TEXT NOT NULL, notes TEXT NOT NULL,
  last_checked_at TEXT, last_success_at TEXT, last_error TEXT
);
CREATE TABLE IF NOT EXISTS tenders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL UNIQUE, source_code TEXT NOT NULL,
  source_url TEXT NOT NULL, title TEXT NOT NULL, buyer TEXT DEFAULT '',
  region TEXT DEFAULT '', budget REAL, published_at TEXT DEFAULT '', deadline_at TEXT DEFAULT '',
  content TEXT DEFAULT '', score INTEGER NOT NULL, match_json TEXT NOT NULL,
  followup_status TEXT NOT NULL DEFAULT 'new', assignee TEXT DEFAULT '', notes TEXT DEFAULT '',
  priority TEXT DEFAULT '一般关注', is_deleted INTEGER DEFAULT 0,
  category TEXT DEFAULT '', agency TEXT DEFAULT '', winner TEXT DEFAULT '',
  evidence_status TEXT DEFAULT '',
  link_ok INTEGER DEFAULT 1,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tenders_score ON tenders(score DESC);
CREATE INDEX IF NOT EXISTS idx_tenders_deadline ON tenders(deadline_at);
CREATE TABLE IF NOT EXISTS buyer_profiles (
  buyer TEXT PRIMARY KEY, company_name TEXT DEFAULT '', credit_code TEXT DEFAULT '',
  reg_status TEXT DEFAULT '', legal_person TEXT DEFAULT '', reg_capital TEXT DEFAULT '',
  estiblished TEXT DEFAULT '', reg_location TEXT DEFAULT '', tags TEXT DEFAULT '',
  match_status TEXT DEFAULT '', raw_json TEXT DEFAULT '', enriched_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS tender_search USING fts5(title, buyer, region, content, content='tenders', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS tenders_ai AFTER INSERT ON tenders BEGIN
  INSERT INTO tender_search(rowid,title,buyer,region,content) VALUES (new.id,new.title,new.buyer,new.region,new.content);
END;
CREATE TRIGGER IF NOT EXISTS tenders_au AFTER UPDATE ON tenders BEGIN
  INSERT INTO tender_search(tender_search,rowid,title,buyer,region,content) VALUES('delete',old.id,old.title,old.buyer,old.region,old.content);
  INSERT INTO tender_search(rowid,title,buyer,region,content) VALUES (new.id,new.title,new.buyer,new.region,new.content);
END;
"""

def now() -> str:
    return datetime.now(timezone.utc).astimezone().replace(microsecond=0).isoformat()

def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # 迁移：去除 tenders.source_code 外键（多来源合并存逗号串如 tianyancha,ccgp_search，单值外键会拦截）
    try:
        tbl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tenders'").fetchone()
        if tbl and "FOREIGN KEY" in (tbl[0] or ""):
            cols = ("id,fingerprint,source_code,source_url,title,buyer,region,budget,published_at,deadline_at,"
                    "content,score,match_json,followup_status,assignee,notes,priority,is_deleted,created_at,updated_at,link_ok")
            conn.executescript("""
BEGIN;
CREATE TABLE tenders_new (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fingerprint TEXT NOT NULL UNIQUE, source_code TEXT NOT NULL,
  source_url TEXT NOT NULL, title TEXT NOT NULL, buyer TEXT DEFAULT '',
  region TEXT DEFAULT '', budget REAL, published_at TEXT DEFAULT '', deadline_at TEXT DEFAULT '',
  content TEXT DEFAULT '', score INTEGER NOT NULL, match_json TEXT NOT NULL,
  followup_status TEXT NOT NULL DEFAULT 'new', assignee TEXT DEFAULT '', notes TEXT DEFAULT '',
  priority TEXT DEFAULT '一般关注', is_deleted INTEGER DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, link_ok INTEGER DEFAULT 1
);
INSERT INTO tenders_new(%s) SELECT %s FROM tenders;
DROP TABLE tenders;
ALTER TABLE tenders_new RENAME TO tenders;
COMMIT;
""" % (cols, cols))
    except sqlite3.Error:
        pass
    try:
        conn.execute("ALTER TABLE tenders ADD COLUMN link_ok INTEGER DEFAULT 1")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE tenders ADD COLUMN priority TEXT DEFAULT '一般关注'")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE tenders ADD COLUMN is_deleted INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    # 新增字段：商机分类打标 + 代理机构 + 中标单位（设计院）
    for _coldef in ("category TEXT DEFAULT ''", "agency TEXT DEFAULT ''", "winner TEXT DEFAULT ''", "evidence_status TEXT DEFAULT ''"):
        try:
            conn.execute(f"ALTER TABLE tenders ADD COLUMN {_coldef}")
        except sqlite3.OperationalError:
            pass
    # 存量数据迁移（仅当 tenders 表已存在；全新库由后续 init_db 建表）
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tenders'").fetchone():
        # 迁移：分类名“前期阶段”统一更名为“前期线索”
        conn.execute("UPDATE tenders SET category='前期线索' WHERE category='前期阶段'")
        # 迁移：根据 score 初始化 priority（仅对尚未设置的记录）；阈值可在“数据规则 → 商机分类”配置
        _lv = (load_config().get("rules") or {}).get("opportunity_levels") or {}
        try: _kt = int(_lv.get("key_threshold", 80))
        except (TypeError, ValueError): _kt = 80
        try: _ft = int(_lv.get("follow_threshold", 50))
        except (TypeError, ValueError): _ft = 50
        conn.execute("UPDATE tenders SET priority='重点关注' WHERE (priority IS NULL OR priority='一般关注') AND score>=?", (_kt,))
        conn.execute("UPDATE tenders SET priority='值得跟进' WHERE (priority IS NULL OR priority='一般关注') AND score>=? AND score<?", (_ft, _kt))
        conn.commit()
    return conn

def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()

def seed_sources(conn: sqlite3.Connection) -> None:
    conn.executemany("""INSERT INTO sources(code,name,base_url,access_mode,status,notes)
      VALUES(?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name,base_url=excluded.base_url,
      access_mode=excluded.access_mode,status=excluded.status,notes=excluded.notes""", SOURCES)
    conn.commit()

def text_of(item: dict) -> str:
    return " ".join(str(item.get(k, "")) for k in ("title", "buyer", "region", "content")).lower()


def ai_review_bucket_ids(bucket: str) -> set[int] | None:
    """读取 AI 后台分流结果。无 AI 库时返回 None，保留传统列表行为。"""
    if not AI_REVIEW_DB.exists():
        return None
    status_map = {
        "direct": ("direct_opportunity", "approved", "approved_manual"),
        "market": ("market_intelligence",),
    }
    statuses = status_map.get(bucket, ())
    if not statuses:
        return set()
    try:
        review_conn = sqlite3.connect(f"file:{AI_REVIEW_DB}?mode=ro", uri=True)
        marks = {int(r[0]) for r in review_conn.execute(
            f"SELECT source_tender_id FROM reviews WHERE ai_status IN ({','.join('?' for _ in statuses)})", statuses
        ).fetchall()}
        review_conn.close()
        return marks
    except Exception:
        return None


def apply_ai_review_gate(items: list[dict]) -> list[dict]:
    """给副本列表补充 AI 标签；实时列表的筛选由查询阶段完成，避免分页错位。"""
    if not items or not AI_REVIEW_DB.exists():
        return items
    try:
        review_conn = sqlite3.connect(f"file:{AI_REVIEW_DB}?mode=ro", uri=True)
        review_conn.row_factory = sqlite3.Row
        ids = [int(x["id"]) for x in items]
        marks = {r["source_tender_id"]: dict(r) for r in review_conn.execute(
            f"SELECT source_tender_id,ai_status,project_type,supplier_lead FROM reviews WHERE source_tender_id IN ({','.join('?' for _ in ids)})", ids
        ).fetchall()}
        review_conn.close()
    except Exception:
        return items  # AI 服务暂不可用时不影响原雷达副本展示
    for item in items:
        mark = marks.get(item["id"])
        if mark:
            item["ai_status"] = mark["ai_status"]
            # AI 分类是后台分流依据，不在用户页面展示标签。
    return items


def sync_ai_review_candidates() -> int | None:
    """抓取完成后通知独立 AI 评审服务同步候选；失败不影响商机抓取。"""
    review_url = os.getenv("AI_REVIEW_SYNC_URL", "http://127.0.0.1:8791/api/sync")
    try:
        headers = {"Content-Type": "application/json"}
        if os.getenv("AOHAI_INTERNAL_TOKEN"):
            headers["X-Aohai-Internal-Token"] = os.environ["AOHAI_INTERNAL_TOKEN"]
        req = Request(review_url, data=b"", headers=headers, method="POST")
        with urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return int(data.get("synced", 0))
    except Exception as exc:
        print(f"  AI 评审候选自动同步失败（不影响本次抓取）：{exc}")
        return None


def auto_analyze_ai_review() -> dict | None:
    """抓取后自动评审：只会处理 pending/failed 状态，不会重评已有结论或人工结论。"""
    review_url = os.getenv("AI_REVIEW_SYNC_URL", "http://127.0.0.1:8791/api/sync")
    analyze_url = review_url.rsplit("/", 1)[0] + "/analyze"
    try:
        # 对所有尚未评审的新增公告完成一次分流；AI 服务自身仍会按日限额兜底。
        body = json.dumps({"limit": 500}).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if os.getenv("AOHAI_INTERNAL_TOKEN"):
            headers["X-Aohai-Internal-Token"] = os.environ["AOHAI_INTERNAL_TOKEN"]
        req = Request(analyze_url, data=body, headers=headers, method="POST")
        with urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        print(f"  AI 自动评审失败（候选已保留，稍后可重试）：{exc}")
        return None


# AIS 在电力行业也可指 Air Insulated Switchgear（空气绝缘开关设备）。不能只因
# 标题中含有 "AIS" 就把公告当作海事商机；须由海事语境确认后才参与 AIS 类别计分。
MARITIME_CONTEXT_TERMS = (
    "船舶", "渔船", "航道", "航标", "海事", "港口", "港务", "通航", "船岸", "岸基",
    "甚高频", "vhf", "vts", "水上", "航运", "航行", "搜救", "运河", "船闸",
    "航海", "海区", "海上", "水运", "渔业", "海洋",
)
MARITIME_STRONG_PATTERNS = (
    r"船舶自动识别", r"ais\s*(?:岸基|基站|航标)", r"(?:船载|航标)\s*ais",
    r"[\u4e00-\u9fff]{1,8}船\s*ais", r"vd(?:e)?s\s*(?:岸基|基站|船载|通信|系统)",
)
ELECTRICAL_AIS_TERMS = (
    "空气绝缘", "开关设备", "开关站", "光伏", "风电", "变电", "输变电", "配电",
    "升压站", "电网", "电力", "mw", "kv", "gis",
)


def ais_vdes_context(item: dict) -> dict:
    """识别 AIS/VDES 是否确属海事语境，避免电力 AIS（空气绝缘开关设备）误判。"""
    title = str(item.get("title", ""))
    corpus = " ".join(str(item.get(k, "")) for k in ("title", "buyer", "content"))
    protocol_hits = sorted({hit.upper() for hit in re.findall(
        r"(?<![A-Za-z0-9])(?:AIS|VDES)(?![A-Za-z0-9])", title, flags=re.I
    )})
    if not protocol_hits:
        return {"confirmed": False, "hits": [], "reason": ""}

    corpus_lower = corpus.lower()
    strong = any(re.search(pattern, corpus, flags=re.I) for pattern in MARITIME_STRONG_PATTERNS)
    context_hits = [term for term in MARITIME_CONTEXT_TERMS if term.lower() in corpus_lower]

    # 仅在 AIS 标题周边识别电力语境；公告可能是 AIS 岸基站的供电改造，强海事证据
    # 应优先保留，不能因为全文出现“电力”而误排除。
    title_lower = title.lower()
    electrical_hits = set()
    for match in re.finditer(r"(?<![A-Za-z0-9])AIS(?![A-Za-z0-9])", title, flags=re.I):
        nearby = title_lower[max(0, match.start() - 40):match.end() + 40]
        electrical_hits.update(term for term in ELECTRICAL_AIS_TERMS if term in nearby)
    electrical_ais = bool(electrical_hits) and not strong

    if electrical_ais:
        return {"confirmed": False, "hits": protocol_hits,
                "reason": "AIS 电力语境：" + "、".join(sorted(electrical_hits))}
    if "VDES" in protocol_hits or strong:
        return {"confirmed": True, "hits": protocol_hits, "reason": "海事强证据"}
    if len(context_hits) >= 2:
        return {"confirmed": True, "hits": protocol_hits,
                "reason": "海事上下文：" + "、".join(context_hits)}
    return {"confirmed": False, "hits": protocol_hits,
            "reason": "海事上下文不足"}


def score_item(item: dict, rules: dict | None = None) -> tuple[int, list[dict]]:
    """根据业务规则对公告评分。rules 来自 config.json 的 rules 字段。"""
    if rules is None:
        rules = _deep_copy(RULES_DEFAULTS)
    text = text_of(item)
    protocol_context = ais_vdes_context(item)
    score, matches = 0, []
    has_business_hit = False
    for cat in rules.get("business_categories", []):
        name, weight, keywords = cat["name"], cat.get("weight", 10), cat.get("keywords", [])
        # AIS/VDES 不能单独作为业务命中；必须先经 ais_vdes_context 的语义确认。
        if name == "AIS/VDES 与海事通信":
            keywords = [word for word in keywords if word.lower() not in {"ais", "vdes"}]
        hits = [word for word in keywords if word.lower() in text]
        if name == "AIS/VDES 与海事通信" and protocol_context["confirmed"]:
            hits.extend(protocol_context["hits"])
        if hits:
            has_business_hit = True
            points = min(weight, 8 + len(hits) * 7)
            score += points
            matches.append({"type": "business", "label": name, "points": points, "hits": hits})
    # 无业务关键词命中 → 直接返回 0 分，不因地区/采购方/预算给分
    if not has_business_hit:
        return 0, []
    # AIS/VDES 经海事语义确认后才额外加 30 分；电力 AIS 不加分也不触发产品分类。
    if protocol_context["confirmed"]:
        score += 30
        matches.append({"type": "hidden_bonus", "label": "AIS/VDES 标题重点规则", "points": 30,
                        "hits": protocol_context["hits"], "reason": protocol_context["reason"]})
    region = str(item.get("region", "")).lower()
    pr = rules.get("priority_regions", {})
    # 只检查 region 字段，不检查 text（避免标题/采购方中的地名误判）
    city_hits = [c for c in rules.get("priority_cities", []) if c.lower() in region]
    if any(p.lower() in region for p in pr.get("一级", [])):
        score += 15; matches.append({"type": "region", "label": "一级重点区域", "points": 15, "hits": city_hits})
    elif any(p.lower() in region for p in pr.get("二级", [])):
        score += 8; matches.append({"type": "region", "label": "二级重点区域", "points": 8, "hits": city_hits})
    # 采购方匹配只检查 buyer 字段
    buyer = str(item.get("buyer", "")).lower()
    client_hits = [word for word in rules.get("client_terms", []) if word.lower() in buyer]
    if client_hits:
        points = min(20, 8 + 4 * len(client_hits))
        score += points; matches.append({"type": "buyer", "label": "政府/央国企/大型集团", "points": points, "hits": client_hits})
    budget = item.get("budget")
    if isinstance(budget, (int, float)) and budget >= 500000:
        score += 8; matches.append({"type": "budget", "label": "预算≥50万元", "points": 8, "hits": []})
    return min(score, 100), matches

def rating_label(score_or_priority, levels: dict | None = None) -> str:
    """将评分或优先级字符串映射为三档汉语评级。阈值取 rules.opportunity_levels（默认 80/50）。"""
    if isinstance(score_or_priority, str):
        if score_or_priority in ('重点关注', '值得跟进', '一般关注'):
            return score_or_priority
        return '一般关注'
    lv = levels or {}
    try: key_t = int(lv.get("key_threshold", 80))
    except (TypeError, ValueError): key_t = 80
    try: follow_t = int(lv.get("follow_threshold", 50))
    except (TypeError, ValueError): follow_t = 50
    if score_or_priority >= key_t:
        return "重点关注"
    if score_or_priority >= follow_t:
        return "值得跟进"
    return "一般关注"

# 核心要点提取：仅对带公告正文的来源（目前只有天眼查，正文约 800 字摘要）生效。
# 公告正文中文数字与单位之间常有空格，故数字两侧均允许空白。
# (字段, 正则, 值模板)——同字段多次命中合并为一个标签（值用顿号隔开）；标签与值输出时以全角冒号连接。
_HIGHLIGHT_PATTERNS = [
    # 金额：总投资/估算/概算/预算/限价/控制价/服务金额（按价值从高到低优先；兼容"概算约/￥"等修饰）
    ("金额", re.compile(r"(?:工程估算总投资|总投资|概算(?:总投资|金额)?|项目投资概算|预算金额|项目预算|采购预算|最高投标限价|最高限价|控制价|合同估算价|服务金额|成交金额|中标金额|总中标金额)[^0-9￥¥。；]{0,12}(?:为|是)?[约￥¥]?\s*([0-9][0-9,，.]*)\s*(万元|亿元|元)"), "{0}{1}"),
    # 工期/服务期限：要求工期/计划工期/服务期限/服务期/总工期 + 时长（月/天），值统一换算为"X个月"
    ("工期", re.compile(r"(?:要求工期|计划(?:总)?工期|工程计划总工期|施工监理服务期限|服务期限|服务期|总工期)[^0-9。；]{0,10}?[为是约：:]?\s*(\d+(?:\.\d+)?)\s*(个?月|日历天|天)"), "{0}{1}"),
    # 资金来源：建设资金来自/为…（到标点或括号为止）
    ("资金来源", re.compile(r"(?:建设资金|资金)来自\s*([^。，；;（）]{2,30})"), "{0}"),
    ("资金来源", re.compile(r"建设资金为\s*([^。，；;（）]{2,20})"), "{0}"),
    # 项目编号：政采网"一、项目编号： 330000…"等写法，值到标点/空白为止（最长 40 位）
    ("项目编号", re.compile(r"项目编号\s*[：:]\s*([A-Za-z0-9【】\[\]（）()\-_·]{6,40})"), "{0}"),
    # 招标/采购方式：出现在“进行/采用/方式”等语境，避免命中正文套话里的泛称
    ("方式", re.compile(r"(?:进行|采用|方式为|方式：|方式:)\s*(公开招标|邀请招标|竞争性谈判|竞争性磋商|单一来源采购|询价|邀请直选\+竞价)"), "{0}"),
    # 工程规模：标签在前（疏浚约 256.35 万方 / 防撞加固桥梁 6 座）；公里/千米另由下方规则提取，避免“疏浚 3.668 公里”误配。
    ("规模", re.compile(r"(疏浚|填槽|吹填|开挖|整治|新建|改建|扩建|护岸|拆除|改造跨河缆线|防撞加固桥梁)[^0-9。；，]{0,6}?\s*(\d+(?:\.\d+)?)\s*(万方|万立方米|万平方米|平方米|米|座|处)"), "{0} {1}{2}"),
    # 工程规模：里程类（213 公里航道 / 长 12.64 公里）
    ("规模", re.compile(r"(\d+(?:\.\d+)?)\s*(公里|千米)\s*(航道|公路|岸线|河段|航线)?"), "{0}{1}{2}"),
]

def _period_to_months(val: str) -> str:
    """工期统一用月份表示：月直接保留，天/日历天按 30 天折算（保留一位小数）。"""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(个?月|日历天|天)", val)
    if not m:
        return ""
    months = float(m.group(1)) if "月" in m.group(2) else float(m.group(1)) / 30
    r = round(months, 1)
    return f"{int(r)}个月" if r == int(r) else f"{r}个月"

def highlight_fields(row: dict) -> None:
    """为列表行附加 key_points（核心要点）与 keywords（命中关键词）字段。"""
    # 命中关键词：来自入库评分记录（match_json）中的业务关键词命中，全来源可用。
    kws: list[str] = []
    try:
        for m in json.loads(row.get("match_json") or "[]"):
            if m.get("type") == "business":
                for h in m.get("hits") or []:
                    if h and h not in kws:
                        kws.append(h)
    except Exception:
        pass
    row["keywords"] = kws[:8]
    # 核心要点：仅在有公告正文时从正文提取关键事实（金额/工期/资金来源/编号/方式/规模），另补充已入库的截止时间。
    pts: list[str] = []
    content = row.get("content") or ""
    if content:
        fields: dict[str, list[str]] = {}  # 保序：字段按模板定义顺序，同字段多个值用顿号合并为一个标签
        for field, pat, tpl in _HIGHLIGHT_PATTERNS:
            if field == "规模":
                matches = list(pat.finditer(content))  # 规模可能多处，全部收集后合并；每字段最多 3 个值防标签过长
            else:
                m0 = pat.search(content)
                matches = [m0] if m0 else []
            for m in matches:
                vals = fields.setdefault(field, [])
                if len(vals) >= 3:
                    break
                groups = [g.strip(" ，、") if isinstance(g, str) else "" for g in m.groups()]
                v = tpl.format(*groups).strip()
                if v and v not in vals:
                    vals.append(v)
        for field, vals in fields.items():
            joined = "、".join(vals)
            if field == "工期":
                joined = _period_to_months(joined)
            if joined:
                pts.append(f"{field}：{joined}")
        # 金额准确性：概要的"总中标金额"偶被源站误标为单价，详情正文另有"投标总价"且两者相差悬殊时以总价为准。
        m_sum = re.search(r"总中标金额[^0-9]{0,8}([0-9][0-9,.]*)\s*(万元|亿元|元)", content)
        if m_sum and pts and pts[0].startswith("金额"):
            m_tot = re.search(r"投标总价[^0-9]{0,12}?([0-9][0-9,.]*)\s*（?元", content)
            if m_tot:
                v_sum = float(m_sum.group(1).replace(",", "")) * {"万元": 1e4, "亿元": 1e8, "元": 1}[m_sum.group(2)]
                v_tot = float(m_tot.group(1).replace(",", ""))
                if v_tot > 0 and abs(v_sum - v_tot) / v_tot > 0.3:
                    pts[0] = "中标金额：" + m_tot.group(1) + "元"
        dl = (row.get("deadline_at") or "").strip()
        if dl:
            pts.append("截止：" + dl[:10])
    row["key_points"] = pts[:7]

def fingerprint(item: dict) -> str:
    stable = "|".join(str(item.get(k, "")).strip().lower() for k in ("source_code", "source_url", "title", "buyer", "published_at"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()

# ---- 近似去重：标题归一化 + 相似判定 ----
# 背景：同一公告经不同检索词/平台返回时，标题常带差异——全/半角括号、开头公告编号（JSHY2026164…）、
# 尾部括号项目编号（(YLHD-FW04)）、标题截断（“…监理二标（”）——精确匹配无法识别。
_TENDER_ROUND_RE = re.compile(r"(?:第?[一二三四五六七八九十百0-9]+次|重新招标|再次招标)")

def normalize_title(title) -> str:
    """标题归一化；保留尾部采购轮次，避免一/二/三次公告错误合并。"""
    s = unicodedata.normalize("NFKC", str(title or "")).lower()
    s = re.sub(r"\s+", "", s)
    # 开头的公告编号（字母数字串，后接中文才算编号前缀，避免误伤“2026年…”类正常开头）
    s = re.sub(r"^[a-z0-9][a-z0-9\-_/．.]{2,24}(?=[\u4e00-\u9fff])", "", s)
    # 尾部悬空括号（截断标题，如“…监理二标（”）
    s = re.sub(r"\([^()]*$", "", s)
    # 尾部完整括号段通常可移除；但“(第二次)”等轮次必须参与数字语义校验。
    tail = re.search(r"\(([^()]{0,40})\)$", s)
    if tail and not _TENDER_ROUND_RE.search(tail.group(1)):
        s = s[:tail.start()]
    # 尾部【…】标签（平台标注，如“【电子标】”）
    s = re.sub(r"【[^【】]{0,30}】$", "", s)
    return s.rstrip("。．.、，, －-—_")

def _digit_multiset(s: str) -> list:
    """提取标题中的数字语义（阿拉伯数字串 + 中文数字），排序后用于一致性校验。
    用途：防止“监理一标/二标”“第二次/第三次”这类仅数字不同的不同公告被误合并。"""
    return sorted(re.findall(r"\d+|[一二三四五六七八九十]", s))

def _title_core(s: str) -> str:
    """截取项目主体部分：从最早出现的公告类型词（招标公告/公示/…）处截断。
    用途：同一公告在不同聚合入口的标题常在公告类型词后拼接不同尾巴（“【电子标】”“-项目名”），
    取主体部分比较可识别这类变体。"""
    idx = len(s)
    for w in ("招标公告", "磋商公告", "谈判公告", "询价公告", "单一来源", "资格预审",
              "中标公告", "成交公告", "结果公告", "候选人公示", "公示", "公告"):
        i = s.find(w)
        if 0 < i < idx:
            idx = i
    return s[:idx]

def same_project_title(a: str, b: str) -> bool:
    """判断两个归一化标题是否为同一公告的变体（需先用 normalize_title 归一化）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    # 数字语义必须一致（一标段≠二标段，第二次≠第三次）
    if _digit_multiset(a) != _digit_multiset(b):
        return False
    # 公告类型词前的项目主体完全一致（覆盖“【电子标】”“-项目名”等尾部拼接差异）
    ca, cb = _title_core(a), _title_core(b)
    if len(ca) >= 12 and ca == cb:
        return True
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    # 截断/尾缀变体：短标题是长标题前缀（短的至少 12 字，避免过短误配）
    if len(short) >= 12 and long.startswith(short):
        return True
    # 其他小差异用序列相似度兜底（阈值取高，因数字差异已被前置校验拦截）
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.92

_MARITIME_CONTEXT_RE = re.compile(
    r"船舶|海事|航道|航标|航运|航行|港口|港航|通航|船岸|岸基|甚高频|"
    r"VHF|VTS|水上|海上|海洋|渔船|渔港|海图|船载|船闸|船队", re.I)

def ccgp_search_keyword_ok(item: dict, keyword: str) -> bool:
    """政采网检索结果二次校验。

    政采网对 AIS 使用子串匹配，曾将 AISPORTS 等教育设备混入结果。AIS/VDES
    必须是独立技术词，且同时具备海事语境；其他行业词继续由统一评分和标题闸门处理。
    """
    keyword = (keyword or "").strip().lower()
    if keyword not in {"ais", "vdes"}:
        return True
    text = f"{item.get('title') or ''} {item.get('content') or ''}"
    if keyword == "ais":
        # 英文数字两侧不能相连，故 AISPORTS、RAIS、AIS2026 都不会被误当作 AIS。
        standalone = bool(re.search(r"(?<![a-z0-9])ais(?![a-z0-9])", text, re.I)) or "船舶自动识别" in text
    else:
        standalone = bool(re.search(r"(?<![a-z0-9])vdes(?![a-z0-9])", text, re.I))
    return standalone and bool(_MARITIME_CONTEXT_RE.search(text))

# 公告阶段词表（长词在前，正则交替式保证长词优先且匹配不重叠）。
_NOTICE_STAGE_RE = re.compile(
    r"中标候选人|招标公告|比选公告|磋商公告|谈判公告|询价公告|中标公告|成交公告|结果公告|候选人公示|"
    r"资格预审|竞争性|单一来源|废标|流标|终止|暂停|作废|合同|验收|比选|公示|公告")

def _notice_stage(norm_title: str) -> str:
    """公告阶段分类：取标题中最后出现的公告类型词（“…招标公告废标公告”的阶段是废标）。
    同一项目的不同阶段公告（招标/中标/废标/成交…）是不同商机线索，模糊去重时不得互相合并。
    截断标题（尾带 …/...）阶段信息可能丢失，返回唯一值不参与模糊合并（仅精确同标题可合并）。"""
    if norm_title.rstrip().endswith(("...", "…")):
        return f"截断@{norm_title}"
    hits = _NOTICE_STAGE_RE.findall(norm_title)
    return hits[-1] if hits else "其他"

def _title_gate_ok(item: dict, rules: dict) -> bool:
    """Compatibility wrapper for the central ingestion policy."""
    return not title_gate_reason(item, rules)

# ---- 商机分类打标（领导打法：前期线索 / 直接产品 / 集成项目）----
# 分类名与关键词已改为可配置：config.json rules.opportunity_categories（界面：数据规则 → 商机分类），
# 按列表顺序匹配标题，先命中者生效；默认词表见 RULES_DEFAULTS。

# ---- 代理机构 / 中标单位（设计院）提取 ----
# 完整标签（带"名称"）优先；裸标签用负向前瞻排除"名称/地址/联系"等复合标签头，避免吞掉标签自身的后续字。
# "招标人/招标代理机构"双列表格拍平后第一个"名称"属于招标人，用回顾断言跳过。
_AGENCY_RE = re.compile(r"(?:采购代理机构名称|招标代理机构名称|代理机构名称|采购代理机构|(?<!招标人/)招标代理机构|招标代理|(?<!招标人/招标)代理机构(?!地址|联系|名称))\s*(?:名称)?\s*[：:为 ]\s*([^\s，,。；;、＜<]{3,60})")
_WINNER_RE = re.compile(r"(?:中标供应商名称|成交供应商名称|供应商名称|中标单位|中标人名称|中标人|成交供应商|成交单位|中标供应商(?!名称|地址|联系)|中标候选人|设计单位|设计院)\s*(?:名称)?\s*[：:为 ]\s*([^\s，,。；;、＜<]{3,60})")
# 政采网结果表格兜底：表格单元格被拍平为空格分隔，标签与值隔行，按"中标供应商后 100 字内、地址前的首个机构名"定位。
_WINNER_TABLE_RE = re.compile(r"中标供应商[\s\S]{0,100}?([\u4e00-\u9fa5A-Za-z（）()]{4,40}?(?:有限公司|股份有限公司|集团公司|集团|研究院|事务所))\s")

def classify_item(item: dict, rules: dict | None = None) -> str:
    """商机分类：按 opportunity_categories 配置顺序匹配标题关键词，返回首个命中的分类名；都不命中返回空，不打标。
    只看标题，避免正文误判。rules 缺失时回落 RULES_DEFAULTS。"""
    title = (item.get("title") or "").lower()
    if not title:
        return ""
    if rules is None:
        rules = RULES_DEFAULTS
    for cat in rules.get("opportunity_categories") or []:
        name = str(cat.get("name") or "").strip()
        if not name:
            continue
        for kw in cat.get("keywords") or []:
            if kw and str(kw).lower() in title:
                return name
    return ""

# 单位名合法结尾词：捕获到的名字可能粘着“日期/地址/电话”等尾巴，截到最后一个合法结尾
_ORG_END_RE = re.compile(r"(?:公司|中心|院|所|集团|事务所|企业|联合体|办事处|分公司|局)")

def _clean_org(s: str) -> str:
    s = (s or "").strip(" ：:、,，")
    # 截掉跟在名称后的日期/地址/联系方式段
    s = re.sub(r"(日期|时间|地址|电话|联系人|邮编|传真|邮箱|网址).*$", "", s)
    ends = list(_ORG_END_RE.finditer(s))
    if ends:
        s = s[:ends[-1].end()]
    return s.strip()

# 表头拍平后标签会连着标签（"中标供应商名称 中标供应商地址"），捕获到标签词/表头词/数字时跳过继续找。
_ORG_JUNK_RE = re.compile(r"^(中标|成交|供应商|代理|采购人|采购单位|名称|地址|序号|评审|金额|含税|响应|总价|报价|项目|负责|[0-9])")

def _pick_org(pat: re.Pattern, text: str) -> str:
    """按标签找机构名：跳过标签词/表头词捕获；同文多处命中时优先取以合法结尾词收尾的完整名（摘要里常有截断简称）。"""
    fallback = ""
    for m in pat.finditer(text):
        cand = _clean_org(m.group(1))
        if not cand or _ORG_JUNK_RE.match(cand):
            continue
        if _ORG_END_RE.search(cand):
            return cand
        if not fallback:
            fallback = cand
    return fallback

def extract_orgs(content: str) -> tuple[str, str]:
    """从公告正文提取 (代理机构, 中标单位/设计单位)。提取不到返回空串，不强求。"""
    text = content or ""
    # 代理：双列守卫——"招标人/招标代理机构"等双列表格拍平后紧邻的第一个值属于招标人，跳过（分隔符不定，按上下文判定）。
    agency = ""
    for m in _AGENCY_RE.finditer(text):
        if "招标人" in text[max(0, m.start() - 6):m.start()]:
            continue
        cand = _clean_org(m.group(1))
        if not cand or _ORG_JUNK_RE.match(cand):
            continue
        if _ORG_END_RE.search(cand):
            agency = cand
            break
        if not agency:
            agency = cand
    if not agency:
        # 守卫全空时补救：不再判上下文，但只认以合法结尾词收尾的完整名，且排除机关名（结尾局/厅/委员会的多为招标人）。
        for m in _AGENCY_RE.finditer(text):
            cand = _clean_org(m.group(1))
            if cand and not _ORG_JUNK_RE.match(cand) and _ORG_END_RE.search(cand) \
                    and not re.search(r"(局|厅|委员会|办公室|政府)$", cand):
                agency = cand
                break
    winner = _pick_org(_WINNER_RE, text)
    if not winner:
        m = _WINNER_TABLE_RE.search(text)
        winner = _clean_org(m.group(1)) if m else ""
    return agency, winner

def _dup_compat(n: dict, item: dict) -> bool:
    """地区/采购方双非空时必须一致，防止不同项目的同名公告被合并。
    采购方允许包含关系（不同平台对同一单位写法有差异，如“唐山市农业农村局”与“…本级”）。"""
    r1, r2 = (n.get("region") or "").strip(), (item.get("region") or "").strip()
    if r1 and r2 and r1 != r2 and r1 not in r2 and r2 not in r1:
        return False
    b1, b2 = (n.get("buyer") or "").strip(), (item.get("buyer") or "").strip()
    if b1 and b2 and b1 != b2 and b1 not in b2 and b2 not in b1:
        return False
    return True

def find_duplicate(conn: sqlite3.Connection, item: dict, days: int = 365):
    """在近 {days} 天未删除记录中查找同一公告的已有记录，返回行或 None。
    窗口取 365 天与留存期一致：官网常见“老公告隔月重新挂网”，窗口过短会让旧发布日期的重复记录隐身。"""
    norm = normalize_title(item.get("title", ""))
    if len(norm) < 8:
        return None
    cutoff = (cn_today() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = conn.execute("""SELECT id, title, buyer, region, source_code FROM tenders
        WHERE is_deleted=0 AND (published_at='' OR published_at >= ?)""", (cutoff,)).fetchall()
    for r in rows:
        rdict = dict(r)
        rn = normalize_title(r["title"])
        # 归一化标题完全一致 → 同一公告（跨站/跨栏目转载的地区写法常有差异，不再做地区/采购方校验）
        if rn == norm:
            return r
        # 标题变体：仅限同一公告阶段（招标≠中标≠废标，同项目不同阶段公告各自独立入库）
        if _notice_stage(norm) == _notice_stage(rn) and same_project_title(norm, rn) and _dup_compat(rdict, item):
            return r
    return None

def merge_into(conn: sqlite3.Connection, keep_id: int, dup: dict) -> None:
    """把重复记录 dup 的信息合并进 keep_id（保留人工字段：优先级/跟进状态/备注/负责人），随后删除重复行。"""
    keep = dict(conn.execute("SELECT * FROM tenders WHERE id=?", (keep_id,)).fetchone())
    title = keep["title"] if len(keep["title"]) >= len(dup.get("title", "")) else dup["title"]
    content = keep["content"] if len(keep["content"] or "") >= len(dup.get("content") or "") else (dup.get("content") or "")
    srcs = keep["source_code"].split(",")
    for s in dup.get("source_code", "").split(","):
        if s and s not in srcs:
            srcs.append(s)
    new_score = max(keep["score"], dup.get("score", 0))
    match_json = keep["match_json"] if keep["score"] >= dup.get("score", 0) else dup.get("match_json", keep["match_json"])
    # 正文被换/被加长时，代理/中标单位随之重提，避免旧正文的提取值与新正文脱节。
    agency, winner = keep["agency"], keep["winner"]
    if content != (keep["content"] or ""):
        agency, winner = extract_orgs(content)
        buyer = keep["buyer"] or dup.get("buyer", "")
        if agency and agency == buyer.strip():
            agency = ""
    conn.execute("""UPDATE tenders SET title=?, content=?, source_code=?,
        buyer=COALESCE(NULLIF(buyer,''),?), region=COALESCE(NULLIF(region,''),?),
        budget=COALESCE(budget,?), deadline_at=COALESCE(NULLIF(deadline_at,''),?),
        score=?, match_json=?, agency=?, winner=?, updated_at=? WHERE id=?""",
        (title, content, ",".join(srcs), dup.get("buyer", ""), dup.get("region", ""),
         dup.get("budget"), dup.get("deadline_at", ""), new_score, match_json,
         agency or "", winner or "", now(), keep_id))
    conn.execute("DELETE FROM tenders WHERE id=?", (dup["id"],))
    conn.commit()

# 商业聚合源：与官方平台数据合并时，标题/正文/链接无条件以官方为准（官方详情页全文优于聚合源摘要）。
COMMERCIAL_CODES = {"tianyancha"}

def sweep_duplicates(conn: sqlite3.Connection) -> int:
    """全局去重兜底：对未删除记录按 id 顺序两两比对（归一化标题完全一致，或标题变体且地区/采购方兼容），
    重复者合并进最早记录。用于入库时去重未拦住（如旧窗口期入库）的历史重复对。返回合并条数。"""
    rows = conn.execute("SELECT * FROM tenders WHERE is_deleted=0 ORDER BY id").fetchall()
    merged = 0
    kept = []  # [(id, norm, row_dict, stage)]
    for r in rows:
        rd = dict(r)
        norm = normalize_title(rd["title"])
        hit = None
        if len(norm) >= 8:
            for kid, knorm, krow, kstage in kept:
                if knorm == norm or (len(knorm) >= 8 and kstage == _notice_stage(norm)
                                     and same_project_title(norm, knorm) and _dup_compat(krow, rd)):
                    hit = kid
                    break
        if hit is not None:
            merge_into(conn, hit, rd)
            merged += 1
        else:
            kept.append((rd["id"], norm, rd, _notice_stage(norm)))
    return merged

def _is_commercial_only(codes: str) -> bool:
    parts = [c.strip() for c in (codes or "").split(",") if c.strip()]
    return bool(parts) and all(c in COMMERCIAL_CODES for c in parts)

def upsert_tender(conn: sqlite3.Connection, item: dict, link_ok: int = 1, rules: dict | None = None) -> tuple[bool, int]:
    required = ["source_code", "source_url", "title"]
    missing = [key for key in required if not item.get(key)]
    if missing: raise ValueError("缺少字段：" + ", ".join(missing))
    if not conn.execute("SELECT 1 FROM sources WHERE code=?", (item["source_code"],)).fetchone():
        raise ValueError("未知来源：" + item["source_code"])
    # ---- 非商机硬闸：招聘、废标/履约阶段、环评公示不能作为可跟进商机入库；中标/成交结果保留为市场情报 ----
    if non_opportunity_reason(item):
        return False, 0
    # ---- 商机分类打标 + 代理机构/中标单位提取（随入库落库）----
    item["category"] = classify_item(item, rules)
    item["agency"], item["winner"] = extract_orgs(item.get("content") or "")
    # 招标组织形式为自行招标时，源站会在代理栏重复填采购人名称，不属于代理机构。
    if item["agency"] and item["agency"] == (item.get("buyer") or "").strip():
        item["agency"] = ""
    score, matches = score_item(item, rules)
    stamp = now(); fp = fingerprint(item)
    # ---- 中央入库策略：评分、标题噪声和来源栏目范围必须同时通过 ----
    if ingestion_issue_reason(item, rules if rules is not None else RULES_DEFAULTS, score):
        return False, 0
    # ---- 已标记无用的记录，跳过不入库（含同指纹与近似标题：防止同一公告换个来源/变体“借尸还魂”）----
    deleted_check = conn.execute("SELECT id, is_deleted FROM tenders WHERE fingerprint=?", (fp,)).fetchone()
    if deleted_check and deleted_check["is_deleted"]:
        return False, 0
    norm_title = normalize_title(item.get("title", ""))
    if len(norm_title) >= 8:
        for drow in conn.execute("SELECT title, buyer, region, source_code FROM tenders WHERE is_deleted=1").fetchall():
            if same_project_title(norm_title, normalize_title(drow["title"])) and _dup_compat(dict(drow), item):
                return False, 0
    # ---- 近似去重：同一公告的不同变体（标点/编号前缀/截断差异）合并入库，不新增 ----
    existing = find_duplicate(conn, item)
    if existing:
        # 合并来源信息（如不同来源/检索词抓到同一条公告）
        merged_sources = existing["source_code"]
        if item["source_code"] not in merged_sources.split(","):
            merged_sources = merged_sources + "," + item["source_code"]
        new_score = max(existing_score := conn.execute("SELECT score FROM tenders WHERE id=?", (existing["id"],)).fetchone()[0], score)
        # 优先保留非空的 deadline_at 和 buyer；人工字段（优先级等）不动。
        cur = conn.execute("SELECT title, content, deadline_at, buyer, region, category, agency, winner, source_url FROM tenders WHERE id=?", (existing["id"],)).fetchone()
        # 官方平台优先于商业聚合源（天眼查）：官方与聚合源合并时，标题/链接无条件取官方，正文取官方（官方无正文时保留聚合源摘要）；同类来源才比长度。
        exist_official = not _is_commercial_only(existing["source_code"])
        new_official = item["source_code"] not in COMMERCIAL_CODES
        if exist_official and not new_official:
            new_title, new_content, new_url = cur["title"], cur["content"] or "", cur["source_url"]
        elif new_official and not exist_official:
            new_title = item["title"]
            new_content = (item.get("content") or "") or (cur["content"] or "")
            new_url = item["source_url"]
        else:
            new_title = cur["title"] if len(cur["title"]) >= len(item["title"]) else item["title"]
            new_content = cur["content"] if len(cur["content"] or "") >= len(item.get("content") or "") else (item.get("content") or "")
            new_url = item["source_url"]
        new_deadline = cur["deadline_at"] or item.get("deadline_at", "")
        new_buyer = cur["buyer"] or item.get("buyer", "")
        new_region = cur["region"] or item.get("region", "")
        new_category = cur["category"] or item.get("category", "")
        new_agency = cur["agency"] or item.get("agency", "")
        new_winner = cur["winner"] or item.get("winner", "")
        conn.execute("""UPDATE tenders SET title=?, content=?, source_code=?, source_url=?, buyer=?, region=?,
            deadline_at=COALESCE(NULLIF(deadline_at,''),?), budget=COALESCE(budget,?),
            category=?, agency=?, winner=?,
            score=?, match_json=?, link_ok=?, updated_at=? WHERE id=?""",
            (new_title, new_content, merged_sources, new_url, new_buyer, new_region,
             new_deadline, item.get("budget"), new_category, new_agency, new_winner,
             new_score, json.dumps(_strip_surrogates(matches), ensure_ascii=False), link_ok, stamp, existing["id"]))
        conn.commit()
        return False, new_score
    # ---- 指纹去重（同一来源同一URL）----
    initial_priority = rating_label(score, (rules or {}).get("opportunity_levels"))
    values = (fp, item["source_code"], item["source_url"], item["title"], item.get("buyer", ""), item.get("region", ""),
              item.get("budget"), item.get("published_at", ""), item.get("deadline_at", ""), item.get("content", ""),
              score, json.dumps(_strip_surrogates(matches), ensure_ascii=False), link_ok, initial_priority, stamp, stamp,
              item.get("category", ""), item.get("agency", ""), item.get("winner", ""))
    cur = conn.execute("""INSERT INTO tenders(fingerprint,source_code,source_url,title,buyer,region,budget,published_at,deadline_at,content,score,match_json,link_ok,priority,created_at,updated_at,category,agency,winner)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(fingerprint) DO UPDATE SET source_url=excluded.source_url, content=excluded.content,
      score=excluded.score,match_json=excluded.match_json,link_ok=excluded.link_ok,updated_at=excluded.updated_at,
      category=COALESCE(NULLIF(tenders.category,''),excluded.category),
      agency=COALESCE(NULLIF(tenders.agency,''),excluded.agency),
      winner=COALESCE(NULLIF(tenders.winner,''),excluded.winner)""", values)
    conn.commit()
    return cur.rowcount == 1, score

def demo_items() -> list[dict]:
    return [
        {"source_code":"sd_ggzy","source_url":"https://example.invalid/sd-001","title":"山东省沿海航道 AIS 岸基站升级改造项目","buyer":"山东省航道事务中心","region":"山东省青岛市","budget":3200000,"published_at":"2026-08-21","deadline_at":"2026-09-10","content":"建设 AIS 岸基站、航标遥测遥控和通航安全监管平台。"},
        {"source_code":"msa","source_url":"https://example.invalid/msa-001","title":"辽宁海事局船舶交通管理系统运维及 VTS 数据服务采购","buyer":"辽宁海事局","region":"辽宁省大连市","budget":860000,"published_at":"2026-08-21","deadline_at":"2026-09-05","content":"海事监管、船舶交通管理和船岸通信服务。"},
        {"source_code":"bj_ggzy","source_url":"https://example.invalid/bj-001","title":"卫星互联网地面应用软件开发项目","buyer":"北京市某大型集团","region":"北京市","budget":1200000,"published_at":"2026-08-20","deadline_at":"2026-09-12","content":"低轨卫星、星地融合、卫星运营支撑软件。"},
        {"source_code":"ceb","source_url":"https://example.invalid/ceb-001","title":"园区办公网络维护服务采购","buyer":"某企业","region":"河北省","budget":120000,"published_at":"2026-08-20","deadline_at":"2026-09-01","content":"办公网络与终端维护。"},
    ]

def rows_as_dicts(rows):
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# 来源适配器：逐站实现，用 Python 标准库从静态 HTML 列表页提取公告
# ---------------------------------------------------------------------------

def _make_ssl_context() -> ssl.SSLContext:
    """创建兼容旧版 SSL 服务端的上下文（华润等站点需 legacy renegotiation）。
    OP_LEGACY_SERVER_CONNECT 仅部分 OpenSSL 构建存在（服务器 OpenSSL 可能无此常量），缺失时跳过。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    legacy = getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0)
    if legacy:
        ctx.options |= legacy
    return ctx

_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

def _http_get(url: str, timeout: int = 20) -> str:
    """获取 URL 内容，返回解码后的 HTML 字符串。"""
    req = Request(url, headers=_HTTP_HEADERS)
    resp = urlopen(req, timeout=timeout, context=_make_ssl_context())
    return resp.read().decode("utf-8", errors="replace")

def _http_head(url: str, timeout: int = 8) -> bool:
    """HEAD 请求验证链接可达性，返回 True 表示 HTTP 状态 < 400。"""
    req = Request(url, method="HEAD", headers=_HTTP_HEADERS)
    try:
        resp = urlopen(req, timeout=timeout, context=_make_ssl_context())
        return resp.status < 400
    except HTTPError as e:
        return e.code < 400
    except Exception:
        return False

def _http_post(url: str, data: dict, timeout: int = 20) -> str:
    """POST 表单数据到 URL，返回响应文本。"""
    body = urlencode(data).encode("utf-8")
    headers = {**_HTTP_HEADERS, "Content-Type": "application/x-www-form-urlencoded"}
    req = Request(url, data=body, headers=headers, method="POST")
    resp = urlopen(req, timeout=timeout, context=_make_ssl_context())
    return resp.read().decode("utf-8", errors="replace")

def _normalize_date(raw: str) -> str:
    """将各种中文日期格式统一为 YYYY-MM-DD。"""
    if not raw:
        return ""
    raw = raw.strip()
    # YYYY/MM/DD HH:MM:SS
    m = re.match(r"(\d{4})/(\d{2})/(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # YYYY年MM月DD日
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # YYYY-MM-DD
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return raw[:10]
    return ""

def _extract_deadline(html: str, source_code: str) -> str:
    """从详情页 HTML 中提取投标截止日期，返回 YYYY-MM-DD 或空字符串。"""
    # 先去掉 HTML 标签以便全文匹配（CSG 的日期被 <span> 拆分）
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text)

    if source_code == "ccgp":
        # 提交投标文件截止时间：2026年09月14日 14点00分
        m = re.search(r'提交投标文件截止时间[：:]\s*(\d{4}年\d{1,2}月\d{1,2}日)', text)
        if m:
            return _normalize_date(m.group(1))
        # 开标时间：2026年09月14日 14:00
        m = re.search(r'开标时间[：:]\s*(\d{4}年\d{1,2}月\d{1,2}日)', text)
        if m:
            return _normalize_date(m.group(1))
        # 表格格式
        m = re.search(r'开标时间</td>\s*<td[^>]*>(\d{4}年\d{1,2}月\d{1,2}日)', text)
        if m:
            return _normalize_date(m.group(1))

    elif source_code == "cjhdj":
        # 截止时间：2026年8月27日15点30分
        m = re.search(r'截止时间[：:]\s*(\d{4}年\d{1,2}月\d{1,2}日)', text)
        if m:
            return _normalize_date(m.group(1))
        # 递交投标文件时间：2026年8月26日9:00至9:30
        m = re.search(r'递交投标文件时间[：:]\s*(\d{4}年\d{1,2}月\d{1,2}日)', text)
        if m:
            return _normalize_date(m.group(1))
        # 开标时间
        m = re.search(r'开标时间[：:]\s*(\d{4}年\d{1,2}月\d{1,2}日)', text)
        if m:
            return _normalize_date(m.group(1))

    elif source_code == "csg":
        # 投标文件递交截止时间：2026年09月15日09时30分00秒
        m = re.search(r'投标文件递交截止时间[：:]\s*(\d{4}年\d{1,2}月\d{1,2}日)', text)
        if m:
            return _normalize_date(m.group(1))
        # 开标时间
        m = re.search(r'开标时间[：:]\s*(\d{4}年\d{1,2}月\d{1,2}日)', text)
        if m:
            return _normalize_date(m.group(1))

    elif source_code == "crc":
        # 截标/开标时间：2026/08/07 09:00:00
        m = re.search(r'截标.*?时间[：:]\s*(\d{4}/\d{2}/\d{2})', text)
        if m:
            return _normalize_date(m.group(1))
        # 提问截止时间 2026年07月27日 17:00
        m = re.search(r'提问截止时间\s*(\d{4}年\d{1,2}月\d{1,2}日)', text)
        if m:
            return _normalize_date(m.group(1))

    # ---- 通用兜底：扫描全文常见截止时间关键词附近的日期 ----
    deadline_patterns = [
        r'(?:投标截止|截标|递交截止|报名截止|响应截止|开标).{0,30}(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})',
        r'(?:截止|截标).{0,20}时间.{0,20}(\d{4}[-/年]\d{1,2}[-/月]\d{1,2})',
    ]
    for pat in deadline_patterns:
        m = re.search(pat, text)
        if m:
            d = _normalize_date(m.group(1))
            if d:
                return d
    return ""

def _html_to_text(html: str) -> str:
    """提取公告正文纯文本，优先剔除网站导航、页脚等与公告无关的固定内容。"""
    s = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html or "")
    # 全站菜单里的“智慧港口”等词不能参与商机评分；先删除典型布局区域。
    s = re.sub(r"(?is)<(header|nav|footer|aside)[^>]*>.*?</\1>", " ", s)
    # 有语义正文容器时优先取它，避免 body 全文把侧栏和友情链接带入评分。
    candidates = []
    for pat in (
        r"(?is)<article\b[^>]*>(.*?)</article>",
        r"(?is)<main\b[^>]*>(.*?)</main>",
        r"(?is)<(?:div|section)\b[^>]*(?:id|class)=[\"'][^\"']*(?:article|content|detail|news-detail|newscontent)[^\"']*[\"'][^>]*>(.*?)</(?:div|section)>",
    ):
        candidates.extend(re.findall(pat, s))
    if candidates:
        candidate = max(candidates, key=len)
        if len(_html_to_text_plain(candidate)) >= 80:
            s = candidate
    return _html_to_text_plain(s)

def _html_to_text_plain(s: str) -> str:
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html_mod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()

def _extract_published_at_text(text: str) -> str:
    """从正文头部提取发布日期；仅用于列表页没有日期的来源。"""
    head = (text or "")[:1200]
    for pat in (
        r"(?:发布时间|发布于|日期|时间)\s*[：:]?\s*(20\d{2}[年./-]\d{1,2}[月./-]\d{1,2})",
        r"\b(20\d{2}[年./-]\d{1,2}[月./-]\d{1,2})\b",
    ):
        m = re.search(pat, head)
        if m:
            d = _normalize_date(m.group(1))
            if d:
                return d
    return ""

def _fetch_detail_full(url: str, source_code: str, timeout: int = 15) -> tuple[str, str]:
    """抓取详情页，返回 (截止日期, 正文纯文本)。失败对应项为空。"""
    try:
        html = _http_get(url, timeout=timeout)
        return _extract_deadline(html, source_code), _html_to_text(html)[:3000]
    except Exception:
        return "", ""

def _fetch_detail_deadline(url: str, source_code: str, timeout: int = 15) -> str:
    """抓取详情页并提取截止日期。失败返回空字符串。"""
    return _fetch_detail_full(url, source_code, timeout)[0]

# ── Kimi WebBridge 浏览器回填 ──────────────────────────────────────
_WB_URL = "http://127.0.0.1:10086/command"
_WB_SESSION = "radar-backfill"
_WB_KEYWORDS = ["截止", "开标", "递交", "截标", "报名", "结束时间", "开标时间"]


def _wb_command(action: str, args: dict, session: str = _WB_SESSION) -> dict:
    """向 Kimi WebBridge 守护进程发送命令。"""
    req = {"action": action, "args": args, "session": session}
    fname = os.path.join(tempfile.gettempdir(), f"wb-radar-{os.getpid()}-{int(time.time()*1000)}.json")
    try:
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(req, f, ensure_ascii=False)
        result = subprocess.run(
            ["curl.exe", "-s", "-X", "POST", _WB_URL,
             "-H", "Content-Type: application/json",
             "--data-binary", f"@{fname}"],
            capture_output=True, timeout=60,
            encoding="utf-8", errors="replace"
        )
        return json.loads(result.stdout) if result.stdout.strip() else {"ok": False}
    except Exception:
        return {"ok": False}
    finally:
        try:
            os.remove(fname)
        except OSError:
            pass


def _wb_extract_deadline(url: str) -> str:
    """通过浏览器打开详情页，提取截止时间。返回 YYYY-MM-DD 或空字符串。"""
    # 检查守护进程是否运行
    try:
        status = subprocess.run(
            ["curl.exe", "-s", "http://127.0.0.1:10086/status"],
            capture_output=True, timeout=5, encoding="utf-8", errors="replace"
        )
        info = json.loads(status.stdout) if status.stdout.strip() else {}
        if not info.get("running"):
            return ""
    except Exception:
        return ""

    # 导航到页面
    resp = _wb_command("navigate", {"url": url})
    if not resp.get("ok"):
        return ""

    # 等待页面加载
    time.sleep(3)

    # JS: 在全文中找关键词，±200字符内搜索日期
    js = r"""(() => {
        const t = document.body.innerText;
        const kw = """ + json.dumps(_WB_KEYWORDS, ensure_ascii=False) + r""";
        const dateRe = /\d{4}[-\/年.]\d{1,2}[-\/月.]\d{1,2}[日]?/g;
        for (const k of kw) {
            let idx = -1;
            while ((idx = t.indexOf(k, idx + 1)) !== -1) {
                const w = t.substring(Math.max(0, idx - 200), Math.min(t.length, idx + 200));
                const dates = w.match(dateRe);
                if (dates && dates.length > 0) {
                    return JSON.stringify({dates: dates});
                }
            }
        }
        const lines = t.split('\n');
        for (const line of lines) {
            if (kw.some(k => line.includes(k))) {
                const dates = line.match(dateRe);
                if (dates && dates.length > 0) {
                    return JSON.stringify({dates: dates});
                }
            }
        }
        return JSON.stringify(null);
    })()"""

    resp = _wb_command("evaluate", {"code": js})
    if not resp.get("ok") or not resp.get("data", {}).get("value"):
        return ""

    try:
        result = json.loads(resp["data"]["value"])
    except (json.JSONDecodeError, TypeError):
        return ""

    if not result or not result.get("dates"):
        return ""

    # 从候选日期中选最合理的（未来0-90天优先）
    now = cn_today()
    best, best_diff = "", None
    for raw in result["dates"]:
        d = raw.replace("年", "-").replace("月", "-").replace("日", "").replace(".", "-")
        parts = re.split(r"[-/]", d)
        if len(parts) < 3:
            continue
        try:
            y, m_, day = int(parts[0]), int(parts[1]), int(parts[2])
            if y < 2020 or y > 2030:
                continue
            from datetime import datetime as _dt
            dt = _dt(y, m_, day)
            diff = (dt - now).days
            if -7 <= diff <= 90:
                if best_diff is None or (0 <= diff < best_diff):
                    best = f"{y:04d}-{m_:02d}-{day:02d}"
                    best_diff = diff
        except (ValueError, TypeError):
            continue

    if not best and result["dates"]:
        raw = result["dates"][0]
        d = raw.replace("年", "-").replace("月", "-").replace("日", "").replace(".", "-")
        parts = re.split(r"[-/]", d)
        if len(parts) >= 3:
            best = f"{parts[0]}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"

    return best

def _find_nearby_date(html: str, pos: int, window: int = 400) -> str:
    """在 pos 附近 window 字符范围内查找日期 yyyy-MM-dd。"""
    start = max(0, pos - window)
    end = min(len(html), pos + window)
    m = re.search(r"(\d{4}-\d{2}-\d{2})", html[start:end])
    return m.group(1) if m else ""

def fetch_cjhdj() -> list[dict]:
    """长江航道局 — 招标公告列表页（静态 HTML）。
    列表地址：https://www.cjhdj.com.cn/xxgk/wsgs/zbgg/
    链接形如 ./202608/t20260819_487694.shtml，日期从文件名提取。
    """
    base = "https://www.cjhdj.com.cn/xxgk/wsgs/zbgg/"
    html = _http_get(base)
    items: list[dict] = []
    # 优先匹配带 title 属性的链接
    pat = re.compile(r'<a[^>]*href="(\./\d{6}/t(\d{8})_\d+\.shtml)"[^>]*title="([^"]+)"', re.I)
    for m in pat.finditer(html):
        href, date_raw, title = m.group(1), m.group(2), m.group(3).strip()
        date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
        items.append({"source_url": urljoin(base, href), "title": title, "published_at": date_str,
                       "buyer": "", "region": "长江航道", "content": ""})
    if not items:
        pat2 = re.compile(r'<a[^>]*href="(\./\d{6}/t(\d{8})_\d+\.shtml)"[^>]*>([^<]+)</a>', re.I)
        for m in pat2.finditer(html):
            href, date_raw, title = m.group(1), m.group(2), m.group(3).strip()
            date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
            items.append({"source_url": urljoin(base, href), "title": title, "published_at": date_str,
                           "buyer": "", "region": "长江航道", "content": ""})
    return items

def fetch_csg() -> list[dict]:
    """南方电网供应链平台 — 采购公告列表页（静态 HTML）。
    列表地址：https://www.bidding.csg.cn/zbcg/index.jhtml
    仅采集招标公告(/zbgg/)、结果公示(/zbhxrgs/)、非招标公告(/fzbgg/)、零星采购(/lxcggg/)。
    """
    base = "https://www.bidding.csg.cn/zbcg/index.jhtml"
    html = _http_get(base)
    items: list[dict] = []
    # 仅匹配招标类公告路径
    pat = re.compile(r'<a[^>]*href="(/(?:zbgg|zbhxrgs|fzbgg|lxcggg)/\d+\.jhtml)"[^>]*title="([^"]+)"', re.I)
    seen: set[str] = set()
    for m in pat.finditer(html):
        href, title = m.group(1), m.group(2).strip()
        if len(title) < 6 or title.startswith("[") or href in seen:
            continue
        seen.add(href)
        date_str = _find_nearby_date(html, m.start())
        items.append({"source_url": urljoin(base, href), "title": title, "published_at": date_str,
                       "buyer": "", "region": "广东", "content": ""})
    # 回退：无 title 属性时用文本内容
    if not items:
        pat2 = re.compile(r'<a[^>]*href="(/(?:zbgg|zbhxrgs|fzbgg|lxcggg)/(\d+)\.jhtml)"[^>]*>(.*?)</a>', re.I | re.S)
        for m in pat2.finditer(html):
            href, title = m.group(1), re.sub(r'<[^>]+>', '', m.group(3)).strip()
            if len(title) < 6 or title.startswith("[") or href in seen:
                continue
            seen.add(href)
            date_str = _find_nearby_date(html, m.start())
            items.append({"source_url": urljoin(base, href), "title": title, "published_at": date_str,
                           "buyer": "", "region": "广东", "content": ""})
    return items

def fetch_crc() -> list[dict]:
    """华润守正电子招标平台 — 招标公告列表页（静态 HTML，需 SSL legacy）。
    列表地址：https://szecp.crc.com.cn/zbxx/006001/006001001/secondpagejy.html
    链接形如 /zbxx/006001/006001001/20260717/uuid.html，日期从路径提取。
    """
    base = "https://szecp.crc.com.cn/zbxx/006001/006001001/secondpagejy.html"
    html = _http_get(base)
    items: list[dict] = []
    pat = re.compile(r'<a[^>]*href="(/zbxx/006001/006001001/(\d{8})/[\w-]+\.html)"[^>]*>(.*?)</a>', re.I | re.S)
    for m in pat.finditer(html):
        href, date_raw, raw_title = m.group(1), m.group(2), m.group(3)
        title = re.sub(r'<[^>]+>', '', raw_title).strip()
        if len(title) < 6:
            continue
        date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
        items.append({"source_url": urljoin("https://szecp.crc.com.cn/", href), "title": title,
                       "published_at": date_str, "buyer": "", "region": "", "content": ""})
    return items

# 天眼查招投标搜索：按业务关键词搜全国招标公告（tyc operation bids，L3 search_bids）。
# 每个关键词每次抓取消耗 1 次账号额度。未指定 TYC_PROFILE 时保留手工/兼容模式；
# 定时任务通过 TYC_PROFILE 使用分层检索，控制额度并对搜索索引延迟做回查补偿。
TYC_SEARCH_PROFILES = {
    "critical": {"keywords": ["AIS", "VDES", "AIS岸基", "AIS基站"], "days": 3, "page_size": 100, "max_pages": 2},
    "core": {"keywords": ["航道", "航标", "甚高频", "海事通信", "数字航道", "智慧航道"], "days": 7, "page_size": 100, "max_pages": 1},
    "expansion": {"keywords": ["船舶", "卫星", "智慧海洋", "智慧港口", "海洋风电"], "days": 14, "page_size": 100, "max_pages": 1},
    # 天眼查详情/移动端与关键词检索索引可能延迟；每天回查关键词最近 7 天。
    "delay_backfill": {"keywords": ["AIS", "VDES", "AIS岸基", "AIS基站"], "days": 7, "page_size": 100, "max_pages": 3},
}

def _tyc_bid_keywords(cfg: dict) -> list[str]:
    """解析天眼查检索词列表。"""
    env_kw = os.environ.get("TYC_KEYWORDS")
    if env_kw:
        return [k.strip() for k in env_kw.split(",") if k.strip()]
    rule_kw = [str(k).strip() for k in ((cfg.get("rules") or {}).get("tyc_search_keywords") or []) if str(k).strip()]
    if rule_kw:
        return rule_kw
    src_cfg = (cfg.get("sources") or {}).get("tianyancha") or {}
    kw_str = src_cfg.get("keywords") or "航道,航标,海事"
    return [k.strip() for k in kw_str.split(",") if k.strip()]

def fetch_tianyancha() -> list[dict]:
    """天眼查 — 招投标公告关键词搜索（走天眼AI官方授权通道）。
    用 tyc CLI 的 search_bids 工具按业务关键词搜索全国招标公告（bidType=2 招标公告），
    返回结构化数据：标题、采购人、省份、发布时间、公告原文摘要、原始来源链接。
    """
    from datetime import timedelta
    cfg = load_config()
    src_cfg = (cfg.get("sources") or {}).get("tianyancha") or {}
    profile_name = os.environ.get("TYC_PROFILE", "").strip().lower()
    profile = TYC_SEARCH_PROFILES.get(profile_name)
    if profile:
        keywords = profile["keywords"]
        days = profile["days"]
        page_size = profile["page_size"]
        max_pages = profile["max_pages"]
    else:
        # 保持 CLI 手动抓取的原有语义，定时任务均会明确选择一个分层 profile。
        profile_name = "legacy"
        keywords = _tyc_bid_keywords(cfg)
        days = src_cfg.get("days", 90)
        page_size = 20
        max_pages = 1
    start = (cn_today() - timedelta(days=days)).strftime("%Y-%m-%d")
    end = cn_today().strftime("%Y-%m-%d")
    print(f"  天眼查 profile={profile_name} | {len(keywords)} 个词 | 最近 {days} 天 | 每页 {page_size} 条 | 最多 {max_pages} 页")
    items: list[dict] = []
    seen_urls: set[str] = set()
    for kw in keywords:
        for page in range(1, max_pages + 1):
            data = _tyc_json("operation", "bids", kw, "--bidType", "2",
                             "--publishStartTime", start, "--publishEndTime", end,
                             "--pageNum", str(page), "--pageSize", str(page_size))
            page_items = data.get("items") or []
            print(f"  [{profile_name}/{kw}] 第 {page} 页返回 {len(page_items)} 条")
            for it in page_items:
                link = it.get("link") or it.get("bidUrl") or ""
                title = str(it.get("title", "")).strip()
                if not link or not title or link in seen_urls:
                    continue
                seen_urls.add(link)
                content = str(it.get("content") or "")[:3000]
                # 广东一体化平台聚合后台接口（api-yst）：链接本身即公告详情（浏览器以聚合模板渲染），
                # 但搜索摘要不全；接口 JSON 的 htmlContent 是完整公告正文，取回补全。
                if "api-yst.gdzwfw.gov.cn" in link:
                    content = _gd_yst_content(link) or content
                items.append({
                    "source_url": link,
                    "title": title,
                    "published_at": it.get("publishTime", ""),
                    "buyer": it.get("purchaser", ""),
                    "region": it.get("province", ""),
                    "content": content,
                })
            # 已知第 2 页为空不扣费；不足一页表示没有后续数据，立即停下。
            if len(page_items) < page_size:
                break
    return items


# 中交招采网校验：天眼查检索返回的是聚合摘要，不能误当作中交公告原文。
# 部分公告的公开详情正文为空，但物资明细仍可匿名读取；仅保存重新拉到的公开明细，
# 并由 AI 评审跳过此类不完整依据。
_ICCEC_MATERIAL_API = "https://sp.iccec.cn/apis/sp/bidc/users/signup/queryProcessSchemeMatDetail"

def _http_post_json(url: str, data: dict, timeout: int = 20) -> dict:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = {**_HTTP_HEADERS, "Content-Type": "application/json;charset=UTF-8", "Accept": "application/json, text/plain, */*"}
    req = Request(url, data=body, headers=headers, method="POST")
    resp = urlopen(req, timeout=timeout, context=_make_ssl_context())
    return json.loads(resp.read().decode("utf-8", errors="replace"))

def _iccec_public_material_snapshot(source_url: str) -> tuple[str, str, str]:
    """返回（证据状态、规范化链接、仅含公开可核验物资明细的正文）。"""
    query = parse_qs(urlparse(source_url).query)
    scheme_id = (query.get("schemeId") or [""])[0].strip()
    scheme_code = (query.get("schemeCode") or [""])[0].strip()
    if not scheme_id or not scheme_code or not scheme_id.isdigit():
        return "链接缺少项目标识，无法公开核验", source_url, ""
    canonical = f"https://sp.iccec.cn/viewNoticeDetail?schemeId={scheme_id}&schemeCode={scheme_code}"
    try:
        raw = _http_post_json(_ICCEC_MATERIAL_API, {"schemeId": scheme_id, "agentId": 100123, "languageType": "zh-cn"})
    except Exception as exc:
        return f"公开物资明细抓取失败：{str(exc)[:80]}", canonical, ""
    mats = ((raw.get("data") or {}).get("matDetailList")) or []
    if not mats:
        return "公开页未返回物资明细，无法核验", canonical, ""
    lines = ["【中交招采网公开可核验信息】"]
    plan = str(mats[0].get("schemeName") or mats[0].get("planName") or "").strip()
    buyer = str(mats[0].get("reqUnitName") or "").strip()
    method = str(mats[0].get("purchaseTypeName") or "").strip()
    if plan: lines.append(f"项目名称：{plan}")
    if buyer: lines.append(f"采购单位：{buyer}")
    if method: lines.append(f"采购方式：{method}")
    lines.append("公开物资/服务明细：")
    for idx, mat in enumerate(mats, 1):
        name = str(mat.get("matName") or "服务/物资").strip()
        desc = str(mat.get("matDesc") or mat.get("remark") or "").strip()
        count = mat.get("purchaseNum") or mat.get("convNum") or ""
        unit = str(mat.get("measureUnitName") or mat.get("convMeasureUnitName") or "").strip()
        tax = str(mat.get("exchangeRangeLabel") or "").strip()
        bits = [f"{idx}. {name}"]
        if desc: bits.append(desc)
        if count != "": bits.append(f"数量 {count}{unit}")
        if tax: bits.append(f"税率 {tax}")
        lines.append("；".join(bits))
    lines.append("核验说明：公开详情接口未提供公告正文和附件；以上仅为重新抓取到的公开物资明细，不作为完整公告原文，也不会送入 AI 自动评审。")
    return "仅公开物资明细；公告正文和附件不可公开获取", canonical, "\n".join(lines)

_GD_YST_TAG_RE = re.compile(r"<[^>]+>")

def _gd_yst_content(api_url: str) -> str:
    """拉取广东一体化平台聚合接口的 JSON，取 data.htmlContent 拍平为正文文本；失败返回空串。"""
    try:
        data = (json.loads(_http_get(api_url, timeout=20)).get("data")) or {}
        html = data.get("htmlContent") or ""
        if not html:
            return ""
        text = _GD_YST_TAG_RE.sub(" ", html)
        return re.sub(r"\s+", " ", text).strip()[:3000]
    except Exception:
        return ""

def fetch_ccgp() -> list[dict]:
    """中国政府采购网 — 公开招标公告列表页（静态 HTML）。
    中央公告：http://www.ccgp.gov.cn/cggg/zygg/gkzb/index.htm
    分页：index.htm (第1页), index_1.htm (第2页), index_2.htm (第3页)...
    每页约 20 条；默认抓取前 3 页。
    """
    cfg = load_config()
    src_cfg = (cfg.get("sources") or {}).get("ccgp") or {}
    base_url = "http://www.ccgp.gov.cn/cggg/zygg/gkzb/"
    max_pages = src_cfg.get("max_pages", 3)
    items: list[dict] = []
    seen: set[str] = set()
    for page in range(max_pages):
        url = f"{base_url}index.htm" if page == 0 else f"{base_url}index_{page}.htm"
        try:
            html = _http_get(url, timeout=15)
        except Exception as e:
            print(f"  [ccgp] 第 {page+1} 页获取失败: {e}")
            break
        # 每个 <li> 包含 <a> 链接 + 发布时间/地域/采购人
        li_pat = re.compile(
            r'<li>\s*<a\s+href="([^"]+)"[^>]*title="([^"]+)"[^>]*>.*?</a>'
            r'(.*?)</li>', re.I | re.S)
        for m in li_pat.finditer(html):
            href, title, meta_block = m.group(1), m.group(2).strip(), m.group(3)
            abs_url = urljoin(url, href)
            if abs_url in seen:
                continue
            seen.add(abs_url)
            pub_m = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})', meta_block)
            reg_m = re.search(r'地域：<em>([^<]+)</em>', meta_block)
            buy_m = re.search(r'采购人：<em>([^<]+)</em>', meta_block)
            items.append({
                "source_url": abs_url,
                "title": title,
                "published_at": pub_m.group(1).strip() if pub_m else "",
                "buyer": buy_m.group(1).strip() if buy_m else "",
                "region": reg_m.group(1).strip() if reg_m else "",
                "content": "",
            })
        time.sleep(0.5)
    return items

# ---------------------------------------------------------------------------
# 中国招标投标公共服务平台 (bulletin.cebpubservice.com)
# 发改委指定法定媒介，依法必须招标项目同步发布。
# 列表页为静态 HTML（第1页），含标题、UUID、地域、行业、发布时间、开标时间。
# ---------------------------------------------------------------------------
def fetch_ceb() -> list[dict]:
    """中国招标投标公共服务平台 — 招标公告列表页（静态 HTML，仅第1页可达）。"""
    base = "https://bulletin.cebpubservice.com/xxfbcmses/search/bulletin.html"
    cfg = load_config()
    src_cfg = (cfg.get("sources") or {}).get("ceb") or {}
    max_pages = src_cfg.get("max_pages", 1)
    items: list[dict] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        url = f"{base}?categoryId=88&page={page}" if page > 1 else f"{base}?categoryId=88"
        try:
            html = _http_get(url, timeout=15)
        except Exception as e:
            print(f"  [ceb] 第 {page} 页获取失败: {e}")
            break
        # 每行: <td name="imgShow" id="时间戳"> <a href="urlOpen('UUID')" title="标题"> ...
        # <span title="行业"> ... <span title="地区"> ... <td>日期</td> ... <td name="openTime" id="开标时间">
        row_pat = re.compile(
            r'<td[^>]*name="imgShow"[^>]*id="([^"]*)"[^>]*>'
            r'.*?urlOpen\(\'([a-f0-9]{32})\'\)'
            r'[^>]*title="([^"]*)"'
            r'.*?<span\s+title\s*=\s*"([^"]*)"[^>]*>'
            r'.*?<span\s+title\s*=\s*"([^"]*)"[^>]*>'
            r'.*?<td[^>]*>([^<]*)</td>'
            r'.*?<td[^>]*>\s*([\d-]+)\s*</td>'
            r'.*?<td[^>]*name="openTime"[^>]*id="([^"]*)"',
            re.I | re.S,
        )
        for m in row_pat.finditer(html):
            _ts, uuid, title, industry, region, _source, pub_date, open_time = m.groups()
            title = title.strip()
            if not title or uuid in seen:
                continue
            seen.add(uuid)
            detail_url = f"https://ctbpsp.com/#/bulletinDetail?uuid={uuid}&inpvalue=&dataSource=0"
            items.append({
                "source_url": detail_url,
                "title": title,
                "published_at": pub_date.strip(),
                "buyer": "",
                "region": region.strip(),
                "content": f"[{industry.strip()}] 来源: {_source.strip()}",
                "deadline_at": _normalize_date(open_time) if open_time else "",
            })
        if len(items) == 0 and page > 1:
            break
        time.sleep(0.5)
    return items

# ---------------------------------------------------------------------------
# 全国公共资源交易平台 (www.ggzy.gov.cn)
# 发改委牵头，汇聚全国各省公共资源交易中心数据。
# 页面为 SPA，但后端 JSON API 可直接 POST 调用。
# ---------------------------------------------------------------------------
# 海事相关省份（沿海 11 省市 + 内陆河省）：全国公共资源交易平台聚合了各省公共资源交易数据，
# 按 DEAL_PROVINCE 分省拉取即可覆盖各省平台（山东/江苏/浙江/广东等），无需逐省写适配器。
GGZY_PROVINCES = {
    "120000": "天津", "130000": "河北", "210000": "辽宁", "310000": "上海",
    "320000": "江苏", "330000": "浙江", "350000": "福建", "370000": "山东",
    "440000": "广东", "450000": "广西", "460000": "海南",
    "220000": "吉林", "230000": "黑龙江", "340000": "安徽", "360000": "江西",
    "410000": "河南", "420000": "湖北", "430000": "湖南", "500000": "重庆",
    "510000": "四川", "520000": "贵州", "530000": "云南",
}

def fetch_ggzy() -> list[dict]:
    """全国公共资源交易平台 — JSON API 按省拉取招标/中标等公告列表。
    覆盖 22 个海事相关省份的省公共资源交易平台聚合数据；中标/成交类公告照常采集（含中标单位线索）。"""
    api_url = "https://www.ggzy.gov.cn/information/pubTradingInfo/getTradList"
    cfg = load_config()
    src_cfg = (cfg.get("sources") or {}).get("ggzy") or {}
    max_pages = src_cfg.get("max_pages", 2)  # 每省拉取页数（近一月窗口）
    items: list[dict] = []
    seen: set[str] = set()
    for prov_code, prov_name in GGZY_PROVINCES.items():
        for page in range(1, max_pages + 1):
            payload = {
                "SOURCE_TYPE": "1",
                "DEAL_TIME": "04",  # 近一月
                "DEAL_CLASSIFY": "",
                "DEAL_STAGE": "",
                "DEAL_PROVINCE": prov_code,
                "DEAL_CITY": "",
                "DEAL_PLATFORM": "",
                "BID_PLATFORM": "",
                "DEAL_TRADE": "",
                "FINDTXT": "",
                "PAGENUMBER": str(page),
            }
            try:
                raw = _http_post(api_url, payload, timeout=15)
                data = json.loads(raw)
            except Exception as e:
                print(f"  [ggzy] {prov_name} 第 {page} 页获取失败: {e}")
                break
            if data.get("code") != 200:
                print(f"  [ggzy] {prov_name} API 返回异常: code={data.get('code')} msg={data.get('message')}")
                break
            records = (data.get("data") or {}).get("records") or []
            if not records:
                break
            for r in records:
                rid = r.get("id", "")
                title = (r.get("title") or "").strip()
                info_type = (r.get("informationTypeText") or "").strip()
                # 合同公告无跟踪价值，其余（招标/资审/中标/成交）照常采集，交由评分管线筛选。
                if "合同" in title or "合同" in info_type:
                    continue
                if not title or rid in seen:
                    continue
                seen.add(rid)
                rel_url = r.get("url", "")
                abs_url = f"https://www.ggzy.gov.cn{rel_url}" if rel_url.startswith("/") else rel_url
                items.append({
                    "source_url": abs_url,
                    "title": title,
                    "published_at": r.get("publishTime", ""),
                    "buyer": "",
                    "region": r.get("provinceText", "") or prov_name,
                    "content": f"[{r.get('businessTypeText', '')}] {info_type} | {r.get('transactionSourcesPlatformText', '')}",
                })
            time.sleep(0.5)
    return items

# ---------------------------------------------------------------------------
# 海事系统与流域机构官网（交通运输部系统多为 TRS/大汉 CMS 静态列表页）。
# ---------------------------------------------------------------------------

def _parse_trs_list(html: str, base: str, region: str) -> list[dict]:
    """TRS CMS 静态列表通用解析：链接形如 .../202608/t20260812_487252.shtml。
    标题优先取 title 属性（完整），日期优先取文件名，其次取条目文本。"""
    items: list[dict] = []
    for m in re.finditer(r'<a\b[^>]*href="([^"]*?(\d{6})/t(\d{8})_\d+\.s?html)"[^>]*>(.*?)</a>', html, re.I | re.S):
        href, title_html, tail = m.group(1), m.group(4), ""
        tm = re.search(r'title="([^"]+)"', m.group(0))
        title = html_mod.unescape((tm.group(1) if tm else re.sub(r"<[^>]+>", "", title_html))).strip()
        if not title:
            continue
        date_raw = m.group(3)
        date_str = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}"
        items.append({"source_url": urljoin(base, href), "title": title, "published_at": date_str,
                      "buyer": "", "region": region, "content": ""})
    return items

def _parse_li_list(html: str, base: str, region: str, link_re: str) -> list[dict]:
    return parse_li_list(html, base, region, link_re)

def _crumb_of(html: str) -> str:
    """从详情页面包屑提取栏目名（如 当前位置：首页>政务公开>采购公告 → 采购公告）。"""
    m = re.search(r'(?:当前位置|您的位置)[：:].{0,120}?[>＞»]\s*([^<>»＞\s]{2,12})\s*(?:</|$)', html)
    return m.group(1) if m else ""

def fetch_cjhy() -> list[dict]:
    """长江航务管理局 — 长航局公告 + 重大建设项目招标公告（长江水系聚合源，TRS 静态列表）。"""
    items: list[dict] = []
    for base in ("https://cjhy.mot.gov.cn/xxgk/xxgkzl/zhjgg/",
                 "https://cjhy.mot.gov.cn/xxgk/xxgkzl/zdjsxm/ztbgg/"):
        try:
            items.extend(_parse_trs_list(_http_get(base), base, "长江航务"))
        except Exception as e:
            print(f"  [cjhy] {base} 获取失败: {e}")
    return items

def fetch_msa_bid() -> list[dict]:
    """交通运输部海事局 — 招标信息栏目（含部机关及直属海事系统采购/中标公告）。"""
    base = "https://www.msa.gov.cn/94232e693f1745eb91c1fde1cc09ecf5/index.jhtml"
    html = _http_get(base)
    return _parse_li_list(html, base, "海事局", r"/html/cnmsa/xxgk/article/")

def fetch_hb_msa() -> list[dict]:
    """河北海事局 — 海事项目招标公告 + 中标（成交）结果栏目。"""
    items: list[dict] = []
    for base in ("https://www.hb.msa.gov.cn/html/hshxmzbgg/",
                 "https://www.hb.msa.gov.cn/html/hshxmzbjg/"):
        try:
            # 站点做了根路径路由：列表页在 /html/ 下，详情链接为根路径 /hshxmzbgg/xxx.jhtml，urljoin 后直接可用。
            items.extend(_parse_li_list(_http_get(base), base, "河北海事", r"hshxmzb[a-z]*/\d+\.jhtml\b"))
        except Exception as e:
            print(f"  [hb_msa] {base} 获取失败: {e}")
    return items

def fetch_cjkhd() -> list[dict]:
    """长江口航道管理局 — 信息公开栏目（招标公告/中标候选人公示，TRS 静态列表）。"""
    base = "https://www.cjkhd.com/zwgk/xxgk/"
    items = _parse_trs_list(_http_get(base), base, "长江口航道")
    return [it for it in items if "/zwgk/xxgk/" in it["source_url"]]

def fetch_ln_msa() -> list[dict]:
    """辽宁海事局 — 招标信息/中标信息/政府集中采购三个栏目。"""
    items: list[dict] = []
    for base in ("https://www.ln.msa.gov.cn/xxgk/hsxm/zbxx/",
                 "https://www.ln.msa.gov.cn/xxgk/hsxm/dbxx/",
                 "https://www.ln.msa.gov.cn/xxgk/hsxm/zfjzcg/"):
        try:
            got = _parse_trs_list(_http_get(base), base, "辽宁海事")
            if not got:
                got = _parse_li_list(_http_get(base), base, "辽宁海事", r"\.s?html\b")
            items.extend(got)
        except Exception as e:
            print(f"  [ln_msa] {base} 获取失败: {e}")
    return items

def fetch_gd_msa() -> list[dict]:
    """广东海事局 — 招标信息栏目（采购量大）。"""
    base = "http://gd.msa.gov.cn/html/gdmsa/xxgk/gkml/zbxx/index.html"
    html = _http_get(base)
    return _parse_li_list(html, base, "广东海事", r"/html/gdmsa/xxgk/article/")

def fetch_cj_msa() -> list[dict]:
    """长江海事局 — 海事项目招标公告 + 中标公告栏目（TRS 静态列表）。"""
    items: list[dict] = []
    for base in ("http://cj.msa.gov.cn/xxgk/xxgkml/hsxm/zbgg/",
                 "http://cj.msa.gov.cn/xxgk/xxgkml/hsxm/zhbgg/"):
        try:
            got = _parse_trs_list(_http_get(base), base, "长江海事")
            items.extend(it for it in got if "/xxgk/xxgkml/hsxm/" in it["source_url"])
        except Exception as e:
            print(f"  [cj_msa] {base} 获取失败: {e}")
    return items

def fetch_sd_msa() -> list[dict]:
    """山东海事局 — 大汉 CMS：首页各栏目块提取 art_ 文章链接，从详情页面包屑识别采购公告栏目。"""
    base = "https://www.sd.msa.gov.cn/"
    html = _http_get(base)
    cands: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a\b[^>]*href=[\'"](/art/20\d{2}/\d{1,2}/\d{1,2}/art_\d+_\d+\.html)[\'"][^>]*title=[\'"]([^\'"]+)[\'"]', html):
        url, title = urljoin(base, m.group(1)), m.group(2).strip()
        if url in seen or len(title) < 6:
            continue
        seen.add(url)
        dm = re.search(r"/art/(20\d{2})/(\d{1,2})/(\d{1,2})/", m.group(1))
        cands.append({"source_url": url, "title": title,
                      "published_at": f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}",
                      "buyer": "", "region": "山东海事", "content": ""})
    # 只保留采购/招标类：优先读详情页面包屑栏目名，提不到时降级按标题关键词判断。
    items: list[dict] = []
    for it in cands[:12]:
        try:
            crumb = _crumb_of(_http_get(it["source_url"], timeout=12))
        except Exception:
            crumb = ""
        if any(k in crumb for k in ("采购", "招标", "中标", "成交")):
            it["region"] = f"山东海事·{crumb}"
            items.append(it)
        elif not crumb and re.search(r"采购|招标|比选|中标|成交|询价", it["title"]):
            items.append(it)
        time.sleep(0.5)
    return items

def fetch_js_msa() -> list[dict]:
    """江苏海事局 — 大汉 CMS 通知公告栏目（首页静态渲染，招标信息在栏目内，由评分管线筛选）。"""
    base = "http://www.js.msa.gov.cn/col/col11434/index.html"
    html = _http_get(base)
    items: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a\b[^>]*href=[\'"](/art/(20\d{2})/(\d{1,2})/(\d{1,2})/art_\d+_\d+\.html)[\'"][^>]*>(.*?)</a>', html, re.I | re.S):
        url = urljoin(base, m.group(1))
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(5))).strip()
        if len(title) < 6:
            continue
        items.append({"source_url": url, "title": title,
                      "published_at": f"{m.group(2)}-{int(m.group(3)):02d}-{int(m.group(4)):02d}",
                      "buyer": "", "region": "江苏海事", "content": ""})
    return items

def fetch_zj_msa() -> list[dict]:
    """浙江海事局 — 采购信息栏目（TRS 静态列表，需先跳 /ZJ/ 子站）。"""
    base = "https://www.zj.msa.gov.cn/ZJ/zwgk/gkml/cgxx/"
    items = _parse_trs_list(_http_get(base), base, "浙江海事")
    # 栏目页含全站导航，只保留采购信息栏目内的链接；"查看详情"等短链已按长度过滤。
    return [it for it in items if "/zwgk/gkml/cgxx/" in it["source_url"] and len(it["title"]) >= 6]

def fetch_hn_msa() -> list[dict]:
    """海南海事局 — 项目招标栏目（JEECMS，日期为 [YY-MM-DD] 两位年格式）。"""
    base = "https://www.hn.msa.gov.cn/xxgk_4_6/index.jhtml"
    # 仅接受项目招标栏目自身的详情链接；不能把页面导航中的其他公开栏目当招标公告。
    items = _parse_li_list(_http_get(base), base, "海南海事", r"/xxgk_4_6/\d+\.jhtml\b")
    for it in items:
        if not it["published_at"]:
            dm = re.search(r"\[(\d{2})-(\d{1,2})-(\d{1,2})\]", it["title"])
            if dm:
                it["published_at"] = f"20{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
            it["title"] = re.sub(r"\[\d{2}-\d{1,2}-\d{1,2}\]", "", it["title"]).strip()
        it["title"] = it["title"].lstrip("·• ").strip()
    return items

def fetch_cq_jtw() -> list[dict]:
    """重庆市交通运输委 — 招标公告栏目（含港航海事中心采购，TRS 静态列表）。"""
    base = "https://jtysw.cq.gov.cn/zwgk_240/zfxxgkml/gggs/zbtbxx/zbgg/"
    return _parse_trs_list(_http_get(base), base, "重庆交通")

def fetch_sc_jtt() -> list[dict]:
    """四川省交通运输厅 — 招投标信息专栏 + 采购公告栏目。"""
    items: list[dict] = []
    seen: set[str] = set()
    for base in ("https://jtt.sc.gov.cn/jtt/c101541/zfxxgk_list_jsgl.shtml",):
        try:
            html = _http_get(base)
        except Exception as e:
            print(f"  [sc_jtt] {base} 获取失败: {e}")
            continue
        for m in re.finditer(r'<a\b[^>]*href="(/jtt/c\d+/(20\d{2})/(\d{1,2})/(\d{1,2})/[^"]+\.s?html)"[^>]*>(.*?)</a>', html, re.I | re.S):
            url = urljoin(base, m.group(1))
            if url in seen:
                continue
            seen.add(url)
            tm = re.search(r'title="([^"]+)"', m.group(0))
            title = html_mod.unescape((tm.group(1) if tm else re.sub(r"<[^>]+>", "", m.group(5)))).strip()
            title = re.sub(r"\s+", " ", title).strip()
            if len(title) < 6:
                continue
            items.append({"source_url": url, "title": title,
                          "published_at": f"{m.group(2)}-{int(m.group(3)):02d}-{int(m.group(4)):02d}",
                          "buyer": "", "region": "四川交通", "content": ""})
    return items

def fetch_henan_jt() -> list[dict]:
    """河南省交通运输厅 — 政府采购 + 通知公告栏目（含水运工程招标/中标公示）。"""
    items: list[dict] = []
    seen: set[str] = set()
    for base in ("https://jtyst.henan.gov.cn/zc/zdlyzfxxgk/zfcg/",
                 "https://jtyst.henan.gov.cn/xw/tzgg"):
        try:
            html = _http_get(base)
        except Exception as e:
            print(f"  [henan_jt] {base} 获取失败: {e}")
            continue
        for m in re.finditer(r'<a\b[^>]*href="(http[^"]*/(20\d{2})/(\d{2}-\d{2})/\d+\.html)"[^>]*>(.*?)</a>', html, re.I | re.S):
            url = m.group(1)
            if url in seen:
                continue
            seen.add(url)
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(4))).strip()
            if len(title) < 6:
                continue
            items.append({"source_url": url, "title": title,
                          "published_at": f"{m.group(2)}-{m.group(3).replace('-', '-')}",
                          "buyer": "", "region": "河南交通", "content": ""})
    return items

def fetch_yn_hw() -> list[dict]:
    """云南省航务管理局 — 通知公告栏目（澜沧江/金沙江航运，由评分管线筛选）。"""
    base = "https://www.ynshwglj.org.cn/info/c/3.html"
    items = _parse_li_list(_http_get(base), base, "云南航务", r"/info/i/\d+\.html\b")
    return items

def fetch_hlj_msa() -> list[dict]:
    """黑龙江海事局 — 首页提取 /content/ 文章链接，面包屑识别采购/招标栏目（站点栏目页 JS 渲染）。"""
    base = "https://hlj.msa.gov.cn/"
    html = _http_get(base)
    items: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a\b[^>]*href="((?:https?://[^"/]+)?/content/(20\d{2})/[a-z]+/\d+\.html)"[^>]*>(.*?)</a>', html, re.I | re.S):
        url = urljoin(base, m.group(1))
        if url in seen:
            continue
        seen.add(url)
        title = html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(3)))).strip()
        if len(title) < 6:
            continue
        items.append({"source_url": url, "title": title, "published_at": m.group(2),
                      "buyer": "", "region": "黑龙江海事", "content": ""})
    # 只保留采购/招标类：优先详情页面包屑，提不到时降级按标题关键词。
    kept: list[dict] = []
    for it in items[:12]:
        try:
            crumb = _crumb_of(_http_get(it["source_url"], timeout=12))
        except Exception:
            crumb = ""
        if any(k in crumb for k in ("采购", "招标", "中标", "成交")):
            it["region"] = f"黑龙江海事·{crumb}"
            kept.append(it)
        elif not crumb and re.search(r"采购|招标|比选|中标|成交|询价", it["title"]):
            kept.append(it)
        time.sleep(0.5)
    return kept

def fetch_fj_msa() -> list[dict]:
    """福建海事局 — 首页提取文章链接（无独立采购栏目入口），标题关键词粗筛后交评分管线。"""
    base = "https://www.fj.msa.gov.cn/fjmsacms/cms/html/fjhsjwwwz/index.html"
    html = _http_get(base)
    items: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a\b[^>]*href="(/fjmsacms/cms/html/fjhsjwwwz/(20\d{2})-(\d{2})-(\d{2})/\d+\.html)"[^>]*>(.*?)</a>', html, re.I | re.S):
        url = urljoin(base, m.group(1))
        if url in seen:
            continue
        seen.add(url)
        title = html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(5)))).strip()
        if len(title) < 6:
            continue
        items.append({"source_url": url, "title": title,
                      "published_at": f"{m.group(2)}-{m.group(3)}-{m.group(4)}",
                      "buyer": "", "region": "福建海事", "content": ""})
    return items

def fetch_hlj_jt() -> list[dict]:
    """黑龙江省交通运输厅 — 通知公告栏目（省府模板，由评分管线筛选）。"""
    base = "https://jt.hlj.gov.cn/jt/c105088/list.shtml"
    items: list[dict] = []
    seen: set[str] = set()
    try:
        html = _http_get(base)
    except Exception as e:
        print(f"  [hlj_jt] 获取失败: {e}")
        return items
    for m in re.finditer(r'<a\b[^>]*href="(/jt/c\d+/(\d{6})/[^"]+\.s?html)"[^>]*>(.*?)</a>', html, re.I | re.S):
        url = urljoin(base, m.group(1))
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(3))).strip()
        # 日期可能写在条目文本尾部，先提取再从标题剔除；提不到时退回目录月份。
        dm = re.search(r"(20\d{2})-(\d{1,2})-(\d{1,2})", m.group(3))
        title = re.sub(r"\s*(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})\s*$", "", title).strip()
        if len(title) < 6:
            continue
        d = m.group(2)
        date_str = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}" if dm else f"{d[:4]}-{d[4:6]}"
        items.append({"source_url": url, "title": title, "published_at": date_str,
                      "buyer": "", "region": "黑龙江交通", "content": ""})
    return items

def fetch_sd_port() -> list[dict]:
    """山东港口阳光慧采 — 招标（预审）公告/采购寻源/采购成交三个静态栏目，日期在 URL 路径中。"""
    items: list[dict] = []
    seen: set[str] = set()
    for base in ("https://yghc.sd-port.com/jyxx/012001/012001001/prej_project.html",
                 "https://yghc.sd-port.com/jyxx/012002/012002001/xjproject.html",
                 "https://yghc.sd-port.com/jyxx/012002/012002002/xjproject.html"):
        try:
            html = _http_get(base)
        except Exception as e:
            print(f"  [sd_port] {base} 获取失败: {e}")
            continue
        for m in re.finditer(r'<a\b[^>]*href="(/jyxx/0120\d{2}/\d+/(20\d{6})/\d+\.html)"[^>]*>(.*?)</a>', html, re.I | re.S):
            url = urljoin(base, m.group(1))
            if url in seen:
                continue
            seen.add(url)
            title = html_mod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(3)))).strip()
            if len(title) < 6:
                continue
            d = m.group(2)
            items.append({"source_url": url, "title": title,
                          "published_at": f"{d[:4]}-{d[4:6]}-{d[6:]}",
                          "buyer": "", "region": "山东港口集团", "content": ""})
    return items

def fetch_tj_port() -> list[dict]:
    """天津港集团 — 集团公告频道（静态列表，招标/环评/公示类由评分管线筛选）。"""
    base = "https://www.ptacn.com/channels/1080.html"
    html = _http_get(base)
    items: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a\b[^>]*href="(/contents/1080/\d+\.html)"[^>]*>(.*?)</a>', html, re.I | re.S):
        url = urljoin(base, m.group(1))
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
        title = re.sub(r"^\d{4}-\d{2}-\d{2}", "", title).strip()
        if len(title) < 6:
            continue
        items.append({"source_url": url, "title": title, "published_at": "",
                      "buyer": "", "region": "天津港集团", "content": ""})
    return items

def fetch_ln_port() -> list[dict]:
    """辽港集团 — 通知公告栏目（静态列表，由评分管线筛选）。"""
    base = "http://www.liaoningport.com/html/web/lnport/gkgg/tzgg/index.html"
    html = _http_get(base)
    items: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'<a\b[^>]*href="(/html/web/lnport//?gkgg/tzgg/\d+\.html)"[^>]*>(.*?)</a>', html, re.I | re.S):
        url = urljoin(base, m.group(1))
        if url in seen:
            continue
        seen.add(url)
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if len(title) < 6:
            continue
        items.append({"source_url": url, "title": title, "published_at": "",
                      "buyer": "", "region": "辽港集团", "content": ""})
    return items

# ---------------------------------------------------------------------------
# 政采网关键词检索（全国含地方采购公告）——有频控，需限速，独立定时调度，不进常规抓取轮次。
# ---------------------------------------------------------------------------

def _parse_ccgp_search_page(html: str) -> list[dict]:
    """解析政采网搜索结果页（服务端渲染静态 HTML）。列表结构：
    <ul class="vT-srch-result-list…"><li><a href=公告URL>标题（关键词<font>高亮）</a><p>摘要</p>
    <span>发布时间 | 采购人 | 代理机构</span>…</li>…</ul>"""
    items: list[dict] = []
    m = re.search(r'<ul class="vT-srch-result-list[^"]*">(.*?)</ul>', html, re.S)
    block = m.group(1) if m else html
    for li in re.findall(r'<li>(.*?)</li>', block, re.S):
        am = re.search(r'<a[^>]*href="(http[^"]+)"[^>]*>(.*?)</a>', li, re.S)
        if not am:
            continue
        url = am.group(1)
        title = re.sub(r'<[^>]+>', '', am.group(2)).strip()
        if not title:
            continue
        # 发布时间：只从 <span> 元信息段取首段（如 2026.08.21 18:23:04），避免误拿正文里的截止日期；
        # 同时解析 采购人/地区（结构：发布时间 | 采购人：xx | 代理机构：xx <br/> 公告类型 | 地区 | 品目）
        pub, buyer, region = "", "", ""
        sm = re.search(r'<span[^>]*>(.*?)</span>', li, re.S)
        if sm:
            span_text = re.sub(r'<[^>]+>', '|', sm.group(1))
            span_text = re.sub(r'[\s|]+', '|', span_text).strip('|')
            parts = [p.strip() for p in span_text.split('|')]
            dm = re.search(r'(\d{4})[年.-](\d{1,2})[月.-](\d{1,2})', parts[0] if parts else "")
            if dm:
                pub = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
            for p in parts[1:]:
                if p.startswith("采购人") and "：" in p:
                    buyer = p.split("：", 1)[1].strip()
            # 地区：公告类型（*公告/*公示，去掉尾部括号后缀如“（第二次）”）之后的短段即为地区
            for idx, p in enumerate(parts):
                p_clean = re.sub(r'[（(].*$', '', p)
                if re.match(r'^[\u4e00-\u9fff]{2,10}(公告|公示)$', p_clean) and idx + 1 < len(parts):
                    nxt = parts[idx + 1]
                    if 1 < len(nxt) <= 12 and '/' not in nxt and '：' not in nxt:
                        region = nxt
                    break
        # 摘要（<p>段）当正文用：供入库后提取核心要点（金额/截止时间等），不再额外请求详情页（防频控）
        pm = re.search(r'<p>(.*?)</p>', li, re.S)
        summary = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', pm.group(1))).strip() if pm else ""
        items.append({"source_url": url, "title": title, "published_at": pub,
                      "buyer": buyer, "region": region, "content": summary})
    return items

def fetch_ccgp_search() -> list[dict]:
    """中国政府采购网·关键词检索——按行业检索词搜全国（含地方）采购公告。
    频控保护：词与词之间强制间隔；命中频控/维护页立即中止本轮（抛异常，剩余词顺延下轮）。"""
    cfg = load_config()
    rules = cfg.get("rules") or {}
    kws = rules.get("tyc_search_keywords") or ["航道", "航标", "海事"]
    src_cfg = (cfg.get("sources") or {}).get("ccgp_search") or {}
    delay = src_cfg.get("delay_seconds", 45)
    days = src_cfg.get("days", 3)
    max_pages = src_cfg.get("max_pages", 2)
    today = cn_today()
    start = (today - timedelta(days=days)).strftime("%Y:%m:%d")
    end = today.strftime("%Y:%m:%d")
    items: list[dict] = []
    seen: set[str] = set()
    for i, kw in enumerate(kws):
        if i:
            print(f"  限速等待 {delay}s …")
            time.sleep(delay)
        for page in range(1, max_pages + 1):
            params = {"searchtype": "1", "page_index": str(page), "bidSort": "0", "pinMu": "0",
                      "bidType": "0", "dbselect": "bidx", "kw": kw,
                      "start_time": start, "end_time": end, "timeType": "1",
                      "displayZone": "", "zoneId": "", "pppStatus": "0", "agentName": ""}
            url = "http://search.ccgp.gov.cn/bxsearch?" + urlencode(params)
            try:
                html = _http_get(url, timeout=25)
            except Exception as e:
                print(f"  [{kw}] p{page} 请求失败: {e}")
                break
            # 频控/维护页：页面极小且无结果列表，立即中止本轮，避免 IP 被长时间封禁
            if "vT-srch-result-list" not in html and ("频繁" in html or "维护" in html or len(html) < 6000):
                raise RuntimeError(f"政采网搜索触发频控/维护（关键词[{kw}] p{page}），本轮中止，待下一轮重试")
            got = _parse_ccgp_search_page(html)
            for it in got:
                # 搜索站是宽松子串匹配，AIS 搜索会命中 AISPORTS 等无关词；
                # 先按“独立术语 + 海事语境”闸门过滤，再进入详情抓取和入库评分。
                if not ccgp_search_keyword_ok(it, kw):
                    print(f"  [{kw}] 跳过(检索词误匹配) | {it['title'][:60]}")
                    continue
                if it["source_url"] in seen:
                    continue
                seen.add(it["source_url"])
                it["search_keyword"] = kw
                items.append(it)
            print(f"  [{kw}] p{page}: {len(got)} 条")
            if len(got) < 20:
                break
            time.sleep(min(delay, 15))
    return items

# ---------------------------------------------------------------------------
# 湖南省公共资源交易服务平台（hnsggzy.com tradeApi）——支持关键词搜索，详情接口返回完整正文，无反爬。
# ---------------------------------------------------------------------------

def fetch_hunan() -> list[dict]:
    """湖南省公共资源交易平台 — 工程建设招标公告（近 7 天，按行业词搜索，含公告正文）。"""
    base = "https://www.hnsggzy.com/tradeApi"
    cfg = load_config()
    rules = cfg.get("rules") or {}
    kws = rules.get("tyc_search_keywords") or ["航道", "航标", "海事"]
    src_cfg = (cfg.get("sources") or {}).get("hn_ggzy") or {}
    days = src_cfg.get("days", 7)
    max_details = src_cfg.get("max_details", 60)
    today = cn_today()
    t_start = (today - timedelta(days=days)).strftime("%Y-%m-%d 00:00:00")
    t_end = today.strftime("%Y-%m-%d 23:59:59")
    items: list[dict] = []
    seen: set[str] = set()
    detail_count = 0
    for kw in kws:
        params = {"current": "1", "size": "100", "notice": "0", "noticeName": kw,
                  "noticeSendTimeStart": t_start, "noticeSendTimeEnd": t_end,
                  "regionCode": "", "descs": "noticeSendTime"}
        url = base + "/constructionTender/listByFile?" + urlencode(params)
        try:
            d = json.loads(_http_get(url, timeout=25))
        except Exception as e:
            print(f"  [{kw}] 列表请求失败: {e}")
            continue
        rows = ((d.get("data") or {}).get("records")) or []
        for r in rows:
            sid = r.get("bidSectionId") or ""
            # 标题：项目全名 + 标段名（列表接口 bidSectionName 常只含标段段，需拼接项目名才能与其他来源对上）
            proj = (r.get("tenderProjectName") or "").strip()
            sect = (r.get("bidSectionName") or "").strip()
            if proj and sect and sect != proj and sect not in proj:
                title = proj + sect
            else:
                title = proj or sect
            if not sid or not title or sid in seen:
                continue
            seen.add(sid)
            # 正文：详情接口 constructionNotice/getBySectionId，控制单轮请求量上限
            content = ""
            if detail_count < max_details:
                try:
                    dd = json.loads(_http_get(f"{base}/constructionNotice/getBySectionId?sectionId={sid}", timeout=20))
                    nl = ((dd.get("data") or {}).get("noticeList")) or []
                    if nl:
                        content = re.sub(r'<[^>]+>', ' ', nl[0].get("noticeContent") or "")
                        content = re.sub(r'\s+', ' ', content).strip()[:3000]
                    detail_count += 1
                except Exception:
                    pass
                time.sleep(1)
            items.append({
                "source_url": f"https://www.hnsggzy.com/#/resources/transactionDetail/construction?bidSectionId={sid}&t=GC",
                "title": title,
                "published_at": (r.get("noticeSendTime") or "")[:10],
                "buyer": "",
                "region": r.get("name") or "湖南省",
                "content": content,
            })
        print(f"  [{kw}] {len(rows)} 条")
        time.sleep(2)
    return items

ADAPTERS: dict[str, callable] = {
    "cjhdj": fetch_cjhdj,
    "csg": fetch_csg,
    "crc": fetch_crc,
    "tianyancha": fetch_tianyancha,
    "ccgp": fetch_ccgp,
    "ceb": fetch_ceb,
    "ggzy": fetch_ggzy,
    "cjhy": fetch_cjhy,
    "msa": fetch_msa_bid,
    "hb_msa": fetch_hb_msa,
    "cjkhd": fetch_cjkhd,
    "ln_msa": fetch_ln_msa,
    "gd_msa": fetch_gd_msa,
    "cj_msa": fetch_cj_msa,
    "sd_msa": fetch_sd_msa,
    "js_msa": fetch_js_msa,
    "zj_msa": fetch_zj_msa,
    "hn_msa": fetch_hn_msa,
    "cq_jtw": fetch_cq_jtw,
    "sc_jtt": fetch_sc_jtt,
    "henan_jt": fetch_henan_jt,
    "yn_hw": fetch_yn_hw,
    "hlj_jt": fetch_hlj_jt,
    "hlj_msa": fetch_hlj_msa,
    "fj_msa": fetch_fj_msa,
    "sd_port": fetch_sd_port,
    "tj_port": fetch_tj_port,
    "ln_port": fetch_ln_port,
    "hn_ggzy": fetch_hunan,
    "ccgp_search": fetch_ccgp_search,
}

# 不进常规抓取轮次（全部来源）的来源：政采网检索有频控，只能独立低频调度。
MANUAL_ONLY_SOURCES = {"ccgp_search", "tianyancha"}

# ---------------------------------------------------------------------------
# 天眼查（tyc CLI）采购单位画像增强
# 前置条件：已安装 tyc-cli（npm install -g tyc-cli）并完成 tyc login（OAuth Device Flow）。
# 每次查询消耗天眼查账号额度，仅对高分采购单位做定向画像，不做全量抓取。
# ---------------------------------------------------------------------------

# 本机兜底路径：Git Bash 下 npm 生成的 tyc sh shim 存在路径损坏问题，直调 node + 入口文件更稳。
# 部署到其他机器时无需此配置：TYC_CMD 环境变量或 PATH 中的 tyc 均可被自动发现。
_TYC_LOCAL_FALLBACK = (
    r"C:\Users\lenovo\.workbuddy\binaries\node\versions\22.22.2\node.exe",
    r"C:\Users\lenovo\.workbuddy\binaries\node\versions\22.22.2\node_modules\tyc-cli\dist\index.js",
)

def _tyc_command() -> list[str] | None:
    """定位可用的 tyc 调用命令。优先级：TYC_CMD 环境变量 → PATH 中的 tyc → 本机 node 直调。"""
    env_cmd = os.environ.get("TYC_CMD")
    if env_cmd:
        return env_cmd.split()
    # Windows 下 npm 生成的是 tyc.cmd（无扩展名的 sh shim 不能被 CreateProcess 直接执行）
    names = ["tyc.cmd", "tyc.exe", "tyc.bat", "tyc"] if os.name == "nt" else ["tyc"]
    for name in names:
        found = shutil.which(name)
        if found:
            return [found]
    node, entry = _TYC_LOCAL_FALLBACK
    if os.path.isfile(node) and os.path.isfile(entry):
        return [node, entry]
    return None

def _tyc_json(*args: str, timeout: int = 90) -> dict:
    """调用 tyc CLI 并解析 JSON 输出。"""
    cmd = _tyc_command()
    if cmd is None:
        raise RuntimeError("tyc CLI 不可用：请先安装（npm install -g tyc-cli）并登录（tyc login），或设置 TYC_CMD 环境变量指向可用命令")
    result = subprocess.run(cmd + list(args), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"tyc 调用失败：{(result.stderr or result.stdout).strip()[:200]}")
    out = result.stdout
    start = out.find("{")
    if start < 0:
        raise RuntimeError("tyc 未返回 JSON 数据")
    return json.loads(out[start:])

def enrich_buyer(conn: sqlite3.Connection, buyer: str, with_risk: bool = False) -> dict:
    """调用天眼查为采购单位生成工商画像，入库 buyer_profiles。返回画像字段 dict。"""
    reg = _tyc_json("company", "registration-info", buyer)
    base = (reg.get("sources") or {}).get("base") or {}
    # 空结果时 base.name 是数据源名称（如"企业基本信息（含企业联系方式）"），需用信用代码/成立时间判定真实命中
    matched = (not base.get("empty")) and bool(base.get("creditCode") or base.get("estiblishTime"))
    risk_raw: dict = {}
    if with_risk and matched:
        risk_raw = _tyc_json("risk", "overview", base["name"])
    row = {
        "buyer": buyer,
        "company_name": base.get("name", "") if matched else "",
        "credit_code": base.get("creditCode", ""),
        "reg_status": base.get("regStatus", ""),
        "legal_person": base.get("legalPersonName", ""),
        "reg_capital": base.get("regCapital", ""),
        "estiblished": str(base.get("estiblishTime") or ""),
        "reg_location": base.get("regLocation", ""),
        "tags": base.get("tags", ""),
        "match_status": "matched" if matched else "no_match",
        "raw_json": json.dumps({"registration": reg, "risk": risk_raw}, ensure_ascii=False),
        "enriched_at": now(),
    }
    conn.execute("""INSERT INTO buyer_profiles(buyer,company_name,credit_code,reg_status,legal_person,reg_capital,estiblished,reg_location,tags,match_status,raw_json,enriched_at)
      VALUES(:buyer,:company_name,:credit_code,:reg_status,:legal_person,:reg_capital,:estiblished,:reg_location,:tags,:match_status,:raw_json,:enriched_at)
      ON CONFLICT(buyer) DO UPDATE SET company_name=excluded.company_name,credit_code=excluded.credit_code,reg_status=excluded.reg_status,
      legal_person=excluded.legal_person,reg_capital=excluded.reg_capital,estiblished=excluded.estiblished,reg_location=excluded.reg_location,
      tags=excluded.tags,match_status=excluded.match_status,raw_json=excluded.raw_json,enriched_at=excluded.enriched_at""", row)
    conn.commit()
    return row

def fetch_source(conn: sqlite3.Connection, source_code: str) -> tuple[int, int]:
    """执行指定来源的适配器，获取公告并入库。返回 (新增数, 更新数, 跳过数)。"""
    adapter = ADAPTERS.get(source_code)
    if not adapter:
        raise ValueError(f"来源 {source_code} 没有已实现的适配器")
    cfg = load_config()
    items = adapter()
    stamp = now()
    created, updated, skipped = 0, 0, 0
    verify = cfg.get("verify_links", True)
    vtimeout = cfg.get("verify_link_timeout", 8)
    # 全量入库（不做发布时间过滤）：海事局/流域机构官网栏目更新频率低（月均数条），
    # 近 7 天窗口会漏掉大部分公告；历史数据由 retention_days（365）留存期统一管理。
    for item in items:
        item["source_code"] = source_code
        try:
            link_ok = 1
            # 天眼查返回的是手机站深链（带签名参数与防爬校验），HEAD 验证必然失败，跳过验证直接入库；
            # hn_ggzy 是 hash 路由页面；多个政务站点拒绝 HEAD 方法（403），同样跳过。
            if verify and source_code not in ("tianyancha", "hn_ggzy", "hb_msa", "ln_msa", "hn_msa", "sd_port"):
                if not _http_head(item["source_url"], timeout=vtimeout):
                    link_ok = 0
                    print(f"  跳过(链接不可达) | {item['title'][:50]}")
                    skipped += 1
                    continue
            # 截止时间 + 正文降级链：①优先详情页正文 ②降级适配器自带摘要 ③都没有则前端显示命中关键词。
            # 天眼查/湖南平台公告原文已随结果返回，直接从内容提取，不重复请求详情页。
            cur_content = item.get("content") or ""
            if source_code in ("tianyancha", "hn_ggzy"):
                if not item.get("deadline_at"):
                    dl = _extract_deadline(cur_content, source_code)
                    if dl:
                        item["deadline_at"] = dl
            elif not item.get("deadline_at") or len(cur_content) < 150:
                dl, dtext = _fetch_detail_full(item["source_url"], source_code)
                if dl and not item.get("deadline_at"):
                    item["deadline_at"] = dl
                if len(cur_content) < 150 and len(dtext) > len(cur_content):
                    item["content"] = dtext  # 详情页正文优先；抓取失败保留原摘要
                # 天津港公告列表未给日期时，必须从详情正文补齐；否则空日期会绕过时效过滤。
                if source_code == "tj_port" and not item.get("published_at"):
                    published = _extract_published_at_text(dtext)
                    if published:
                        item["published_at"] = published
            was_created, score = upsert_tender(conn, item, link_ok=link_ok, rules=cfg.get("rules"))
            if score == 0:
                # 未命中业务关键词被丢弃，不计入更新数（避免假更新干扰统计）
                continue
            if was_created:
                created += 1
            else:
                updated += 1
            print(f"  {'新增' if was_created else '更新'} {rating_label(score, (cfg.get('rules') or {}).get('opportunity_levels'))} | {item['title'][:60]}")
        except Exception as e:
            print(f"  跳过: {e}")
    # ---- 正文回填（详情→摘要→关键词降级链）：库里正文仍单薄的记录逐条抓详情页补齐 ----
    # 天眼查自带约 800 字摘要、湖南自带完整正文，无需回填；存量瘦记录每轮限量补，逐步清零。
    if source_code not in ("tianyancha", "hn_ggzy"):
        refill_cfg = (cfg.get("sources") or {}).get(source_code) or {}
        refill_limit = int(refill_cfg.get("max_details", 30))
        thin_rows = conn.execute(
            """SELECT id, source_url FROM tenders
               WHERE is_deleted=0 AND source_code LIKE ? AND LENGTH(COALESCE(content,'')) < 150
               ORDER BY id DESC LIMIT ?""", (f"%{source_code}%", refill_limit)).fetchall()
        if thin_rows:
            print(f"  正文回填：{len(thin_rows)} 条记录正文单薄，尝试抓取详情页…")
            for r in thin_rows:
                try:
                    dl, dtext = _fetch_detail_full(r["source_url"], source_code)
                    if len(dtext) >= 150:
                        conn.execute("""UPDATE tenders SET content=?,
                            deadline_at=CASE WHEN deadline_at='' AND ?!='' THEN ? ELSE deadline_at END,
                            updated_at=? WHERE id=?""",
                            (dtext, dl, dl, stamp, r["id"]))
                        conn.commit()
                except Exception:
                    pass
                time.sleep(2)
    # 更新来源状态
    conn.execute(
        "UPDATE sources SET status='connected', last_checked_at=?, last_success_at=?, last_error=NULL WHERE code=?",
        (stamp, stamp, source_code),
    )
    conn.commit()
    return created, updated, skipped


INDEX_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>遨海商机雷达</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{font-size:14px}
body{
  background:#f0f2f5;
  color:#333;
  font-family:-apple-system,"Noto Sans SC","PingFang SC","Microsoft YaHei","Helvetica Neue",sans-serif;
  line-height:1.5;
}
a{color:#409eff;text-decoration:none}
a:hover{color:#66b1ff}

/* ---- sidebar ---- */
.sidebar{
  position:fixed;left:0;top:0;bottom:0;
  width:210px;
  background:#001529;
  color:#fff;
  z-index:100;
  display:flex;flex-direction:column;
  overflow-y:auto;
}
.sidebar-brand{
  height:56px;
  display:flex;align-items:center;
  padding:0 16px;
  font-size:1rem;font-weight:600;
  border-bottom:1px solid rgba(255,255,255,.08);
  flex-shrink:0;
}
.sidebar-brand span{color:#409eff;margin-right:6px;font-size:1.1rem}
.sidebar-nav{padding:8px 0;flex:1}
.nav-group-title{
  padding:12px 16px 6px;
  font-size:.72rem;color:rgba(255,255,255,.35);
  text-transform:uppercase;letter-spacing:.05em;
}
.nav-item{
  display:flex;align-items:center;gap:8px;
  padding:10px 16px 10px 24px;
  font-size:.88rem;color:rgba(255,255,255,.65);
  cursor:pointer;transition:all .2s;
  border-left:3px solid transparent;
}
.nav-item:hover{color:#fff;background:rgba(255,255,255,.06)}
.nav-item.active{color:#fff;background:rgba(64,158,255,.15);border-left-color:#409eff}
.nav-item .nav-icon{font-size:1rem;width:18px;text-align:center}

/* ---- main area ---- */
.main{margin-left:210px;min-height:100vh;display:flex;flex-direction:column}
.topbar{
  height:56px;background:#fff;
  border-bottom:1px solid #e8e8e8;
  display:flex;align-items:center;
  padding:0 24px;
  position:sticky;top:0;z-index:50;
}
.breadcrumb{font-size:.88rem;color:#666}
.breadcrumb a{color:#999}
.breadcrumb .sep{margin:0 8px;color:#ccc}
.breadcrumb .current{color:#333;font-weight:500}
/* 顶栏字号切换：改 html 根字号，所有 rem 尺寸（表格/标题/标签）等比放大 */
.font-switch{margin-left:auto;display:flex;align-items:center;gap:6px}
.font-switch-label{font-size:.78rem;color:#91a0b6}
.font-btn{border:1px solid #d7e2f0;background:#fff;color:#606266;font-size:.76rem;padding:3px 10px;border-radius:5px;cursor:pointer;transition:all .15s}
.font-btn:hover{border-color:#9ac5fa;color:var(--blue-600, #2f87ee)}
.font-btn.active{background:linear-gradient(135deg,#2f87ee,#1465ce);border-color:transparent;color:#fff;font-weight:600}
.content{padding:20px 24px;flex:1}

/* ---- page sections ---- */
.page{display:none}
.page.active{display:block}

/* ---- stats cards ---- */
.stats-row{display:flex;gap:16px;margin-bottom:20px;flex-wrap:wrap}
.stat-card{
  background:#fff;border-radius:4px;
  padding:20px 24px;flex:1;min-width:160px;
  box-shadow:0 1px 4px rgba(0,0,0,.06);
  display:flex;flex-direction:column;gap:4px;
}
.stat-card .stat-num{font-size:1.8rem;font-weight:700;color:#333;line-height:1.2}
.stat-card .stat-label{font-size:.8rem;color:#999}
.stat-card.key .stat-num{color:#f56c6c}
.stat-card.follow .stat-num{color:#e6a23c}

/* ---- card panel ---- */
.card{
  background:#fff;border-radius:4px;
  box-shadow:0 1px 4px rgba(0,0,0,.06);
  margin-bottom:20px;
}
.card-header{
  padding:14px 20px;
  border-bottom:1px solid #ebeef5;
  font-size:.95rem;font-weight:600;color:#333;
  display:flex;align-items:center;justify-content:space-between;
}
.card-body{padding:20px}

/* ---- filter bar ---- */
.filter-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.filter-bar input{
  border:1px solid #dcdfe6;border-radius:4px;
  padding:6px 12px;font-size:.88rem;
  outline:none;transition:border-color .2s;
  min-width:240px;max-width:400px;
}
.filter-bar input:focus{border-color:#409eff}
.filter-bar select{
  border:1px solid #dcdfe6;border-radius:4px;
  padding:6px 12px;font-size:.85rem;
  background:#fff;cursor:pointer;outline:none;
}
.filter-bar select:focus{border-color:#409eff}
.btn-primary{
  background:#409eff;color:#fff;border:none;
  border-radius:4px;padding:7px 18px;
  font-size:.85rem;cursor:pointer;
  transition:background .2s;
}
.btn-primary:hover{background:#66b1ff}
.btn-default{
  background:#fff;color:#606266;border:1px solid #dcdfe6;
  border-radius:4px;padding:7px 18px;
  font-size:.85rem;cursor:pointer;
  transition:all .2s;
}
.btn-default:hover{color:#409eff;border-color:#c6e2ff;background:#ecf5ff}
.btn-success{
  background:#67c23a;color:#fff;border:none;
  border-radius:4px;padding:7px 18px;
  font-size:.85rem;cursor:pointer;
}
.btn-success:hover{background:#85ce61}
.btn-danger{
  background:#f56c6c;color:#fff;border:none;
  border-radius:4px;padding:7px 18px;
  font-size:.85rem;cursor:pointer;
}
.btn-danger:hover{background:#f78989}
.hint{font-size:.78rem;color:#999;margin-top:10px}

/* ---- table ---- */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse}
thead th{
  text-align:left;font-size:.8rem;font-weight:600;
  color:#909399;padding:10px 14px;
  border-bottom:1px solid #ebeef5;
  background:#fafafa;white-space:nowrap;
}
tbody td{
  padding:12px 14px;border-bottom:1px solid #ebeef5;
  font-size:.88rem;vertical-align:top;
}
tbody tr:hover{background:#f5f7fa}
tbody tr:last-child td{border-bottom:none}

/* rating badge */
.rating{
  display:inline-block;font-size:.72rem;font-weight:600;
  padding:2px 10px;border-radius:3px;white-space:nowrap;
}
.rating-key{background:#fef0f0;color:#f56c6c}
.rating-follow{background:#fdf6ec;color:#e6a23c}
.rating-watch{background:#f4f4f5;color:#909399}

.tender-title{font-weight:500;line-height:1.45}
.tender-title a{color:#333}
.tender-title a:hover{color:#409eff}
/* 标题同行右侧：数据来源悬浮提示圆圈 */
.src-q{display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;margin-left:8px;border:1px solid #b9c6d8;border-radius:50%;font-size:10px;font-weight:600;color:#8492a7;cursor:help;position:relative;vertical-align:2px;flex:none}
.src-q:hover{border-color:var(--blue-500);color:var(--blue-600)}
.src-q::after{content:attr(data-tip);position:absolute;left:50%;transform:translateX(-50%) translateY(4px);bottom:calc(100% + 6px);background:#1f2d3d;color:#fff;font-size:12px;font-weight:400;padding:5px 10px;border-radius:6px;white-space:nowrap;opacity:0;pointer-events:none;transition:opacity .15s,transform .15s;z-index:20}
.src-q:hover::after{opacity:1;transform:translateX(-50%) translateY(0)}
/* 标题下方：核心要点 / 命中关键词 */
.tender-points{margin-top:5px;font-size:.75rem;display:flex;flex-wrap:wrap;gap:5px;align-items:center}
.points-label{color:#8492a7;font-size:.72rem}
.kp-tag{background:#eefaf1;color:#2e7d46;border-radius:3px;padding:1px 8px;font-size:.85rem}
.kw-tag{background:#eef5ff;color:#3974bd;border-radius:3px;padding:1px 8px;font-size:.85rem}
/* ---- 卡片式公告列表（方案B）：每条公告一张卡片，左侧优先级色条 ---- */
.tcard-list{display:flex;flex-direction:column;gap:10px;padding:14px 2px 6px}
.tcard{background:#fff;border:1px solid var(--line);border-radius:10px;padding:13px 18px 12px;position:relative;overflow:hidden;box-shadow:0 1px 3px rgba(16,42,84,.04);transition:box-shadow .15s,border-color .15s}
.tcard:hover{box-shadow:0 4px 14px rgba(16,42,84,.09);border-color:#d7e4f5}
.tcard::before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px}
.tcard.tk::before{background:#d94c60}
.tcard.tf::before{background:#c87a18}
.tcard.tw::before{background:#3974bd}
.tcard-top{display:flex;align-items:center;gap:9px;flex-wrap:wrap;font-size:.78rem}
.tcard-prio{font-weight:700}
.tcard-prio.pk{color:#d94c60}
.tcard-prio.pf{color:#c87a18}
.tcard-prio.pw{color:#3974bd}
.ai-recommend-tag{display:inline-flex;align-items:center;padding:3px 9px;border-radius:99px;background:#e9f8ef;color:#168653;font-weight:700;font-size:.72rem;border:1px solid #c8ead6}
.ai-manual-tag{display:inline-flex;align-items:center;padding:3px 9px;border-radius:99px;background:#eef5ff;color:#256fc4;font-weight:700;font-size:.72rem;border:1px solid #d5e5f8}
.tcard-sep{width:3px;height:3px;border-radius:50%;background:#c6cdd8;flex:none}
.tcard-src,.tcard-date{color:#98a1ae}
.tcard-src{margin-left:auto}
.tcard-date{margin-left:auto;font-variant-numeric:tabular-nums;white-space:nowrap}
.tcard-title{margin:8px 0 7px;font-size:.98rem;font-weight:600;line-height:1.55}
.tcard-title a{color:#173b75}
.tcard-title a:hover{color:var(--blue-500)}
.tcard-bottom{display:flex;flex-wrap:wrap;gap:6px 8px;align-items:center;font-size:.75rem;color:#8492a7}
/* 操作按钮组：永不换行；把来源文字放进组内，两者作为整体随右缘对齐，空间不足时由左侧胶囊区折行让位 */
.tcard-actions{margin-left:auto;display:inline-flex;gap:7px;flex:none;flex-wrap:nowrap;align-items:center}
.tcard-actions .btn-act{flex:none;white-space:nowrap}
.tcard-actions .tcard-src{margin-left:3px;white-space:nowrap}
.tcard-bottom .kp-tag,.tcard-bottom .kw-tag{font-size:.72rem;padding:2px 9px;border-radius:99px;white-space:nowrap}
.tcard-kw{display:inline-flex;gap:5px;flex-wrap:wrap;padding-left:11px;border-left:1px solid var(--line);min-width:0}
.tcard-buyer{color:#5b6572}
.tcard-org{color:#98a1ae}
.tcard-empty{text-align:center;color:#999;padding:2rem}
/* 商机分类规则页：级别阈值行 */
.level-row{display:flex;align-items:center;gap:10px;padding:10px 4px;border-bottom:1px dashed var(--line);font-size:.88rem}
.level-row:last-child{border-bottom:0}
.level-dot{width:9px;height:9px;border-radius:50%;flex:none;display:inline-block}
.level-rule{color:#8492a7;font-size:.8rem;flex:1}
.level-input input{width:76px;height:34px;border:1px solid #d8e3f1;border-radius:7px;padding:0 10px;font-weight:700}
.oppcat-head{display:flex;align-items:center;gap:8px}
.oppcat-name{font-size:.88rem;font-weight:700}
/* 入库规则：二级规则子页签 */
.sub-tabs{display:inline-flex;gap:6px;background:#f2f6fc;border:1px solid var(--line);border-radius:9px;padding:4px;margin-bottom:14px}
.sub-tab{border:0;background:transparent;padding:6px 16px;border-radius:7px;font-size:.82rem;font-weight:600;color:var(--muted);cursor:pointer;transition:all .15s}
.sub-tab:hover{color:var(--blue-600)}
.sub-tab.active{background:#fff;color:var(--blue-600);box-shadow:0 1px 4px rgba(16,42,84,.12)}
.sub-pane{display:none}
.sub-pane.active{display:block}
.pub-date{font-size:.78rem;color:#606266;white-space:nowrap;text-align:center}
/* 标题前置：商机分类彩色标签（直接产品/集成项目/前期阶段）*/
.cat-tag{display:inline-block;border-radius:3px;padding:0 7px;font-size:.74rem;font-weight:600;margin-right:7px;vertical-align:1px;white-space:nowrap}
.cat-direct{background:#e6f7ec;color:#1e8e4e}
.cat-integrated{background:#e8f1fd;color:#2563c9}
.cat-early{background:#fff3e0;color:#d97706}
.cat-other{background:#f2f4f8;color:#5b6572}
.tender-meta{font-size:.75rem;color:#999;margin-top:2px}
.buyer-name{font-weight:500}
.deadline{font-size:.82rem;color:#606266;white-space:nowrap}
.status-badge{
  display:inline-block;font-size:.7rem;
  padding:1px 8px;border-radius:10px;
  background:#f0f9eb;color:#67c23a;font-weight:500;
}

/* ---- pagination ---- */
.pagination{
  display:flex;align-items:center;justify-content:center;
  gap:4px;padding:14px 16px;
  border-top:1px solid #ebeef5;
  font-size:.85rem;
}
.pagination button{
  border:1px solid #dcdfe6;background:#fff;
  border-radius:3px;padding:4px 12px;
  cursor:pointer;color:#606266;transition:all .2s;
}
.pagination button:hover:not(:disabled){color:#409eff;border-color:#409eff}
.pagination button:disabled{opacity:.4;cursor:default}
.pagination .pg-current{
  font-weight:600;color:#409eff;
  border:1px solid #409eff;border-radius:3px;
  padding:4px 12px;background:#ecf5ff;
}
.pagination .pg-info{color:#909399;margin:0 8px}

/* ---- action button group ---- */
.action-btns{display:flex;gap:6px;flex-wrap:nowrap}
.action-btns .btn-act,.tcard-actions .btn-act{
  border:none;border-radius:3px;
  padding:4px 10px;font-size:.78rem;
  cursor:pointer;white-space:nowrap;
  transition:all .15s;
}
.btn-expire{background:#f56c6c;color:#fff}
.btn-expire:hover{background:#f78989}
.btn-priority{background:#409eff;color:#fff}
.btn-priority:hover{background:#66b1ff}
.btn-useless{background:#909399;color:#fff}
.btn-useless:hover{background:#a6a9ad}

/* priority dropdown */
.priority-select{
  position:absolute;z-index:200;
  background:#fff;border:1px solid #dcdfe6;
  border-radius:4px;box-shadow:0 2px 12px rgba(0,0,0,.12);
  padding:4px 0;min-width:100px;
}
.priority-select .prio-option{
  display:block;width:100%;text-align:left;
  border:none;background:none;padding:6px 14px;
  font-size:.82rem;cursor:pointer;color:#606266;
}
.priority-select .prio-option:hover{background:#ecf5ff;color:#409eff}
.priority-select .prio-option.active{color:#409eff;font-weight:600;background:#ecf5ff}

/* confirm dialog */
.confirm-overlay{
  position:fixed;top:0;left:0;right:0;bottom:0;
  background:rgba(0,0,0,.4);z-index:9998;
  display:flex;align-items:center;justify-content:center;
}
.confirm-box{
  background:#fff;border-radius:6px;padding:24px;
  width:360px;max-width:90vw;
  box-shadow:0 4px 20px rgba(0,0,0,.15);
  text-align:center;
}
.confirm-box .confirm-msg{font-size:.92rem;color:#333;margin-bottom:20px;line-height:1.6}
.confirm-box .confirm-btns{display:flex;gap:10px;justify-content:center}
.confirm-box .confirm-btns button{
  border:none;border-radius:4px;padding:7px 24px;
  font-size:.85rem;cursor:pointer;
}
.confirm-box .btn-confirm-yes{background:#f56c6c;color:#fff}
.confirm-box .btn-confirm-yes:hover{background:#f78989}
.confirm-box .btn-confirm-no{background:#fff;color:#606266;border:1px solid #dcdfe6!important}
.confirm-box .btn-confirm-no:hover{color:#409eff;border-color:#409eff!important}

/* ---- sources list ---- */
.src-group-title{font-size:.82rem;font-weight:600;margin:16px 0 8px}
.src-group-title.ok{color:#67c23a}
.src-group-title.pending{color:#e6a23c}
.src-item{
  display:flex;align-items:baseline;gap:8px;
  padding:6px 0;font-size:.85rem;
}
.src-item b{font-weight:600;color:#333}
.src-note{font-size:.75rem;color:#999}
.src-link{font-size:.75rem;color:#2774c9;text-decoration:none;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.src-link:hover{text-decoration:underline}
.src-tag{
  display:inline-block;font-size:.68rem;
  padding:1px 6px;border-radius:3px;font-weight:500;
}
.src-tag.ok{background:#f0f9eb;color:#67c23a}
.src-tag.warn{background:#fdf6ec;color:#e6a23c}
.src-tag.err{background:#fef0f0;color:#f56c6c}

/* ---- rules editor ---- */
.rule-section{margin-bottom:24px}
.rule-section-title{
  font-size:.92rem;font-weight:600;color:#333;
  margin-bottom:12px;padding-bottom:8px;
  border-bottom:1px solid #ebeef5;
  display:flex;align-items:center;gap:8px;
}
.rule-section-title .badge{
  font-size:.68rem;padding:1px 8px;border-radius:10px;
  font-weight:500;
}
.rule-section-title .badge-blue{background:#ecf5ff;color:#409eff}
.rule-section-title .badge-green{background:#f0f9eb;color:#67c23a}
.rule-section-title .badge-orange{background:#fdf6ec;color:#e6a23c}

/* category card */
.cat-card{
  border:1px solid #ebeef5;border-radius:4px;
  margin-bottom:12px;overflow:hidden;
}
.cat-card-header{
  display:flex;align-items:center;gap:10px;
  padding:10px 16px;background:#fafafa;
  border-bottom:1px solid #ebeef5;
}
.cat-card-header .cat-name{
  flex:1;font-size:.88rem;font-weight:600;color:#333;
}
.cat-card-header .cat-weight{
  font-size:.78rem;color:#999;
}
.cat-card-header .cat-weight input{
  width:50px;border:1px solid #dcdfe6;border-radius:3px;
  padding:2px 6px;font-size:.78rem;text-align:center;
  outline:none;
}
.cat-card-header .cat-weight input:focus{border-color:#409eff}
.cat-card-header .btn-remove-cat{
  background:none;border:none;color:#f56c6c;
  cursor:pointer;font-size:.85rem;padding:2px 6px;
}
.cat-card-header .btn-remove-cat:hover{color:#f78989}

.cat-card-body{padding:12px 16px}
.tag-flow{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.tag{
  display:inline-flex;align-items:center;gap:4px;
  padding:3px 10px;border-radius:3px;
  font-size:.8rem;
  background:#ecf5ff;color:#409eff;
  border:1px solid #d9ecff;
  transition:all .15s;
}
.tag .tag-remove{
  cursor:pointer;font-size:.7rem;
  color:#999;margin-left:2px;
}
.tag .tag-remove:hover{color:#f56c6c}
.tag-input{
  border:1px dashed #dcdfe6;border-radius:3px;
  padding:3px 10px;font-size:.8rem;
  outline:none;width:120px;
  background:#fff;
}
.tag-input:focus{border-color:#409eff;border-style:solid}
.btn-add-tag{
  background:none;border:1px dashed #409eff;
  border-radius:3px;padding:3px 10px;
  font-size:.78rem;color:#409eff;
  cursor:pointer;
}
.btn-add-tag:hover{background:#ecf5ff}

/* region/city tag style */
.tag-region{background:#f0f9eb;color:#67c23a;border-color:#e1f3d8}
.tag-client{background:#fdf6ec;color:#e6a23c;border-color:#faecd8}

/* add category row */
.btn-add-cat{
  display:inline-flex;align-items:center;gap:4px;
  background:none;border:1px dashed #dcdfe6;
  border-radius:4px;padding:8px 16px;
  font-size:.85rem;color:#999;cursor:pointer;
  transition:all .2s;margin-top:4px;
}
.btn-add-cat:hover{border-color:#409eff;color:#409eff}

/* save bar */
.save-bar{
  display:flex;align-items:center;gap:12px;
  padding:16px 0;
}
.save-msg{font-size:.82rem;color:#67c23a;display:none}

/* ---- modal dialog ---- */
.dialog-overlay{
  position:fixed;top:0;left:0;right:0;bottom:0;
  background:rgba(0,0,0,.45);z-index:9999;
  display:flex;align-items:center;justify-content:center;
  animation:fadeIn .15s ease;
}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes slideUp{from{transform:translateY(20px);opacity:0}to{transform:translateY(0);opacity:1}}
.dialog-box{
  background:#fff;border-radius:8px;
  width:420px;max-width:90vw;
  box-shadow:0 8px 32px rgba(0,0,0,.18);
  animation:slideUp .2s ease;
  overflow:hidden;
}
.dialog-header{
  display:flex;align-items:center;justify-content:space-between;
  padding:16px 20px 12px;border-bottom:1px solid #ebeef5;
}
.dialog-header .dialog-title{font-size:1rem;font-weight:600;color:#303133}
.dialog-header .dialog-close{
  background:none;border:none;font-size:1.2rem;color:#909399;
  cursor:pointer;padding:0 4px;line-height:1;
}
.dialog-header .dialog-close:hover{color:#303133}
.dialog-body{
  padding:20px;font-size:.92rem;color:#606266;
  line-height:1.7;white-space:pre-wrap;word-break:break-word;
  max-height:60vh;overflow-y:auto;
}
.dialog-footer{
  padding:12px 20px 16px;text-align:right;
}
.dialog-footer .btn-dialog-ok{
  background:#409eff;color:#fff;border:none;
  padding:7px 24px;border-radius:4px;cursor:pointer;
  font-size:.88rem;
}
.dialog-footer .btn-dialog-ok:hover{background:#66b1ff}
/* 公告详情：先看本系统抓取内容，底部才跳转原站 */
.tender-detail-box{width:min(1080px,calc(100vw - 34px));max-height:calc(100vh - 38px);display:flex;flex-direction:column}
.tender-detail-box{background:#f5f8fc;border-radius:16px}.tender-detail-meta{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:9px;padding:0 0 14px;color:#70809a;font-size:.82rem;border-bottom:0}.tender-detail-meta span{padding:10px 11px;background:#fff;border:1px solid #e2ebf5;border-radius:9px;box-shadow:0 2px 7px rgba(32,75,127,.05)}.tender-detail-meta b{display:block;margin-bottom:3px;color:#355273;font-weight:600}.tender-detail-content{margin-top:0;max-height:calc(100vh - 340px);overflow:auto;word-break:break-word;line-height:1.82;color:#34445b;background:transparent;border:0;border-radius:0;padding:0}.detail-section{padding:15px 16px;margin:0 0 11px;border:1px solid #e1ebf6;border-radius:10px;background:#fff;box-shadow:0 2px 7px rgba(32,75,127,.045)}.detail-section:last-child{margin-bottom:0}.detail-section-title{display:flex;align-items:center;gap:7px;margin:0 0 9px;color:#173b75;font-size:.94rem;font-weight:700}.detail-section-title:before{content:"";display:inline-block;width:4px;height:15px;border-radius:3px;background:#2f87ee}.detail-section-text{white-space:pre-wrap;color:#465b76}.detail-lead{background:#edf6ff;border:1px solid #d7e8fc;border-radius:10px;padding:14px 15px;margin-bottom:11px;color:#36577e;line-height:1.8}.tender-detail-box .dialog-body{background:#f5f8fc}.tender-detail-box .dialog-footer{background:#fff}
.tender-detail-empty{color:#8594aa;font-style:italic}
.tender-detail-footer{display:flex;justify-content:space-between;align-items:center;gap:10px}
.tender-detail-footer .capture-tip{color:#8796aa;font-size:.78rem}
.btn-source-link{display:inline-flex;align-items:center;justify-content:center;min-width:112px;padding:8px 16px;border-radius:7px;background:linear-gradient(135deg,#2f87ee,#1465ce);color:#fff!important;text-decoration:none;font-weight:600;font-size:.88rem;box-shadow:0 2px 5px rgba(24,72,142,.16)}
.btn-source-link:hover{background:linear-gradient(135deg,#3e96f7,#176fdc);transform:translateY(-1px)}
.tender-detail-heading{display:flex;flex-direction:column;gap:3px;min-width:0}.tender-detail-heading .dialog-title{font-size:1.05rem;line-height:1.45}.tender-detail-kicker{font-size:.72rem;font-weight:700;color:#2677d1;letter-spacing:.08em}.tender-detail-subtitle{color:#7a8ca4;font-size:.78rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tender-detail-grid{display:grid;grid-template-columns:minmax(0,1.55fr) minmax(290px,.8fr);gap:14px}.detail-focus-card,.detail-ai-card{background:#fff;border:1px solid #e1ebf6;border-radius:11px;padding:15px 16px;box-shadow:0 2px 7px rgba(32,75,127,.045);margin-bottom:13px}.detail-focus-card h3,.detail-ai-card h3,.detail-full-head{margin:0 0 9px;color:#173b75;font-size:.94rem}.detail-focus-card h3:before,.detail-ai-card h3:before,.detail-full-head:before{content:"";display:inline-block;width:4px;height:15px;margin-right:7px;border-radius:3px;background:#2f87ee;vertical-align:-2px}.detail-focus-text{line-height:1.8;color:#405b7d}.detail-chip-row{display:flex;flex-wrap:wrap;gap:6px}.detail-chip{display:inline-block;padding:4px 8px;border-radius:99px;background:#edf5ff;color:#2966a7;font-size:.76rem;font-weight:600}.detail-ai-card{background:linear-gradient(150deg,#fff,#f6faff)}.detail-ai-status{display:inline-block;margin-bottom:8px;padding:4px 9px;border-radius:99px;background:#e8f8ee;color:#138452;font-size:.78rem;font-weight:700}.detail-ai-status.manual{background:#fff4df;color:#b6750b}.detail-ai-status.none{background:#eff3f8;color:#71809a}.detail-full{margin-top:2px}.detail-full summary{list-style:none;cursor:pointer;padding:12px 15px;border:1px solid #dfe9f5;border-radius:9px;background:#fff;color:#173b75;font-weight:700}.detail-full summary::-webkit-details-marker{display:none}.detail-full summary:after{content:"展开";float:right;color:#3974bd;font-size:.78rem}.detail-full[open] summary{border-radius:9px 9px 0 0;border-bottom:0}.detail-full[open] summary:after{content:"收起"}.detail-full .tender-detail-content{padding-top:11px}.detail-empty-card{padding:16px;background:#fff;border:1px dashed #d8e4f1;border-radius:10px;color:#8594aa}
@media(max-width:760px){.tender-detail-grid{grid-template-columns:1fr}.tender-detail-meta{grid-template-columns:repeat(2,minmax(0,1fr))}.tender-detail-box{width:calc(100vw - 20px)}.tender-detail-footer .capture-tip{display:none}}

/* ---- visual refresh: blue enterprise console ---- */
:root{--blue-900:#071d49;--blue-800:#0a2c68;--blue-700:#0d4fa3;--blue-600:#1469d8;--blue-500:#2b7de9;--blue-100:#eaf3ff;--blue-50:#f5f9ff;--ink:#12213d;--muted:#71809a;--line:#e4ebf5;--shadow:0 10px 28px rgba(22,62,122,.08)}
body{background:linear-gradient(135deg,#f5f9ff 0%,#f8fbff 48%,#eef5ff 100%);color:var(--ink);letter-spacing:.01em}
a{color:var(--blue-600)}a:hover{color:var(--blue-800)}
.sidebar{width:224px;background:linear-gradient(180deg,var(--blue-900),#0a3577 100%);box-shadow:7px 0 24px rgba(7,29,73,.14)}
.sidebar-brand{height:68px;padding:0 20px;font-size:1.04rem;border-bottom-color:rgba(255,255,255,.12);letter-spacing:.04em}
.sidebar-brand span{color:#71b4ff;text-shadow:0 0 16px rgba(113,180,255,.7)}
.sidebar-nav{padding:14px 0}.nav-group-title{padding:10px 20px 8px;color:rgba(210,228,255,.48);font-size:.7rem}
.nav-item{margin:3px 12px;padding:11px 13px;border-left:0;border-radius:8px;color:rgba(229,240,255,.75);gap:10px}
.nav-item:hover{background:rgba(124,184,255,.12);color:#fff}.nav-item.active{background:linear-gradient(90deg,rgba(59,139,244,.48),rgba(93,167,255,.15));border-left:0;box-shadow:inset 3px 0 #8cc6ff;color:#fff}.ai-review-count{margin-left:auto;min-width:19px;padding:1px 6px;text-align:center;border-radius:99px;background:#e89a28;color:#fff;font-size:.72rem;font-weight:700}.ai-review-count.zero{display:none}
.main{margin-left:224px}.topbar{height:68px;padding:0 30px;background:rgba(255,255,255,.9);backdrop-filter:blur(12px);border-bottom-color:var(--line);box-shadow:0 2px 14px rgba(23,57,112,.04)}
.breadcrumb{font-size:.9rem}.breadcrumb a{color:#91a0b6}.breadcrumb .current{color:var(--ink);font-weight:700}.content{padding:24px 30px 34px}
.stats-row{gap:18px;margin-bottom:22px}.stat-card{position:relative;overflow:hidden;min-height:108px;border:1px solid rgba(225,234,247,.9);border-radius:12px;padding:21px 22px;box-shadow:var(--shadow);transition:transform .2s,box-shadow .2s}
.stat-card:before{content:"";position:absolute;left:0;top:20px;bottom:20px;width:4px;border-radius:0 4px 4px 0;background:var(--blue-500)}.stat-card:hover{transform:translateY(-2px);box-shadow:0 14px 30px rgba(22,62,122,.13)}
.stat-card .stat-num{font-size:2rem;color:var(--blue-800)}.stat-card .stat-label{color:var(--muted);font-size:.82rem}.stat-card.key .stat-num{color:#e45d6d}.stat-card.key:before{background:#e45d6d}.stat-card.follow .stat-num{color:#df8d24}.stat-card.follow:before{background:#eead45}
.card{border:1px solid rgba(225,234,247,.95);border-radius:12px;box-shadow:var(--shadow);overflow:hidden;margin-bottom:22px}.card-header{min-height:58px;padding:17px 22px;background:linear-gradient(90deg,#fff,#f7faff);border-bottom-color:var(--line);font-size:1rem;color:var(--ink)}.card-header>span:first-child{display:flex;align-items:center;gap:9px}.card-header>span:first-child:before{content:"";width:4px;height:17px;border-radius:99px;background:linear-gradient(#4d9bff,#1264cf)}.card-body{padding:22px}
.filter-bar{gap:11px}.filter-bar input,.filter-bar select,.cat-card-header .cat-weight input,.tag-input{height:38px;border-color:#d8e3f1;border-radius:7px;color:var(--ink);background:#fff;box-shadow:0 1px 2px rgba(12,41,82,.02)}.filter-bar input{padding:8px 13px;min-width:260px}.filter-bar input:focus,.filter-bar select:focus,.cat-card-header .cat-weight input:focus,.tag-input:focus{border-color:var(--blue-500);box-shadow:0 0 0 3px rgba(43,125,233,.13)}.filter-bar select{padding:0 32px 0 12px}
.btn-primary,.btn-default,.btn-success,.btn-danger,.action-btns .btn-act,.pagination button,.dialog-footer .btn-dialog-ok{border-radius:7px;font-weight:600;box-shadow:0 2px 5px rgba(24,72,142,.1);transition:transform .16s,box-shadow .16s,background .16s,border-color .16s}.btn-primary{background:linear-gradient(135deg,#2f87ee,#1465ce);padding:8px 19px}.btn-primary:hover{background:linear-gradient(135deg,#3e96f7,#176fdc);transform:translateY(-1px);box-shadow:0 6px 12px rgba(20,105,216,.22)}.btn-default{border-color:#d7e2f0;padding:8px 19px}.btn-default:hover{color:var(--blue-600);border-color:#9ac5fa;background:var(--blue-50);transform:translateY(-1px)}.btn-success{background:linear-gradient(135deg,#2385ec,#1261c9);padding:8px 19px}.btn-success:hover{background:linear-gradient(135deg,#3d98f4,#176ed6);transform:translateY(-1px);box-shadow:0 6px 12px rgba(20,105,216,.22)}.btn-danger{background:#e65d70}.btn-danger:hover{background:#ed7182}
#fetch-btn{background:linear-gradient(135deg,#1d85ee,#1263d0)!important;border-radius:7px!important;box-shadow:0 4px 10px rgba(20,105,216,.2)}#fetch-btn:hover{transform:translateY(-1px);box-shadow:0 7px 15px rgba(20,105,216,.28)}#fetch-btn:disabled{opacity:.7;cursor:wait;transform:none}
thead th{padding:13px 14px;background:linear-gradient(90deg,#092451,#0b3476);color:#eaf4ff;border-bottom:0;font-size:.82rem;letter-spacing:.03em;text-align:center}tbody td{padding:14px;border-bottom-color:#eaf0f7}tbody tr{transition:background .15s}tbody tr:hover{background:#f3f8ff}.tender-title a{color:#173b75;font-weight:600}.tender-title a:hover{color:var(--blue-500)}.tender-meta{color:#8492a7}.buyer-name{color:#263b5d}.rating{padding:4px 10px;border-radius:99px}.rating-key{background:#fff0f1;color:#d94c60}.rating-follow{background:#fff7e8;color:#c87a18}.rating-watch{background:#eef5ff;color:#3974bd}.status-badge{background:#eaf4ff;color:#2770c8}
.pagination{border-top-color:var(--line);padding:16px}.pagination button{border-color:#d8e4f2;padding:5px 13px}.pagination button:hover:not(:disabled){color:#fff;background:var(--blue-600);border-color:var(--blue-600);transform:translateY(-1px)}.pagination .pg-current{color:#fff;border-color:var(--blue-600);background:var(--blue-600);border-radius:7px;padding:5px 13px}.pagination .pg-info{color:var(--muted)}
.action-btns{gap:7px}.action-btns .btn-act,.tcard-actions .btn-act{padding:5px 10px}.btn-expire{background:#fff3f4;color:#d95769;border:1px solid #ffd5da!important}.btn-expire:hover{background:#e65d70;color:#fff}.btn-priority{background:#ebf4ff;color:#1969ca;border:1px solid #c6e0ff!important}.btn-priority:hover{background:#1d75d8;color:#fff}.btn-useless{background:#f3f6fa;color:#65748b;border:1px solid #dce4ed!important}.btn-useless:hover{background:#718099;color:#fff}
.priority-select{border-color:#dce7f4;border-radius:9px;box-shadow:0 10px 24px rgba(12,48,99,.15);padding:6px}.priority-select .prio-option{border-radius:6px;padding:8px 12px}.priority-select .prio-option:hover,.priority-select .prio-option.active{background:var(--blue-50);color:var(--blue-600)}
.cat-card{border-color:#dfe9f4;border-radius:9px}.cat-card-header{background:#f7faff;border-bottom-color:#e5edf6}.tag{border-radius:99px;background:#eef6ff;color:#1d6dca;border-color:#d1e6ff}.tag-region{background:#edf7ff;color:#2581bb;border-color:#d5edff}.tag-client{background:#f1f6ff;color:#4d65a9;border-color:#dce6ff}.btn-add-tag,.btn-add-cat{border-color:#9cc7f7;color:#2375d3;border-radius:7px}.btn-add-tag:hover,.btn-add-cat:hover{background:#f0f7ff;border-color:#4d9cf2;color:#1469d8}
.src-item{padding:11px 12px;border-bottom:1px solid #edf1f6;border-radius:7px}.src-item:hover{background:#f6faff}.src-tag{border-radius:99px;padding:3px 8px}.src-tag.ok{background:#eaf4ff;color:#2774c9}.src-tag.warn{background:#fff7e9;color:#bd7c25}.src-tag.err{background:#fff0f1;color:#d65b6b}
.dialog-overlay,.confirm-overlay{background:rgba(5,23,57,.48);backdrop-filter:blur(4px)}.dialog-box,.confirm-box{border:1px solid rgba(255,255,255,.75);border-radius:14px;box-shadow:0 20px 56px rgba(2,23,59,.28)}.dialog-header{padding:18px 22px 14px;background:linear-gradient(100deg,#f8fbff,#eef6ff);border-bottom-color:#e3edf8}.dialog-header .dialog-title{color:var(--blue-900)}.dialog-body{padding:23px 22px;color:#52647e}.dialog-footer{padding:13px 22px 19px}.dialog-footer .btn-dialog-ok{background:linear-gradient(135deg,#2f87ee,#1465ce);padding:8px 25px}.confirm-box{padding:26px}.confirm-box .confirm-msg{color:var(--ink);font-size:.95rem}.confirm-box .confirm-btns button{border-radius:7px;padding:8px 25px;font-weight:600}.confirm-box .btn-confirm-yes{background:#e65d70}.confirm-box .btn-confirm-no{border-color:#d7e2f0!important}.confirm-box .btn-confirm-no:hover{color:var(--blue-600);border-color:#9ec7f8!important}
/* global request feedback */
.loading-overlay{position:fixed;inset:0;z-index:9997;display:none;align-items:center;justify-content:center;background:rgba(246,250,255,.68);backdrop-filter:blur(2px)}.loading-overlay.visible{display:flex}.loading-panel{display:flex;align-items:center;gap:12px;padding:16px 20px;background:rgba(255,255,255,.96);border:1px solid #dceafa;border-radius:11px;box-shadow:0 12px 30px rgba(11,52,113,.18);font-size:.9rem;font-weight:600;color:var(--blue-800)}.loading-spinner{width:22px;height:22px;border:3px solid #cfe5ff;border-top-color:var(--blue-600);border-radius:50%;animation:spin .72s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}

/* ---- rules page: tabs & orderly layout ---- */
.rules-tabs{display:inline-flex;gap:6px;margin-bottom:20px;background:rgba(255,255,255,.82);border:1px solid var(--line);border-radius:11px;padding:6px;box-shadow:var(--shadow)}
.rules-tab{border:0;background:transparent;padding:9px 24px;border-radius:8px;font-size:.9rem;font-weight:600;color:var(--muted);cursor:pointer;display:inline-flex;align-items:center;gap:7px;transition:all .18s}
.rules-tab:hover{color:var(--blue-600)}
.rules-tab.active{background:linear-gradient(135deg,#2f87ee,#1465ce);color:#fff;box-shadow:0 4px 12px rgba(20,105,216,.25)}
.rules-pane{display:none}
.rules-pane.active{display:block}
.rules-sub-note{margin-left:auto;font-size:.78rem;color:var(--muted);font-weight:400}
.rules-sub-note.over{color:#d95769;font-weight:600}
.rules-tip{background:var(--blue-50);border:1px solid #d9e8fb;border-left:3px solid var(--blue-500);border-radius:8px;padding:12px 16px;font-size:.83rem;color:#41597c;line-height:1.7;margin-bottom:18px}
.rules-tip b{color:#1469d8}
.rules-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(440px,1fr));gap:16px}
.rules-grid .cat-card{margin-bottom:0}
.cat-card{box-shadow:0 2px 8px rgba(22,62,122,.04);transition:box-shadow .18s}
.cat-card:hover{box-shadow:0 6px 16px rgba(22,62,122,.09)}
.tag-tyc{padding:6px 12px;font-size:.83rem;font-weight:600}
.tag-tyc .tag-idx{display:inline-flex;align-items:center;justify-content:center;min-width:20px;height:20px;border-radius:6px;background:rgba(29,109,202,.12);color:#1d6dca;font-size:.68rem;font-weight:700;margin-right:6px;padding:0 4px}
#tyc-tags{gap:10px}
#tyc-tags .tag-input{width:150px}
.help-tabs{display:inline-flex;gap:6px;margin-bottom:18px;background:rgba(255,255,255,.82);border:1px solid var(--line);border-radius:11px;padding:6px;box-shadow:var(--shadow)}
.help-tab,.help-detail-tab{border:0;background:transparent;padding:9px 22px;border-radius:8px;font-size:.9rem;font-weight:600;color:var(--muted);cursor:pointer;transition:all .18s}
.help-tab.active,.help-detail-tab.active{background:linear-gradient(135deg,#2f87ee,#1465ce);color:#fff;box-shadow:0 4px 12px rgba(20,105,216,.25)}
.help-pane,.help-detail-pane{display:none}.help-pane.active,.help-detail-pane.active{display:block}
.help-detail-tabs{display:flex;gap:8px;margin:0 0 16px;padding-bottom:12px;border-bottom:1px solid var(--line)}
.help-card{margin-bottom:16px}.help-card h3{font-size:1rem;color:#233b5d;margin-bottom:8px}.help-card p,.help-card li{color:#52657d;line-height:1.8}.help-card ul{padding-left:20px;margin-top:7px}.help-card .tag-flow{margin-top:10px}
.help-step{display:flex;gap:12px;padding:13px 0;border-bottom:1px solid #edf1f6}.help-step:last-child{border-bottom:0}.help-step-no{width:25px;height:25px;flex:0 0 25px;display:grid;place-items:center;border-radius:50%;background:#e8f2ff;color:#176fd2;font-weight:700}.help-note{background:#fff8e9;border:1px solid #f2ddb0;border-radius:8px;padding:11px 14px;color:#7b5a18;margin-top:12px}
@media(max-width:960px){.rules-grid{grid-template-columns:1fr}}

/* ---- responsive ---- */
@media(max-width:768px){
  .sidebar{width:0;overflow:hidden}
  .main{margin-left:0}
  .stats-row{flex-direction:column}
  .stat-card{min-width:auto}
  table th:nth-child(4),table td:nth-child(4){display:none}
  .content{padding:12px}
}
</style>
</head>
<body>

<!-- Sidebar -->
<aside class="sidebar">
  <div class="sidebar-brand"><span>&#9670;</span> 遨海商机雷达</div>
  <nav class="sidebar-nav">
    <div class="nav-group-title">商机雷达</div>
    <div class="nav-item active" onclick="switchPage('dashboard',this)">
      <span class="nav-icon">&#9632;</span> 实时商机
    </div>
    <div class="nav-item" onclick="switchPage('history',this)">
      <span class="nav-icon">&#128196;</span> 历史商机
    </div>
    <div class="nav-item" onclick="switchPage('rules',this)">
      <span class="nav-icon">&#9881;</span> 数据规则
    </div>
    <div class="nav-item" onclick="switchPage('help',this)">
      <span class="nav-icon">&#10068;</span> 帮助
    </div>
    <div class="nav-item" onclick="switchPage('sources',this)">
      <span class="nav-icon">&#9733;</span> 来源状态
    </div>
    <div class="nav-item" onclick="switchPage('deleted',this)">
      <span class="nav-icon">&#128465;</span> 回收站
    </div>
    <div class="nav-item" onclick="switchPage('ai-review',this)">
      <span class="nav-icon">&#129302;</span> AI评审 <span id="ai-review-count" class="ai-review-count zero">0</span>
    </div>
  </nav>
</aside>

<!-- Main -->
<div class="main">
  <div class="topbar">
    <div class="breadcrumb">
      <a href="#">首页</a><span class="sep">/</span>
      <span class="current" id="page-title">实时商机</span>
    </div>
    <div class="font-switch" title="调整页面字号">
      <span class="font-switch-label">字号</span>
      <button type="button" class="font-btn" data-fs="std" onclick="setFontSize('std')">标准</button>
      <button type="button" class="font-btn" data-fs="lg" onclick="setFontSize('lg')">大</button>
      <button type="button" class="font-btn" data-fs="xl" onclick="setFontSize('xl')">特大</button>
    </div>
    <div id="account-box" style="margin-left:12px;padding-left:12px;border-left:1px solid #e2eaf4;font-size:13px;color:#58708f"><span id="account-name">已登录</span><button type="button" onclick="logout()" style="margin-left:8px;border:0;background:none;color:#2679d8;cursor:pointer">退出</button></div>
  </div>

  <div class="content">

    <!-- Page: Dashboard -->
    <div class="page active" id="page-dashboard">
      <div class="stats-row" id="stats"></div>

      <div class="card">
        <div class="card-header">
          <span>商机列表</span>
        </div>
        <div class="card-body">
          <div class="filter-bar">
            <input id="q" placeholder="搜索标题、采购单位、关键词…" onkeydown="if(event.key==='Enter')loadTenders()">
            <select id="min">
              <option value="" selected>全部</option>
              <option value="重点关注">重点关注</option>
              <option value="值得跟进">值得跟进</option>
              <option value="一般关注">一般关注</option>
            </select>
            <button class="btn-primary" onclick="loadTenders()">筛选</button>
            <button class="btn-success" id="fetch-btn" onclick="doFetch()" style="margin-left:auto;background:#67c23a;border:none;color:#fff;padding:8px 18px;border-radius:4px;cursor:pointer;font-size:14px">获取最新数据</button>
          </div>
          <div class="sub-tabs" style="margin-bottom:14px">
            <button class="sub-tab active" data-market-tab="direct" onclick="switchMarketTab('direct',this)">直接商机 <span id="direct-tab-count">0</span></button>
            <button class="sub-tab" data-market-tab="market" onclick="switchMarketTab('market',this)">市场情报 <span id="market-tab-count">0</span></button>
          </div>
          <div class="tcard-list" id="rows"></div>
          <div class="pagination" id="pagination"></div>
        </div>
      </div>
    </div>

    <!-- Page: History -->
    <div class="page" id="page-history">
      <div class="stats-row" id="history-stats"></div>
      <div class="card">
        <div class="card-header">
          <span>历史公告（全库）</span>
        </div>
        <div class="card-body">
          <div class="filter-bar">
            <input id="hq" placeholder="搜索标题、采购单位、关键词…" onkeydown="if(event.key==='Enter')loadHistoryTenders()">
            <select id="hmin">
              <option value="" selected>全部</option>
              <option value="重点关注">重点关注</option>
              <option value="值得跟进">值得跟进</option>
              <option value="一般关注">一般关注</option>
            </select>
            <button class="btn-primary" onclick="loadHistoryTenders()">筛选</button>
          </div>
          <div class="tcard-list" id="hrows"></div>
          <div class="pagination" id="hpagination"></div>
        </div>
      </div>
    </div>

    <!-- Page: Rules -->
    <div class="page" id="page-rules">
      <div class="rules-tabs">
        <button class="rules-tab active" onclick="switchRulesTab('search',this)">&#128269; 天眼查检索词配置</button>
        <button class="rules-tab" onclick="switchRulesTab('ingest',this)">&#9878; 入库规则</button>
        <button class="rules-tab" onclick="switchRulesTab('opportunity',this)">&#127919; 商机分类</button>
      </div>

      <div class="rules-pane active" id="rules-pane-search">
        <div class="card">
          <div class="card-header">
            <span>天眼查检索词</span>
            <span class="rules-sub-note" id="tyc-quota-note"></span>
          </div>
          <div class="card-body">
            <div class="rules-tip">
              检索词仅用于天眼查来源的全国招标公告搜索；其他来源抓取列表页全量公告，再由「入库规则」过滤打分。
              <b>每个关键词每次抓取消耗 1 次天眼查额度</b>，服务器每 4 小时抓取一次（一天 6 次），请注意账号日限额（100 次）。
            </div>
            <div class="tag-flow" id="tyc-tags"></div>
          </div>
        </div>
      </div>

      <div class="rules-pane" id="rules-pane-ingest">
        <div class="card">
          <div class="card-header"><span>一级规则：业务关键词分类（决定能否入库 + 主体得分）</span></div>
          <div class="card-body" id="rules-business"></div>
        </div>

        <div class="card">
          <div class="card-header"><span>二级规则：加分项（仅在命中一级规则后生效）</span></div>
          <div class="card-body">
            <div class="sub-tabs">
              <button class="sub-tab active" onclick="switchIngestSub('regions',this)">重点区域（+15/+8）</button>
              <button class="sub-tab" onclick="switchIngestSub('cities',this)">重点城市</button>
              <button class="sub-tab" onclick="switchIngestSub('clients',this)">采购方关键词（最高+20）</button>
            </div>
            <div class="sub-pane active" id="ingest-sub-regions">
              <div class="rules-tip">只看公告的「地区」字段：命中一级省/市 <b>+15 分</b>，命中二级 <b>+8 分</b>（两级不叠加，取高者）。</div>
              <div id="rules-regions"></div>
            </div>
            <div class="sub-pane" id="ingest-sub-cities">
              <div class="rules-tip">重点城市名单目前不单独加分，仅随地区规则命中时作为命中明细展示，可按需维护。</div>
              <div id="rules-cities"></div>
            </div>
            <div class="sub-pane" id="ingest-sub-clients">
              <div class="rules-tip">只看「采购单位」字段：命中 1 个词 +12 分，每多 1 个 +4 分，封顶 <b>+20 分</b>。</div>
              <div id="rules-clients"></div>
            </div>
          </div>
        </div>
      </div>

      <div class="rules-pane" id="rules-pane-opportunity">
        <div class="card">
          <div class="card-header"><span>商机级别：按评分自动初始化优先级</span></div>
          <div class="card-body">
            <div class="rules-tip">
              新公告入库时按「入库规则」打分，并根据下方阈值自动初始化优先级；<b>仅影响入库时的初始值，不会覆盖人工调整过的优先级</b>，阈值调整从下次抓取开始生效。
            </div>
            <div id="level-rows"></div>
          </div>
        </div>
        <div class="card">
          <div class="card-header"><span>商机种类：按标题关键词打标</span></div>
          <div class="card-body" id="rules-oppcats"></div>
        </div>
      </div>

      <div class="save-bar">
        <button class="btn-success" onclick="saveRules()">保存规则</button>
        <button class="btn-default" onclick="loadRulesEditor()">重置</button>
        <span class="save-msg" id="rules_msg">规则已保存，重新抓取后将生效</span>
      </div>
    </div>

    <!-- Page: Help -->
    <div class="page" id="page-help">
      <div class="help-tabs">
        <button class="help-tab active" onclick="switchHelpTab('rules',this)">&#128214; 当前规则说明</button>
      </div>
      <div class="help-pane active" id="help-pane-rules">
        <div class="help-detail-tabs">
          <button class="help-detail-tab active" onclick="switchHelpDetail('search',this)">&#128269; 检索规则</button>
          <button class="help-detail-tab" onclick="switchHelpDetail('ingest',this)">&#128230; 入库规则</button>
        </div>
        <div class="help-detail-pane active" id="help-detail-search"></div>
        <div class="help-detail-pane" id="help-detail-ingest"></div>
      </div>
    </div>

    <!-- Page: Sources -->
    <div class="page" id="page-sources">
      <div class="card">
        <div class="card-header"><span>来源接入状态</span></div>
        <div class="card-body" id="sources"></div>
      </div>
    </div>

    <!-- Page: Deleted (Recycle Bin) -->
    <div class="page" id="page-deleted">
      <div class="card">
        <div class="card-header">
          <span>回收站 — 已标记无用的公告</span>
          <span style="margin-left:auto;font-size:13px;color:#909399" id="deleted-hint"></span>
        </div>
        <div class="card-body">
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th style="width:80px">优先级</th>
                  <th>公告</th>
                  <th style="width:180px">采购单位 / 地区</th>
                  <th style="width:100px">操作</th>
                </tr>
              </thead>
              <tbody id="deleted-rows"></tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

    <!-- Page: AI Review — independent reviewer service, fed only from this cloned database. -->
    <div class="page" id="page-ai-review">
      <div class="card">
        <div class="card-header"><span>AI评审（测试副本）</span></div>
        <div class="card-body" style="padding:0">
          <iframe id="ai-review-frame" src="about:blank" title="AI评审记录与动态配置" style="width:100%;height:calc(100vh - 190px);min-height:720px;border:0;display:block"></iframe>
        </div>
      </div>
    </div>

  </div>
</div>

<!-- Shared loading feedback for every page request -->
<div class="loading-overlay" id="loading-overlay" aria-live="polite" aria-busy="true">
  <div class="loading-panel"><span class="loading-spinner"></span><span id="loading-text">正在加载数据…</span></div>
</div>

<!-- Modal Dialog -->
<div class="dialog-overlay" id="dialog-overlay" style="display:none" onclick="if(event.target===this)closeDialog()">
  <div class="dialog-box">
    <div class="dialog-header">
      <span class="dialog-title" id="dialog-title">提示</span>
      <button class="dialog-close" onclick="closeDialog()">&times;</button>
    </div>
    <div class="dialog-body" id="dialog-body"></div>
    <div class="dialog-footer">
      <button class="btn-dialog-ok" onclick="closeDialog()">确定</button>
    </div>
  </div>
</div>

<!-- Tender detail: stored capture is shown before opening the external source. -->
<div class="dialog-overlay" id="tender-detail-overlay" style="display:none" onclick="if(event.target===this)closeTenderDetail()">
  <div class="dialog-box tender-detail-box" role="dialog" aria-modal="true" aria-labelledby="tender-detail-title">
    <div class="dialog-header">
      <div class="tender-detail-heading"><span class="tender-detail-kicker" id="tender-detail-kicker">商机详情</span><span class="dialog-title" id="tender-detail-title">公告详情</span><span class="tender-detail-subtitle" id="tender-detail-subtitle"></span></div>
      <button class="dialog-close" onclick="closeTenderDetail()" aria-label="关闭">&times;</button>
    </div>
    <div class="dialog-body" id="tender-detail-body"></div>
    <div class="dialog-footer tender-detail-footer">
      <span class="capture-tip">以上为本系统抓取并保存的内容</span>
      <a class="btn-source-link" id="tender-source-link" href="#" target="_blank" rel="noopener">访问原网页 ↗</a>
    </div>
  </div>
</div>

<script>
let loadingRequests=0;
function setLoading(show,message){
  loadingRequests=Math.max(0,loadingRequests+(show?1:-1));
  let overlay=document.getElementById('loading-overlay');
  if(message)document.getElementById('loading-text').textContent=message;
  overlay.classList.toggle('visible',loadingRequests>0);
}
async function api(u,opts){
  opts=opts||{};
  let method=(opts.method||'GET').toUpperCase();
  if(!['GET','HEAD','OPTIONS'].includes(method)){
    let csrf=(document.cookie.match(/(?:^|; )aohai_csrf=([^;]+)/)||[])[1];
    opts.headers=Object.assign({},opts.headers,csrf?{'X-CSRF-Token':decodeURIComponent(csrf)}:{});
  }
  setLoading(true);
  try{
    let r;
    // Relative API URLs keep the app usable behind an Nginx subpath (for example /radar/).
    let endpoint=u.startsWith('/')?u.slice(1):u;
    try{r=await fetch(endpoint,opts);}catch(e){console.error('fetch error',endpoint,e);showDialog('网络错误',e.message);throw e;}
    let j;
    try{j=await r.json();}catch(e){console.error('json parse error',u,r.status);throw e;}
    if(!r.ok){console.error('api error',u,r.status,j);throw new Error(j.error||'请求失败('+r.status+')');}
    return j;
  }finally{setLoading(false);}
}
async function logout(){
  try{await api('/api/auth/logout',{method:'POST'});}finally{location.reload();}
}
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
/* 标题同行：数据来源悬浮提示圆圈（多来源逗号串逐个映射为中文名） */
function srcLabel(codes){
  return String(codes||'').split(',').map(s=>SRC_NAMES[s.trim()]||s.trim()).filter(Boolean).join(' / ');
}
/* 标题前置：商机分类彩色标签 */
function catTag(x){
  const M={'直接产品':'cat-direct','集成项目':'cat-integrated','前期线索':'cat-early','前期阶段':'cat-early'};
  const c=x.category;
  if(!c)return '';
  return `<span class="cat-tag ${M[c]||'cat-other'}" title="商机类型">${esc(c)}</span>`;
}
/* 来源偶尔把同一公告标题拼接两次；展示层去掉“公告-”前缀和完整重复段，原始数据不改。 */
function displayTitle(value){
  let s=String(value||'').replace(/^\s*公告\s*[-：:]\s*/,'').replace(/\s+/g,' ').trim();
  let cut=s.indexOf(' - ');
  if(cut>12&&s.slice(0,cut).trim()===s.slice(cut+3).trim())s=s.slice(0,cut).trim();
  if(s.length>24){
    let seed=s.slice(0,Math.min(32,s.length)),again=s.indexOf(seed,seed.length);
    if(again>24&&s.slice(again).replace(/^[-—\s]+/,'')===s.slice(0,again).replace(/[-—\s]+$/,''))s=s.slice(0,again).replace(/[-—\s]+$/,'');
  }
  return s||'未命名公告';
}
let SRC_NAMES={},currentPage=1,pageSize=15,currentMarketTab='direct'
let currentRules=null
/* 当前页面已加载的公告。点击标题时优先展示数据库保存的抓取正文，不直接离开系统。 */
let tenderDetailMap={};

function ratingCls(r){
  if(r==='重点关注')return'rating-key';
  if(r==='值得跟进')return'rating-follow';
  return'rating-watch';
}
/* 卡片式列表：优先级→卡片色条/文字色映射 */
function cardCls(r){return r==='重点关注'?'tk':(r==='值得跟进'?'tf':'tw')}
function prioCls(r){return r==='重点关注'?'pk':(r==='值得跟进'?'pf':'pw')}
/* 卡片底部：核心要点/关键词胶囊 + 代理/中标单位 + 数据来源（右对齐） */
function cardBottom(x,actions){
  let parts=(x.key_points||[]).map(p=>`<span class="kp-tag">${esc(p)}</span>`);
  let kws=(x.keywords||[]).map(k=>`<span class="kw-tag">${esc(k)}</span>`).join('');
  if(kws)parts.push(`<span class="tcard-kw">${kws}</span>`);
  if(x.agency)parts.push(`<span class="tcard-org">代理：${esc(x.agency)}</span>`);
  if(x.winner)parts.push(`<span class="tcard-org">中标/设计：${esc(x.winner)}</span>`);
  parts.push(`<span class="tcard-src">${esc(srcLabel(x.source_code))}</span>`);
  /* 来源文字移入操作按钮组内：无按钮的列表（历史商机）保持原位右对齐 */
  let srcHtml=actions?parts.pop():'';
  return `<div class="tcard-bottom">${parts.join('')}${actions?actions.replace('</span>',srcHtml+'</span>'):srcHtml}</div>`;
}
/* 卡片外壳：顶部元信息行（优先级·分类·采购单位·地区·日期）→ 标题 → 底部信息 */
function cardHtml(x,actions){
  tenderDetailMap[String(x.id)]=x;
  return `<div class="tcard ${cardCls(x.rating)}">
    <div class="tcard-top">
      <span class="tcard-prio ${prioCls(x.rating)}">${esc(x.rating)}</span>
      ${catTag(x)}
      <span class="tcard-sep"></span>
      <span class="tcard-buyer">${esc(x.buyer||'—')}${x.region?' · '+esc(x.region):''}</span>
      <span class="tcard-date">${esc(x.published_at)||'—'}</span>
    </div>
    <div class="tcard-title"><a title="${esc(displayTitle(x.title))}" href="${esc(x.source_url)}" target="_blank" rel="noopener">${esc(displayTitle(x.title))}</a></div>
    ${cardBottom(x,actions)}
  </div>`;
}

/* ---- navigation ---- */
function switchPage(name,el){
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  if(el)el.classList.add('active');
  document.getElementById('page-'+name).classList.add('active');
  let titles={dashboard:'实时商机',history:'历史商机',rules:'数据规则',help:'帮助',sources:'来源状态',deleted:'回收站','ai-review':'AI评审'};
  document.getElementById('page-title').textContent=titles[name]||name;
  if(name==='rules')loadRulesEditor();
  if(name==='help')loadHelp();
  if(name==='sources')loadSources();
  if(name==='history'){loadHistoryStats();loadHistoryTenders(1);}
  if(name==='deleted')loadDeleted();
  if(name==='ai-review')loadAiReviewCount();
  if(name==='ai-review')ensureAiReviewFrame();
}

/* ---- help: plain-language documentation generated from the active rules ---- */
function helpTags(items){return (items||[]).map(x=>`<span class="tag">${esc(x)}</span>`).join('')||'<span class="muted">未配置</span>'}
function switchHelpTab(name,el){
  document.querySelectorAll('.help-tab').forEach(t=>t.classList.remove('active'));
  if(el)el.classList.add('active');
  document.querySelectorAll('.help-pane').forEach(p=>p.classList.remove('active'));
  document.getElementById('help-pane-'+name).classList.add('active');
}
function switchHelpDetail(name,el){
  document.querySelectorAll('.help-detail-tab').forEach(t=>t.classList.remove('active'));
  if(el)el.classList.add('active');
  document.querySelectorAll('.help-detail-pane').forEach(p=>p.classList.remove('active'));
  document.getElementById('help-detail-'+name).classList.add('active');
}
async function loadHelp(){
  currentRules=await api('/api/rules');
  renderHelpRules();
}
function renderHelpRules(){
  if(!currentRules)return;
  const rules=currentRules, kws=rules.tyc_search_keywords||[], lv=rules.opportunity_levels||{};
  const search=document.querySelector('#help-detail-search'), ingest=document.querySelector('#help-detail-ingest');
  if(!search||!ingest)return;
  search.innerHTML=`
    <div class="card help-card"><div class="card-body">
      <h3>这套规则在做什么？</h3>
      <p>系统有两种获取方式：一类是从各公开网站的公告栏目逐条获取，再由入库规则筛选；另一类是按下方检索词在全国公告库中搜索。检索词改变后，下一次检索会自动使用新词。</p>
      <div class="help-note"><b>重要：</b>检索到不代表会出现在商机列表。所有结果仍要通过“入库规则”页的业务相关性、公告类型和去重检查。</div>
    </div></div>
    <div class="card help-card"><div class="card-body">
      <h3>当前全国检索词（${kws.length} 个）</h3>
      <p>这些词同时用于天眼查、政府采购网关键词检索和湖南公共资源交易平台的关键词搜索。</p>
      <div class="tag-flow">${helpTags(kws)}</div>
      <p style="margin-top:12px">天眼查会优先回查 AIS、VDES、AIS岸基、AIS基站等核心词；普通定时检索按配置词执行。AIS 与 VDES 必须是独立技术词且有海事语境，AISPORTS 等字符串不会被当作 AIS。</p>
    </div></div>
    <div class="card help-card"><div class="card-body">
      <h3>公开网站栏目获取</h3>
      <p>其他已接入来源以“公告栏目”为入口，不会只靠关键词搜索，以免漏掉标题没有关键词、但正文中有明确产品需求的项目。它们包括海事局、航道局、交通运输部门、港口平台、全国公共资源交易平台等；获取到的每一条仍会经过入库筛选。</p>
      <p>中国政府采购网关键词检索单独低频运行，以避免频控；天眼查也单独按额度运行，不混入常规全站抓取。</p>
    </div></div>`;
  const cats=(rules.business_categories||[]).map(cat=>`<div class="help-step"><span class="help-step-no">✓</span><div><b>${esc(cat.name)}</b>（最多 ${Number(cat.weight)||0} 分）<div class="tag-flow">${helpTags(cat.keywords)}</div></div></div>`).join('');
  ingest.innerHTML=`
    <div class="card help-card"><div class="card-body">
      <h3>一条公告怎样进入商机库</h3>
      <div class="help-step"><span class="help-step-no">1</span><div><b>先检查是不是可用公告。</b>没有标题、招聘/录用、废标/流标、终止/暂停/作废、合同或验收、环评公示会被排除。<b>中标、成交和候选人结果不会因“结果”身份被排除</b>：若业务相关，会保留为供应商、采购方和竞争格局情报。</div></div>
      <div class="help-step"><span class="help-step-no">2</span><div><b>检查业务相关性。</b>标题、采购单位、地区和公告正文会与下方业务词比对；至少命中一个业务分类才有分数、才可能入库。每个分类命中越多得分越高，单类最高不超过该分类的上限。</div></div>
      <div class="help-step"><span class="help-step-no">3</span><div><b>过滤政策新闻和网页噪声。</b>纯通知、政策解读、工作总结等不会仅因正文偶然出现业务词而进入；系统还会清理页面导航、页脚文字，并检查链接可访问性。</div></div>
      <div class="help-step"><span class="help-step-no">4</span><div><b>合并重复公告。</b>同一链接、同一标题或高度相似的跨站转载会合并为一条，保留更完整的正文和官方来源链接。</div></div>
    </div></div>
    <div class="card help-card"><div class="card-body"><h3>当前业务相关性词</h3><p>以下内容来自“数据规则 → 入库规则”。在该页保存修改后，这里的说明会同步更新。</p>${cats}</div></div>
    <div class="card help-card"><div class="card-body"><h3>加分与展示级别</h3>
      <ul><li>命中一级重点地区加 15 分；二级重点地区加 8 分。</li><li>采购单位命中重点客户词：首次加 12 分，每多命中一个加 4 分，最高加 20 分。</li><li>预算达到 50 万元加 8 分。</li><li>AIS/VDES 仅在确认海事语境后额外加 30 分；电力行业的 AIS（空气绝缘开关设备）不会按海事项目计算。</li><li>总分最高 100 分。${Number(lv.key_threshold)||80} 分及以上显示为“重点关注”；${Number(lv.follow_threshold)||50} 分及以上显示为“值得跟进”；低于该值为“一般关注”。这些级别用于排序，<b>并不是入库门槛</b>。</li></ul>
    </div></div>`;
}

async function loadAiReviewCount(){
  try{
    let s=await fetch(aiReviewBase()+'api/stats').then(r=>r.ok?r.json():Promise.reject());
    let el=document.getElementById('ai-review-count'),n=Number(s.manual_review||0);
    el.textContent=n;el.classList.toggle('zero',n===0);
  }catch(e){console.warn('AI评审待办数读取失败',e);}
}
function aiReviewBase(){return ['127.0.0.1','localhost'].includes(location.hostname)?'http://127.0.0.1:8791/':'/radar-ai-review/'}
function ensureAiReviewFrame(){let frame=document.getElementById('ai-review-frame'),src=aiReviewBase();if(frame&&frame.dataset.loaded!==src){frame.src=src;frame.dataset.loaded=src}}

/* ---- dashboard ---- */
async function loadStats(){
  let s=await api('/api/stats');
  document.querySelector('#direct-tab-count').textContent=`(${Number(s.direct||0)})`;
  document.querySelector('#market-tab-count').textContent=`(${Number(s.market||0)})`;
  document.querySelector('#stats').innerHTML=`
    <div class="stat-card"><div class="stat-num">${s.total}</div><div class="stat-label">有效公告</div></div>
    <div class="stat-card key"><div class="stat-num">${s.key}</div><div class="stat-label">重点关注</div></div>
    <div class="stat-card follow"><div class="stat-num">${s.follow}</div><div class="stat-label">值得跟进</div></div>
    <div class="stat-card"><div class="stat-num">${s.watch}</div><div class="stat-label">一般关注</div></div>
    <div class="stat-card"><div class="stat-num">${s.connected}</div><div class="stat-label">已接入来源</div></div>`;
}

async function loadTenders(pg){
  if(pg)currentPage=pg;
  let q=encodeURIComponent(document.querySelector('#q').value.trim());
  let priority=encodeURIComponent(document.querySelector('#min').value);
  let r=await api(`/api/tenders?q=${q}&priority=${priority}&bucket=${currentMarketTab}&page=${currentPage}&page_size=${pageSize}`);
  let total=r.total,rows=r.rows;
  let totalPages=Math.max(1,Math.ceil(total/pageSize));
  if(currentPage>totalPages)currentPage=totalPages;
  let list=document.querySelector('#rows');
  if(!rows.length){
    list.innerHTML='<div class="tcard-empty">暂无数据</div>';
    document.querySelector('#pagination').innerHTML='';
    return;
  }
  list.innerHTML=rows.map(x=>cardHtml(x,`<span class="tcard-actions">
      <button class="btn-act btn-expire" onclick="markExpired(${x.id})" title="标记过期，移至历史商机">标记过期</button>
      <button class="btn-act btn-priority" onclick="showPriorityMenu(event,${x.id},'${esc(x.rating)}')" title="设置跟进优先级">优先级</button>
      <button class="btn-act btn-useless" onclick="markUseless(${x.id})" title="标记无用，从所有列表移除">标记无用</button>
    </span>`)).join('');
  let ph=`<button onclick="goPage(${currentPage-1})" ${currentPage<=1?'disabled':''}>上一页</button>`;
  let start=Math.max(1,currentPage-2),end=Math.min(totalPages,start+4);
  start=Math.max(1,end-4);
  for(let i=start;i<=end;i++){
    if(i===currentPage)ph+=`<span class="pg-current">${i}</span>`;
    else ph+=`<button onclick="goPage(${i})">${i}</button>`;
  }
  ph+=`<button onclick="goPage(${currentPage+1})" ${currentPage>=totalPages?'disabled':''}>下一页</button>`;
  ph+=`<span class="pg-info">${total} 条</span>`;
  document.querySelector('#pagination').innerHTML=ph;
}
function goPage(p){loadTenders(p)}

/* ---- dialog ---- */
function showDialog(title,msg){
  document.getElementById('dialog-title').textContent=title||'提示';
  document.getElementById('dialog-body').textContent=msg||'';
  document.getElementById('dialog-overlay').style.display='flex';
}
function closeDialog(){
  document.getElementById('dialog-overlay').style.display='none';
}
function switchMarketTab(bucket,el){
  currentMarketTab=bucket;currentPage=1;
  document.querySelectorAll('[data-market-tab]').forEach(x=>x.classList.toggle('active',x===el));
  loadTenders(1);
}
function detailValue(label,value){
  if(value===undefined||value===null||String(value).trim()==='')return '';
  return `<span><b>${esc(label)}：</b>${esc(value)}</span>`;
}
function renderTenderContent(raw){
  const text=String(raw||'').replace(/\r/g,'').trim();
  if(!text)return '<span class="tender-detail-empty">本次抓取未获得正文内容；可通过下方“访问原网页”查看来源页面。</span>';
  const markers=/((?:[一二三四五六七八九十]+、|(?:^|\s)\d{1,2}[\.、](?=[^\d])|项目概况|采购需求|申请人资格要求|供应商资格要求|获取采购文件|响应文件提交|投标文件递交|开标时间|联系方式|公告期限))/g;
  const isMarker=/^(?:[一二三四五六七八九十]+、|\d{1,2}[\.、](?=[^\d])|项目概况|采购需求|申请人资格要求|供应商资格要求|获取采购文件|响应文件提交|投标文件递交|开标时间|联系方式|公告期限)/;
  const parts=text.split(markers).filter(Boolean);
  let blocks=[],lead=[];
  for(let i=0;i<parts.length;i++){
    const part=parts[i].trim();
    if(!part)continue;
    if(isMarker.test(part)){
      const body=(parts[++i]||'').trim();
      blocks.push(`<section class="detail-section"><div class="detail-section-title">${esc(part.replace(/^\s+/,''))}</div><div class="detail-section-text">${esc(body)}</div></section>`);
    }else lead.push(part);
  }
  if(!blocks.length)return `<div class="detail-section-text">${esc(text)}</div>`;
  return `${lead.length?`<div class="detail-lead">${esc(lead.join('\n'))}</div>`:''}${blocks.join('')}`;
}
function compactText(value,max=180){
  const s=String(value||'').replace(/\s+/g,' ').trim();
  return s.length>max?s.slice(0,max)+'…':s;
}
function titleParts(value){
  const title=displayTitle(value);
  const cues=['采购','服务','设备','系统','平台','招标公告','项目'];
  let at=-1;
  cues.forEach(c=>{let p=title.lastIndexOf(c);if(p>at)at=p;});
  if(at>18&&at<title.length-4){
    let start=Math.max(title.lastIndexOf('施工期',at),title.lastIndexOf('航标',at),title.lastIndexOf('AIS',at),title.lastIndexOf('VDES',at));
    if(start>12)return {title:title.slice(start),subtitle:title.slice(0,start).replace(/[—-\s]+$/,'')};
  }
  return {title,subtitle:''};
}
function findTextAfter(raw,labels){
  const s=String(raw||'');
  for(const label of labels){
    const m=s.match(new RegExp(label+'[：:]?\\s*([^。；;\\n]{12,260})'));
    if(m&&m[1])return compactText(m[1]);
  }
  return '';
}
function showTenderDetail(id){
  const x=tenderDetailMap[String(id)];
  if(!x){showDialog('提示','该公告详情尚未加载，请刷新列表后重试。');return;}
  const parts=titleParts(x.title);
  document.getElementById('tender-detail-title').textContent=parts.title;
  document.getElementById('tender-detail-subtitle').textContent=parts.subtitle;
  const meta=[
    detailValue('采购单位',x.buyer), detailValue('地区',x.region),
    detailValue('发布日期',x.published_at), detailValue('截止时间',x.deadline_at),
    detailValue('来源',srcLabel(x.source_code)), detailValue('关键词评分',x.score)
  ].filter(Boolean).join('');
  const focus=(x.key_points||[]).join('；')||findTextAfter(x.content,['采购需求','招标范围','设备物资名称'])||compactText(x.content,170);
  const keywords=(x.keywords||[]).slice(0,8);
  const aiLabel=x.ai_label||'尚未进入 AI 评审';
  const aiClass=x.ai_status==='approved'?'':(x.ai_status?'manual':'none');
  const aiText=x.ai_status==='approved'?'AI 已推荐，可直接跟进。':(x.ai_status==='approved_manual'?'人工已通过，可直接跟进。':(x.ai_status?'尚需在 AI 评审页查看结论与人工处理。':'该公告尚未进入 AI 评审。'));
  document.getElementById('tender-detail-body').innerHTML=`
    <div class="tender-detail-meta">${meta||'<span>暂无公告元信息</span>'}</div>
    <div class="tender-detail-grid">
      <div><section class="detail-focus-card"><h3>采购重点</h3><div class="detail-focus-text">${esc(focus||'暂无可提取的采购重点，请查看完整公告内容。')}</div>${keywords.length?`<div class="detail-chip-row" style="margin-top:11px">${keywords.map(k=>`<span class="detail-chip">${esc(k)}</span>`).join('')}</div>`:''}</section></div>
      <aside><section class="detail-ai-card"><h3>AI 研判</h3><span class="detail-ai-status ${aiClass}">${esc(aiLabel)}</span><div class="detail-focus-text">${esc(aiText)}</div></section></aside>
    </div>
    <details class="detail-full"><summary>完整公告内容</summary><div class="tender-detail-content">${renderTenderContent(x.content)}</div></details>`;
  const source=document.getElementById('tender-source-link');
  const url=String(x.source_url||'').trim();
  source.href=url||'#';
  source.style.display=url?'inline-flex':'none';
  document.getElementById('tender-detail-overlay').style.display='flex';
}
function closeTenderDetail(){
  document.getElementById('tender-detail-overlay').style.display='none';
}

/* ---- history page ---- */
let historyPage=1;
async function loadHistoryStats(){
  let s=await api('/api/history-stats');
  document.querySelector('#history-stats').innerHTML=`
    <div class="stat-card"><div class="stat-num">${s.total}</div><div class="stat-label">全库公告</div></div>
    <div class="stat-card key"><div class="stat-num">${s.key}</div><div class="stat-label">重点关注</div></div>
    <div class="stat-card follow"><div class="stat-num">${s.follow}</div><div class="stat-label">值得跟进</div></div>
    <div class="stat-card"><div class="stat-num">${s.sources}</div><div class="stat-label">数据来源</div></div>`;
}
async function loadHistoryTenders(pg){
  if(pg)historyPage=pg;
  let q=encodeURIComponent(document.querySelector('#hq').value.trim());
  let priority=encodeURIComponent(document.querySelector('#hmin').value);
  let r=await api(`/api/history?q=${q}&priority=${priority}&page=${historyPage}&page_size=${pageSize}`);
  let total=r.total,rows=r.rows;
  let totalPages=Math.max(1,Math.ceil(total/pageSize));
  if(historyPage>totalPages)historyPage=totalPages;
  let list=document.querySelector('#hrows');
  if(!rows.length){
    list.innerHTML='<div class="tcard-empty">暂无数据</div>';
    document.querySelector('#hpagination').innerHTML='';
    return;
  }
  list.innerHTML=rows.map(x=>cardHtml(x,'')).join('');
  let ph=`<button onclick="goHistoryPage(${historyPage-1})" ${historyPage<=1?'disabled':''}>上一页</button>`;
  let start=Math.max(1,historyPage-2),end=Math.min(totalPages,start+4);
  start=Math.max(1,end-4);
  for(let i=start;i<=end;i++){
    if(i===historyPage)ph+=`<span class="pg-current">${i}</span>`;
    else ph+=`<button onclick="goHistoryPage(${i})">${i}</button>`;
  }
  ph+=`<button onclick="goHistoryPage(${historyPage+1})" ${historyPage>=totalPages?'disabled':''}>下一页</button>`;
  ph+=`<span class="pg-info">${total} 条</span>`;
  document.querySelector('#hpagination').innerHTML=ph;
}
function goHistoryPage(p){loadHistoryTenders(p)}

/* ---- fetch latest data ---- */
async function doFetch(){
  let btn=document.querySelector('#fetch-btn');
  btn.disabled=true; btn.textContent='抓取中…';
  setLoading(true,'正在获取最新数据…');
  try{
    let r=await fetch('api/fetch',{method:'POST'});
    let j=await r.json();
    if(j.ok){
      showDialog('抓取完成',j.message);
      loadStats(); loadTenders();
    }else{
      showDialog('抓取失败',j.error||'未知错误');
    }
  }catch(e){
    showDialog('请求失败',e.message);
  }finally{
    setLoading(false);
    btn.disabled=false; btn.textContent='获取最新数据';
  }
}

/* ---- sources ---- */
async function loadSources(){
  let r=await api('/api/sources');
  SRC_NAMES=Object.fromEntries(r.map(x=>[x.code,x.name]));
  let conn=r.filter(x=>x.status==='connected');
  let pend=r.filter(x=>x.status!=='connected');
  let tagCls=s=>s==='connected'||s==='covered'?'ok':(s==='awaiting_authorization'||s==='not_automated'?'err':'warn');
  let label=s=>({'connected':'已接入','covered':'已由全国平台覆盖','unreachable':'无法访问','awaiting_authorization':'待授权','not_automated':'不自动化','pending_js':'JS 渲染','pending_timeout':'连接超时','pending_ssl':'SSL 不兼容','pending_structure':'无公告 API','pending_antibot':'反爬拦截','pending_search':'搜索不可用','manual_review':'待人工核验','planned':'计划中'}[s]||s);
  let addr=x=>x.base_url?`<a class="src-link" href="${esc(x.base_url)}" target="_blank" rel="noopener" title="${esc(x.base_url)}">${esc(x.base_url.replace(/^https?:\/\//,''))}</a>`:'';
  let h='';
  if(conn.length){
    h+=`<div class="src-group-title ok">已接入（${conn.length}）</div>`;
    h+=conn.map(x=>`<div class="src-item"><b>${esc(x.name)}</b><span class="src-tag ok">${label(x.status)}</span>${addr(x)}<span class="src-note">${x.last_success_at?`上次 ${esc(x.last_success_at.slice(5,16))} &middot;`:''}${esc(x.notes)}</span></div>`).join('');
  }
  if(pend.length){
    h+=`<div class="src-group-title pending">待接入（${pend.length}）</div>`;
    h+=pend.map(x=>`<div class="src-item"><b>${esc(x.name)}</b><span class="src-tag ${tagCls(x.status)}">${label(x.status)}</span>${addr(x)}<span class="src-note">${esc(x.notes)}</span></div>`).join('');
  }
  document.querySelector('#sources').innerHTML=h;
}

/* ---- rules editor ---- */
async function loadRulesEditor(){
  currentRules=await api('/api/rules');
  renderTycKeywords();
  renderBusinessRules();
  renderRegionRules();
  renderCityRules();
  renderClientRules();
  renderLevelRules();
  renderOppCats();
}

function switchRulesTab(name,el){
  document.querySelectorAll('.rules-tab').forEach(t=>t.classList.remove('active'));
  if(el)el.classList.add('active');
  document.querySelectorAll('.rules-pane').forEach(p=>p.classList.remove('active'));
  document.getElementById('rules-pane-'+name).classList.add('active');
}
function switchIngestSub(name,el){
  document.querySelectorAll('.sub-tab').forEach(t=>t.classList.remove('active'));
  if(el)el.classList.add('active');
  document.querySelectorAll('.sub-pane').forEach(p=>p.classList.remove('active'));
  document.getElementById('ingest-sub-'+name).classList.add('active');
}

/* tyc search keywords */
function renderTycKeywords(){
  let kws=currentRules.tyc_search_keywords||[];
  let h=kws.map((kw,i)=>`<span class="tag tag-tyc"><span class="tag-idx">${i+1}</span>${esc(kw)}<span class="tag-remove" onclick="removeTycKeyword(${i})">&times;</span></span>`).join('');
  h+=`<input class="tag-input" id="tyc-new-kw" placeholder="添加检索词…" onkeydown="if(event.key==='Enter'){addTycKeyword(this.value);this.value=''}">`;
  h+=`<button class="btn-add-tag" onclick="let inp=document.querySelector('#tyc-new-kw');addTycKeyword(inp.value);inp.value=''">+ 添加</button>`;
  document.querySelector('#tyc-tags').innerHTML=h;
  let n=kws.length,daily=n*6;
  let note=document.querySelector('#tyc-quota-note');
  if(note){
    note.textContent=`共 ${n} 个词 · 预计消耗 ≈ ${daily} 次/天（每 4 小时抓取 1 次，日限额 100）`;
    note.classList.toggle('over',daily>100);
  }
}
function addTycKeyword(v){
  v=v.trim();if(!v)return;
  currentRules.tyc_search_keywords=currentRules.tyc_search_keywords||[];
  if(currentRules.tyc_search_keywords.includes(v))return;
  currentRules.tyc_search_keywords.push(v);
  renderTycKeywords();
}
function removeTycKeyword(idx){
  currentRules.tyc_search_keywords.splice(idx,1);
  renderTycKeywords();
}

function renderBusinessRules(){
  let cats=currentRules.business_categories||[];
  let cards='';
  cats.forEach((cat,i)=>{
    cards+=`<div class="cat-card" data-idx="${i}">
      <div class="cat-card-header">
        <input class="cat-name-input" value="${esc(cat.name)}" style="flex:1;border:1px solid #dcdfe6;border-radius:3px;padding:4px 8px;font-size:.88rem;font-weight:600;outline:none" onchange="currentRules.business_categories[${i}].name=this.value">
        <span class="cat-weight">权重 <input type="number" value="${cat.weight||10}" min="1" max="100" onchange="currentRules.business_categories[${i}].weight=parseInt(this.value)||10"></span>
        <button class="btn-remove-cat" onclick="removeCat(${i})" title="删除分类">&#10005;</button>
      </div>
      <div class="cat-card-body">
        <div class="tag-flow" id="cat-tags-${i}">
          ${(cat.keywords||[]).map((kw,ki)=>`<span class="tag">${esc(kw)}<span class="tag-remove" onclick="removeKeyword(${i},${ki})">&times;</span></span>`).join('')}
          <input class="tag-input" placeholder="添加关键词…" onkeydown="if(event.key==='Enter'){addKeyword(${i},this.value);this.value=''}">
          <button class="btn-add-tag" onclick="let inp=document.querySelector('#cat-tags-${i} .tag-input');addKeyword(${i},inp.value);inp.value=''">+ 添加</button>
        </div>
      </div>
    </div>`;
  });
  let h=`<div class="rules-tip">打分方法：一条公告的标题/采购单位/地区/正文中，某个分类的关键词命中得越多，这个分类给的分越高——<b>命中 1 个词 +15 分、2 个词 +22 分、3 个词 +29 分，依此类推</b>，但最多不超过该分类的权重（权重就是“这个分类最高能得多少分”）。<br>所有命中分类的得分相加，再叠加下方的地区/采购方/预算加分，总分封顶 100。<b>一个关键词都没命中 → 0 分，不入库。</b>各分类权重相加不需要等于 100。</div><div class="rules-grid">${cards}</div>`;
  h+=`<button class="btn-add-cat" onclick="addCategory()">+ 新增业务分类</button>`;
  document.querySelector('#rules-business').innerHTML=h;
}

function renderRegionRules(){
  let pr=currentRules.priority_regions||{};
  let h='<div style="margin-bottom:16px"><div style="font-size:.85rem;font-weight:600;color:#67c23a;margin-bottom:8px">一级重点区域</div><div class="tag-flow" id="region-l1">';
  (pr['一级']||[]).forEach((r,i)=>{h+=`<span class="tag tag-region">${esc(r)}<span class="tag-remove" onclick="removeRegion('一级',${i})">&times;</span></span>`});
  h+=`<input class="tag-input" placeholder="添加省份…" onkeydown="if(event.key==='Enter'){addRegion('一级',this.value);this.value=''}">`;
  h+=`<button class="btn-add-tag" onclick="let inp=document.querySelector('#region-l1 .tag-input');addRegion('一级',inp.value);inp.value=''">+ 添加</button>`;
  h+='</div></div>';
  h+='<div><div style="font-size:.85rem;font-weight:600;color:#e6a23c;margin-bottom:8px">二级重点区域</div><div class="tag-flow" id="region-l2">';
  (pr['二级']||[]).forEach((r,i)=>{h+=`<span class="tag tag-region">${esc(r)}<span class="tag-remove" onclick="removeRegion('二级',${i})">&times;</span></span>`});
  h+=`<input class="tag-input" placeholder="添加省份…" onkeydown="if(event.key==='Enter'){addRegion('二级',this.value);this.value=''}">`;
  h+=`<button class="btn-add-tag" onclick="let inp=document.querySelector('#region-l2 .tag-input');addRegion('二级',inp.value);inp.value=''">+ 添加</button>`;
  h+='</div></div>';
  document.querySelector('#rules-regions').innerHTML=h;
}

function renderCityRules(){
  let cities=currentRules.priority_cities||[];
  let h='<div class="tag-flow" id="city-tags">';
  cities.forEach((c,i)=>{h+=`<span class="tag tag-region">${esc(c)}<span class="tag-remove" onclick="removeCity(${i})">&times;</span></span>`});
  h+=`<input class="tag-input" placeholder="添加城市…" onkeydown="if(event.key==='Enter'){addCity(this.value);this.value=''}">`;
  h+=`<button class="btn-add-tag" onclick="let inp=document.querySelector('#city-tags .tag-input');addCity(inp.value);inp.value=''">+ 添加</button>`;
  h+='</div>';
  document.querySelector('#rules-cities').innerHTML=h;
}

function renderClientRules(){
  let terms=currentRules.client_terms||[];
  let h='<div class="tag-flow" id="client-tags">';
  terms.forEach((t,i)=>{h+=`<span class="tag tag-client">${esc(t)}<span class="tag-remove" onclick="removeClient(${i})">&times;</span></span>`});
  h+=`<input class="tag-input" placeholder="添加采购方关键词…" onkeydown="if(event.key==='Enter'){addClient(this.value);this.value=''}">`;
  h+=`<button class="btn-add-tag" onclick="let inp=document.querySelector('#client-tags .tag-input');addClient(inp.value);inp.value=''">+ 添加</button>`;
  h+='</div>';
  document.querySelector('#rules-clients').innerHTML=h;
}

/* keyword CRUD */
function addKeyword(catIdx,kw){
  kw=kw.trim();if(!kw)return;
  currentRules.business_categories[catIdx].keywords=currentRules.business_categories[catIdx].keywords||[];
  if(currentRules.business_categories[catIdx].keywords.includes(kw))return;
  currentRules.business_categories[catIdx].keywords.push(kw);
  renderBusinessRules();
}
function removeKeyword(catIdx,kwIdx){
  currentRules.business_categories[catIdx].keywords.splice(kwIdx,1);
  renderBusinessRules();
}
function addCategory(){
  currentRules.business_categories.push({name:'新分类',weight:10,keywords:[]});
  renderBusinessRules();
}
function removeCat(idx){
  if(!confirm('确定删除分类 "'+currentRules.business_categories[idx].name+'" ？'))return;
  currentRules.business_categories.splice(idx,1);
  renderBusinessRules();
}

/* region CRUD */
function addRegion(level,val){
  val=val.trim();if(!val)return;
  currentRules.priority_regions=currentRules.priority_regions||{};
  currentRules.priority_regions[level]=currentRules.priority_regions[level]||[];
  if(currentRules.priority_regions[level].includes(val))return;
  currentRules.priority_regions[level].push(val);
  renderRegionRules();
}
function removeRegion(level,idx){
  currentRules.priority_regions[level].splice(idx,1);
  renderRegionRules();
}

/* city CRUD */
function addCity(val){
  val=val.trim();if(!val)return;
  currentRules.priority_cities=currentRules.priority_cities||[];
  if(currentRules.priority_cities.includes(val))return;
  currentRules.priority_cities.push(val);
  renderCityRules();
}
function removeCity(idx){
  currentRules.priority_cities.splice(idx,1);
  renderCityRules();
}

/* client CRUD */
function addClient(val){
  val=val.trim();if(!val)return;
  currentRules.client_terms=currentRules.client_terms||[];
  if(currentRules.client_terms.includes(val))return;
  currentRules.client_terms.push(val);
  renderClientRules();
}
function removeClient(idx){
  currentRules.client_terms.splice(idx,1);
  renderClientRules();
}

/* 商机分类：级别阈值 + 种类关键词 */
function renderLevelRules(){
  let lv=currentRules.opportunity_levels||{};
  if(typeof lv.key_threshold!=='number')lv.key_threshold=80;
  if(typeof lv.follow_threshold!=='number')lv.follow_threshold=50;
  currentRules.opportunity_levels=lv;
  const rows=[
    ['重点关注','key_threshold','#d94c60','评分 ≥ 该阈值 → 重点关注'],
    ['值得跟进','follow_threshold','#c87a18','评分 ≥ 该阈值且低于重点关注阈值 → 值得跟进'],
    ['一般关注','','#3974bd','评分低于“值得跟进”阈值 → 一般关注']
  ];
  document.querySelector('#level-rows').innerHTML=rows.map(([label,key,color,desc])=>{
    let input=key?`<input type="number" min="1" max="100" value="${lv[key]}" style="color:${color}" onchange="currentRules.opportunity_levels.${key}=parseInt(this.value)||0"> 分`:'';
    return `<div class="level-row"><span class="level-dot" style="background:${color}"></span><b style="color:${color};min-width:64px">${label}</b><span class="level-rule">${desc}</span><span class="level-input">${input}</span></div>`;
  }).join('');
}
function renderOppCats(){
  let cats=currentRules.opportunity_categories||[];
  const COLOR={'橙':'#d97706','绿':'#1e8e4e','蓝':'#2563c9'};
  let cards=cats.map((cat,i)=>`<div class="cat-card">
    <div class="cat-card-header oppcat-head">
      <span class="level-dot" style="background:${COLOR[cat.color]||'#8a94a6'}"></span>
      <span class="oppcat-name" style="color:${COLOR[cat.color]||'#5b6572'}">${esc(cat.name)}</span>
      <span style="font-size:.75rem;color:#98a1ae">标签色：${esc(cat.color||'—')}</span>
    </div>
    <div class="cat-card-body">
      <div class="tag-flow" id="oppcat-tags-${i}">
        ${(cat.keywords||[]).map((kw,ki)=>`<span class="tag">${esc(kw)}<span class="tag-remove" onclick="removeOppKw(${i},${ki})">&times;</span></span>`).join('')}
        <input class="tag-input" placeholder="添加关键词…" onkeydown="if(event.key==='Enter'){addOppKw(${i},this.value);this.value=''}">
        <button class="btn-add-tag" onclick="let inp=document.querySelector('#oppcat-tags-${i} .tag-input');addOppKw(${i},inp.value);inp.value=''">+ 添加</button>
      </div>
    </div>
  </div>`).join('');
  document.querySelector('#rules-oppcats').innerHTML=`<div class="rules-tip">按列表自上而下匹配标题，<b>先命中者生效</b>；都不命中则不打标（不显示分类）。新打标在下次抓取入库时生效。</div><div class="rules-grid">${cards}</div>`;
}
function addOppKw(catIdx,kw){
  kw=kw.trim();if(!kw)return;
  let kws=currentRules.opportunity_categories[catIdx].keywords=currentRules.opportunity_categories[catIdx].keywords||[];
  if(kws.includes(kw))return;
  kws.push(kw);
  renderOppCats();
}
function removeOppKw(catIdx,kwIdx){
  currentRules.opportunity_categories[catIdx].keywords.splice(kwIdx,1);
  renderOppCats();
}

async function saveRules(){
  setLoading(true,'正在保存规则…');
  try{
    let resp=await fetch('api/rules',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(currentRules)});
    let j=await resp.json();
    if(j.ok){
      // 帮助页直接复用 currentRules；保存后无需等待刷新即可看到同步后的说明。
      renderHelpRules();
      let msg=document.querySelector('#rules_msg');msg.style.display='inline';
      setTimeout(()=>msg.style.display='none',3000);
    }else{
      let errors=j.errors||['保存失败'];
      showDialog('规则验证失败',errors.join('\n'));
    }
  }catch(e){showDialog('保存失败',e.message||'请求失败');}
  finally{setLoading(false);}
}

/* ---- action buttons ---- */
async function markExpired(id){
  confirmAction('确定标记为过期？\n该公告将从实时商机移至历史商机。', async function(){
    try{
      await api('/api/tenders/mark-expired',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
      await loadStats();await loadTenders();
    }catch(e){console.error(e);showDialog('操作失败',e.message);}
  });
}

async function markUseless(id){
  confirmAction('确定标记为无用信息？\n该公告将从实时和历史列表中移除，后续抓取也不会再出现。', async function(){
    try{
      await api('/api/tenders/mark-useless',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
      await loadStats();await loadTenders();
    }catch(e){console.error(e);showDialog('操作失败',e.message);}
  });
}

function showPriorityMenu(event, id, current){
  // 关闭已有菜单
  closePriorityMenu();
  let btn=event.currentTarget;
  let rect=btn.getBoundingClientRect();
  let menu=document.createElement('div');
  menu.className='priority-select';
  menu.id='prio-menu';
  // 固定定位使用按钮的视口坐标，避免页面滚动后下拉菜单跑到其它卡片。
  menu.style.position='fixed';
  menu.style.top=(rect.bottom+4)+'px';
  menu.style.left=rect.left+'px';
  let levels=['重点关注','值得跟进','一般关注'];
  levels.forEach(lv=>{
    let opt=document.createElement('button');
    opt.className='prio-option'+(lv===current?' active':'');
    opt.textContent=lv;
    opt.onclick=function(){setPriority(id,lv);};
    menu.appendChild(opt);
  });
  document.body.appendChild(menu);
  // 点击外部关闭
  setTimeout(()=>{document.addEventListener('click',closePriorityMenuOutside);},0);
  window.addEventListener('scroll',closePriorityMenu,true);
  window.addEventListener('resize',closePriorityMenu);
}
function closePriorityMenu(){
  let m=document.getElementById('prio-menu');
  if(m)m.remove();
  document.removeEventListener('click',closePriorityMenuOutside);
  window.removeEventListener('scroll',closePriorityMenu,true);
  window.removeEventListener('resize',closePriorityMenu);
}
function closePriorityMenuOutside(e){
  let m=document.getElementById('prio-menu');
  if(m&&!m.contains(e.target))closePriorityMenu();
}

async function setPriority(id, priority){
  closePriorityMenu();
  try{
    await api('/api/tenders/set-priority',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,priority})});
    await loadStats();await loadTenders();
  }catch(e){console.error(e);showDialog('操作失败',e.message);}
}

/* confirm dialog */
function confirmAction(msg, onConfirm){
  let overlay=document.createElement('div');
  overlay.className='confirm-overlay';
  overlay.innerHTML=`<div class="confirm-box">
    <div class="confirm-msg">${msg.replace(/\n/g,'<br>')}</div>
    <div class="confirm-btns">
      <button class="btn-confirm-no" id="cfm-no">取消</button>
      <button class="btn-confirm-yes" id="cfm-yes">确定</button>
    </div>
  </div>`;
  document.body.appendChild(overlay);
  overlay.querySelector('#cfm-no').onclick=function(){overlay.remove();};
  overlay.querySelector('#cfm-yes').onclick=async function(){overlay.remove();try{await onConfirm();}catch(e){console.error(e);}};
  overlay.onclick=function(e){if(e.target===overlay)overlay.remove();};
}

/* ---- recycle bin ---- */
async function loadDeleted(){
  try{
    let r=await api('/api/deleted');
    let rows=r.rows||[];
    document.getElementById('deleted-hint').textContent=rows.length?`共 ${rows.length} 条已标记无用的记录`:'回收站为空';
    let tbody=document.getElementById('deleted-rows');
    if(!rows.length){
      tbody.innerHTML='<tr><td colspan="4" style="text-align:center;color:#999;padding:2rem">暂无已删除记录</td></tr>';
      return;
    }
    tbody.innerHTML=rows.map(x=>{
      let rc=ratingCls(x.rating);
      return `<tr>
        <td><span class="rating ${rc}">${esc(x.rating)}</span></td>
        <td>
          <div class="tender-title"><a href="${esc(x.source_url)}" target="_blank" rel="noopener">${esc(x.title)}</a></div>
          <div class="tender-meta">${esc(srcLabel(x.source_code))} &middot; ${esc(x.published_at)}</div>
        </td>
        <td>
          <div class="buyer-name">${esc(x.buyer)||'—'}</div>
          <div class="tender-meta">${esc(x.region)}</div>
        </td>
        <td>
          <button class="btn-act" style="background:#67c23a;color:#fff" onclick="restoreTender(${x.id})">恢复</button>
        </td>
      </tr>`;
    }).join('');
  }catch(e){console.error(e);showDialog('加载失败',e.message);}
}

async function restoreTender(id){
  try{
    await api('/api/tenders/restore',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})});
    await loadDeleted();
  }catch(e){console.error(e);showDialog('恢复失败',e.message);}
}

/* ---- init ---- */
/* 字号切换：改根字号让全站 rem 等比缩放，选择记住在浏览器本地 */
var FONT_SIZES={std:'14px',lg:'16.5px',xl:'19px'};
function setFontSize(fs){
  if(!FONT_SIZES[fs])fs='std';
  document.documentElement.style.fontSize=FONT_SIZES[fs];
  try{localStorage.setItem('radar_font_size',fs);}catch(e){}
  document.querySelectorAll('.font-btn').forEach(b=>b.classList.toggle('active',b.dataset.fs===fs));
}
(function(){var saved='std';try{saved=localStorage.getItem('radar_font_size')||'std';}catch(e){}setFontSize(saved);})();
api('/api/auth/me').then(x=>{let box=document.getElementById('account-box');if(!x.authenticated){box.style.display='none';return;}document.getElementById('account-name').textContent=x.username+(x.role==='admin'?'（管理员）':'');}).catch(()=>{});
loadStats();
loadAiReviewCount();
setInterval(loadAiReviewCount,60000);
loadSources().then(()=>loadTenders(1));
</script>
</body>
</html>'''

def _strip_surrogates(obj):
    """递归清理数据结构中的 Unicode 代理字符（U+D800-U+DFFF），替换为空串。"""
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace").replace("\ufffd", "")
    if isinstance(obj, dict):
        return {k: _strip_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_strip_surrogates(v) for v in obj]
    return obj

def make_handler(db_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args): print("[web] " + fmt % args)
        def send_json(self, data, code=200, cookies=()):
            data = _strip_surrogates(data)
            body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code); self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            for cookie in cookies: self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def send_html(self, body):
            raw = body.encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        def read_body(self):
            length = int(self.headers.get("Content-Length", 0) or 0)
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        def require_user(self, write=False):
            if not auth_enabled():
                return {"id": 0, "username": "", "role": "admin"}
            user = current_user(self)
            if not user:
                self.send_json({"error": "请先登录"}, 401); return None
            if write and not csrf_valid(self, user):
                self.send_json({"error": "请求校验失败，请刷新页面后重试"}, 403); return None
            return user
        def do_GET(self):
            url = urlparse(self.path); params = parse_qs(url.query)
            if url.path == "/api/auth/me":
                if not auth_enabled(): self.send_json({"authenticated": False}); return
                user = current_user(self)
                if not user: self.send_json({"authenticated": False}, 401)
                else: self.send_json({"authenticated": True, "username": user["username"], "role": user["role"]})
                return
            if auth_enabled():
                user = current_user(self)
                if not user:
                    if url.path == "/": self.send_html(LOGIN_HTML)
                    else: self.send_json({"error": "请先登录"}, 401)
                    return
            conn = connect(db_path)
            cfg = load_config()
            today = cn_today().strftime("%Y-%m-%d")
            try:
                if url.path == "/":
                    self.send_html(INDEX_HTML); return
                # 过期过滤条件片段
                exp_where = ""
                exp_params = []
                if cfg.get("filter_expired", True):
                    auto_days = cfg.get("auto_expire_days", 30)
                    exp_where = "AND (deadline_at = '' OR deadline_at >= ?) AND (published_at = '' OR date(published_at, '+' || ? || ' days') >= ?)"
                    exp_params = [today, str(auto_days), today]
                if url.path == "/api/stats":
                    # visible = non-expired, non-deleted records
                    del_filter = "AND is_deleted = 0 AND followup_status != 'expired'"
                    direct_ids = ai_review_bucket_ids("direct")
                    ai_filter = f"AND id IN ({','.join('?' for _ in direct_ids)})" if direct_ids is not None else ""
                    stat_params = exp_params + sorted(direct_ids or ())
                    total_vis = conn.execute(f"SELECT count(*) FROM tenders WHERE 1=1 {exp_where} {del_filter} {ai_filter}", stat_params).fetchone()[0]
                    # by priority (only among visible records)
                    key_cnt = conn.execute(f"SELECT count(*) FROM tenders WHERE priority='重点关注' {exp_where} {del_filter} {ai_filter}", stat_params).fetchone()[0]
                    follow_cnt = conn.execute(f"SELECT count(*) FROM tenders WHERE priority='值得跟进' {exp_where} {del_filter} {ai_filter}", stat_params).fetchone()[0]
                    watch_cnt = conn.execute(f"SELECT count(*) FROM tenders WHERE priority='一般关注' {exp_where} {del_filter} {ai_filter}", stat_params).fetchone()[0]
                    def visible_bucket_count(bucket):
                        ids = ai_review_bucket_ids(bucket)
                        if not ids:
                            return 0
                        marks = ','.join('?' for _ in ids)
                        return conn.execute(
                            f"SELECT count(*) FROM tenders WHERE id IN ({marks}) {exp_where} {del_filter}",
                            sorted(ids) + exp_params,
                        ).fetchone()[0]
                    self.send_json({
                        "total": total_vis,
                        "key": key_cnt,
                        "follow": follow_cnt,
                        "watch": watch_cnt,
                        "direct": visible_bucket_count("direct"),
                        "market": visible_bucket_count("market"),
                        "sources": conn.execute("SELECT count(*) FROM sources").fetchone()[0],
                        "connected": conn.execute("SELECT count(*) FROM sources WHERE status='connected'").fetchone()[0]
                    }); return
                if url.path == "/api/sources": self.send_json(rows_as_dicts(conn.execute("SELECT * FROM sources ORDER BY CASE status WHEN 'connected' THEN 0 WHEN 'awaiting_authorization' THEN 2 WHEN 'not_automated' THEN 3 ELSE 1 END,name").fetchall())); return
                if url.path == "/api/config": self.send_json(load_config()); return
                if url.path == "/api/rules": self.send_json(load_config().get("rules", RULES_DEFAULTS)); return
                if url.path == "/api/tenders":
                    q=params.get("q",[""])[0].strip(); min_score=int(params.get("min_score",["0"])[0] or 0)
                    priority=params.get("priority",[""])[0].strip()
                    bucket=params.get("bucket",["direct"])[0].strip()
                    page=int(params.get("page",["1"])[0] or 1); page_size=int(params.get("page_size",[str(cfg.get("page_size",15))])[0] or 15)
                    where_clauses = ["score>=?", "is_deleted=0", "followup_status!='expired'"]
                    where_vals = [min_score]
                    if priority in ("重点关注", "值得跟进", "一般关注"):
                        where_clauses.append("priority=?")
                        where_vals.append(priority)
                    if cfg.get("filter_expired", True):
                        auto_days = cfg.get("auto_expire_days", 30)
                        where_clauses.append("(deadline_at = '' OR deadline_at >= ?)")
                        where_vals.append(today)
                        where_clauses.append("(published_at = '' OR date(published_at, '+' || ? || ' days') >= ?)")
                        where_vals.extend([str(auto_days), today])
                    if q:
                        like = f"%{q}%"
                        where_clauses.append("(title LIKE ? OR buyer LIKE ? OR region LIKE ? OR content LIKE ?)")
                        where_vals.extend([like,like,like,like])
                    # AI 分流仅在后台生效；页面只按“直接商机/市场情报”读取对应记录。
                    bucket_ids = ai_review_bucket_ids("market" if bucket == "market" else "direct")
                    if bucket_ids is not None:
                        if bucket_ids:
                            where_clauses.append(f"id IN ({','.join('?' for _ in bucket_ids)})")
                            where_vals.extend(sorted(bucket_ids))
                        else:
                            where_clauses.append("1=0")
                    where_sql = " AND ".join(where_clauses)
                    total=conn.execute(f"SELECT count(*) FROM tenders WHERE {where_sql}", where_vals).fetchone()[0]
                    # 实时商机：优先级为第一排序规则（重点关注 > 值得跟进 > 一般关注），同优先级按发布时间倒序；
                    # priority 由入库时按分数初始化且可手动调整，与界面展示档位一致。
                    order_sql = "ORDER BY CASE priority WHEN '重点关注' THEN 0 WHEN '值得跟进' THEN 1 ELSE 2 END, published_at DESC"
                    rows=conn.execute(f"SELECT * FROM tenders WHERE {where_sql} {order_sql} LIMIT ? OFFSET ?",
                        where_vals+[page_size,(page-1)*page_size]).fetchall()
                    result = rows_as_dicts(rows)
                    for r in result:
                        r["rating"] = r.get("priority") or rating_label(r["score"], (cfg.get("rules") or {}).get("opportunity_levels"))
                        highlight_fields(r)
                    result = apply_ai_review_gate(result)
                    self.send_json({"rows":result,"total":total}); return
                # ---- 历史商机：全库数据，不过滤过期，但过滤已删除 ----
                if url.path == "/api/history-stats":
                    # 历史商机：全库展示（留存一年，不设 7 天窗口；近 7 天的也在实时页同步展示）
                    del_filter = "WHERE is_deleted = 0"
                    total_all = conn.execute(f"SELECT count(*) FROM tenders {del_filter}").fetchone()[0]
                    key_cnt = conn.execute(f"SELECT count(*) FROM tenders WHERE priority='重点关注' AND is_deleted=0").fetchone()[0]
                    follow_cnt = conn.execute(f"SELECT count(*) FROM tenders WHERE priority='值得跟进' AND is_deleted=0").fetchone()[0]
                    src_cnt = conn.execute(f"SELECT count(DISTINCT source_code) FROM tenders {del_filter}").fetchone()[0]
                    self.send_json({"total": total_all, "key": key_cnt, "follow": follow_cnt, "sources": src_cnt}); return
                if url.path == "/api/history":
                    q=params.get("q",[""])[0].strip(); min_score=int(params.get("min_score",["0"])[0] or 0)
                    priority=params.get("priority",[""])[0].strip()
                    page=int(params.get("page",["1"])[0] or 1); page_size=int(params.get("page_size",[str(cfg.get("page_size",15))])[0] or 15)
                    # 历史商机：全库展示，支持按标题/采购单位/地区/正文/代理机构/中标单位检索
                    where_clauses = ["score>=?", "is_deleted=0"]
                    where_vals = [min_score]
                    if priority in ("重点关注", "值得跟进", "一般关注"):
                        where_clauses.append("priority=?")
                        where_vals.append(priority)
                    if q:
                        like = f"%{q}%"
                        where_clauses.append("(title LIKE ? OR buyer LIKE ? OR region LIKE ? OR content LIKE ? OR agency LIKE ? OR winner LIKE ?)")
                        where_vals.extend([like,like,like,like,like,like])
                    where_sql = " AND ".join(where_clauses)
                    total=conn.execute(f"SELECT count(*) FROM tenders WHERE {where_sql}", where_vals).fetchone()[0]
                    # 历史商机：完全按发布时间倒序；同一天内按入库新→旧兜底，保证分页稳定。
                    rows=conn.execute(f"SELECT * FROM tenders WHERE {where_sql} ORDER BY published_at DESC, id DESC LIMIT ? OFFSET ?",
                        where_vals+[page_size,(page-1)*page_size]).fetchall()
                    result = rows_as_dicts(rows)
                    for r in result:
                        r["rating"] = r.get("priority") or rating_label(r["score"], (cfg.get("rules") or {}).get("opportunity_levels"))
                        highlight_fields(r)
                    result = apply_ai_review_gate(result)
                    self.send_json({"rows":result,"total":total}); return
                # ---- 回收站：已标记无用的记录 ----
                if url.path == "/api/deleted":
                    rows=conn.execute("SELECT * FROM tenders WHERE is_deleted=1 ORDER BY updated_at DESC").fetchall()
                    result = rows_as_dicts(rows)
                    for r in result:
                        r["rating"] = r.get("priority") or rating_label(r["score"], (cfg.get("rules") or {}).get("opportunity_levels"))
                    self.send_json({"rows":result}); return
                self.send_error(404)
            except Exception as exc: self.send_error(500, str(exc))
            finally: conn.close()
        def do_POST(self):
            url = urlparse(self.path)
            if url.path == "/api/auth/login":
                if not auth_enabled(): self.send_json({"error": "登录功能尚未启用"}, 404); return
                try:
                    data = self.read_body()
                    user, error = auth_login(data.get("username", ""), data.get("password", ""), self.headers.get("X-Real-IP", self.client_address[0]))
                    if error: self.send_json({"error": error}, 429 if "过多" in error else 401)
                    else: self.send_json({"ok": True, "username": user["username"], "role": user["role"]}, cookies=session_cookies(user))
                except Exception:
                    self.send_json({"error": "账号或密码错误"}, 401)
                return
            user = self.require_user(write=True)
            if not user: return
            if url.path == "/api/auth/logout":
                auth_logout(self); self.send_json({"ok": True}, cookies=clear_cookies()); return
            if url.path == "/api/fetch":
                def _bg_fetch():
                    c = connect(db_path); init_db(c); seed_sources(c)
                    total_c, total_u, total_s = 0, 0, 0
                    errors = []
                    # 常规抓取不含频控敏感来源（ccgp_search 由独立定时任务低频调度）
                    for code in [c for c in ADAPTERS.keys() if c not in MANUAL_ONLY_SOURCES]:
                        if code not in ADAPTERS:
                            continue
                        try:
                            cr, up, sk = fetch_source(c, code)
                            total_c += cr; total_u += up; total_s += sk
                        except Exception as e:
                            errors.append(f"{code}: {e}")
                            stamp = now()
                            c.execute("UPDATE sources SET last_checked_at=?,last_error=? WHERE code=?", (stamp, str(e), code))
                            c.commit()
                    # auto cleanup
                    cfg = load_config()
                    retention = cfg.get("retention_days", 180)
                    cutoff = (cn_today() - timedelta(days=retention)).strftime("%Y-%m-%d")
                    deleted = c.execute("DELETE FROM tenders WHERE published_at != '' AND published_at < ?", (cutoff,)).rowcount
                    c.commit()
                    merged_dups = sweep_duplicates(c)
                    c.close()
                    ai_synced = sync_ai_review_candidates()
                    ai_reviewed = auto_analyze_ai_review() if ai_synced is not None else None
                    msg = f"新增 {total_c} 条，更新 {total_u} 条，跳过 {total_s} 条"
                    if ai_synced is not None:
                        msg += f"\nAI 评审候选已自动同步 {ai_synced} 条"
                    if ai_reviewed is not None:
                        msg += f"；自动评审 {ai_reviewed.get('processed', 0)} 条"
                    if deleted:
                        msg += f"\n自动清理 {deleted} 条超期公告"
                    if merged_dups:
                        msg += f"\n去重兜底合并 {merged_dups} 条重复公告"
                    if errors:
                        msg += "\n失败: " + "; ".join(errors)
                    return msg
                try:
                    result_msg = _bg_fetch()
                    self.send_json({"ok": True, "message": result_msg})
                except Exception as exc:
                    self.send_json({"ok": False, "error": str(exc)})
            elif url.path == "/api/config":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                try:
                    new_cfg = json.loads(body)
                    cfg = load_config()
                    for k, v in new_cfg.items():
                        cfg[k] = v
                    save_config(cfg)
                    self.send_json({"ok": True})
                except Exception as exc:
                    self.send_error(400, str(exc))
            elif url.path == "/api/tenders/mark-expired":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                try:
                    data = json.loads(body)
                    tid = data.get("id")
                    if not tid:
                        self.send_json({"ok": False, "error": "缺少 id 参数"})
                        return
                    conn = connect(db_path)
                    conn.execute("UPDATE tenders SET followup_status='expired', updated_at=? WHERE id=?", (now(), tid))
                    conn.commit()
                    conn.close()
                    self.send_json({"ok": True})
                except Exception as exc:
                    self.send_json({"ok": False, "error": str(exc)})
            elif url.path == "/api/tenders/set-priority":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                try:
                    data = json.loads(body)
                    tid = data.get("id")
                    priority = data.get("priority")
                    if not tid or priority not in ('重点关注', '值得跟进', '一般关注'):
                        self.send_json({"ok": False, "error": "参数无效"})
                        return
                    conn = connect(db_path)
                    conn.execute("UPDATE tenders SET priority=?, updated_at=? WHERE id=?", (priority, now(), tid))
                    conn.commit()
                    conn.close()
                    self.send_json({"ok": True})
                except Exception as exc:
                    self.send_json({"ok": False, "error": str(exc)})
            elif url.path == "/api/tenders/mark-useless":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                try:
                    data = json.loads(body)
                    tid = data.get("id")
                    if not tid:
                        self.send_json({"ok": False, "error": "缺少 id 参数"})
                        return
                    conn = connect(db_path)
                    conn.execute("UPDATE tenders SET is_deleted=1, updated_at=? WHERE id=?", (now(), tid))
                    conn.commit()
                    conn.close()
                    self.send_json({"ok": True})
                except Exception as exc:
                    self.send_json({"ok": False, "error": str(exc)})
            elif url.path == "/api/tenders/restore":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                try:
                    data = json.loads(body)
                    tid = data.get("id")
                    if not tid:
                        self.send_json({"ok": False, "error": "缺少 id 参数"})
                        return
                    conn = connect(db_path)
                    conn.execute("UPDATE tenders SET is_deleted=0, updated_at=? WHERE id=?", (now(), tid))
                    conn.commit()
                    conn.close()
                    self.send_json({"ok": True})
                except Exception as exc:
                    self.send_json({"ok": False, "error": str(exc)})
            else:
                self.send_error(404)
        def do_PUT(self):
            url = urlparse(self.path)
            if not self.require_user(write=True): return
            if url.path == "/api/rules":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                try:
                    new_rules = json.loads(body)
                    # 验证规则
                    errors = validate_rules(new_rules)
                    if errors:
                        self.send_json({"ok": False, "errors": errors})
                        return
                    cfg = load_config()
                    cfg["rules"] = new_rules
                    save_config(cfg)
                    self.send_json({"ok": True})
                except Exception as exc:
                    self.send_error(400, str(exc))
            else:
                self.send_error(404)
    if auth_enabled():
        init_auth()
    return Handler

def validate_rules(rules: dict) -> list[str]:
    """验证规则配置的有效性，返回错误信息列表。空列表表示验证通过。"""
    errors = []
    # 验证业务分类
    cats = rules.get("business_categories", [])
    if not isinstance(cats, list):
        errors.append("business_categories 必须是数组")
    else:
        names_seen = set()
        for i, cat in enumerate(cats):
            if not isinstance(cat, dict):
                errors.append(f"第 {i+1} 个分类必须是对象")
                continue
            name = cat.get("name", "").strip()
            if not name:
                errors.append(f"第 {i+1} 个分类缺少名称")
            elif name in names_seen:
                errors.append(f"分类名称重复：{name}")
            else:
                names_seen.add(name)
            weight = cat.get("weight", 10)
            if not isinstance(weight, (int, float)) or weight < 1 or weight > 100:
                errors.append(f"分类「{name}」权重必须在 1-100 之间")
            keywords = cat.get("keywords", [])
            if not isinstance(keywords, list):
                errors.append(f"分类「{name}」关键词必须是数组")
            elif len(keywords) == 0:
                errors.append(f"分类「{name}」至少需要 1 个关键词")
    # 验证重点区域
    pr = rules.get("priority_regions", {})
    if not isinstance(pr, dict):
        errors.append("priority_regions 必须是对象")
    else:
        for level in ["一级", "二级"]:
            regions = pr.get(level, [])
            if not isinstance(regions, list):
                errors.append(f"{level}重点区域必须是数组")
    # 验证重点城市
    cities = rules.get("priority_cities", [])
    if not isinstance(cities, list):
        errors.append("priority_cities 必须是数组")
    # 验证采购方关键词
    terms = rules.get("client_terms", [])
    if not isinstance(terms, list):
        errors.append("client_terms 必须是数组")
    # 验证天眼查检索词（额度保护：每词每次抓取消耗 1 次，每天抓 6 次，日限额 100）
    tyc_kw = rules.get("tyc_search_keywords", [])
    if not isinstance(tyc_kw, list):
        errors.append("tyc_search_keywords 必须是数组")
    else:
        words = [str(k).strip() for k in tyc_kw if str(k).strip()]
        if len(words) > 16:
            errors.append(f"天眼查检索词不能超过 16 个（当前 {len(words)} 个）：每词每次抓取消耗 1 次额度，每天抓取 6 次将超出日限额")
    # 验证商机级别阈值（1-100 整数，重点 > 跟进）
    lv = rules.get("opportunity_levels")
    if lv is not None:
        if not isinstance(lv, dict):
            errors.append("opportunity_levels 必须是对象")
        else:
            kt, ft = lv.get("key_threshold"), lv.get("follow_threshold")
            for nm, v in (("key_threshold", kt), ("follow_threshold", ft)):
                if not isinstance(v, int) or not (1 <= v <= 100):
                    errors.append(f"商机级别阈值 {nm} 必须是 1-100 的整数")
            if isinstance(kt, int) and isinstance(ft, int) and 1 <= kt <= 100 and 1 <= ft <= 100 and kt <= ft:
                errors.append("「重点关注」阈值必须大于「值得跟进」阈值")
    # 验证商机种类（分类名非空、关键词为数组）
    oc = rules.get("opportunity_categories")
    if oc is not None:
        if not isinstance(oc, list):
            errors.append("opportunity_categories 必须是数组")
        else:
            for i, cat in enumerate(oc):
                if not isinstance(cat, dict) or not str(cat.get("name", "")).strip():
                    errors.append(f"第 {i+1} 个商机种类缺少名称")
                elif not isinstance(cat.get("keywords", []), list):
                    errors.append(f"商机种类「{cat.get('name')}」关键词必须是数组")
    return errors

def cmd_init(args):
    conn=connect(args.db); init_db(conn); seed_sources(conn); conn.close(); print(f"已初始化：{args.db}")
def cmd_seed(args):
    conn=connect(args.db); init_db(conn); seed_sources(conn)
    for item in demo_items(): upsert_tender(conn,item)
    conn.close(); print("已导入 4 条本地演示公告。")
def cmd_import(args):
    items=json.loads(Path(args.file).read_text(encoding="utf-8")); items=items if isinstance(items,list) else [items]
    conn=connect(args.db); init_db(conn); seed_sources(conn); created=0
    for item in items:
        was_created, score=upsert_tender(conn,item); created+=int(was_created); print(f"{'新增' if was_created else '更新'} {rating_label(score)} | {item['title']}")
    conn.close(); print(f"处理完成：{len(items)} 条，新增 {created} 条。")
def cmd_list(args):
    conn=connect(args.db); rows=conn.execute("SELECT score,title,buyer,region,priority,source_url FROM tenders WHERE score>=? AND is_deleted=0 ORDER BY score DESC",(args.min_score,)).fetchall(); conn.close()
    for r in rows: print(f"[{rating_label(r.get('priority') or r['score']):<6}] {r['title']} | {r['buyer']} | {r['region']}")
    print(f"共 {len(rows)} 条")
def cmd_sources(args):
    conn=connect(args.db); rows=conn.execute("SELECT code,name,access_mode,status,notes,last_success_at FROM sources ORDER BY CASE status WHEN 'connected' THEN 0 WHEN 'awaiting_authorization' THEN 2 WHEN 'not_automated' THEN 3 ELSE 1 END,name").fetchall(); conn.close()
    connected=[r for r in rows if r["status"]=="connected"]
    pending=[r for r in rows if r["status"]!="connected"]
    status_labels={"connected":"已接入","awaiting_authorization":"待授权","not_automated":"不自动化","pending_js":"JS渲染","pending_timeout":"连接超时","pending_ssl":"SSL不兼容","pending_structure":"无公告API","pending_antibot":"反爬拦截","pending_search":"搜索不可用","manual_review":"待人工核验","planned":"计划中"}
    if connected:
        print(f"=== 已接入（{len(connected)}）===")
        for r in connected:
            ts=r["last_success_at"][:16] if r["last_success_at"] else "—"
            print(f"  {r['code']:<12} {status_labels.get(r['status'],r['status']):<8} {r['name']}")
            print(f"    上次成功: {ts}  {r['notes']}")
        print()
    print(f"=== 待接入（{len(pending)}）===")
    for r in pending:
        print(f"  {r['code']:<12} {status_labels.get(r['status'],r['status']):<12} {r['name']}")
        print(f"    {r['notes']}")
def cmd_fetch(args):
    conn=connect(args.db); init_db(conn); seed_sources(conn)
    sources=[args.source] if args.source else [c for c in ADAPTERS.keys() if c not in MANUAL_ONLY_SOURCES]
    total_created,total_updated,total_skipped=0,0,0
    for code in sources:
        if code not in ADAPTERS:
            print(f"跳过 {code}：无适配器实现"); continue
        print(f"\n抓取 {code} ...")
        try:
            created,updated,skipped=fetch_source(conn,code)
            total_created+=created; total_updated+=updated; total_skipped+=skipped
            print(f"  完成：新增 {created}，更新 {updated}，跳过 {skipped}")
        except Exception as e:
            print(f"  失败: {e}")
            stamp=now()
            conn.execute("UPDATE sources SET last_checked_at=?,last_error=? WHERE code=?",(stamp,str(e),code))
            conn.commit()

    # ---- 自动清理超期数据 ----
    cfg = load_config()
    retention = cfg.get("retention_days", 180)
    cutoff_date = (cn_today() - timedelta(days=retention)).strftime("%Y-%m-%d")
    deleted = conn.execute("DELETE FROM tenders WHERE published_at != '' AND published_at < ?", (cutoff_date,)).rowcount
    conn.commit()
    merged_dups = sweep_duplicates(conn)
    conn.close()
    ai_synced = sync_ai_review_candidates()
    ai_reviewed = auto_analyze_ai_review() if ai_synced is not None else None
    print(f"\n全部完成：新增 {total_created} 条，更新 {total_updated} 条，跳过 {total_skipped} 条。")
    if ai_synced is not None:
        print(f"AI 评审候选已自动同步：{ai_synced} 条。")
    if ai_reviewed is not None:
        print(f"AI 自动评审：完成 {ai_reviewed.get('processed', 0)} 条，失败 {ai_reviewed.get('failed', 0)} 条。")
    if deleted:
        print(f"自动清理：删除 {deleted} 条超过 {retention} 天的公告。")
    if merged_dups:
        print(f"去重兜底：合并 {merged_dups} 条重复公告。")

def cmd_backfill(args):
    """为已有公告回填截止时间（从详情页抓取）。"""
    conn = connect(args.db)
    rows = conn.execute(
        "SELECT id, source_code, source_url FROM tenders WHERE (deadline_at IS NULL OR deadline_at='') AND source_url != ''"
    ).fetchall()
    if not rows:
        print("所有公告已有截止时间，无需回填。")
        conn.close()
        return
    print(f"共 {len(rows)} 条公告缺少截止时间，开始逐条抓取详情页…")
    filled_http = 0
    filled_browser = 0
    failed = 0
    browser_available = True  # 惰性检测 Kimi WebBridge 是否可用
    for r in rows:
        url, src, tid = r["source_url"], r["source_code"], r["id"]
        deadline = ""
        # 第一步：HTTP 抓取（除非 --browser-only）
        if not getattr(args, "browser_only", False):
            deadline = _fetch_detail_deadline(url, src)
            if deadline:
                conn.execute("UPDATE tenders SET deadline_at=? WHERE id=?", (deadline, tid))
                filled_http += 1
                print(f"  [HTTP] {src} {url[:60]} → {deadline}")
                continue
        # 第二步：浏览器回填（Kimi WebBridge）
        if browser_available:
            deadline = _wb_extract_deadline(url)
            if deadline:
                conn.execute("UPDATE tenders SET deadline_at=? WHERE id=?", (deadline, tid))
                filled_browser += 1
                print(f"  [浏览器] {src} {url[:60]} → {deadline}")
            else:
                print(f"  [--] {src} {url[:60]} → 未提取到")
                failed += 1
        else:
            print(f"  [--] {src} {url[:60]} → HTTP 失败，浏览器不可用")
            failed += 1
    conn.commit()
    conn.close()
    total = filled_http + filled_browser
    print(f"\n回填完成：HTTP {filled_http} + 浏览器 {filled_browser} = {total}/{len(rows)} 条成功。")
    if failed:
        print(f"  仍有 {failed} 条未提取到截止时间（可能需要登录或内容在 PDF 中）。")

def cmd_audit_iccec(args):
    """重新核验天眼查导入的中交招采链接；不删除原记录，只更新可核验证据。"""
    conn = connect(args.db); init_db(conn)
    rows = conn.execute("""SELECT id, source_url FROM tenders
      WHERE is_deleted=0 AND source_code LIKE '%tianyancha%' AND source_url LIKE '%sp.iccec.cn/viewNoticeDetail%'""").fetchall()
    if not rows:
        print("没有需要核验的中交招采网记录。")
        conn.close(); return
    print(f"开始重新核验 {len(rows)} 条中交招采网记录…")
    refreshed = unavailable = 0
    stamp = now(); audited_ids: list[int] = []
    for r in rows:
        status, canonical, content = _iccec_public_material_snapshot(r["source_url"])
        audited_ids.append(int(r["id"]))
        if content:
            conn.execute("""UPDATE tenders SET source_url=?, content=?, deadline_at='', evidence_status=?,
                updated_at=? WHERE id=?""", (canonical, content, status, stamp, r["id"]))
            refreshed += 1
            print(f"  [公开明细] #{r['id']} {canonical}")
        else:
            conn.execute("UPDATE tenders SET evidence_status=?, link_ok=0, updated_at=? WHERE id=?",
                         (status, stamp, r["id"]))
            unavailable += 1
            print(f"  [不可核验] #{r['id']} {status}")
    conn.commit(); conn.close()
    # 旧聚合摘要生成的 AI 结论没有完整原文依据：先归档再标为来源待核验，
    # 不删除任何人工结论或学习样本，后续可从 review_history 恢复。
    if AI_REVIEW_DB.exists() and audited_ids:
        try:
            review_conn = sqlite3.connect(AI_REVIEW_DB)
            review_conn.row_factory = sqlite3.Row
            placeholders = ','.join('?' for _ in audited_ids)
            review_rows = review_conn.execute(
                f"SELECT * FROM reviews WHERE source_tender_id IN ({placeholders})", audited_ids
            ).fetchall()
            review_conn.executemany("""INSERT INTO review_history(review_id,previous_status,previous_label,previous_fit_score,
                previous_confidence,previous_reason_json,previous_evidence_json,archived_at,archive_reason)
                VALUES(?,?,?,?,?,?,?,?,?)""", [
                    (r["id"], r["ai_status"], r["ai_label"], r["ai_fit_score"], r["ai_confidence"],
                     r["ai_reason_json"], r["ai_evidence_json"], stamp, "中交招采公开正文不可获取，旧聚合摘要不再作为 AI 依据")
                    for r in review_rows
                ])
            review_conn.execute(f"""UPDATE reviews SET ai_status='expired', ai_label='来源待核验',
                error='中交招采网公开详情未提供公告正文/附件，旧聚合摘要已归档，不再作为 AI 依据', analyzed_at=?, synced_at=?
                WHERE source_tender_id IN ({placeholders})""", (stamp, stamp, *audited_ids))
            review_conn.commit(); review_conn.close()
        except Exception as exc:
            print(f"  AI 评审记录归档失败（不影响原始商机数据）：{exc}")
    print(f"核验完成：重新获取公开物资明细 {refreshed} 条；无法公开核验 {unavailable} 条。")

def cmd_enrich(args):
    conn=connect(args.db); init_db(conn); seed_sources(conn)
    if args.buyer:
        targets=[{"buyer":args.buyer,"n":1,"s":0}]
    else:
        targets=conn.execute("""SELECT buyer, count(*) AS n, max(score) AS s FROM tenders
          WHERE buyer != '' AND score >= ? GROUP BY buyer ORDER BY s DESC, n DESC LIMIT ?""",
          (args.min_score,args.limit)).fetchall()
    if not targets:
        print("没有符合条件的采购单位（需公告带 buyer 字段且评分达标）。"); conn.close(); return
    print(f"待画像采购单位 {len(targets)} 家；每次查询消耗天眼查账号额度。\n")
    ok=fail=skip=0
    for t in targets:
        buyer=t["buyer"]
        if not args.refresh:
            hit=conn.execute("SELECT enriched_at FROM buyer_profiles WHERE buyer=?",(buyer,)).fetchone()
            if hit:
                skip+=1; print(f"  跳过（已画像 {hit['enriched_at'][:16]}）：{buyer}")
                continue
        try:
            row=enrich_buyer(conn,buyer,with_risk=args.with_risk)
            if row["match_status"]=="matched":
                ok+=1
                print(f"  [OK] {buyer} -> {row['company_name']} | {row['reg_status']} | 法定代表人 {row['legal_person']} | {row['reg_capital']}")
                if row["tags"]: print(f"       标签：{row['tags']}")
            else:
                fail+=1; print(f"  [未匹配] 天眼查未返回该主体：{buyer}")
        except Exception as e:
            fail+=1; print(f"  [失败] {buyer}: {e}")
    if ok:
        stamp=now()
        conn.execute("UPDATE sources SET last_checked_at=?,last_success_at=?,last_error=NULL WHERE code='tianyancha'",(stamp,stamp))
        conn.commit()
    conn.close()
    print(f"\n画像完成：成功 {ok}，未匹配/失败 {fail}，跳过 {skip}。")

def cmd_serve(args):
    conn=connect(args.db); init_db(conn); seed_sources(conn); conn.close()
    server=ThreadingHTTPServer((args.host,args.port),make_handler(args.db)); print(f"本地看板：http://{args.host}:{args.port}（按 Ctrl+C 停止）")
    # 每次服务发布/重启后补同步一次，确保在发布期间积压的新公告也完成 AI 分流。
    def analyze_after_start():
        synced = sync_ai_review_candidates()
        if synced is not None:
            result = auto_analyze_ai_review()
            if result is not None:
                print(f"启动后 AI 分流：同步 {synced} 条，完成 {result.get('processed', 0)} 条，失败 {result.get('failed', 0)} 条。")
    threading.Thread(target=analyze_after_start, name="ai-review-startup", daemon=True).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()

def cmd_dedupe(args):
    """扫描全库未删除公告，找出同一公告的变体（默认预览，--apply 才真正合并）。"""
    conn=connect(args.db); init_db(conn); seed_sources(conn)
    rows=[dict(r) for r in conn.execute("SELECT * FROM tenders WHERE is_deleted=0").fetchall()]
    norm=[normalize_title(r["title"]) for r in rows]
    n=len(rows); parent=list(range(n))
    def find(i):
        while parent[i]!=i: parent[i]=parent[parent[i]]; i=parent[i]
        return i
    def union(i,j):
        ri,rj=find(i),find(j)
        if ri!=rj: parent[rj]=ri
    for i in range(n):
        if len(norm[i])<8: continue
        for j in range(i+1,n):
            if same_project_title(norm[i],norm[j]) and _dup_compat(rows[i],rows[j]):
                union(i,j)
    groups={}
    for i in range(n):
        if len(norm[i])<8: continue
        groups.setdefault(find(i),[]).append(i)
    merged=0
    for members in groups.values():
        if len(members)<2: continue
        # 保留者选择：有正文 > 标题长 > id 小（保留最早入库的人工设置）
        members.sort(key=lambda i:(bool(rows[i]["content"]), len(rows[i]["title"]), -rows[i]["id"]), reverse=True)
        keep=members[0]
        for idx in members[1:]:
            if args.apply:
                merge_into(conn, rows[keep]["id"], rows[idx])
            merged+=1
            print(f"合并 #{rows[idx]['id']} -> #{rows[keep]['id']}")
            print(f"  保留: {rows[keep]['title']}")
            print(f"  重复: {rows[idx]['title']}")
    conn.close()
    if merged==0:
        print("未发现重复公告。")
    else:
        print(f"\n共 {merged} 条重复" + ("，已合并完成。" if args.apply else "。以上为预览，加 --apply 参数执行实际合并。"))

def cmd_rescore(args):
    """按当前规则重算独立测试库全部商机评分，用于规则权重调整后回填存量数据。"""
    conn = connect(args.db); init_db(conn)
    rules = load_config().get("rules") or {}
    rows = conn.execute("SELECT * FROM tenders WHERE is_deleted=0").fetchall()
    changed = 0
    for row in rows:
        item = dict(row)
        score, matches = score_item(item, rules)
        priority = rating_label(score, rules.get("opportunity_levels"))
        if score != row["score"] or priority != (row["priority"] or ""):
            conn.execute("UPDATE tenders SET score=?, match_json=?, priority=?, updated_at=? WHERE id=?",
                         (score, json.dumps(_strip_surrogates(matches), ensure_ascii=False), priority, now(), row["id"]))
            changed += 1
    conn.commit(); conn.close()
    ai_synced = sync_ai_review_candidates()
    print(f"评分重算完成：共检查 {len(rows)} 条，更新 {changed} 条。")
    if ai_synced is not None:
        print(f"AI 评审候选已自动同步：{ai_synced} 条。")

def quality_issue_reason(row: dict, rules: dict) -> str:
    """全库质量审计的硬性剔除条件；命中即不再作为商机展示。"""
    score, _ = score_item(row, rules)
    reason = ingestion_issue_reason(row, rules, score)
    if reason:
        return reason
    codes = {x.strip() for x in str(row.get("source_code") or "").split(",")}
    title = str(row.get("title") or "")
    match = str(row.get("match_json") or "")
    # 天津港全站页面若取不到发布日期且“智慧港口”仅出现在导航/页脚，不能作为有效商机。
    if "tj_port" in codes and not row.get("published_at") and "智慧港口" in match and "智慧港口" not in title:
        return "网站导航/页脚关键词污染且发布日期缺失"
    return ""

def cmd_quality_audit(args):
    """审计全库误入数据；--apply 仅移入回收站，可恢复，不物理删除。"""
    conn = connect(args.db); init_db(conn)
    rules = load_config().get("rules") or {}
    ai_excluded_ids: set[int] = set()
    if getattr(args, "include_ai_excluded", False) and AI_REVIEW_DB.exists():
        try:
            review_conn = sqlite3.connect(f"file:{AI_REVIEW_DB}?mode=ro", uri=True)
            ai_excluded_ids = {int(row[0]) for row in review_conn.execute(
                "SELECT source_tender_id FROM reviews WHERE ai_status='exclude'"
            )}
            review_conn.close()
        except Exception as exc:
            print(f"AI 排除记录读取失败，本次仅执行确定性规则审计：{exc}")
    rows = [dict(r) for r in conn.execute("SELECT * FROM tenders WHERE is_deleted=0 ORDER BY id").fetchall()]
    issues = []
    rescored = 0
    for row in rows:
        score, matches = score_item(row, rules)
        priority = rating_label(score, rules.get("opportunity_levels"))
        reason = quality_issue_reason(row, rules)
        if not reason and row["id"] in ai_excluded_ids:
            reason = "AI 评审明确排除（可在 AI 排除页复核）"
        if reason:
            issues.append((row, reason))
            continue
        if score != row["score"] or priority != (row["priority"] or ""):
            rescored += 1
            if args.apply:
                conn.execute("UPDATE tenders SET score=?,match_json=?,priority=?,updated_at=? WHERE id=?",
                             (score, json.dumps(_strip_surrogates(matches), ensure_ascii=False), priority, now(), row["id"]))
    counts: dict[str, int] = {}
    for _, reason in issues: counts[reason] = counts.get(reason, 0) + 1
    print(f"质量审计：检查 {len(rows)} 条；明确误入 {len(issues)} 条；有效记录待重算 {rescored} 条。")
    for reason, count in counts.items(): print(f"  - {reason}：{count} 条")
    for row, reason in issues:
        print(f"  #{row['id']} [{reason}] {row['title']}")
    if args.apply and issues:
        stamp = now(); ids = []
        for row, reason in issues:
            ids.append(row["id"])
            note = f"系统质量审计移入回收站：{reason}（{stamp}）"
            conn.execute("UPDATE tenders SET is_deleted=1,notes=?,updated_at=? WHERE id=?", (note, stamp, row["id"]))
        conn.commit()
        # 同步隐藏这些源记录对应的 AI 评审，保留记录但不再计入待审/推荐列表。
        if AI_REVIEW_DB.exists() and ids:
            try:
                review_conn = sqlite3.connect(AI_REVIEW_DB)
                review_conn.execute(f"UPDATE reviews SET ai_status='expired',ai_label='已移出商机库',error='源公告质量审计已移出商机库' WHERE source_tender_id IN ({','.join('?' for _ in ids)})", ids)
                review_conn.commit(); review_conn.close()
            except Exception as exc:
                print(f"AI 评审状态同步跳过：{exc}")
        print(f"已将 {len(ids)} 条明确误入记录移入回收站（可恢复，未物理删除）。")
    elif issues:
        print("以上为预览；加 --apply 执行移入回收站。")
    conn.close()

def parser():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--db",type=Path,default=DEFAULT_DB,help="SQLite 数据库路径")
    sub=p.add_subparsers(required=True); sub.add_parser("init").set_defaults(func=cmd_init); sub.add_parser("seed-demo").set_defaults(func=cmd_seed)
    x=sub.add_parser("import-json"); x.add_argument("file"); x.set_defaults(func=cmd_import)
    x=sub.add_parser("list"); x.add_argument("--min-score",type=int,default=0); x.set_defaults(func=cmd_list)
    sub.add_parser("sources").set_defaults(func=cmd_sources)
    x=sub.add_parser("fetch"); x.add_argument("source",nargs="?",help="来源编码（省略则抓取所有已接入来源）"); x.set_defaults(func=cmd_fetch)
    bp = sub.add_parser("backfill",help="为已有公告回填截止时间（从详情页抓取）")
    bp.add_argument("--browser-only",action="store_true",help="仅使用浏览器回填（跳过 HTTP 抓取）")
    bp.set_defaults(func=cmd_backfill)
    x=sub.add_parser("audit-iccec",help="重新核验天眼查导入的中交招采网记录；仅采用公开可验证明细")
    x.set_defaults(func=cmd_audit_iccec)
    x=sub.add_parser("enrich"); x.add_argument("buyer",nargs="?",help="直接指定采购单位名称（省略则自动挑选高分单位）")
    x.add_argument("--min-score",type=int,default=80,help="自动模式下的最低评分（默认 80）")
    x.add_argument("--limit",type=int,default=20,help="最多画像的采购单位数量（默认 20）")
    x.add_argument("--refresh",action="store_true",help="已画像的单位也重新查询")
    x.add_argument("--with-risk",action="store_true",help="同时查询风险总览（每家多消耗 1 次额度）")
    x.set_defaults(func=cmd_enrich)
    x=sub.add_parser("dedupe",help="检测并合并重复公告（同一公告的标题变体）")
    x.add_argument("--apply",action="store_true",help="实际执行合并（默认仅预览）")
    x.set_defaults(func=cmd_dedupe)
    x=sub.add_parser("rescore",help="按当前规则重算所有商机评分")
    x.set_defaults(func=cmd_rescore)
    x=sub.add_parser("quality-audit",help="全库审计招聘/结果公告、零分和页面导航污染数据")
    x.add_argument("--apply",action="store_true",help="将明确误入数据移入回收站（不物理删除）")
    x.add_argument("--include-ai-excluded",action="store_true",help="同时移入 AI 已明确排除的记录（可恢复）")
    x.set_defaults(func=cmd_quality_audit)
    x=sub.add_parser("serve"); x.add_argument("--host",default="127.0.0.1"); x.add_argument("--port",type=int,default=8787); x.set_defaults(func=cmd_serve)
    return p
if __name__ == "__main__":
    args=parser().parse_args(); args.func(args)
