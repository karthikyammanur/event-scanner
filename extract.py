"""LLM extraction: one call per surviving candidate.

This is the step that turns a raw listing into the Event data contract and, more
importantly, decides whether the thing is an event at all. The deterministic
prefilter in filters.py has already thrown out the obvious job postings; this
call adjudicates the ambiguous remainder and fills in dates and location.

Cost control matters here because the whole tool runs on a free Actions budget:
  - Candidates are batched, several per call, instead of one call each.
  - The stable instruction block is cached so repeat runs pay ~0.1x on it.
  - Anything already in the state file never reaches this module at all.

Without ANTHROPIC_API_KEY the module degrades to a deterministic fallback that
maps candidates straight to Events, so --dry-run still works offline.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional

from filters import matched_event_keyword, passes_hard_filters
from models import Candidate, Event

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"
BATCH_SIZE = 8
MAX_TOKENS = 8000

SYSTEM = """You classify listings scraped from company career pages, hackathon \
platforms, and web search results. For each listing you decide whether it is a \
real student-facing tech EVENT or PROGRAM, as opposed to a standard job posting.

Counts as an event: hackathons, summits, insight or discovery programs, \
externships, fellowships, conferences, code-for-good style competitions, \
immersion and campus programs students apply to attend.

Does NOT count: ordinary job listings of any kind, including internships, new \
grad roles, co-ops, part time roles, and recruiting-team jobs that merely \
mention events. "Software Engineering Intern, Summer 2027" is a job. "University \
Recruiter, Campus Events" is a job. A team or product named "Summit" or \
"Insight" does not make a listing an event.

Also reject anything that is not tech related, and anything that is neither \
US-based nor virtual.

Extract dates only when the listing states them. Never invent a date. Use null \
when a field is genuinely unknown, including for travel_credit_mentioned when \
the listing simply does not say either way."""

SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "is_event": {"type": "boolean"},
                    "reject_reason": {"type": ["string", "null"]},
                    "company": {"type": "string"},
                    "event_name": {"type": "string"},
                    "event_type": {
                        "type": "string",
                        "enum": [
                            "hackathon",
                            "summit",
                            "insight_program",
                            "fellowship",
                            "externship",
                            "conference",
                            "other",
                        ],
                    },
                    "start_date": {"type": ["string", "null"]},
                    "application_deadline": {"type": ["string", "null"]},
                    "location_city_state": {"type": ["string", "null"]},
                    "travel_credit_mentioned": {"type": ["boolean", "null"]},
                },
                "required": [
                    "index",
                    "is_event",
                    "reject_reason",
                    "company",
                    "event_name",
                    "event_type",
                    "start_date",
                    "application_deadline",
                    "location_city_state",
                    "travel_credit_mentioned",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _client():
    """Return an Anthropic client, or None when unavailable."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic package not installed, using fallback extraction")
        return None
    try:
        return anthropic.Anthropic()
    except Exception:
        log.exception("could not construct Anthropic client")
        return None


def _render(cands: List[Candidate]) -> str:
    lines = []
    for i, c in enumerate(cands):
        lines.append(
            json.dumps(
                {
                    "index": i,
                    "company": c.company,
                    "title": c.title,
                    "location": c.location or "",
                    "url": c.url,
                    "notes": (c.description or "")[:400],
                    "source": c.source,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines)


def _fallback(cands: List[Candidate]) -> List[Event]:
    """Deterministic mapping used when no API key is configured.

    Keeps --dry-run useful offline. The deterministic hard filters still apply,
    so this path cannot emit an obvious job posting either.
    """
    out = []
    for c in cands:
        kw = matched_event_keyword(c.title)
        ev = Event(
            company=c.company,
            event_name=c.title,
            event_type=kw[1] if kw else "other",
            url=c.url,
            source=c.source,
            start_date=c.extra.get("start_date"),
            location_city_state=c.location or None,
        )
        ok, reason = passes_hard_filters(ev)
        if ok:
            out.append(ev)
        else:
            log.debug("fallback rejected %s: %s", c.title[:60], reason)
    return out


def _one_batch(client, cands: List[Candidate]) -> List[Event]:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": SYSTEM,
                # Stable across every run and batch, so it caches.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[
            {
                "role": "user",
                "content": (
                    "Classify each listing below. Return one result per index.\n\n"
                    + _render(cands)
                ),
            }
        ],
    )

    if resp.stop_reason == "refusal":
        log.warning("extraction refused for a batch, skipping it")
        return []

    text = next((b.text for b in resp.content if b.type == "text"), "")
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except ValueError:
        log.warning("could not parse extraction output as JSON")
        return []

    out: List[Event] = []
    for r in parsed.get("results", []):
        idx = r.get("index")
        if not isinstance(idx, int) or not 0 <= idx < len(cands):
            continue
        if not r.get("is_event"):
            log.debug(
                "LLM rejected %s: %s",
                cands[idx].title[:60],
                r.get("reject_reason"),
            )
            continue

        cand = cands[idx]
        ev = Event(
            company=(r.get("company") or cand.company).strip(),
            event_name=(r.get("event_name") or cand.title).strip(),
            event_type=r.get("event_type") or "other",
            url=cand.url,
            source=cand.source,
            start_date=r.get("start_date") or cand.extra.get("start_date"),
            application_deadline=r.get("application_deadline"),
            location_city_state=r.get("location_city_state") or cand.location,
            travel_credit_mentioned=r.get("travel_credit_mentioned"),
        )

        # The hard filters are enforced here too, never only in the prompt.
        ok, reason = passes_hard_filters(ev)
        if not ok:
            log.debug("hard filter rejected %s: %s", ev.event_name[:60], reason)
            continue
        out.append(ev)

    return out


def extract(cands: List[Candidate]) -> List[Event]:
    """Turn candidates into Events, enforcing the hard filters."""
    if not cands:
        return []

    client = _client()
    if client is None:
        log.info("no API key, using deterministic fallback for %d candidates", len(cands))
        return _fallback(cands)

    out: List[Event] = []
    for i in range(0, len(cands), BATCH_SIZE):
        batch = cands[i : i + BATCH_SIZE]
        try:
            out.extend(_one_batch(client, batch))
        except Exception:
            # A failed batch must not lose the whole run.
            log.exception("extraction batch failed, continuing")

    log.info("extraction: %d candidates -> %d events", len(cands), len(out))
    return out
