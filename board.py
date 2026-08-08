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
    ev = Event(
        company=rec.get("company") or "",
        event_name=rec.get("event_name") or "",
        event_type=rec.get("event_type") or "other",
        url=rec.get("url") or "",
        source=rec.get("source") or "",
        location_city_state=rec.get("location_city_state"),
        travel_credit_mentioned=rec.get("travel_credit_mentioned"),
    )
    return " ⚑" if ev.needs_travel_flag() else ""


def _sort_key(rec: dict):
    # Newest first by when we found it, then by posting date.
    return (rec.get("emailed_at") or "", rec.get("date_posted") or "")


def render_table(seen: Dict[str, dict]) -> str:
    rows = [r for r in seen.values() if isinstance(r, dict) and r.get("event_name")]
    rows.sort(key=_sort_key, reverse=True)

    if not rows:
        return "No events found yet. The scanner runs every 4 hours."

    out = [
        f"**{len(rows)} events found.** Updated "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.",
        "",
        "⚑ means out of state with no travel support mentioned, still worth a look.",
        "",
        "| Event | Company | Type | Location | Posted | Deadline | Apply |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        name = _cell(r.get("event_name"))
        url = (r.get("url") or "").strip()
        link = f"[Apply]({url})" if url.startswith("http") else "-"
        out.append(
            "| {name}{flag} | {company} | {etype} | {loc} | {posted} | {deadline} | {link} |".format(
                name=name,
                flag=_flag(r),
                company=_cell(r.get("company")),
                etype=_cell(TYPE_LABELS.get(r.get("event_type"), "Program")),
                loc=_cell(r.get("location_city_state")),
                posted=_cell(r.get("date_posted")),
                deadline=_cell(r.get("application_deadline")),
                link=link,
            )
        )
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
