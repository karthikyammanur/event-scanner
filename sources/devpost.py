"""Devpost hackathon feed."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional

from models import Candidate

from .base import Budget, Context, get_json

log = logging.getLogger(__name__)

API = "https://devpost.com/api/hackathons"
MAX_PAGES = 6  # 9 per page; enough for the open, student-relevant front of the list


def _parse_period(text: Optional[str], year_hint: int) -> Optional[str]:
    """Turn 'May 19 - Aug 17, 2026' or 'Aug 17, 2026' into an ISO start date."""
    if not text:
        return None
    s = text.strip()
    year_m = re.search(r"\b(20\d{2})\b", s)
    year = int(year_m.group(1)) if year_m else year_hint
    first = re.split(r"\s*[-–]\s*", s)[0]
    m = re.match(r"([A-Za-z]{3,9})\s+(\d{1,2})", first.strip())
    if not m:
        return None
    month_name, day = m.group(1)[:3].lower(), int(m.group(2))
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    if month_name not in months:
        return None
    try:
        return datetime(year, months[month_name], day).date().isoformat()
    except ValueError:
        return None


def discover(ctx: Context) -> List[Candidate]:
    budget = Budget(ctx.per_source_budget_s)
    out: List[Candidate] = []
    seen = set()

    for page in range(1, MAX_PAGES + 1):
        if budget.expired():
            log.warning("devpost hit time budget at page %d", page)
            break
        data = get_json(f"{API}?page={page}", timeout=ctx.request_timeout)
        if not isinstance(data, dict):
            break
        rows = data.get("hackathons") or []
        if not rows:
            break

        for h in rows:
            if not isinstance(h, dict):
                continue
            title = (h.get("title") or "").strip()
            url = (h.get("url") or "").strip()
            if not title or not url or url in seen:
                continue
            seen.add(url)

            # Invite-only events are not actionable for a student cold applying.
            if h.get("invite_only"):
                continue

            loc = h.get("displayed_location") or {}
            location = (loc.get("location") if isinstance(loc, dict) else None) or ""

            org = (h.get("organization_name") or "").strip()
            themes = ", ".join(
                t.get("name", "") for t in (h.get("themes") or []) if isinstance(t, dict)
            )

            out.append(
                Candidate(
                    company=org or title,
                    title=title,
                    url=url if url.startswith("http") else f"https:{url}",
                    source="devpost",
                    location=location,
                    description=themes,
                    extra={
                        # Devpost is an event-only feed, so prefilter does not
                        # require an event keyword in the title.
                        "source_is_event_feed": True,
                        "open_state": h.get("open_state"),
                        "start_date": _parse_period(
                            h.get("submission_period_dates"), ctx.year
                        ),
                        "prize_amount": re.sub(r"<[^>]+>", "", h.get("prize_amount") or ""),
                    },
                )
            )

    log.info("devpost: %d candidates", len(out))
    return out
