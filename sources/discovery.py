"""Broad web discovery via Tavily, for companies outside the ATS feeds."""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import date
from typing import List, Optional
from urllib.parse import urlparse

from models import Candidate

from .base import Budget, Context, session

log = logging.getLogger(__name__)

ENDPOINT = "https://api.tavily.com/search"

EVENT_TERMS = [
    "hackathon",
    "code for good",
    "engineering summit",
    "insight program",
    "externship",
    "fellowship program",
    "discovery day",
    "tech immersion program",
    "student summit",
    "engineering open house",
]

AUDIENCE_TERMS = [
    "students apply",
    "undergraduate students",
    "college students apply",
    "university students",
]

# Domains that are aggregators or noise rather than a company's own posting.
SKIP_DOMAINS = {
    "reddit.com", "quora.com", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "tiktok.com", "pinterest.com",
    "indeed.com", "glassdoor.com", "ziprecruiter.com", "simplyhired.com",
    "joinhandshake.com", "handshake.com",  # explicitly out of scope
    "wikipedia.org", "medium.com",
}

# 5/run x 6 runs/day x 30 days = 900 credits/month, under the 1,000 free tier.
QUERIES_PER_RUN = 5
DELAY_S = 0.3


def _rotation_offset(year: int) -> int:
    """Deterministic per day rotation so consecutive runs vary their queries."""
    seed = f"{date.today().isoformat()}:{year}"
    return int(hashlib.sha256(seed.encode()).hexdigest(), 16)


def _build_queries(ctx: Context) -> List[str]:
    combos = []
    for ev in EVENT_TERMS:
        for aud in AUDIENCE_TERMS:
            combos.append(f'"{ev}" {aud} {ctx.year}')
            combos.append(f'"{ev}" {aud} {ctx.year + 1}')
    offset = _rotation_offset(ctx.year) % len(combos)
    rotated = combos[offset:] + combos[:offset]
    return rotated[:QUERIES_PER_RUN]


def _domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _company_from(url: str, title: str) -> str:
    """Best effort company name from the domain, refined later by the LLM."""
    host = _domain(url)
    if not host:
        return title[:60]
    parts = [p for p in host.split(".") if p not in {"com", "org", "net", "io", "co", "careers", "jobs"}]
    return (parts[-1] if parts else host).replace("-", " ").title()


def discover(ctx: Context) -> List[Candidate]:
    key = ctx.discovery_api_key
    if not key:
        log.info("discovery: TAVILY_API_KEY not set, skipping broad discovery")
        return []

    budget = Budget(ctx.per_source_budget_s)
    out: List[Candidate] = []
    seen = set()

    for q in _build_queries(ctx):
        if budget.expired():
            log.warning("discovery: time budget reached")
            break
        try:
            r = session().post(
                ENDPOINT,
                json={
                    "api_key": key,
                    "query": q,
                    "search_depth": "basic",
                    "max_results": 20,
                    "country": "united states",
                },
                timeout=ctx.request_timeout,
            )
        except Exception as exc:
            log.warning("discovery: query failed (%s): %s", q, exc)
            continue

        if r.status_code == 429:
            log.warning("discovery: rate limited, backing off")
            time.sleep(2.0)
            continue
        if r.status_code == 401:
            log.error("discovery: invalid TAVILY_API_KEY, stopping this source")
            break
        if r.status_code != 200:
            log.warning("discovery: HTTP %s for query %s", r.status_code, q)
            continue

        try:
            results = r.json().get("results") or []
        except ValueError:
            continue

        for res in results:
            url = (res.get("url") or "").strip()
            title = (res.get("title") or "").strip()
            if not url or not title:
                continue
            host = _domain(url)
            if not host or any(host == d or host.endswith("." + d) for d in SKIP_DOMAINS):
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append(
                Candidate(
                    company=_company_from(url, title),
                    title=title,
                    url=url,
                    source="tavily",
                    location=None,
                    description=(res.get("content") or "")[:500],
                    extra={"query": q},
                )
            )

        time.sleep(DELAY_S)

    log.info("discovery: %d candidates from %d queries", len(out), QUERIES_PER_RUN)
    return out
