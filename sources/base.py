"""Shared HTTP plumbing and the source isolation contract.

Every discovery source is a module exposing `discover(ctx) -> list[Candidate]`.
`run_source()` wraps that call so any single source failing (network, parse,
rate limit, anything) is logged and skipped, never propagated. This is the
spec's "never let a single broken source crash the whole run".
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

import requests

log = logging.getLogger(__name__)

# Devpost 403s without a browser UA (verified). Harmless everywhere else.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9"}


@dataclass
class Context:
    """Per run configuration handed to each source."""

    year: int
    max_companies: int = 400
    request_timeout: int = 20
    per_source_budget_s: int = 240
    brave_api_key: Optional[str] = None
    dry_run: bool = False
    companies: List[str] = field(default_factory=list)


class Budget:
    """Wall clock guard so one slow source cannot eat the Actions minutes."""

    def __init__(self, seconds: int):
        self.deadline = time.monotonic() + seconds

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline


_session: Optional[requests.Session] = None


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(DEFAULT_HEADERS)
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=32, pool_maxsize=32, max_retries=0
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _session = s
    return _session


def get(url: str, timeout: int = 20, **kw) -> Optional[requests.Response]:
    """GET that returns None instead of raising. Callers check for None."""
    try:
        r = session().get(url, timeout=timeout, **kw)
    except requests.RequestException as exc:
        log.debug("GET failed %s: %s", url, exc)
        return None
    if r.status_code != 200:
        log.debug("GET %s -> %s", url, r.status_code)
        return None
    return r


def get_json(url: str, timeout: int = 20, max_bytes: int = 8_000_000, **kw):
    """GET returning parsed JSON, or None. Guards against huge payloads.

    Ashby boards can exceed 2MB, so the cap is generous but present.
    """
    r = get(url, timeout=timeout, **kw)
    if r is None:
        return None
    if len(r.content) > max_bytes:
        log.warning("payload too large, skipping: %s (%d bytes)", url, len(r.content))
        return None
    try:
        return r.json()
    except ValueError:
        log.debug("non-JSON response from %s", url)
        return None


def run_source(name: str, fn: Callable[[Context], List], ctx: Context) -> List:
    """Run one source in isolation. Never raises."""
    start = time.monotonic()
    try:
        out = fn(ctx) or []
        log.info("source %s: %d candidates in %.1fs", name, len(out), time.monotonic() - start)
        return out
    except Exception:
        log.exception("source %s failed, continuing with other sources", name)
        return []
