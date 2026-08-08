"""Seen-event state, committed back to the repo after every run.

The dedupe guarantee ("no event is ever emailed twice") rests on this file plus
the stable IDs in models.py. Writes are atomic so a crashed or cancelled run
cannot leave a truncated state file that would resurrect old events.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Tuple

from models import Event

log = logging.getLogger(__name__)

DEFAULT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")

# Entries older than this are pruned so the file does not grow without bound.
# Comfortably longer than any event announcement cycle.
RETENTION_DAYS = 400


def load(path: str = DEFAULT_PATH) -> Dict[str, dict]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError):
        # A corrupt state file must not crash the run, but it also must not
        # silently re-send everything, so this is logged loudly.
        log.exception("could not read state file %s, treating as empty", path)
        return {}
    if not isinstance(data, dict):
        return {}
    return data.get("seen", data) if "seen" in data else data


def save(seen: Dict[str, dict], path: str = DEFAULT_PATH) -> None:
    """Atomically write the state file."""
    payload = {
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "count": len(seen),
        "seen": seen,
    }
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".seen-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def split_new(events: List[Event], seen: Dict[str, dict]) -> List[Event]:
    """Return only events not already recorded, de-duplicated within the batch."""
    out: List[Event] = []
    batch_ids = set()
    for ev in events:
        if ev.id in seen or ev.id in batch_ids:
            continue
        batch_ids.add(ev.id)
        out.append(ev)
    return out


def record(events: List[Event], seen: Dict[str, dict]) -> Dict[str, dict]:
    """Mark events as sent. Call only after delivery succeeds.

    Only the opaque event ID is load-bearing for dedupe. The readable metadata
    beside it is debugging convenience, so it is omitted by default: the repo is
    public, and event names plus company names would otherwise expose what
    Karthik is applying to. Set STATE_VERBOSE=1 locally to keep it.
    """
    verbose = os.environ.get("STATE_VERBOSE") == "1"
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    for ev in events:
        entry = {"emailed_at": now}
        if verbose:
            entry.update(
                {
                    "company": ev.company,
                    "event_name": ev.event_name,
                    "url": ev.url,
                    "source": ev.source,
                }
            )
        seen[ev.id] = entry
    return seen


def prune(seen: Dict[str, dict], days: int = RETENTION_DAYS) -> Dict[str, dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    kept = {}
    for k, v in seen.items():
        stamp = (v or {}).get("emailed_at")
        if not stamp:
            kept[k] = v
            continue
        try:
            when = datetime.fromisoformat(stamp)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        except ValueError:
            kept[k] = v
            continue
        if when >= cutoff:
            kept[k] = v
    if len(kept) != len(seen):
        log.info("pruned %d expired state entries", len(seen) - len(kept))
    return kept
