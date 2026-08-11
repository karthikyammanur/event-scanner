"""Email digest composition and delivery over Gmail SMTP."""

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

# No em dashes in anything Karthik reads. See CLAUDE.md.
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
    """Internship-leading events first, then soonest deadline, then name."""
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
    n = len(events)
    lines = [
        "NEW TECH EVENTS FOR YOU",
        f"{n} new {'event' if n == 1 else 'events'} since the last check.",
    ]
    for label, group in _group(events):
        lines.append("")
        lines.append(f"{label.upper()} ({len(group)})")
        lines.append("-" * 58)
        for ev in group:
            lines.append("")
            lines.append(scrub(ev.event_name))
            lines.append(
                scrub(f"{ev.company} - {TYPE_LABELS.get(ev.event_type, 'Program')}")
            )
            lines.append(f"  When   : {_when(ev)}")
            lines.append(f"  Where  : {scrub(_place(ev))}")
            lines.append(f"  Posted : {ev.date_posted or 'Not stated'}")
            note = _travel_note(ev)
            if note:
                lines.append(f"  Note   : {note}")
            lines.append(f"  Apply  : {ev.url}")

    if any(e.needs_travel_flag() for e in events):
        lines.append("")
        lines.append("-" * 58)
        lines.append("Flagged entries are out of state with no travel support")
        lines.append("mentioned. They are shown because the listing was silent")
        lines.append("on it, not because they are out of reach.")

    body = scrub("\n".join(lines))
    assert_no_em_dash(body)
    return body


# Accent per event type, so the eye can sort the digest at a glance.
_TYPE_COLOR = {
    "insight_program": "#1d6f42",
    "externship": "#1d6f42",
    "fellowship": "#7048a8",
    "hackathon": "#0b5fa5",
    "summit": "#0b5fa5",
    "conference": "#8a5300",
    "other": "#555555",
}

_SECTIONS = (
    ("insight_program", "Internship pathways"),
    ("externship", "Internship pathways"),
    ("fellowship", "Fellowships"),
    ("hackathon", "Hackathons and competitions"),
    ("summit", "Summits and conferences"),
    ("conference", "Summits and conferences"),
    ("other", "Other programs"),
)


def _group(events: List[Event]):
    """Group events into display sections, preserving section order."""
    order, buckets = [], {}
    for etype, label in _SECTIONS:
        if label not in buckets:
            buckets[label] = []
            order.append(label)
    for ev in events:
        label = dict(_SECTIONS).get(ev.event_type, "Other programs")
        buckets[label].append(ev)
    return [(lbl, buckets[lbl]) for lbl in order if buckets[lbl]]


def _esc(text: Optional[str]) -> str:
    return html.escape(scrub(text or ""))


def _event_card(ev: Event) -> str:
    accent = _TYPE_COLOR.get(ev.event_type, "#555555")
    note = _travel_note(ev)

    meta_rows = [
        ("When", _when(ev)),
        ("Where", _place(ev)),
        ("Posted", ev.date_posted or "Not stated"),
    ]
    meta = "".join(
        "<tr>"
        f"<td style=\"padding:2px 12px 2px 0;color:#6b7280;font-size:13px;"
        f"white-space:nowrap;vertical-align:top\">{_esc(label)}</td>"
        f"<td style=\"padding:2px 0;color:#111827;font-size:13px\">{_esc(value)}</td>"
        "</tr>"
        for label, value in meta_rows
    )

    flag = (
        "<tr><td colspan=\"2\" style=\"padding-top:8px\">"
        "<div style=\"font-size:12px;color:#92400e;background:#fef3c7;"
        "border-radius:6px;padding:6px 10px\">"
        f"{_esc(note)}</div></td></tr>"
        if note
        else ""
    )

    return (
        "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" "
        "width=\"100%\" style=\"border-collapse:separate;margin:0 0 14px\">"
        "<tr><td style=\"background:#ffffff;border:1px solid #e5e7eb;"
        f"border-left:4px solid {accent};border-radius:8px;padding:14px 16px\">"
        f"<div style=\"font-size:16px;font-weight:600;color:#111827;"
        f"line-height:1.35\">{_esc(ev.event_name)}</div>"
        f"<div style=\"font-size:13px;color:{accent};font-weight:600;"
        f"margin:3px 0 10px\">{_esc(ev.company)}"
        f"<span style=\"color:#9ca3af;font-weight:400\"> &middot; "
        f"{_esc(TYPE_LABELS.get(ev.event_type, 'Program'))}</span></div>"
        "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\">"
        f"{meta}{flag}</table>"
        "<div style=\"margin-top:12px\">"
        f"<a href=\"{html.escape(ev.url)}\" "
        f"style=\"display:inline-block;background:{accent};color:#ffffff;"
        "text-decoration:none;font-size:13px;font-weight:600;"
        "padding:8px 16px;border-radius:6px\">View and apply</a>"
        f"<span style=\"color:#9ca3af;font-size:12px;padding-left:10px\">"
        f"via {_esc(ev.source)}</span>"
        "</div></td></tr></table>"
    )


def render_html(events: List[Event]) -> str:
    events = sorted(events, key=_sort_key)
    n = len(events)
    flagged = sum(1 for e in events if e.needs_travel_flag())

    parts = [
        "<div style=\"background:#f3f4f6;padding:24px 12px;"
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
        "Helvetica,Arial,sans-serif\">",
        "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" "
        "align=\"center\" width=\"100%\" style=\"max-width:600px;margin:0 auto\">",
        # Header
        "<tr><td style=\"padding:0 0 18px\">"
        "<div style=\"font-size:20px;font-weight:700;color:#111827\">"
        "New tech events for you</div>"
        f"<div style=\"font-size:14px;color:#6b7280;margin-top:4px\">"
        f"{n} new {'event' if n == 1 else 'events'} since the last check</div>"
        "</td></tr>",
    ]

    for label, group in _group(events):
        parts.append(
            "<tr><td style=\"padding:6px 0 8px\">"
            "<div style=\"font-size:12px;font-weight:700;color:#6b7280;"
            "letter-spacing:.06em;text-transform:uppercase\">"
            f"{_esc(label)} ({len(group)})</div></td></tr>"
        )
        parts.append("<tr><td>")
        parts.extend(_event_card(ev) for ev in group)
        parts.append("</td></tr>")

    if flagged:
        parts.append(
            "<tr><td style=\"padding:6px 0 0\">"
            "<div style=\"font-size:12px;color:#6b7280;line-height:1.6;"
            "border-top:1px solid #e5e7eb;padding-top:12px\">"
            "Highlighted entries are out of state with no travel support "
            "mentioned. They are shown because the listing was silent on it, "
            "not because they are out of reach."
            "</div></td></tr>"
        )

    parts.append(
        "<tr><td style=\"padding:14px 0 0\">"
        "<div style=\"font-size:11px;color:#9ca3af\">"
        "Sent by your event scanner. Runs every 4 hours."
        "</div></td></tr>"
    )
    parts.append("</table></div>")

    body = scrub("".join(parts))
    assert_no_em_dash(body)
    return body


def send(events: List[Event], dry_run: bool = False) -> bool:
    """Send the digest. Returns True when delivery succeeded (or was skipped)."""
    # A past event is still recorded and still shown in the board archive, it
    # just does not belong in an email telling him what to apply to.
    stale = [e for e in events if e.freshness() == "past"]
    if stale:
        log.info("holding back %d past event(s) from the digest", len(stale))
    events = [e for e in events if e.freshness() != "past"]

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
