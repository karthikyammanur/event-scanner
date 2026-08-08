#!/usr/bin/env python3
"""Event Scanner: discover US student-facing tech events and email new ones.

Usage:
    python scanner.py --dry-run --source=greenhouse   # print, do not send
    python scanner.py --dry-run                       # all sources, no email
    python scanner.py                                 # full run, sends digest

Run order: discover candidates from each source in isolation, prefilter them
deterministically, drop anything already seen, extract and hard-filter the
remainder, email what survives, then record state.

The state check runs BEFORE extraction so a repeat run costs no LLM tokens.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List

import digest
import state
from extract import extract
from filters import prefilter
from models import Candidate, Event
from sources import ats, devpost, discovery, mlh
from sources.base import Context, run_source

log = logging.getLogger("scanner")

# Each entry is (name, callable taking Context and returning candidates).
SOURCES = {
    "greenhouse": lambda ctx: ats.discover(ctx, only_platform="greenhouse"),
    "lever": lambda ctx: ats.discover(ctx, only_platform="lever"),
    "ashby": lambda ctx: ats.discover(ctx, only_platform="ashby"),
    "discovery": discovery.discover,
    "devpost": devpost.discover,
    "mlh": mlh.discover,
}

DEFAULT_SOURCES = ["greenhouse", "lever", "ashby", "discovery", "devpost", "mlh"]


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # These are noisy at DEBUG and never useful here.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def dedupe_candidates(cands: List[Candidate]) -> List[Candidate]:
    seen, out = set(), []
    for c in cands:
        k = c.key()
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def run(args) -> int:
    ctx = Context(
        year=datetime.now(timezone.utc).year,
        max_companies=args.max_companies,
        per_source_budget_s=args.source_budget,
        discovery_api_key=os.environ.get("TAVILY_API_KEY"),
        dry_run=args.dry_run,
    )

    names = [args.source] if args.source else DEFAULT_SOURCES
    unknown = [n for n in names if n not in SOURCES]
    if unknown:
        log.error("unknown source(s): %s", ", ".join(unknown))
        return 2

    # 1. Discovery. Each source is isolated, one failure never stops the run.
    raw: List[Candidate] = []
    for name in names:
        raw.extend(run_source(name, SOURCES[name], ctx))
    raw = dedupe_candidates(raw)
    log.info("discovery: %d unique candidates from %s", len(raw), ", ".join(names))

    # 2. Deterministic prefilter, keeps LLM spend down.
    kept: List[Candidate] = []
    for c in raw:
        ok, reason = prefilter(c)
        if ok:
            kept.append(c)
        else:
            log.debug("prefilter dropped %s: %s", c.title[:60], reason)
    log.info("prefilter: %d candidates survive", len(kept))

    # 3. Drop anything already emailed, before spending any tokens on it.
    seen = state.load(args.state)
    fresh = [c for c in kept if c.key() not in seen]
    log.info(
        "state: %d already seen, %d new candidates to extract",
        len(kept) - len(fresh),
        len(fresh),
    )

    # 4. Extraction plus hard filters.
    events: List[Event] = extract(fresh)

    # 5. Final dedupe against state. Extraction can rewrite a title, which
    #    changes the ID, so this check is repeated on the extracted events.
    new_events = state.split_new(events, seen)
    log.info("%d new events after final dedupe", len(new_events))

    if not new_events:
        log.info("nothing new to send")
        return 0

    # 6. Deliver, then record. Recording only after a successful send is what
    #    guarantees a failed email is retried rather than silently lost.
    sent = digest.send(new_events, dry_run=args.dry_run)
    if not sent:
        log.error("digest delivery failed, state not updated so nothing is lost")
        return 1

    if args.dry_run:
        log.info("dry run, state not written")
        return 0

    seen = state.record(new_events, seen)
    seen = state.prune(seen)
    state.save(seen, args.state)
    log.info("state updated, %d total events tracked", len(seen))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Discover student-facing tech events.")
    p.add_argument(
        "--source",
        help="run a single source (%s)" % ", ".join(sorted(SOURCES)),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print the digest to stdout, send no email, write no state",
    )
    p.add_argument(
        "--state",
        default=state.DEFAULT_PATH,
        help="path to the seen-events state file",
    )
    p.add_argument(
        "--max-companies",
        type=int,
        default=400,
        help="cap on ATS board targets per run (Actions minute budget)",
    )
    p.add_argument(
        "--source-budget",
        type=int,
        default=240,
        help="wall clock seconds allowed per source",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    configure_logging(args.verbose)
    try:
        return run(args)
    except KeyboardInterrupt:
        log.warning("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
