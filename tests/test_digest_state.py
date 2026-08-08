"""Dedupe and digest formatting tests."""

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


def test_date_posted_shown_when_known():
    ev = _ev()
    ev.date_posted = "2026-07-30"
    assert "Posted: 2026-07-30" in digest.render_text([ev])
    assert "2026-07-30" in digest.render_html([ev])


def test_date_posted_absent_says_not_stated():
    text = digest.render_text([_ev()])
    assert "Posted: not stated" in text


def test_to_iso_date_handles_each_ats_format():
    from models import to_iso_date
    assert to_iso_date("2026-07-30T06:59:38-04:00") == "2026-07-30"   # greenhouse
    assert to_iso_date(1711403416463) == "2024-03-25"                  # lever millis
    assert to_iso_date("2026-04-07T17:12:35.753+00:00") == "2026-04-07"  # ashby
    assert to_iso_date(None) is None
    assert to_iso_date("") is None
    assert to_iso_date("garbage") is None


def test_internship_leading_events_sort_first():
    events = [
        _ev(name="Some Hackathon", url="https://x.com/1", etype="hackathon"),
        _ev(name="Insight Program", url="https://x.com/2", etype="insight_program"),
    ]
    text = digest.render_text(events)
    assert text.index("Insight Program") < text.index("Some Hackathon")


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


def test_state_stores_full_record_for_the_board():
    """README.md is rendered from this file, so it needs the details."""
    ev = _ev(name="Code for Good", company="JPMorgan")
    rec = state.record([ev], {})[ev.id]
    assert rec["event_name"] == "Code for Good"
    assert rec["company"] == "JPMorgan"
    assert rec["url"] == ev.url
    assert "emailed_at" in rec


def test_dedupe_still_works_with_full_record():
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
