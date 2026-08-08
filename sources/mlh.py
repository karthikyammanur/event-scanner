"""MLH season events.

Open item from the spec ("find MLH's lazy-loaded endpoint"), now RESOLVED
differently than expected (Aug 7 2026): the season page is NOT lazy loaded from
a hidden API. Everything is server rendered, so plain requests is enough. No
headless browser, no internal endpoint.

The page carries the full season as an embedded JSON array (escaped inside the
framework payload), which is richer than the schema.org microdata also present:

  {"id":..., "slug":"hackdavis-2026-hackathon", "name":"HackDavis 2026 Hackathon",
   "status":"ended", "startsAt":"2026-05-09T11:30:00Z", "dateRange":"MAY 09 - 10",
   "location":"Davis, California", "formatType":"physical",
   "websiteUrl":"https://hackdavis.io", "region":"NA",
   "venueAddress":{"city":"Davis","state":"California","country":"US"}}

We parse that blob for real event names and use venueAddress.country for clean
US filtering, since MLH is heavily international and only US events are in
scope. The microdata parser is kept as a fallback if the blob shape changes.
"""

from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser
from typing import Dict, List, Optional

from models import Candidate

from .base import Context, get

log = logging.getLogger(__name__)

SEASON_URLS = [
    "https://mlh.com/seasons/{year}/events",
    "https://mlh.com/seasons/{next_year}/events",
]


class _EventMicrodataParser(HTMLParser):
    """Collect schema.org/Event blocks from the season page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: List[Dict[str, str]] = []
        self._cur: Optional[Dict[str, str]] = None
        self._depth = 0
        self._capture_name = False
        self._name_buf: List[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        itemtype = (a.get("itemtype") or "").lower()

        if "schema.org/event" in itemtype:
            self._cur = {}
            self._depth = 1
            if a.get("href"):
                self._cur["href"] = a["href"]
            return

        if self._cur is None:
            return

        self._depth += 1
        prop = a.get("itemprop")
        if prop and a.get("content"):
            # Nested Place also has a "name"/"url"; keep the first Event level
            # value by not overwriting an existing key.
            self._cur.setdefault(prop, a["content"])
        if prop == "name" and not a.get("content"):
            self._capture_name = True
            self._name_buf = []

    def handle_data(self, data):
        if self._capture_name:
            self._name_buf.append(data)

    def handle_endtag(self, tag):
        if self._cur is None:
            return
        if self._capture_name:
            text = "".join(self._name_buf).strip()
            if text:
                self._cur.setdefault("location_name", text)
            self._capture_name = False
        self._depth -= 1
        if self._depth <= 0:
            if self._cur:
                self.events.append(self._cur)
            self._cur = None


def _title_from(ev: Dict[str, str]) -> str:
    for k in ("name", "title"):
        if ev.get(k):
            return ev[k].strip()
    # Fall back to the event slug in the URL.
    url = ev.get("url") or ev.get("href") or ""
    slug = url.rstrip("/").split("/")[-1].split("?")[0]
    return slug.replace("-", " ").replace("_", " ").title() or "MLH Event"


_EVENT_OBJ = re.compile(
    r'\{"id":"[0-9a-f-]{8,}","slug":"[^"]+","name":".*?"venueAddress":\{.*?\}\}',
    re.S,
)


def _extract_embedded_events(html: str) -> List[Dict]:
    """Pull the embedded season JSON objects out of the page payload.

    The blob lives inside an escaped framework string, so backslash escapes are
    unescaped first. Each object is parsed independently: one malformed record
    must not lose the whole season.
    """
    text = html.replace("\\/", "/").replace('\\"', '"')
    out: List[Dict] = []
    for m in _EVENT_OBJ.finditer(text):
        try:
            rec = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("name"):
            out.append(rec)
    return out


def _from_embedded(rec: Dict) -> Optional[Candidate]:
    venue = rec.get("venueAddress") or {}
    country = str(venue.get("country") or "").upper()
    fmt = str(rec.get("formatType") or "").lower()
    is_virtual = fmt in {"digital", "online", "virtual", "hybrid"}

    # Only US events are in scope. Virtual events often carry no country, so
    # absence is not a rejection.
    if country and country not in {"US", "USA"} and not is_virtual:
        return None

    city = str(venue.get("city") or "").strip()
    state = str(venue.get("state") or "").strip()
    location = ", ".join(p for p in (city, state) if p) or (
        "virtual" if is_virtual else str(rec.get("location") or "").strip()
    )

    url = (rec.get("websiteUrl") or "").strip()
    if not url:
        slug = rec.get("slug") or ""
        url = f"https://mlh.com/events/{slug}" if slug else ""
    if not url:
        return None

    return Candidate(
        company="Major League Hacking",
        title=str(rec.get("name")).strip(),
        url=url,
        source="mlh",
        location=location,
        extra={
            "source_is_event_feed": True,
            "start_date": (rec.get("startsAt") or "")[:10] or None,
            "status": rec.get("status"),
        },
    )


def discover(ctx: Context) -> List[Candidate]:
    out: List[Candidate] = []
    seen = set()

    for template in SEASON_URLS:
        url = template.format(year=ctx.year, next_year=ctx.year + 1)
        resp = get(url, timeout=ctx.request_timeout)
        if resp is None:
            continue

        # Preferred path: the embedded season JSON, which has real event names.
        embedded = _extract_embedded_events(resp.text)
        for rec in embedded:
            cand = _from_embedded(rec)
            if cand is None or cand.url in seen:
                continue
            # Skip events that already finished.
            if str(rec.get("status", "")).lower() in {"ended", "cancelled", "canceled"}:
                continue
            seen.add(cand.url)
            out.append(cand)

        if embedded:
            continue  # blob parsed fine, no need for the microdata fallback

        log.warning("mlh: embedded JSON not found for %s, falling back to microdata", url)
        parser = _EventMicrodataParser()
        try:
            parser.feed(resp.text)
        except Exception:
            log.exception("mlh: microdata parse failed for %s", url)
            continue

        for ev in parser.events:
            link = ev.get("url") or ev.get("href") or ""
            if not link or link in seen:
                continue

            country = (ev.get("addressCountry") or "").strip().upper()
            # MLH is heavily international; only US events are in scope. Virtual
            # events often carry no country, so absence is not a rejection.
            if country and country not in {"US", "USA"}:
                continue
            seen.add(link)

            city = (ev.get("addressLocality") or "").strip()
            region = (ev.get("addressRegion") or "").strip()
            location = ", ".join(p for p in (city, region) if p) or ev.get(
                "location_name", ""
            )
            attendance = (ev.get("eventAttendanceMode") or "").lower()
            if "online" in attendance and not location:
                location = "virtual"

            out.append(
                Candidate(
                    company="Major League Hacking",
                    title=_title_from(ev),
                    url=link,
                    source="mlh",
                    location=location,
                    extra={
                        "source_is_event_feed": True,
                        "start_date": (ev.get("startDate") or "")[:10] or None,
                    },
                )
            )

    log.info("mlh: %d US candidates", len(out))
    return out
