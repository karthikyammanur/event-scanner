"""Seen-event state, committed back to the repo after every run."""

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

# Pruned after this so the file does not grow without bound.
RETENTION_DAYS = 400


def load(path: str = DEFAULT_PATH) -> Dict[str, dict]:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (ValueError, OSError):
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

    Stores only the opaque ID by default since the repo is public.
    Set STATE_VERBOSE=1 to keep readable metadata for debugging.
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
