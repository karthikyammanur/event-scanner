"""Gemini extraction: classify candidates and fill in the Event fields."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Dict, List, Optional

from filters import matched_event_keyword, passes_hard_filters
from models import Candidate, Event

log = logging.getLogger(__name__)

# Rolling aliases, since pinned IDs get retired for new keys. See CLAUDE.md.
MODEL = "gemini-flash-latest"
MODEL_FALLBACKS = (
    "gemini-2.5-flash-preview-09-2025",
    "gemini-flash-lite-latest",
    "gemini-2.0-flash",
    "gemini-2.5-flash",
)
BATCH_SIZE = 8
MAX_TOKENS = 8000

# Free tier caps requests per minute, so pace the batches.
REQUEST_SPACING_S = 4.0
RETRY_BACKOFF_S = 5.0
CIRCUIT_BREAK_AFTER = 3

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

The audience is a US university upperclassman. Reject events aimed at anyone \
younger than college: high school, middle school, K-12, teen and youth \
competitions, and events run by a high school or its clubs. If the organizer \
is a school rather than a university, a company, or a student organization at \
a university, treat it as pre-college and reject it. Note that "junior" and \
"senior" usually mean college year levels, not junior high.

Reject events whose organizer or audience is outside the US, including virtual \
ones. A virtual event listed as "Online" run by a non-US university, a non-US \
foundation, or aimed at a non-US region is out of scope. When an online event \
gives no signal of where its organizer is based, keep it.

Extract dates only when the listing states them. Never invent a date. Use an \
empty string "" when a text field is genuinely unknown.

One exception: when the event name carries a year ("Hacklytics 2027", "2026 \
SEC Summit") and no explicit start date is given, set start_date to a date in \
that year, using the month if the listing names one and January otherwise. \
This is what lets an obviously upcoming event be told apart from one that \
already happened.

For date_posted, give the date the announcement or listing was published, as \
YYYY-MM-DD, when the text says so ("Posted 3 days ago" relative to today, \
"Published March 4, 2026", a dateline, and so on). Leave it empty if the \
listing does not indicate when it went up. Do not confuse it with the event \
start date or the application deadline.

For \
travel_credit_mentioned, answer "true" when travel credit or travel support is \
explicitly offered, "false" when the listing explicitly says it is not \
provided, and "unknown" when the listing simply does not say either way."""

# Single-typed fields only: Gemini's structured output is unreliable with
# nullable type arrays. Empty string means unknown, converted back in _one_batch.
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
                    "date_posted": {"type": "string"},
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
                    "date_posted",
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


def _probe(client, model: str) -> bool:
    """Return True when this key can actually generate with `model`.

    Listing is not enough, some listed models 404 at call time.
    """
    from google.genai import types

    try:
        client.models.generate_content(
            model=model,
            contents="ok",
            config=types.GenerateContentConfig(max_output_tokens=1),
        )
        return True
    except Exception as exc:
        log.info("model %s unusable (%s): %s", model, type(exc).__name__, str(exc)[:160])
        return False


def resolve_model(client) -> Optional[str]:
    """Pick a model this API key can actually call, verified by calling it."""
    for want in (MODEL, *MODEL_FALLBACKS):
        if _probe(client, want):
            if want != MODEL:
                log.warning("preferred model %s unusable, falling back to %s", MODEL, want)
            return want

    # Nothing preferred worked, so try whatever the key exposes.
    try:
        listed = [
            (getattr(m, "name", "") or "").removeprefix("models/")
            for m in client.models.list()
        ]
    except Exception as exc:
        log.error("no usable model and could not list models (%s)", type(exc).__name__)
        return None

    for name in listed:
        if "embedding" in name or "tts" in name or "image" in name:
            continue
        if _probe(client, name):
            log.warning("using discovered model %s", name)
            return name

    log.error("no usable Gemini model for this key (listed: %s)", ", ".join(listed[:10]))
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
    """Used when no API key is set. Hard filters still apply."""
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
            date_posted=c.extra.get("date_posted"),
            location_city_state=c.location or None,
        )
        ok, reason = passes_hard_filters(ev)
        if ok:
            out.append(ev)
        else:
            log.debug("fallback rejected %s: %s", c.title[:60], reason)
    return out


def _str_or_none(v: Optional[str]) -> Optional[str]:
    """Empty string is the schema's null sentinel."""
    v = (v or "").strip()
    return v or None


def _tri_state(v: Optional[str]) -> Optional[bool]:
    return {"true": True, "false": False}.get((v or "").strip().lower())


def _one_batch(client, cands: List[Candidate], model: str) -> List[Event]:
    from google.genai import types

    resp = client.models.generate_content(
        model=model,
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

    # A safety block surfaces as empty text rather than an exception.
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
            date_posted=_str_or_none(r.get("date_posted")) or cand.extra.get("date_posted"),
            location_city_state=_str_or_none(r.get("location_city_state")) or cand.location,
            travel_credit_mentioned=_tri_state(r.get("travel_credit_mentioned")),
        )

        # Enforced here too, never only in the prompt.
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

    model = resolve_model(client)
    if model is None:
        log.error("no usable Gemini model, falling back to deterministic filtering")
        return _fallback(cands)
    log.info("extracting %d candidates with %s", len(cands), model)

    out: List[Event] = []
    total = (len(cands) + BATCH_SIZE - 1) // BATCH_SIZE
    consecutive_failures = 0

    for i in range(0, len(cands), BATCH_SIZE):
        batch = cands[i : i + BATCH_SIZE]
        n = i // BATCH_SIZE + 1
        if i:
            time.sleep(REQUEST_SPACING_S)
        try:
            out.extend(_one_batch(client, batch, model))
            consecutive_failures = 0
        except Exception as exc:
            # Log the server's message, a 404 and a quota error look alike without it.
            consecutive_failures += 1
            status = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            log.error(
                "extraction batch %d/%d failed (%s, status=%s): %s",
                n, total, type(exc).__name__, status, str(exc)[:400],
            )
            if consecutive_failures >= CIRCUIT_BREAK_AFTER:
                log.error(
                    "%d consecutive extraction failures, stopping early to "
                    "avoid burning quota on a systematic problem",
                    consecutive_failures,
                )
                break
            time.sleep(RETRY_BACKOFF_S * consecutive_failures)

    log.info("extraction: %d candidates -> %d events", len(cands), len(out))
    if cands and not out:
        log.warning(
            "extraction produced no events from %d candidates, check the "
            "batch errors above before trusting this as a real result",
            len(cands),
        )
    return out
