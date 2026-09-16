"""BadgeUp: a curated GitHub list of conferences and summits undergrads can attend.

The ATS boards, Devpost, and MLH structurally cannot surface summits: a summit
lives on its own conference site, not on a company job board, which is why the
digest was nearly all hackathons. This source fills that gap.

The data lives in Markdown tables in the repo README, one table per discipline
section. `web/lib/conferences.ts` looks like a tidier source but is a build
artifact of the site, so the README is the contract (the same trap as
speedyapply's TS build scripts, see CLAUDE.md).

Row shape, eight cells:

    | Status | Conference | Focus | Dates & Location | Access | Audience |
      Register / Apply | Deadline |

"Dates & Location" is a human string ("Oct 28-29, 2026 · San Francisco, CA"),
and the register cell is an HTML anchor wrapping a shields.io badge image, so
both need parsing rather than a straight read.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import List, Optional, Tuple

from models import Candidate

from .base import Budget, Context, get

log = logging.getLogger(__name__)

README = "https://raw.githubusercontent.com/Jose-Gael-Cruz-Lopez/BadgeUp/main/README.md"

# Sections whose events are tech relevant for a CS upperclassman. The list also
# covers law, medicine, and the arts, which are out of scope here.
TECH_SECTIONS = {
    "tech & computing",
    "engineering",
    "multidisciplinary / general",
}

# Identity sections are where the big tech recruiting conferences live (GHC,
# NSBE, SHPE, ColorStack, AfroTech), but they also carry medical and legal
# conventions (LMSA, SNMA, Lavender Law). Names alone cannot tell those apart:
# "SHPE National Convention" and "LMSA National Conference" read identically to
# a keyword matcher. So these sections are admitted only when the row's own
# focus text shows a tech or engineering signal.
MIXED_SECTIONS = {
    "business & finance",
    "sciences & research",
    "women in their field",
    "black professionals & students",
    "latinx professionals & students",
    "lgbtq+ in their field",
}

# Focus-cell signals that mark a row in a mixed section as in scope.
_TECH_FOCUS = re.compile(
    r"tech|comput|engineer|software|develop|\bdata\b|\bai\b|machine learning|"
    r"cloud|cyber|security|quantum|\bstem\b|robotic|informatics|\bhpc\b|"
    r"scientists? & engineers|startup",
    re.I,
)

# The table above the event tables explains the badges, so it has to be skipped.
_HEADER_CELLS = {"status", "conference", "focus", "dates & location"}

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# The register cell is <a href="URL"><img .../></a>, so take the anchor target.
_HREF = re.compile(r'href=["\']([^"\']+)["\']', re.I)
_TAGS = re.compile(r"<[^>]+>")
# Status badges and section markers carry emoji that are noise in an event name.
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F900-\U0001F9FF️]+"
)


def _clean(cell: str) -> str:
    """Strip HTML, markdown bold, and emoji from a table cell."""
    text = _TAGS.sub(" ", cell or "")
    text = text.replace("**", "").replace("`", "")
    text = _EMOJI.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _split_row(line: str) -> List[str]:
    """Split a Markdown table row into cells.

    Escaped pipes inside a cell would otherwise split it, and the leading and
    trailing pipes produce empty edge cells that are dropped.
    """
    parts = re.split(r"(?<!\\)\|", line.strip())
    if parts and not parts[0].strip():
        parts = parts[1:]
    if parts and not parts[-1].strip():
        parts = parts[:-1]
    return [p.replace(r"\|", "|") for p in parts]


def _is_separator(cells: List[str]) -> bool:
    return all(re.fullmatch(r":?-{2,}:?", c.strip() or "-") for c in cells)


def parse_dates_location(text: str, today: Optional[date] = None) -> Tuple[Optional[str], Optional[str]]:
    """Parse "Oct 28-29, 2026 · San Francisco, CA" into (ISO start, location).

    Also handles "May 2027 · Long Beach, CA" (no day), "Nov 30-Dec 4, 2026"
    (spanning two months), and rows that give a location but no usable date.
    """
    raw = _clean(text)
    if not raw:
        return None, None

    # The middot separates the date part from the venue. Some rows use a plain
    # hyphen or an en dash surrounded by spaces instead.
    parts = re.split(r"\s*[·•]\s*", raw, maxsplit=1)
    date_part = parts[0].strip()
    location = parts[1].strip() if len(parts) > 1 else None

    # "TBD 2027" and similar carry no month, so there is no date to report.
    year_m = re.search(r"\b(20\d{2})\b", date_part)
    month_m = re.search(r"\b([A-Za-z]{3,9})\b", date_part)
    if not month_m or month_m.group(1)[:3].lower() not in _MONTHS:
        return None, location or None

    month = _MONTHS[month_m.group(1)[:3].lower()]
    # Take the first day number that follows the month, not any number in the
    # string: "Nov 30-Dec 4, 2026" must start on Nov 30, and a bare "May 2027"
    # has no day at all, so it defaults to the first of the month.
    after_month = date_part[month_m.end():]
    day_m = re.match(r"\s*(\d{1,2})\b", after_month)
    day = int(day_m.group(1)) if day_m else 1

    if year_m:
        year = int(year_m.group(1))
    else:
        # A row with no year means the next occurrence of that month.
        today = today or date.today()
        year = today.year if month >= today.month else today.year + 1

    try:
        return datetime(year, month, day).date().isoformat(), (location or None)
    except ValueError:
        return None, location or None


def parse_deadline(
    text: str, start_date: Optional[str] = None, today: Optional[date] = None
) -> Optional[str]:
    """Pull an ISO application deadline out of the free-text deadline cell.

    The cell is prose ("Application deadline: Sep 21, 2026", "Early bird ends
    Aug 20, 2026; free virtual pass open"), and some rows say only that a
    deadline has passed, which must not be read as a live date.

    A yearless deadline ("convention begins Aug 9") is anchored to the event's
    own year, never rolled forward to the next occurrence. Rolling forward
    turned expired 2026 deadlines into live 2027 ones, which would resurrect a
    finished event as upcoming, the exact failure `freshness()` exists to stop.
    """
    raw = _clean(text)
    if not raw:
        return None
    # "scholarships closed (Mar 2026)" is a past-tense note, not a deadline.
    if re.search(r"\b(closed|passed|ended|not yet|tba|tbd)\b", raw, re.I) and not re.search(
        r"\b(deadline|due|apply by|ends?)\b[^;]*\d", raw, re.I
    ):
        return None

    m = re.search(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:,)?\s*(20\d{2})?", raw)
    if not m or m.group(1)[:3].lower() not in _MONTHS:
        return None
    month = _MONTHS[m.group(1)[:3].lower()]
    day = int(m.group(2))
    if m.group(3):
        year = int(m.group(3))
    elif start_date:
        # A deadline falls on or before the event, so a month later than the
        # event's month belongs to the year before it.
        start_year, start_month = int(start_date[:4]), int(start_date[5:7])
        year = start_year if month <= start_month else start_year - 1
    else:
        return None

    try:
        deadline = datetime(year, month, day).date()
    except ValueError:
        return None
    # A deadline after the event itself means the cell was misread.
    if start_date and deadline.isoformat() > start_date:
        return None
    return deadline.isoformat()


def _event_type(name: str, focus: str) -> str:
    """Classify the row so the digest files it under the right section.

    The LLM re-decides this later; this is the fallback path's answer and a hint.
    """
    blob = f"{name} {focus}".lower()
    if "career fair" in blob or "job fair" in blob:
        return "career_fair"
    if "hackathon" in blob or "datathon" in blob:
        return "hackathon"
    if "summit" in blob:
        return "summit"
    return "conference"


def parse_readme(text: str, today: Optional[date] = None) -> List[Candidate]:
    """Parse every in-scope table row of the README into a Candidate."""
    out: List[Candidate] = []
    seen = set()
    section = ""

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip().lower()
            section = _EMOJI.sub("", section).strip()
            continue
        if not stripped.startswith("|"):
            continue
        if section not in TECH_SECTIONS and section not in MIXED_SECTIONS:
            continue

        cells = _split_row(stripped)
        # Event tables have 8 columns; the badge legend tables have 2.
        if len(cells) < 8 or _is_separator(cells):
            continue
        if _clean(cells[0]).lower() in _HEADER_CELLS:
            continue

        status = _clean(cells[0])
        name = _clean(cells[1])
        focus = _clean(cells[2])
        access = _clean(cells[4])
        audience = _clean(cells[5])
        if not name:
            continue

        href = _HREF.search(cells[6] or "")
        url = href.group(1).strip() if href else ""
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)

        start_date, location = parse_dates_location(cells[3], today=today)
        deadline = parse_deadline(cells[7], start_date=start_date, today=today)

        # Students are the whole point of the list, but a few rows are aimed at
        # professionals only ("Early career · Professionals · Allies").
        if audience and not re.search(r"student|early career|first-gen", audience, re.I):
            continue

        # In a mixed-discipline section, the row has to show its own tech signal.
        if section in MIXED_SECTIONS and not _TECH_FOCUS.search(f"{name} {focus}"):
            continue

        out.append(
            Candidate(
                company=name,
                title=name,
                url=url,
                source="badgeup",
                location=location,
                description=" ".join(filter(None, [focus, access, audience])),
                extra={
                    # A curated conference list is an event-only feed, so the
                    # prefilter does not demand an event keyword in the title.
                    "source_is_event_feed": True,
                    "start_date": start_date,
                    "application_deadline": deadline,
                    "event_type": _event_type(name, focus),
                    "status": status,
                    "section": section,
                },
            )
        )

    return out


def discover(ctx: Context) -> List[Candidate]:
    budget = Budget(ctx.per_source_budget_s)
    if budget.expired():
        return []

    r = get(README, timeout=ctx.request_timeout)
    if r is None:
        log.warning("badgeup: could not fetch the curated list")
        return []

    out = parse_readme(r.text)
    log.info("badgeup: %d candidates", len(out))
    return out
