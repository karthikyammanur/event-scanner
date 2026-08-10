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
    assert "2026-07-30" in digest.render_text([ev])
    assert "2026-07-30" in digest.render_html([ev])


def test_date_posted_absent_says_not_stated():
    text = digest.render_text([_ev()])
    assert "Not stated" in text


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


# --- Duplicate suppression (the repeat-email bug) ----------------------------

def test_llm_title_and_company_drift_does_not_resend():
    """Same URL, LLM phrased the name differently. Must not send twice."""
    first = Event("JPMorgan Chase", "2027 Code for Good Hackathon", "hackathon",
                  "https://careers.jpmorgan.com/cfg", "greenhouse")
    later = Event("JPMorganChase", "2027 Code for Good Hackathon - SWE Program",
                  "hackathon", "https://careers.jpmorgan.com/cfg", "greenhouse")
    seen = state.record([first], {})
    assert state.split_new([later], seen) == []


def test_same_event_on_different_job_boards_sends_once():
    """Syndicated copies share a content key, so only one is emailed."""
    a = Event("JPMorgan Chase", "2027 Code for Good Hackathon", "hackathon",
              "https://career.fitchburgstate.edu/jobs/jpmc-cfg", "discovery")
    b = Event("JPMorgan Chase", "2027 Code for Good Hackathon", "hackathon",
              "https://careers.wgu.edu/jobs/jpmc-cfg", "discovery")
    assert len(state.split_new([a, b], {})) == 1


def test_syndicated_copy_suppressed_across_runs():
    a = Event("JPMorgan Chase", "2027 Code for Good Hackathon", "hackathon",
              "https://career.fitchburgstate.edu/jobs/jpmc-cfg", "discovery")
    b = Event("JPMorgan Chase", "Code for Good Hackathon 2027", "hackathon",
              "https://careers.wgu.edu/jobs/jpmc-cfg", "discovery")
    seen = state.record([a], {})
    assert state.split_new([b], seen) == []


def test_legacy_entries_without_content_key_still_suppress():
    """Rows written before content_key existed must still dedupe."""
    ev = Event("JPMorgan Chase", "2027 Code for Good Hackathon", "hackathon",
               "https://x.com/a", "greenhouse")
    legacy = {"oldhash": {"emailed_at": "2026-01-01T00:00:00+00:00",
                          "company": "JPMorgan Chase",
                          "event_name": "2027 Code for Good Hackathon"}}
    assert state.split_new([ev], legacy) == []


def test_genuinely_different_events_both_send():
    a = Event("Meta", "Meta Discovery Day", "insight_program", "https://m.com/1", "brave")
    b = Event("Meta", "Meta Engineering Summit", "summit", "https://m.com/2", "brave")
    assert len(state.split_new([a, b], {})) == 2


# --- Digest formatting -------------------------------------------------------

def test_html_groups_into_sections():
    evs = [
        _ev(name="A Hack", url="https://x.com/1", etype="hackathon"),
        _ev(name="B Insight", url="https://x.com/2", etype="insight_program"),
    ]
    out = digest.render_html(evs)
    assert "Internship pathways" in out
    assert "Hackathons and competitions" in out


def test_html_is_email_client_safe():
    out = digest.render_html([_ev()])
    assert "<style" not in out and "<link" not in out, "must be inline CSS only"
    assert out.count("<table") == out.count("</table>")
    assert out.count("<div") == out.count("</div>")
    assert 'role="presentation"' in out


def test_html_escapes_event_names():
    out = digest.render_html([_ev(name='Hack <script>alert(1)</script>')])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_text_digest_groups_and_lists_apply_links():
    out = digest.render_text([_ev(url="https://x.com/apply")])
    assert "https://x.com/apply" in out
    assert "NEW TECH EVENTS FOR YOU" in out


# --- Company aliasing and title suffixes (the JPMorgan repeat) ---------------

CFG_VARIANTS = [
    ("J.P. Morgan", "2027 Code for Good Hackathon"),
    ("JPMorgan Chase", "2027 Code for Good Hackathon"),
    ("JPMorganChase",
     "2027 Code for Good Hackathon - Software Engineer Program - Summer Internship"),
]

DFG_VARIANTS = [
    ("JPMorgan Chase", "2027 Data for Good Hackathon"),
    ("JPMorganChase", "2027 Data for Good Hackathon - Data & AI Program"),
    ("JPMorgan Chase",
     "2027 Data for Good Hackathon - Data & AI Program - Summer Internship"),
]


def _events(variants):
    return [
        Event(c, t, "hackathon", f"https://board{i}.edu/jobs/x", "discovery")
        for i, (c, t) in enumerate(variants)
    ]


def test_company_aliases_and_title_suffixes_collapse_to_one():
    """The exact six rows that kept re-sending, from real state data."""
    assert len(state.split_new(_events(CFG_VARIANTS), {})) == 1
    assert len(state.split_new(_events(DFG_VARIANTS), {})) == 1


def test_code_for_good_and_data_for_good_stay_separate():
    """Different events despite near-identical names."""
    both = state.split_new(_events(CFG_VARIANTS + DFG_VARIANTS), {})
    assert len(both) == 2


def test_alias_variant_suppressed_across_runs():
    seen = state.record(_events(CFG_VARIANTS[:1]), {})
    assert state.split_new(_events(CFG_VARIANTS[1:]), seen) == []


def test_same_event_credited_to_two_organizers_merges():
    a = Event("SEC", "2026 SEC Engineering Leadership Summit", "summit",
              "https://purplepass.com/e", "discovery")
    b = Event("The University of Alabama", "SEC Engineering Leadership Summit",
              "summit", "https://eng.ua.edu/summit", "discovery")
    assert len(state.split_new([a, b], {})) == 1


def test_generic_titles_do_not_collide_across_companies():
    """A bare name is not enough to identify an event, so company still counts."""
    a = Event("Meta", "Hackathon", "hackathon", "https://meta.com/h", "discovery")
    b = Event("Google", "Hackathon", "hackathon", "https://google.com/h", "discovery")
    assert len(state.split_new([a, b], {})) == 2
