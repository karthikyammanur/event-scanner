"""Tests for the dedupe guarantee and the digest formatting rules."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import digest  # noqa: E402
import state  # noqa: E402
from models import Event  # noqa: E402


def _ev(name="Code for Good Hackathon", company="JPMorgan", url="https://x.com/cfg",
        loc="Plano, TX", travel=None, etype="hackathon", source="greenhouse"):
    return Event(
        company=company,
        event_name=name,
        event_type=etype,
        url=url,
        source=source,
        location_city_state=loc,
        travel_credit_mentioned=travel,
    )


# --- The no-em-dash rule ------------------------------------------------------

def test_scrub_removes_em_dash():
    assert "—" not in digest.scrub("Summit — apply now")
    assert digest.scrub("Summit — apply now") == "Summit , apply now"


def test_rendered_body_has_no_em_dash():
    events = [
        _ev(name="Insight Day — Engineering", company="Meta — Reality Labs",
            loc="Menlo Park, CA"),
        _ev(name="Summit", url="https://x.com/s", loc="New York, NY"),
    ]
    text = digest.render_text(events)
    html_body = digest.render_html(events)
    for bad in ("—", "―"):
        assert bad not in text
        assert bad not in html_body


def test_subject_has_no_em_dash():
    assert "—" not in digest.subject_line([_ev()])


def test_assert_no_em_dash_raises():
    with pytest.raises(AssertionError):
        digest.assert_no_em_dash("a — b")


# --- Flag, never drop ---------------------------------------------------------

def test_out_of_state_event_appears_and_is_flagged():
    ev = _ev(name="Bloomberg Engineering Summit", company="Bloomberg",
             url="https://bloomberg.com/s", loc="New York, NY", travel=None)
    text = digest.render_text([ev])
    assert "Bloomberg Engineering Summit" in text, "event must not be dropped"
    assert "no travel support mentioned" in text, "event must be flagged"


def test_texas_event_not_flagged_in_digest():
    text = digest.render_text([_ev(loc="Plano, TX")])
    assert "travel support" not in text.split("Flagged entries")[0]


def test_explicit_no_travel_credit_is_flagged_distinctly():
    ev = _ev(loc="Seattle, WA", travel=False)
    assert "says no travel support" in digest.render_text([ev])


def test_internship_leading_events_sort_first():
    events = [
        _ev(name="Some Hackathon", url="https://x.com/1", etype="hackathon"),
        _ev(name="Insight Program", url="https://x.com/2", etype="insight_program"),
    ]
    text = digest.render_text(events)
    assert text.index("Insight Program") < text.index("Some Hackathon")


# --- Dedupe -------------------------------------------------------------------

def test_split_new_filters_seen():
    ev = _ev()
    seen = {ev.id: {"emailed_at": "2026-01-01T00:00:00+00:00"}}
    assert state.split_new([ev], seen) == []
    assert state.split_new([ev], {}) == [ev]


def test_split_new_dedupes_within_one_batch():
    a, b = _ev(), _ev()  # identical, so identical IDs
    assert len(state.split_new([a, b], {})) == 1


def test_record_then_split_suppresses(tmp_path):
    path = str(tmp_path / "seen.json")
    ev = _ev()
    seen = state.record([ev], state.load(path))
    state.save(seen, path)

    reloaded = state.load(path)
    assert ev.id in reloaded
    assert state.split_new([ev], reloaded) == [], "run 2 must send nothing"


def test_save_is_atomic_and_reloadable(tmp_path):
    path = str(tmp_path / "seen.json")
    state.save(state.record([_ev()], {}), path)
    assert len(state.load(path)) == 1
    # Second write must not corrupt the first.
    state.save(state.record([_ev(url="https://x.com/other")], state.load(path)), path)
    assert len(state.load(path)) == 2


def test_missing_state_file_is_empty(tmp_path):
    assert state.load(str(tmp_path / "nope.json")) == {}


def test_corrupt_state_file_does_not_crash(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert state.load(str(path)) == {}


def test_state_leaks_no_event_details_by_default(monkeypatch):
    """The repo is public, so seen.json must not name what he applied to."""
    monkeypatch.delenv("STATE_VERBOSE", raising=False)
    ev = _ev(name="Code for Good", company="JPMorgan")
    seen = state.record([ev], {})
    blob = repr(seen)
    assert "Code for Good" not in blob
    assert "JPMorgan" not in blob
    assert "cfg" not in blob
    # The opaque ID is the only key, and it is all dedupe needs.
    assert list(seen) == [ev.id]


def test_state_verbose_opt_in_keeps_details(monkeypatch):
    monkeypatch.setenv("STATE_VERBOSE", "1")
    seen = state.record([_ev(name="Code for Good")], {})
    assert "Code for Good" in repr(seen)


def test_dedupe_still_works_without_metadata(monkeypatch):
    monkeypatch.delenv("STATE_VERBOSE", raising=False)
    ev = _ev()
    seen = state.record([ev], {})
    assert state.split_new([ev], seen) == []


def test_prune_drops_only_expired():
    seen = {
        "old": {"emailed_at": "2020-01-01T00:00:00+00:00"},
        "new": {"emailed_at": "2026-08-01T00:00:00+00:00"},
    }
    kept = state.prune(seen, days=400)
    assert "new" in kept and "old" not in kept


def test_empty_digest_sends_nothing():
    assert digest.send([], dry_run=False) is True
