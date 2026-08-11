"""Render the events table into README.md between marker comments."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from digest import TYPE_LABELS, scrub, assert_no_em_dash
from models import Event

log = logging.getLogger(__name__)

DEFAULT_README = os.path.join(os.path.dirname(os.path.abspath(__file__)), "README.md")

START = "<!-- EVENTS:START -->"
END = "<!-- EVENTS:END -->"


def _cell(text: Optional[str]) -> str:
    """Escape a value so it cannot break out of a markdown table cell."""
    s = scrub(str(text or "")).strip()
    return s.replace("|", "\\|").replace("\n", " ") or "-"


def _flag(rec: dict) -> str:
    """Travel flag, reusing the Event rule so the board and email agree."""
    return " ⚑" if _as_event(rec).needs_travel_flag() else ""


def _sort_key(rec: dict):
    # Newest first by when we found it, then by posting date.
    return (rec.get("emailed_at") or "", rec.get("date_posted") or "")


def _as_event(rec: dict) -> Event:
    return Event(
        company=rec.get("company") or "",
        event_name=rec.get("event_name") or "",
        event_type=rec.get("event_type") or "other",
        url=rec.get("url") or "",
        source=rec.get("source") or "",
        start_date=rec.get("start_date"),
        application_deadline=rec.get("application_deadline"),
        date_posted=rec.get("date_posted"),
        location_city_state=rec.get("location_city_state"),
        travel_credit_mentioned=rec.get("travel_credit_mentioned"),
    )


def _age_cell(rec: dict) -> str:
    """Listing age, in the compact form SimplifyJobs uses (0d, 12d, 3mo)."""
    days = _as_event(rec).age_days()
    if days is None:
        return "-"
    return f"{days // 30}mo" if days > 30 else f"{days}d"


_HEADER = (
    "| Event | Company | Type | Location | Age | Deadline | Apply |\n"
    "| --- | --- | --- | --- | --- | --- | --- |"
)


def _row(rec: dict) -> str:
    url = (rec.get("url") or "").strip()
    link = f"[Apply]({url})" if url.startswith("http") else "-"
    return (
        f"| {_cell(rec.get('event_name'))}{_flag(rec)} "
        f"| {_cell(rec.get('company'))} "
        f"| {_cell(TYPE_LABELS.get(rec.get('event_type'), 'Program'))} "
        f"| {_cell(rec.get('location_city_state'))} "
        f"| {_age_cell(rec)} "
        f"| {_cell(rec.get('application_deadline'))} "
        f"| {link} |"
    )


def render_table(seen: Dict[str, dict]) -> str:
    rows = [r for r in seen.values() if isinstance(r, dict) and r.get("event_name")]
    rows.sort(key=_sort_key, reverse=True)

    if not rows:
        return "No events found yet. The scanner runs every 4 hours."

    current = [r for r in rows if _as_event(r).freshness() != "past"]
    past = [r for r in rows if _as_event(r).freshness() == "past"]

    out = [
        f"**{len(current)} current events.** Updated "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.",
        "",
        "⚑ means out of state with no travel support mentioned, still worth a look.",
        "Age is how long ago the listing was posted. Events whose date has",
        "passed move to the archive at the bottom.",
        "",
    ]
    if current:
        out.append(_HEADER)
        out.extend(_row(r) for r in current)
    else:
        out.append("Nothing open right now. New events are added every 4 hours.")

    if past:
        out.extend(
            [
                "",
                "<details>",
                f"<summary>Past events ({len(past)})</summary>",
                "",
                _HEADER,
            ]
        )
        out.extend(_row(r) for r in past)
        out.extend(["", "</details>"])

    return "\n".join(out)


def update_readme(seen: Dict[str, dict], path: str = DEFAULT_README) -> bool:
    """Rewrite the table between the markers. Returns True when the file changed."""
    try:
        with open(path, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        log.exception("could not read %s", path)
        return False

    if START not in content or END not in content:
        log.error("README markers missing, not touching %s", path)
        return False

    table = render_table(seen)
    assert_no_em_dash(table)

    head, _, rest = content.partition(START)
    _, _, tail = rest.partition(END)
    updated = f"{head}{START}\n\n{table}\n\n{END}{tail}"

    if updated == content:
        return False

    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(updated)
    os.replace(tmp, path)
    log.info("README updated with %d events", len(seen))
    return True
