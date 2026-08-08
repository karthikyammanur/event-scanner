"""LLM extraction: one call per surviving candidate.

This is the step that turns a raw listing into the Event data contract and, more
importantly, decides whether the thing is an event at all. The deterministic
prefilter in filters.py has already thrown out the obvious job postings; this
call adjudicates the ambiguous remainder and fills in dates and location.

Cost control matters here because the whole tool runs on a free Actions budget:
  - Candidates are batched, several per call, instead of one call each.
  - Anything already in the state file never reaches this module at all.

Uses Gemini (google-genai SDK) with structured JSON output. Without
GEMINI_API_KEY the module degrades to a deterministic fallback that maps
candidates straight to Events, so --dry-run still works offline.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional

from filters import matched_event_keyword, passes_hard_filters
from models import Candidate, Event

log = logging.getLogger(__name__)

MODEL = "gemini-2.5-flash"
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

Extract dates only when the listing states them. Never invent a date. Use an \
empty string "" when a text field is genuinely unknown. For \
travel_credit_mentioned, answer "true" when travel credit or travel support is \
explicitly offered, "false" when the listing explicitly says it is not \
provided, and "unknown" when the listing simply does not say either way."""

# Gemini's structured output does not reliably support JSON Schema's nullable
# type-array form (["string", "null"]), so every field here is single-typed.
# Unknown text is an empty string; the tri-state travel field is a plain
# string enum. _one_batch() converts both back to the None/bool the Event
# data contract expects.
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
                    "reject_reason": {"type": "string"},
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
                    "start_date": {"type": "string"},
                    "application_deadline": {"type": "string"},
                    "location_city_state": {"type": "string"},
                    "travel_credit_mentioned": {
                        "type": "string",
                        "enum": ["true", "false", "unknown"],
                    },
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
            },
        }
    },
    "required": ["results"],
}


def _client():
    """Return a Gemini client, or None when unavailable."""
    if not os.environ.get("GEMINI_API_KEY"):
        return None
    try:
        from google import genai
    except ImportError:
        log.warning("google-genai package not installed, using fallback extraction")
        return None
    try:
        return genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    except Exception:
        log.exception("could not construct Gemini client")
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


def _str_or_none(v: Optional[str]) -> Optional[str]:
    """Empty string is this schema's null sentinel, see SCHEMA's comment."""
    v = (v or "").strip()
    return v or None


def _tri_state(v: Optional[str]) -> Optional[bool]:
    return {"true": True, "false": False}.get((v or "").strip().lower())


def _one_batch(client, cands: List[Candidate]) -> List[Event]:
    from google.genai import types

    resp = client.models.generate_content(
        model=MODEL,
        contents=(
            "Classify each listing below. Return one result per index.\n\n"
            + _render(cands)
        ),
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM,
            response_mime_type="application/json",
            response_json_schema=SCHEMA,
            max_output_tokens=MAX_TOKENS,
        ),
    )

    # A safety block or empty candidate list surfaces as no usable text rather
    # than an exception. Treat it the same as any other failed batch: skip it,
    # do not crash the run.
    text = getattr(resp, "text", None)
    if not text:
        log.warning("extraction returned no text for a batch, skipping it")
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
            start_date=_str_or_none(r.get("start_date")) or cand.extra.get("start_date"),
            application_deadline=_str_or_none(r.get("application_deadline")),
            location_city_state=_str_or_none(r.get("location_city_state")) or cand.location,
            travel_credit_mentioned=_tri_state(r.get("travel_credit_mentioned")),
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
