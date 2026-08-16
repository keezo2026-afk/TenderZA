#!/usr/bin/env python3
"""Source-health check + pager (Blueprint §15, §16).

    DATABASE_URL=postgresql://... python scripts/check_source_health.py
    ... --notify ops@example.com        # email the alarm digest
    ... --json                          # machine-readable, for a monitor
    ... --quiet                         # print nothing when healthy

Exit codes (so cron/systemd/Nagios can act on it):
    0  all clear (or only non-paging states)
    1  one or more sources need human action
    2  could not run the check (no DATABASE_URL, DB unreachable)

This is the "dashboard must page someone" requirement (§15) made real: it
reads the same verdicts the /ops endpoints serve, so the pager and the
dashboard can never disagree. Run it every 15 minutes alongside the crawler.

Notification uses the alerts emailer (SMTP when configured, dev outbox
otherwise), so ops paging needs no extra infrastructure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import psycopg

from tenderza.health import should_page, summarise_verdicts
from tenderza.health.queries import crawl_stats, mttd_mttr_report, source_verdicts

_ICON = {
    "OK": "🟢",
    "STALE": "🟡",
    "DEGRADED": "🟠",
    "FAILED": "🔴",
    "NEVER_RUN": "⚫",
    "DISCOVERY": "🔵",
    "PUBLISH_NOTHING": "⚪",
}


def render_report(summary: dict, verdicts: list, crawl: dict,
                  reliability: dict, *, show_all: bool) -> str:
    lines: list[str] = []
    pct = summary["healthy_pct"]
    lines.append("TenderZA — source health")
    lines.append(
        f"  {summary['healthy']}/{summary['crawlable']} crawlable sources healthy"
        + (f" ({pct}%)" if pct is not None else "")
        + (f"  [target ≥{summary['sla_target_pct']}%"
           f" — {'MET' if summary['sla_met'] else 'BELOW TARGET'}]"
           if pct is not None else "")
    )
    states = ", ".join(f"{k} {v}" for k, v in sorted(summary["by_state"].items()))
    lines.append(f"  states: {states}")
    lines.append(
        f"  24h: {crawl['jobs_total']} jobs"
        + (f", {crawl['job_success_pct']}% success" if crawl["job_success_pct"] is not None else "")
        + f", {crawl['tenders_created']} new / {crawl['tenders_updated']} updated tenders"
        + f", {crawl['review_queue_open']} review items open"
    )
    if reliability["episodes"]:
        lines.append(
            f"  30d reliability: {reliability['episodes']} breakage episode(s), "
            f"{reliability['open']} open"
            + (f", avg MTTR {reliability['avg_mttr_min']:.0f} min"
               if reliability["avg_mttr_min"] else "")
        )

    shown = verdicts if show_all else [v for v in verdicts if v.page]
    if shown:
        lines.append("")
        header = "ALARMS — action required:" if not show_all else "All sources:"
        lines.append(header)
        for v in shown:
            icon = _ICON.get(v.state, "•")
            tier = f"T{v.tier}" if v.tier else "T?"
            age = (f"{v.minutes_since_success:.0f} min ago"
                   if v.minutes_since_success is not None else "never")
            lines.append(f"  {icon} [{tier}] {v.name} — {v.state} (last success: {age})")
            lines.append(f"      {v.reason}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--notify", metavar="EMAIL",
                    help="email the alarm digest to this address when paging")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    ap.add_argument("--all", action="store_true", help="list every source, not just alarms")
    ap.add_argument("--quiet", action="store_true", help="print nothing when healthy")
    args = ap.parse_args()

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2

    try:
        conn = psycopg.connect(dsn)
    except psycopg.Error as exc:
        print(f"database unreachable: {exc}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    with conn:
        verdicts = source_verdicts(conn, now=now)
        summary = summarise_verdicts(verdicts)
        crawl = crawl_stats(conn, hours=24)
        reliability = mttd_mttr_report(conn, days=30)

    paging = should_page(verdicts)
    ordered = sorted(verdicts, key=lambda v: (not v.page, v.name))

    if args.json:
        print(json.dumps({
            "generated_at": now.isoformat(),
            "summary": summary,
            "crawl_24h": crawl,
            "reliability": reliability,
            "alarms": [v.as_dict() for v in paging],
        }, indent=2, default=str))
    elif paging or args.all or not args.quiet:
        print(render_report(summary, ordered, crawl, reliability, show_all=args.all))

    if paging and args.notify:
        from tenderza.alerts.emailer import send_email

        body = render_report(summary, ordered, crawl, reliability, show_all=False)
        subject = (f"[TenderZA] {len(paging)} source(s) need attention — "
                   f"{paging[0].state}: {paging[0].name}")
        ref = send_email(args.notify, subject, body)
        print(f"[notify] {ref}")

    return 1 if paging else 0


if __name__ == "__main__":
    raise SystemExit(main())
