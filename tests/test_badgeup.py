"""BadgeUp curated-conference source: table parsing and scope filtering."""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sources.badgeup import (  # noqa: E402
    parse_dates_location,
    parse_deadline,
    parse_readme,
)

# Anchored so the tests read the same way every day. Real rows carry explicit
# years, so only the yearless-inference cases depend on this at all.
TODAY = date(2026, 9, 16)


def _badge(url: str, label: str = "Register") -> str:
    """The register cell is an anchor wrapping a shields.io image, not a link."""
    return (
        f'<a href="{url}"><img src="https://img.shields.io/badge/{label}-blue'
        f'?style=for-the-badge" alt="{label}"></a>'
    )


def _table(*rows: str, section: str = "Tech & Computing") -> str:
    head = (
        "| Status | Conference | Focus | Dates & Location | Access | Audience "
        "| Register / Apply | Deadline |\n"
        "| ------ | ---------- | ----- | ---------------- | ------ | -------- "
        "| ---------------- | -------- |"
    )
    return "\n".join([f"## {section}", "", head, *rows, ""])


def _row(
    name="IBM Quantum Developer Conference",
    focus="Quantum computing",
    dates="Nov 11-13, 2026 · Chicago, IL",
    access="Free",
    audience="Students · Early career · Researchers",
    url="https://www.ibm.com/quantum/qdc",
    deadline="Application deadline: Sep 21, 2026",
    status="✅ **[OPEN]**",
):
    return (
        f"| {status} | {name} | {focus} | {dates} | {access} | {audience} "
        f"| {_badge(url)} | {deadline} |"
    )


# --- dates and location ---------------------------------------------------


def test_parses_day_range_and_location():
    assert parse_dates_location("Oct 28-29, 2026 · San Francisco, CA") == (
        "2026-10-28",
        "San Francisco, CA",
    )


def test_range_spanning_two_months_starts_in_the_first():
    """"Nov 30-Dec 4, 2026" starts Nov 30, not Dec 30 and not Dec 4."""
    assert parse_dates_location("Nov 30-Dec 4, 2026 · Las Vegas, NV") == (
        "2026-11-30",
        "Las Vegas, NV",
    )


def test_month_and_year_without_a_day_defaults_to_the_first():
    assert parse_dates_location("May 2027 · Long Beach, CA") == (
        "2027-05-01",
        "Long Beach, CA",
    )


def test_undated_row_still_yields_its_location():
    """A date we cannot read must not cost us the venue."""
    assert parse_dates_location("Dates TBA · Location TBA") == (None, "Location TBA")


def test_yearless_date_rolls_to_the_next_occurrence():
    start, _ = parse_dates_location("Mar 4 · Austin, TX", today=TODAY)
    assert start == "2027-03-04"


# --- deadlines ------------------------------------------------------------


def test_parses_an_explicit_deadline():
    assert parse_deadline("Application deadline: Sep 21, 2026") == "2026-09-21"


def test_reads_the_first_date_out_of_prose():
    assert parse_deadline("Early bird ends Aug 20, 2026; free virtual pass open") == (
        "2026-08-20"
    )


def test_a_closed_note_is_not_a_deadline():
    """"scholarships closed (Mar 2026)" is a past-tense note, not a date to show."""
    assert parse_deadline("Reg open; scholarships closed (Mar 2026)") is None


def test_yearless_deadline_anchors_to_the_event_year_not_the_next_one():
    """The bug this guards: a yearless "Aug 9" on an Aug 2026 event was rolled
    forward to Aug 2027, turning a finished event into an upcoming one."""
    assert parse_deadline("Reg open; convention begins Aug 9", start_date="2026-08-09") == (
        "2026-08-09"
    )


def test_deadline_in_a_later_month_belongs_to_the_previous_year():
    """A Dec deadline on a Feb event is the December before it."""
    assert parse_deadline("Early bird ends Dec 20", start_date="2027-02-09") == (
        "2026-12-20"
    )


def test_deadline_after_the_event_is_discarded():
    assert parse_deadline("Something Nov 30, 2026", start_date="2026-10-01") is None


def test_yearless_deadline_without_an_event_date_is_not_guessed():
    assert parse_deadline("Applications close Aug 9") is None


# --- row parsing ----------------------------------------------------------


def test_parses_a_full_row():
    (cand,) = parse_readme(_table(_row()), today=TODAY)
    assert cand.company == "IBM Quantum Developer Conference"
    assert cand.url == "https://www.ibm.com/quantum/qdc"
    assert cand.source == "badgeup"
    assert cand.location == "Chicago, IL"
    assert cand.extra["start_date"] == "2026-11-11"
    assert cand.extra["application_deadline"] == "2026-09-21"
    # A curated event list needs no event keyword in the title to qualify.
    assert cand.extra["source_is_event_feed"] is True


def test_status_emoji_is_stripped_from_the_name():
    (cand,) = parse_readme(_table(_row(name="🔥 **GitHub Universe**")), today=TODAY)
    assert cand.title == "GitHub Universe"


def test_rows_without_a_real_link_are_skipped():
    assert parse_readme(_table(_row(url="#tbd")), today=TODAY) == []


def test_the_badge_legend_table_is_not_read_as_events():
    """A two-column legend sits above the event tables in the same section."""
    legend = "## Tech & Computing\n\n| Badge | Meaning |\n| ----- | ------- |\n| ✅ | Open |"
    assert parse_readme(legend, today=TODAY) == []


def test_header_and_separator_rows_are_skipped():
    cands = parse_readme(_table(_row(), _row(name="Other", url="https://o.co")), today=TODAY)
    assert [c.title for c in cands] == ["IBM Quantum Developer Conference", "Other"]


def test_the_same_event_is_not_emitted_twice():
    dupe = _table(_row(), _row())
    assert len(parse_readme(dupe, today=TODAY)) == 1


def test_escaped_pipe_in_a_cell_does_not_split_the_row():
    row = _row(name=r"Hack \| Build")
    (cand,) = parse_readme(_table(row), today=TODAY)
    assert cand.title == "Hack | Build"


# --- scope ----------------------------------------------------------------


def test_out_of_scope_sections_are_ignored():
    """The list also covers law, medicine, and the arts."""
    assert parse_readme(_table(_row(), section="Law & Government"), today=TODAY) == []


def test_professional_only_rows_are_dropped():
    """"Early career" stays in scope, since that is what an upperclassman is
    and GHC and KubeCon both label their student track that way."""
    row = _row(audience="Professionals · Allies · Executives")
    assert parse_readme(_table(row), today=TODAY) == []

    kept = _row(audience="Early career · Professionals")
    assert len(parse_readme(_table(kept), today=TODAY)) == 1


def test_identity_section_keeps_a_tech_row():
    """GHC and SHPE live in the identity sections and are the whole point."""
    row = _row(
        name="SHPE National Convention",
        focus="Hispanic engineers",
        dates="Oct 28-31, 2026 · Indianapolis, IN",
        url="https://shpe.org/",
    )
    (cand,) = parse_readme(
        _table(row, section="Latinx Professionals & Students"), today=TODAY
    )
    assert cand.title == "SHPE National Convention"


def test_identity_section_drops_a_non_tech_row():
    """"LMSA National Conference" reads like "SHPE National Convention" to a
    keyword matcher, so the focus cell is what separates them."""
    row = _row(
        name="LMSA National Conference",
        focus="Latino medical students",
        url="https://national.lmsa.net/",
    )
    assert parse_readme(_table(row, section="Latinx Professionals & Students"), today=TODAY) == []


def test_event_type_is_classified_from_the_row():
    summit = _row(name="ColorStack Stacked Up Summit", focus="Black & Latinx computing students", url="https://colorstack.org/")
    fair = _row(name="Fall Tech Career Fair", focus="Software engineering", url="https://example.com/fair")
    cands = parse_readme(_table(summit, fair), today=TODAY)
    assert [c.extra["event_type"] for c in cands] == ["summit", "career_fair"]
