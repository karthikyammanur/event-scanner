"""ATS sweep across the Greenhouse, Lever, and Ashby public board APIs."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from models import Candidate, to_iso_date

from .base import Budget, Context, get_json
from .companies import guess_tokens, load_companies

log = logging.getLogger(__name__)

MAX_WORKERS = 12

# (title, url, location, date_posted)
Row = Tuple[str, str, Optional[str], Optional[str]]


def _greenhouse(token: str, timeout: int) -> List[Row]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    data = get_json(url, timeout=timeout)
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("jobs") or []:
        title = (j.get("title") or "").strip()
        link = j.get("absolute_url") or ""
        loc = (j.get("location") or {}).get("name") if isinstance(j.get("location"), dict) else None
        if title and link:
            out.append((title, link, loc, to_iso_date(j.get("first_published"))))
    return out


def _lever(token: str, timeout: int) -> List[Row]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"
    data = get_json(url, timeout=timeout)
    if not isinstance(data, list):
        return []
    out = []
    for j in data:
        if not isinstance(j, dict):
            continue
        title = (j.get("text") or "").strip()
        link = j.get("hostedUrl") or j.get("applyUrl") or ""
        cats = j.get("categories") or {}
        loc = cats.get("location") if isinstance(cats, dict) else None
        if title and link:
            out.append((title, link, loc, to_iso_date(j.get("createdAt"))))
    return out


def _ashby(token: str, timeout: int) -> List[Row]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    data = get_json(url, timeout=timeout)
    if not isinstance(data, dict):
        return []
    out = []
    for j in data.get("jobs") or []:
        if not isinstance(j, dict):
            continue
        title = (j.get("title") or "").strip()
        link = j.get("jobUrl") or j.get("applyUrl") or ""
        loc = j.get("location")
        if isinstance(loc, dict):
            loc = loc.get("name")
        if title and link:
            out.append((title, link, loc, to_iso_date(j.get("publishedAt"))))
    return out


FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


def _scan_one(
    company: str, platform: str, token: str, timeout: int
) -> List[Candidate]:
    fetch = FETCHERS.get(platform)
    if fetch is None:
        return []
    rows = fetch(token, timeout)
    cands = []
    for title, link, loc, posted in rows:
        cands.append(
            Candidate(
                company=company,
                title=title,
                url=link,
                source=platform,
                location=loc,
                extra={"ats_token": token, "date_posted": posted},
            )
        )
    return cands


def _targets(
    ctx: Context, only_platform: Optional[str]
) -> List[Tuple[str, str, str]]:
    """Build the (company, platform, token) work list."""
    companies, boards = load_companies(ctx)
    ctx.companies = companies

    targets: List[Tuple[str, str, str]] = []
    seen = set()

    # Known boards first.
    for company, (platform, token) in boards.items():
        if only_platform and platform != only_platform:
            continue
        k = (platform, token)
        if k not in seen:
            seen.add(k)
            targets.append((company, platform, token))

    # Then guessed tokens for the rest, capped by max_companies.
    remaining = max(0, ctx.max_companies - len(targets))
    if remaining:
        platforms = [only_platform] if only_platform else ["greenhouse", "lever", "ashby"]
        for company in companies:
            if company in boards or remaining <= 0:
                continue
            for token in guess_tokens(company):
                for platform in platforms:
                    k = (platform, token)
                    if k in seen:
                        continue
                    seen.add(k)
                    targets.append((company, platform, token))
                    remaining -= 1
                    if remaining <= 0:
                        break
                if remaining <= 0:
                    break
            if remaining <= 0:
                break

    return targets


def discover(ctx: Context, only_platform: Optional[str] = None) -> List[Candidate]:
    """Sweep ATS boards and return raw candidates."""
    targets = _targets(ctx, only_platform)
    log.info("ATS sweep: %d board targets (%s)", len(targets), only_platform or "all")

    budget = Budget(ctx.per_source_budget_s)
    out: List[Candidate] = []
    hits = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_scan_one, c, p, t, ctx.request_timeout): (c, p, t)
            for c, p, t in targets
        }
        for fut in as_completed(futures):
            if budget.expired():
                log.warning("ATS sweep hit time budget, stopping early")
                for f in futures:
                    f.cancel()
                break
            company, platform, token = futures[fut]
            try:
                rows = fut.result()
            except Exception as exc:
                log.debug("board %s/%s failed: %s", platform, token, exc)
                continue
            if rows:
                hits += 1
                out.extend(rows)

    log.info("ATS sweep: %d live boards, %d raw listings", hits, len(out))
    return out
