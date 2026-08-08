"""Company names and ATS board tokens, refreshed each run."""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .base import Context, get_json

log = logging.getLogger(__name__)

SIMPLIFY_FEEDS = [
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
]

SPEEDYAPPLY_FEEDS = [
    "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/speedyapply/2027-SWE-College-Jobs/main/listings.json",
]

# ATS board URL patterns -> (platform, token)
_ATS_PATTERNS = [
    (re.compile(r"boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)", re.I), "greenhouse"),
    (re.compile(r"job-boards\.greenhouse\.io/([a-z0-9_-]+)", re.I), "greenhouse"),
    (re.compile(r"gh_jid=", re.I), None),  # marker only, token unknown
    (re.compile(r"jobs\.lever\.co/([a-z0-9_-]+)", re.I), "lever"),
    (re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]+)", re.I), "ashby"),
]


def _slug(name: str) -> str:
    """Best guess ATS token from a company name.

    Greenhouse/Lever tokens are usually the lowercased name with punctuation
    stripped. Guesses that 404 are simply skipped by the ATS sweep.
    """
    s = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    return s


def detect_ats(url: Optional[str]) -> Optional[Tuple[str, str]]:
    """Return (platform, token) if a URL points at a known ATS board."""
    if not url:
        return None
    for pat, platform in _ATS_PATTERNS:
        if platform is None:
            continue
        m = pat.search(url)
        if m and m.group(1):
            token = m.group(1).lower()
            if token in {"embed", "job_board", "jobs"}:
                continue
            return platform, token
    return None


def load_companies(ctx: Context) -> Tuple[List[str], Dict[str, Tuple[str, str]]]:
    """Return (company_names, {company: (platform, token)}).

    The board map is authoritative where a real ATS URL was seen in the feed.
    Companies without one fall back to a guessed token in the ATS sweep.
    """
    names: Dict[str, None] = {}
    boards: Dict[str, Tuple[str, str]] = {}

    for url in SIMPLIFY_FEEDS + SPEEDYAPPLY_FEEDS:
        data = get_json(url, timeout=ctx.request_timeout, max_bytes=32_000_000)
        if not isinstance(data, list):
            continue
        for rec in data:
            if not isinstance(rec, dict):
                continue
            if rec.get("active") is False:
                continue
            name = (rec.get("company_name") or rec.get("company") or "").strip()
            if not name or len(name) > 80:
                continue
            names.setdefault(name, None)
            if name not in boards:
                for cand_url in (rec.get("url"), rec.get("company_url")):
                    hit = detect_ats(cand_url)
                    if hit:
                        boards[name] = hit
                        break
        # One feed is enough to fill the universe; keep going only if thin.
        if len(names) > 500:
            break

    ordered = sorted(names)
    log.info(
        "company universe: %d names, %d with known ATS boards",
        len(ordered),
        len(boards),
    )
    return ordered, boards


def guess_tokens(company: str) -> List[str]:
    """Candidate ATS tokens to try for a company with no known board URL."""
    base = _slug(company)
    if not base or len(base) < 2:
        return []
    out = [base]
    # "Acme Technologies" also worth trying as "acme"
    first = re.split(r"[^a-z0-9]+", (company or "").lower())
    if first and first[0] and first[0] != base and len(first[0]) > 2:
        out.append(first[0])
    for suffix in ("inc", "llc", "corp", "technologies", "labs"):
        if base.endswith(suffix) and len(base) > len(suffix) + 2:
            out.append(base[: -len(suffix)])
    seen, uniq = set(), []
    for t in out:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq[:3]
