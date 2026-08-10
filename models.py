"""Core data contract for discovered events."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional

EVENT_TYPES = (
    "hackathon",
    "summit",
    "insight_program",
    "fellowship",
    "externship",
    "conference",
    "other",
)


def stable_id(company: str, title: str, url: str) -> str:
    """Stable hash keyed on the URL, which is the one field that does not drift.

    Company and title come from the LLM and get rephrased between runs
    ("JPMorgan Chase" vs "JPMorganChase"), so hashing them re-sends events that
    were already emailed. They are only used as a fallback when there is no URL.
    """
    url_norm = _normalize_url(url)
    basis = url_norm or "|".join([_squash(company).lower(), _squash(title).lower()])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _squash(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


_TRACKING = re.compile(
    r"(?:^|&)(utm_[^=]*|gh_src|ref|source|fbclid|gclid|mc_cid|mc_eid)=[^&]*", re.I
)


def _normalize_url(url: Optional[str]) -> str:
    u = _squash(url).lower()
    if not u:
        return ""
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    if "?" in u:
        base, _, query = u.partition("?")
        query = _TRACKING.sub("", query).strip("&")
        u = f"{base}?{query}" if query else base
    return u.rstrip("/")


_NOISE_WORDS = re.compile(
    r"\b(20\d{2}|program|programme|the|a|an|for|and|of|apply|application|"
    r"applications|now|open|register|registration|software|engineer|"
    r"engineering|student|students|university|college|virtual|online|us|usa)\b"
)


def content_key(company: str, title: str) -> str:
    """Loose key identifying the same event across different listing URLs.

    A JPMorgan Code for Good posting syndicated to three university job boards
    is one event, so the digest should mention it once.
    """
    def squeeze(s: Optional[str]) -> str:
        s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
        s = _NOISE_WORDS.sub(" ", s)
        return re.sub(r"\s+", "", s)

    t, c = squeeze(title), squeeze(company)
    if not t:
        return ""
    # Company is included only when known, so a missing company does not split
    # one event into two keys.
    return hashlib.sha256(f"{c}|{t}".encode("utf-8")).hexdigest()[:16]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def to_iso_date(value) -> Optional[str]:
    """Normalize a date to YYYY-MM-DD. Accepts ISO strings or epoch millis."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            secs = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(secs, timezone.utc).date().isoformat()
        except (ValueError, OSError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{13}", s):
        return to_iso_date(int(s))
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None


@dataclass
class Event:
    company: str
    event_name: str
    event_type: str
    url: str
    source: str
    start_date: Optional[str] = None
    application_deadline: Optional[str] = None
    date_posted: Optional[str] = None
    location_city_state: Optional[str] = None
    travel_credit_mentioned: Optional[bool] = None
    discovered_at: str = field(default_factory=utcnow_iso)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = stable_id(self.company, self.event_name, self.url)
        if self.event_type not in EVENT_TYPES:
            self.event_type = "other"

    def content_key(self) -> str:
        """Key that matches the same event listed at different URLs."""
        return content_key(self.company, self.event_name)

    def is_virtual(self) -> bool:
        return (self.location_city_state or "").strip().lower() in {
            "virtual",
            "online",
            "remote",
        }

    def is_out_of_texas(self) -> bool:
        if self.is_virtual():
            return False
        loc = (self.location_city_state or "").strip()
        if not loc:
            return True  # unknown location gets flagged, not assumed local
        return not re.search(r"(?:,\s*(?:TX|Texas)\b)|(?:\bTexas\b)", loc, re.I)

    def needs_travel_flag(self) -> bool:
        """Out-of-Texas, in person, and no confirmed travel support."""
        return self.is_out_of_texas() and self.travel_credit_mentioned is not True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in allowed})


@dataclass
class Candidate:
    """A pre-LLM hit from a discovery source.

    Sources emit these cheaply; only survivors of prefilter() reach the LLM.
    """

    company: str
    title: str
    url: str
    source: str
    location: Optional[str] = None
    description: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def key(self) -> str:
        return stable_id(self.company, self.title, self.url)
