#!/usr/bin/env python3
"""Send a DingTalk alert only when the generated quality report has failures."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from radar_notify import send_markdown


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REPORT = ROOT / "data" / "reports" / "quality-latest.json"


def collect_alerts(report: dict) -> list[str]:
    lines = []
    for item in report.get("sources", {}).get("alerts", []):
        lines.append(f"- 来源 `{item.get('source','未知')}`：{item.get('reason','异常')}")
    for reason, count in report.get("quality", {}).get("deterministic_issues", {}).items():
        lines.append(f"- 数据规则：{reason}（{count} 条）")
    ai = report.get("ai", {})
    if ai.get("mismatch"):
        lines.append(f"- AI 展示一致性：发现 {ai['mismatch']} 条不一致")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"无法读取质量报告：{exc}", file=sys.stderr)
        return 2
    alerts = collect_alerts(report)
    if not alerts:
        print("质量正常；不发送钉钉告警。")
        return 0
    text = "### 遨海商机雷达｜数据质量告警\n\n" + "\n".join(alerts) + "\n\n请在来源状态和运行账本中复核。"
    if args.dry_run:
        print(json.dumps({"alerts": alerts, "send": False}, ensure_ascii=False))
        return 0
    webhook = os.environ.get("DINGTALK_WEBHOOK_URL", "")
    secret = os.environ.get("DINGTALK_WEBHOOK_SECRET", "")
    if not webhook or not secret:
        print("未配置钉钉机器人凭据", file=sys.stderr)
        return 2
    send_markdown(webhook, secret, "商机雷达质量告警", text)
    print(f"质量告警已发送：{len(alerts)} 项")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
