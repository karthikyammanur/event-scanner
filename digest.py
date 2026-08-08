"""Email digest composition and delivery over Gmail SMTP.

Two standing rules from the spec are enforced mechanically here rather than by
care alone:

  1. No em dash appears anywhere in generated text, subject lines included.
     `scrub()` runs over every outgoing string and `assert_no_em_dash()` is
     checked in the tests.
  2. An out-of-Texas in-person event with no stated travel support is flagged,
     never dropped. The flag is derived at render time from location plus
     travel_credit_mentioned, exactly as the data contract requires.
"""

from __future__ import annotations

import html
import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Iterable, List, Optional

from models import Event

log = logging.getLogger(__name__)

# Em dash, en dash, and the horizontal bar, plus the "space dash space" form
# that reads as one. Replaced with punctuation Karthik actually wants.
_DASHES = {
    "—": ",",   # em dash
    "–": "-",   # en dash, safe as a plain hyphen in ranges
    "―": ",",   # horizontal bar
}


def scrub(text: Optional[str]) -> str:
    """Remove em dashes from any text the tool generates."""
    if not text:
        return ""
    out = text
    for bad, good in _DASHES.items():
        out = out.replace(bad, good)
    return out


def assert_no_em_dash(text: str) -> None:
    for bad in ("—", "―"):
        if bad in text:
            raise AssertionError(f"em dash leaked into generated text: {text[:120]!r}")


TYPE_LABELS = {
    "hackathon": "Hackathon",
    "summit": "Summit",
    "insight_program": "Insight program",
    "fellowship": "Fellowship",
    "externship": "Externship",
    "conference": "Conference",
    "other": "Program",
}


def _sort_key(ev: Event):
    """Internship-leading events first, then soonest deadline, then name.

    The spec puts internship-leading programs above general networking events.
    """
    priority = {
        "insight_program": 0,
        "externship": 0,
        "fellowship": 1,
        "hackathon": 2,
        "summit": 2,
        "conference": 3,
        "other": 4,
    }.get(ev.event_type, 4)
    deadline = ev.application_deadline or ev.start_date or "9999-12-31"
    return (priority, deadline, ev.event_name.lower())


def subject_line(events: List[Event]) -> str:
    n = len(events)
    noun = "event" if n == 1 else "events"
    subject = f"Event Scanner: {n} new student tech {noun}"
    subject = scrub(subject)
    assert_no_em_dash(subject)
    return subject


def _when(ev: Event) -> str:
    bits = []
    if ev.start_date:
        bits.append(f"Starts {ev.start_date}")
    if ev.application_deadline:
        bits.append(f"Apply by {ev.application_deadline}")
    return ", ".join(bits) if bits else "Dates not stated"


def _place(ev: Event) -> str:
    if ev.is_virtual():
        return "Virtual"
    return ev.location_city_state or "Location not stated"


def _travel_note(ev: Event) -> Optional[str]:
    if not ev.needs_travel_flag():
        return None
    if ev.travel_credit_mentioned is False:
        return "Out of state, posting says no travel support"
    if not ev.location_city_state:
        return "Location not stated, travel support unknown"
    return "Out of state, no travel support mentioned"


def render_text(events: List[Event]) -> str:
    events = sorted(events, key=_sort_key)
    lines = [
        f"{len(events)} new event(s) found since the last run.",
        "",
    ]
    for ev in events:
        lines.append(scrub(f"{ev.event_name} ({ev.company})"))
        lines.append(f"  Type: {TYPE_LABELS.get(ev.event_type, 'Program')}")
        lines.append(f"  When: {_when(ev)}")
        lines.append(f"  Where: {scrub(_place(ev))}")
        note = _travel_note(ev)
        if note:
            lines.append(f"  Note: {note}")
        lines.append(f"  Link: {ev.url}")
        lines.append(f"  Found via: {ev.source}")
        lines.append("")

    lines.append("Flagged entries are still worth a look, they are listed")
    lines.append("because travel support was not stated, not because they are")
    lines.append("out of reach.")
    body = "\n".join(lines)
    body = scrub(body)
    assert_no_em_dash(body)
    return body


def render_html(events: List[Event]) -> str:
    events = sorted(events, key=_sort_key)
    parts = [
        "<div style=\"font-family:system-ui,Segoe UI,Arial,sans-serif;"
        "max-width:640px;color:#111;line-height:1.5\">",
        f"<p>{len(events)} new event(s) found since the last run.</p>",
    ]
    for ev in events:
        note = _travel_note(ev)
        parts.append(
            "<div style=\"border:1px solid #e2e2e2;border-radius:8px;"
            "padding:12px 14px;margin:0 0 12px\">"
        )
        parts.append(
            f"<div style=\"font-weight:600;font-size:15px\">"
            f"{html.escape(scrub(ev.event_name))}</div>"
        )
        parts.append(
            f"<div style=\"color:#555;font-size:13px;margin-bottom:6px\">"
            f"{html.escape(scrub(ev.company))} &middot; "
            f"{html.escape(TYPE_LABELS.get(ev.event_type, 'Program'))}</div>"
        )
        parts.append(
            f"<div style=\"font-size:13px\">{html.escape(_when(ev))}</div>"
        )
        parts.append(
            f"<div style=\"font-size:13px\">{html.escape(scrub(_place(ev)))}</div>"
        )
        if note:
            parts.append(
                "<div style=\"font-size:13px;color:#8a5300;background:#fff6e5;"
                "border-radius:4px;padding:4px 8px;margin-top:6px\">"
                f"{html.escape(note)}</div>"
            )
        parts.append(
            f"<div style=\"margin-top:8px\"><a href=\"{html.escape(ev.url)}\">"
            "Open listing</a>"
            f"<span style=\"color:#888;font-size:12px\"> (via "
            f"{html.escape(ev.source)})</span></div>"
        )
        parts.append("</div>")

    parts.append(
        "<p style=\"color:#555;font-size:12px\">Flagged entries are still worth "
        "a look, they are listed because travel support was not stated, not "
        "because they are out of reach.</p>"
    )
    parts.append("</div>")
    body = scrub("".join(parts))
    assert_no_em_dash(body)
    return body


def send(events: List[Event], dry_run: bool = False) -> bool:
    """Send the digest. Returns True when delivery succeeded (or was skipped)."""
    if not events:
        log.info("no new events, no email sent")
        return True

    subject = subject_line(events)
    text = render_text(events)
    html_body = render_html(events)

    if dry_run:
        print("=" * 70)
        print(f"SUBJECT: {subject}")
        print("=" * 70)
        print(text)
        return True

    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to_addr = os.environ.get("DIGEST_TO") or user

    if not user or not password:
        log.error("GMAIL_USER or GMAIL_APP_PASSWORD not set, cannot send digest")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to_addr
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as s:
            s.login(user, password)
            s.send_message(msg)
    except Exception:
        log.exception("SMTP delivery failed")
        return False

    log.info("digest sent to %s with %d events", to_addr, len(events))
    return True
