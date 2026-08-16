"""Digest rendering + delivery (Blueprint §14).

Rendering is pure (unit-tested). Delivery goes through SMTP when SMTP_HOST
is configured; otherwise falls back to a filesystem OUTBOX (dev mode) so
the whole flow is testable without credentials. SendGrid slots in later
behind the same send_email() signature (§18).

Content policy (§2.4/§17): the email carries summaries + links to the
original source only — never document attachments or full reproductions.
Unverified closing dates are labelled "verify at source" (§10.3).
"""

from __future__ import annotations

import os
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from tenderza.alerts.matcher import closing_verified
from tenderza.timeutil import SAST


def _fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "no date published"
    return dt.astimezone(SAST).strftime("%d %b %Y, %H:%M SAST")


def render_digest(alert: dict[str, Any], tenders: list[dict[str, Any]],
                  *, base_url: str = "") -> tuple[str, str]:
    """(subject, plain-text body) for one alert's digest."""
    n = len(tenders)
    label = (alert.get("keywords") or "your saved search").strip() or "your saved search"
    subject = f"TenderZA: {n} new tender{'s' if n != 1 else ''} for “{label}”"

    lines = [
        f"Hi{',' if not alert.get('user_name') else ' ' + alert['user_name'] + ','}",
        "",
        f"{n} new tender{'s match' if n != 1 else ' matches'} your alert "
        f"({label}):",
        "",
    ]
    for t in tenders:
        num = f"{t['tender_number']} — " if t.get("tender_number") else ""
        lines.append(f"• {num}{t['title']}")
        buyer = t.get("buyer_name") or "Unknown buyer"
        prov = t.get("province") or ""
        lines.append(f"  {buyer}{' · ' + prov if prov else ''}")
        if closing_verified(t):
            closes = _fmt_dt(t.get("closing_at"))
            lines.append(f"  Closes: {closes}")
        else:
            lines.append("  Closing date UNVERIFIED — verify at source before relying on it")
        if t.get("compulsory_briefing"):
            lines.append("  ⚠ COMPULSORY briefing — missing it disqualifies your bid")
        detail = f"{base_url}/tender/{t['id']}" if base_url else (t.get("original_url") or "")
        if detail:
            lines.append(f"  {detail}")
        lines.append("")

    lines += [
        "—",
        "TenderZA aggregates official sources and always links to the original.",
        "eTender OCDS data © National Treasury, CC BY 4.0.",
        "Manage your alerts: " + (f"{base_url}/alerts" if base_url else "TenderZA → Alerts"),
    ]
    return subject, "\n".join(lines)


def send_email(to: str, subject: str, body: str) -> str:
    """Send via SMTP if configured, else write to the dev outbox.

    Returns a delivery ref: 'smtp:<msgid>' or 'outbox:<path>'.
    """
    host = os.environ.get("SMTP_HOST")
    sender = os.environ.get("SMTP_FROM", "alerts@tenderza.example")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)

    if host:
        port = int(os.environ.get("SMTP_PORT", "587"))
        user = os.environ.get("SMTP_USER")
        password = os.environ.get("SMTP_PASSWORD")
        with smtplib.SMTP(host, port) as smtp:
            smtp.starttls()
            if user and password:
                smtp.login(user, password)
            smtp.send_message(msg)
        return f"smtp:{msg['Message-Id'] or 'sent'}"

    outbox = Path(os.environ.get("ALERT_OUTBOX", "./data/outbox"))
    outbox.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    safe_to = to.replace("@", "_at_").replace("/", "_")
    path = outbox / f"{stamp}_{safe_to}.eml"
    path.write_text(str(msg))
    return f"outbox:{path}"
