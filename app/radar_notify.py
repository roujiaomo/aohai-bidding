#!/usr/bin/env python3
"""Send scheduled DingTalk notifications for current bidding opportunities.

Credentials are intentionally read from environment variables rather than the
application config, because the dashboard exposes a read-only config endpoint.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "radar.db"
DEFAULT_CONFIG = ROOT / "config.json"
DEFAULT_REVIEW_DB = Path(os.getenv("AI_REVIEW_DB", "/opt/bidding-ai-review/data/ai_review.db"))


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def approved_tender_ids(review_db_path: Path) -> set[int]:
    """Return tender ids that the formal AI-review workflow allows to display.

    The dashboard hides unreviewed and AI-excluded tenders.  Notifications must
    use the same gate; sending a fallback list when the review store is absent
    would reintroduce records users cannot see in the current radar.
    """
    if not review_db_path.exists():
        raise RuntimeError(f"AI review database is unavailable: {review_db_path}")
    conn = sqlite3.connect(f"file:{review_db_path}?mode=ro", uri=True)
    try:
        return {
            int(row[0])
            for row in conn.execute(
                """SELECT source_tender_id FROM reviews
                   WHERE ai_status IN ('approved', 'approved_manual')
                     AND bucket IN ('direct_opportunity', 'market_intelligence')"""
            ).fetchall()
        }
    finally:
        conn.close()


def current_rows(
    db_path: Path,
    config: dict,
    priorities: tuple[str, ...],
    review_db_path: Path,
) -> list[sqlite3.Row]:
    """Use exactly the same current-list and formal-review gates as the UI."""
    approved_ids = approved_tender_ids(review_db_path)
    if not approved_ids:
        return []
    today = datetime.now().date().isoformat()
    clauses = ["is_deleted=0", "followup_status!='expired'", "priority IN ({})".format(
        ",".join("?" for _ in priorities)
    )]
    clauses.append("id IN ({})".format(",".join("?" for _ in approved_ids)))
    values: list[object] = list(priorities) + sorted(approved_ids)
    if config.get("filter_expired", True):
        days = int(config.get("auto_expire_days", 30))
        clauses.extend([
            "(deadline_at='' OR deadline_at>=?)",
            "(published_at='' OR date(published_at, '+' || ? || ' days')>=?)",
        ])
        values.extend([today, str(days), today])
    sql = f"""
        SELECT title, priority, buyer, region, deadline_at, published_at, source_url, score
        FROM tenders
        WHERE {' AND '.join(clauses)}
        ORDER BY score DESC, CASE priority WHEN '重点关注' THEN 0 ELSE 1 END, published_at DESC, id DESC
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, values).fetchall()
    finally:
        conn.close()


def markdown(slot: str, rows: list[sqlite3.Row], dashboard_url: str) -> str:
    title = {
        "morning": "遨海商机雷达｜每日实时商机提醒",
        "key": "遨海商机雷达｜重点商机复提醒",
        "test": "遨海商机雷达｜每日实时商机提醒",
    }[slot]
    if not rows:
        return f"### {title}\n\n当前没有满足条件的实时商机。"

    lines = [f"### {title}", ""]
    # Keep notifications readable and focused on the three strongest leads.
    shown = rows[:3]
    for index, row in enumerate(shown, 1):
        meta = " · ".join(part for part in (
            row["buyer"] or "采购方未提取",
            row["region"] or "地区未提取",
            f"截止：{row['deadline_at']}" if row["deadline_at"] else "截止时间待确认",
        ) if part)
        lines.extend([
            f"**{index}. [{row['priority']}] {row['title']}**",
            "",
            f"{meta}（评分 {row['score']}）  ",
            f"[查看公告]({row['source_url']})",
            "",
        ])
    remaining = len(rows) - len(shown)
    if remaining:
        lines.append(f"> 更多 {remaining} 条见 [商机雷达]({dashboard_url})")
    else:
        lines.append(f"> 更多商机见 [商机雷达]({dashboard_url})")
    return "\n".join(lines)


def send_markdown(webhook: str, secret: str, title: str, text: str) -> dict:
    timestamp = str(round(time.time() * 1000))
    digest = hmac.new(secret.encode("utf-8"), f"{timestamp}\n{secret}".encode("utf-8"), hashlib.sha256).digest()
    signed_url = f"{webhook}{'&' if '?' in webhook else '?'}timestamp={timestamp}&sign={quote_plus(base64.b64encode(digest))}"
    payload = json.dumps({"msgtype": "markdown", "markdown": {"title": title, "text": text}}, ensure_ascii=False).encode("utf-8")
    request = Request(signed_url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("errcode") != 0:
        raise RuntimeError(f"DingTalk rejected notification: {result}")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--review-db", type=Path, default=DEFAULT_REVIEW_DB)
    parser.add_argument("--slot", choices=("morning", "key", "test"), required=True)
    parser.add_argument("--dry-run", action="store_true", help="validate selection without sending DingTalk")
    parser.add_argument(
        "--connectivity-test",
        action="store_true",
        help="send a fixed data-free DingTalk message to verify the robot connection",
    )
    args = parser.parse_args()

    dashboard_url = os.environ.get("RADAR_DASHBOARD_URL", "http://39.96.217.93/radar-ai/")
    if args.connectivity_test:
        webhook = os.environ.get("DINGTALK_WEBHOOK_URL", "")
        secret = os.environ.get("DINGTALK_WEBHOOK_SECRET", "")
        if not webhook or not secret:
            print("DINGTALK_WEBHOOK_URL and DINGTALK_WEBHOOK_SECRET must be set", file=sys.stderr)
            return 2
        send_markdown(
            webhook,
            secret,
            "商机雷达测试",
            "### 遨海商机雷达｜钉钉链路测试\n\n新版通知链路连接正常；本消息未包含任何商机数据。\n\n[打开新版商机雷达](%s)" % dashboard_url,
        )
        print("DingTalk connectivity test sent.")
        return 0

    priorities = ("重点关注", "值得跟进") if args.slot in ("morning", "test") else ("重点关注",)
    try:
        rows = current_rows(args.db, load_config(args.config), priorities, args.review_db)
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"DingTalk notification selection failed: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(json.dumps({"slot": args.slot, "rows": len(rows), "titles": [r["title"] for r in rows], "dashboard_url": dashboard_url}, ensure_ascii=False))
        return 0
    if not rows and args.slot != "test":
        print("No matching current opportunities; notification skipped.")
        return 0
    webhook = os.environ.get("DINGTALK_WEBHOOK_URL", "")
    secret = os.environ.get("DINGTALK_WEBHOOK_SECRET", "")
    if not webhook or not secret:
        print("DINGTALK_WEBHOOK_URL and DINGTALK_WEBHOOK_SECRET must be set", file=sys.stderr)
        return 2
    heading = "商机雷达提醒"
    send_markdown(webhook, secret, heading, markdown(args.slot, rows, dashboard_url))
    print(f"DingTalk notification sent: slot={args.slot}, rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
