"""Filter logic tested against the labeled fixture set."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from filters import (  # noqa: E402
    is_tech_related,
    is_us_or_virtual,
    looks_like_job_posting,
    matched_event_keyword,
    passes_hard_filters,
    prefilter,
)
from models import Candidate, Event, stable_id  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "labeled_postings.json")

with open(FIXTURES, encoding="utf-8") as fh:
    POSTINGS = json.load(fh)

EVENTS = [p for p in POSTINGS if p["label"] == "event"]
NON_EVENTS = [p for p in POSTINGS if p["label"] == "not_event"]


def _candidate(p) -> Candidate:
    extra = {}
    if p.get("source_is_event_feed"):
        extra["source_is_event_feed"] = True
    return Candidate(
        company=p["company"],
        title=p["title"],
        url=p["url"],
        source=p["source"],
        location=p.get("location"),
        extra=extra,
    )


def _event(p) -> Event:
    kw = matched_event_keyword(p["title"])
    return Event(
        company=p["company"],
        event_name=p["title"],
        event_type=kw[1] if kw else "other",
        url=p["url"],
        source=p["source"],
        location_city_state=p.get("location"),
    )


@pytest.mark.parametrize("p", EVENTS, ids=[p["title"][:45] for p in EVENTS])
def test_no_false_negatives_in_prefilter(p):
    keep, reason = prefilter(_candidate(p))
    assert keep, f"FALSE NEGATIVE, real event dropped: {p['title']} ({reason})"


@pytest.mark.parametrize("p", EVENTS, ids=[p["title"][:45] for p in EVENTS])
def test_no_false_negatives_in_hard_filters(p):
    ok, reason = passes_hard_filters(_event(p))
    assert ok, f"FALSE NEGATIVE at hard filter: {p['title']} ({reason})"


def test_false_positive_rate_is_bounded():
    """Job postings should mostly be rejected before the LLM spend."""
    survivors = [p for p in NON_EVENTS if prefilter(_candidate(p))[0]]
    assert len(survivors) <= 2, (
        "too many job postings surviving prefilter: "
        + ", ".join(s["title"] for s in survivors)
    )


@pytest.mark.parametrize("p", NON_EVENTS, ids=[p["title"][:45] for p in NON_EVENTS])
def test_non_events_rejected_somewhere_in_the_pipeline(p):
    """A job posting must not reach the digest via the deterministic path."""
    keep, _ = prefilter(_candidate(p))
    if not keep:
        return
    ok, _ = passes_hard_filters(_event(p))
    assert not ok, f"job posting would be emailed as an event: {p['title']}"


def test_rejects_non_us_non_virtual():
    assert not is_us_or_virtual("London, United Kingdom")
    assert not is_us_or_virtual("Hyderabad, Telangana")
    assert not is_us_or_virtual("Toronto, Canada")


def test_accepts_us_and_virtual():
    for loc in ["Austin, TX", "New York, NY", "Virtual", "Online", "Remote",
                "San Francisco, California", "Seattle, WA, USA"]:
        assert is_us_or_virtual(loc), loc


def test_multi_location_with_one_us_option_is_kept():
    """Real posting shape seen live: multi-location with one US option."""
    loc = "London, UK; Ontario, CAN; Remote-Friendly, United States; San Francisco, CA"
    assert is_us_or_virtual(loc)


def test_multi_location_all_foreign_is_rejected():
    assert not is_us_or_virtual("London, UK; Toronto, Canada")


def test_unknown_location_is_not_dropped():
    """Absence of location must never be a silent rejection."""
    assert is_us_or_virtual(None)
    assert is_us_or_virtual("")


def test_tech_detection():
    assert is_tech_related("Software Engineering Insight Day")
    assert is_tech_related("Cybersecurity Summit for Students")
    assert not is_tech_related("Retail Store Leadership Summit")


def test_job_posting_detection():
    assert looks_like_job_posting("Senior Software Engineer, Summit Platform")
    assert looks_like_job_posting("Data Scientist, Insight Analytics")
    assert looks_like_job_posting("Software Engineering Intern, Summer 2027")
    assert not looks_like_job_posting("Code for Good Hackathon 2027")
    assert not looks_like_job_posting("Engineering Insight Series 2027")


def test_out_of_texas_without_travel_credit_is_flagged_not_dropped():
    ev = Event(
        company="Bloomberg",
        event_name="Bloomberg Engineering Summit for Students",
        event_type="summit",
        url="https://bloomberg.com/summit",
        source="greenhouse",
        location_city_state="New York, NY",
        travel_credit_mentioned=None,
    )
    ok, _ = passes_hard_filters(ev)
    assert ok, "out of state event must survive the hard filters"
    assert ev.needs_travel_flag(), "must be flagged for the digest"


def test_texas_event_not_flagged():
    ev = Event("Dell", "Dell Tech Summit", "summit", "https://x.com/a", "tavily",
               location_city_state="Round Rock, TX")
    assert not ev.needs_travel_flag()


def test_virtual_event_not_flagged():
    ev = Event("Meta", "Virtual Hackathon", "hackathon", "https://x.com/b", "tavily",
               location_city_state="Virtual")
    assert not ev.needs_travel_flag()


def test_confirmed_travel_credit_not_flagged():
    ev = Event("Meta", "Meta Summit", "summit", "https://x.com/c", "tavily",
               location_city_state="Menlo Park, CA", travel_credit_mentioned=True)
    assert not ev.needs_travel_flag()


def test_unknown_location_is_flagged_rather_than_assumed_local():
    ev = Event("X", "X Hackathon", "hackathon", "https://x.com/d", "tavily",
               location_city_state=None)
    assert ev.needs_travel_flag()


def test_id_is_stable_across_cosmetic_url_changes():
    a = stable_id("Meta", "Discovery Day", "https://meta.com/dd")
    b = stable_id("meta", "  discovery day ", "http://www.meta.com/dd/?utm_source=x")
    assert a == b


def test_id_differs_for_different_events():
    a = stable_id("Meta", "Discovery Day", "https://meta.com/dd")
    b = stable_id("Meta", "Summit", "https://meta.com/summit")
    assert a != b


def test_event_type_falls_back_to_other():
    ev = Event("X", "Y", "not_a_real_type", "https://x.com/e", "tavily")
    assert ev.event_type == "other"


# --- Audience: US college upperclassmen --------------------------------------

def _mk(name, company, loc="Online", source="devpost"):
    return Event(company, name, "hackathon", "https://x.com/a", source,
                 location_city_state=loc)


@pytest.mark.parametrize("name,company", [
    ("AQX Sports Analytics Data Bowl 3.0", "James Logan High School"),
    ("3D Websites Hackathon", "high school"),
    ("AI YES: International Youth AI Competition", "International AI Youth Society"),
    ("Teen Code Jam", "TeenHacks"),
    ("Middle School Robotics Challenge", "Some District"),
])
def test_pre_college_events_rejected(name, company):
    ok, reason = passes_hard_filters(_mk(name, company))
    assert not ok and "pre-college" in reason


@pytest.mark.parametrize("name,company", [
    ("NTU InnovateX Hackathon 2026", "nanyang technological university"),
    ("Africa Deep Tech Challenge 2026", "Africa Deep Tech Foundation"),
    ("Win4AISafety", "SAIN Utrecht"),
    ("The Great Agent Hackathon", "Women In Product India"),
])
def test_non_us_virtual_events_rejected(name, company):
    ok, reason = passes_hard_filters(_mk(name, company))
    assert not ok and "non-US" in reason


@pytest.mark.parametrize("name,company,loc,src", [
    ("2027 Code for Good Hackathon", "JPMorgan Chase", "Plano, TX", "greenhouse"),
    ("Anthropic Fellows Program", "Anthropic", "San Francisco, CA", "greenhouse"),
    ("HackDavis 2026", "Major League Hacking", "Davis, California", "mlh"),
    ("Engineering Insight Series 2027", "Goldman Sachs", "New York, NY", "greenhouse"),
    ("Rowdy Hacks", "Major League Hacking", "San Antonio, Texas", "mlh"),
    ("Build with Gemini XPRIZE", "XPRIZE", "Online", "devpost"),
])
def test_real_college_events_still_pass(name, company, loc, src):
    ok, reason = passes_hard_filters(_mk(name, company, loc, src))
    assert ok, f"false negative on a real target: {name} ({reason})"


def test_college_junior_is_not_junior_high():
    """'Junior' means a college year level, not middle school."""
    ok, _ = passes_hard_filters(
        _mk("Junior Engineering Summit", "Meta", "Menlo Park, CA", "greenhouse")
    )
    assert ok


def test_us_based_in_person_event_not_dropped_by_org_check():
    """The non-US organizer check applies only to virtual events."""
    ok, _ = passes_hard_filters(
        _mk("India House Hackathon", "Rice University", "Houston, TX", "mlh")
    )
    assert ok


@pytest.mark.parametrize("name,company,loc", [
    ("INNOVIK 6.0 : International Hackathon 2026", "shekunj", "VITM, Indore"),
    ("Nexora", "hackhere", "SNS Innovation HUB"),
])
def test_unrecognized_venue_on_open_feed_rejected(name, company, loc):
    """Open submission feeds take worldwide organizers, so require a US signal."""
    ok, reason = passes_hard_filters(_mk(name, company, loc, "devpost"))
    assert not ok and "US location signal" in reason


@pytest.mark.parametrize("loc", ["RWC HQ", "SF", "Washington D.C.", "North America", ""])
def test_ats_boards_keep_benefit_of_the_doubt(loc):
    """US company boards keep vague venues rather than dropping real events."""
    ok, _ = passes_hard_filters(_mk("Builder Fellowship", "WindBorne", loc, "ashby"))
    assert ok, f"ATS event wrongly dropped for venue {loc!r}"


def test_virtual_event_on_open_feed_still_allowed():
    ok, _ = passes_hard_filters(_mk("Build with Gemini XPRIZE", "XPRIZE", "Online", "devpost"))
    assert ok
