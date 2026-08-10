"""Deterministic prefilter (pre-LLM) and the three hard filters (post-LLM)."""

from __future__ import annotations

import re
from typing import Optional, Tuple

from models import Candidate, Event

EVENT_KEYWORDS = {
    "hackathon": "hackathon",
    "code for good": "hackathon",
    "codeforgood": "hackathon",
    "hack day": "hackathon",
    "hack-a-thon": "hackathon",
    "datathon": "hackathon",
    "summit": "summit",
    "insight": "insight_program",
    "insight program": "insight_program",
    "discovery day": "insight_program",
    "immersion": "insight_program",
    "externship": "externship",
    "fellowship": "fellowship",
    "conference": "conference",
    "symposium": "conference",
    "bootcamp": "other",
    "workshop": "other",
    "career day": "insight_program",
    "open house": "insight_program",
    "scholars program": "fellowship",
    "fellows program": "fellowship",
    "fellows": "fellowship",
    "explore program": "insight_program",
    "launchpad": "insight_program",
}

# Words that mark a listing as a real job rather than an event.
JOB_TITLE_MARKERS = (
    r"\bsoftware engineer(?:ing)?\b",
    r"\bengineer\b",
    r"\bdeveloper\b",
    r"\bscientist\b",
    r"\banalyst\b",
    r"\bmanager\b",
    r"\bdirector\b",
    r"\brecruiter\b",
    r"\bdesigner\b",
    r"\baccountant\b",
    r"\bconsultant\b",
    r"\brepresentative\b",
    r"\bassociate\b",
    r"\bspecialist\b",
    r"\barchitect\b",
    r"\bcounsel\b",
    r"\btechnician\b",
    r"\badministrator\b",
)

SENIORITY_MARKERS = (
    r"\bsenior\b",
    r"\bstaff\b",
    r"\bprincipal\b",
    r"\blead\b",
    r"\bjunior\b",
    r"\bmid-level\b",
    r"\bvp\b",
    r"\bhead of\b",
    r"\bl[3-7]\b",
    r"\bii+\b",
)

# Full time / permanent role signals.
EMPLOYMENT_MARKERS = (
    r"\bfull[- ]time\b",
    r"\bpart[- ]time\b",
    r"\bcontractor\b",
    r"\bpermanent\b",
    r"\bsalary\b",
    r"\bnew grad(?:uate)?\b",
    r"\bentry[- ]level\b",
)

# Prefix-anchored (no trailing \b) so "Cybersecurity", "Technology", and
# "Engineering" all match without needing a variant entry each.
TECH_MARKERS = (
    r"\bsoftware", r"\bengineer", r"\btech", r"\bcoding\b", r"\bcode\b",
    r"\bdevelop", r"\bdata\b", r"\bai\b", r"\bml\b", r"\bmachine learning\b",
    r"\bcomputer\b", r"\bcs\b", r"\bcyber", r"\bsecurity\b", r"\bcloud\b",
    r"\bproduct\b", r"\bhack", r"\bdevops\b", r"\bprogramming\b",
    r"\brobotics\b", r"\bquant", r"\bblockchain\b", r"\bstem\b", r"\bdigital\b",
    r"\binformation systems\b", r"\bapi\b", r"\binfrastructure\b",
)

STUDENT_MARKERS = (
    r"\bstudent\b", r"\buniversity\b", r"\bcollege\b", r"\bcampus\b",
    r"\bundergrad(?:uate)?\b", r"\bfreshman\b", r"\bsophomore\b", r"\bjunior\b",
    r"\bsenior\b", r"\bearly career\b", r"\bemerging talent\b", r"\bclass of\b",
    r"\bgraduating\b", r"\bintern\b",
)

# Pre-college audiences. Karthik is an upperclassman, so these are out of scope.
PRE_COLLEGE_MARKERS = (
    r"\bhigh\s?school(?:s|ers?)?\b",
    r"\bhs\s+(?:student|hacker|hackathon)",
    r"\bmiddle\s?school\b",
    r"\bteens?\b",
    r"\bteenager",
    r"\byouth\b",
    r"\bk-?12\b",
    r"\bgrades?\s*[1-9]\b",
    r"\b(?:9|10|11|12)th\s+grade",
    r"\bjunior\s+high\b",
    r"\bunder\s*1[6-8]\b",
    r"\bages?\s*1[0-7]\b",
    r"\bminors?\b",
    r"\bprimary\s+school\b",
    r"\bsecondary\s+school\b",
    r"\bpre-?college\b",
    r"\belementary\b",
)

# Non-US organizers seen in practice on the open hackathon feeds.
NON_US_ORG_MARKERS = (
    r"\bnanyang\b", r"\bntu\b", r"\biit\b", r"\bnit\b", r"\butrecht\b",
    r"\bkang chiao\b", r"\bafrica\b", r"\bafrican\b", r"\beurope(?:an)?\b",
    r"\basia(?:n)?\b", r"\blatam\b", r"\bindia\b", r"\bnigeria\b", r"\bkenya\b",
    r"\bpakistan\b", r"\bbangladesh\b", r"\bsingapore\b", r"\bmalaysia\b",
    r"\bindonesia\b", r"\bvietnam\b", r"\bphilippines\b", r"\bsri lanka\b",
    r"\bnepal\b", r"\bghana\b", r"\begypt\b", r"\bmorocco\b", r"\btunisia\b",
    r"\buae\b", r"\bqatar\b", r"\bsaudi\b", r"\bturkey\b", r"\bbrazil\b",
    r"\bcolombia\b", r"\bargentina\b", r"\bperu\b", r"\bchile\b",
    r"\bglobal south\b", r"\binternational youth\b",
    r"\bcanadian\b", r"\bcanada\b", r"\bbritish\b", r"\bgerman\b",
    r"\bfrench\b", r"\bspanish\b", r"\bitalian\b", r"\bdutch\b",
    r"\bswedish\b", r"\bnorwegian\b", r"\bdanish\b", r"\bswiss\b",
    r"\baustralian\b", r"\bjapanese\b", r"\bkorean\b", r"\bchinese\b",
    r"\bmexican\b", r"\bbrazilian\b", r"\bisraeli\b", r"\bemirati\b",
)

US_STATE_ABBR = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}

US_STATE_NAMES = {
    "alabama","alaska","arizona","arkansas","california","colorado","connecticut",
    "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
    "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
    "minnesota","mississippi","missouri","montana","nebraska","nevada",
    "new hampshire","new jersey","new mexico","new york","north carolina",
    "north dakota","ohio","oklahoma","oregon","pennsylvania","rhode island",
    "south carolina","south dakota","tennessee","texas","utah","vermont",
    "virginia","washington","west virginia","wisconsin","wyoming",
    "district of columbia","washington dc","washington, d.c.",
}

# Unambiguous non-US signals only, to avoid false rejections.
NON_US_MARKERS = (
    r"\bunited kingdom\b", r"\bengland\b", r"\bscotland\b", r"\bireland\b",
    r"\bcanada\b", r"\bontario\b", r"\bquebec\b", r"\bindia\b", r"\bgermany\b",
    r"\bfrance\b", r"\bspain\b", r"\bitaly\b", r"\bnetherlands\b", r"\bpoland\b",
    r"\bsweden\b", r"\bnorway\b", r"\bdenmark\b", r"\bswitzerland\b",
    r"\baustralia\b", r"\bsingapore\b", r"\bjapan\b", r"\bchina\b", r"\bkorea\b",
    r"\bbrazil\b", r"\bmexico\b", r"\bisrael\b", r"\buae\b", r"\bdubai\b",
    r"\bnigeria\b", r"\bkenya\b", r"\bpakistan\b", r"\bbangladesh\b",
    r"\bphilippines\b", r"\bvietnam\b", r"\bindonesia\b", r"\bthailand\b",
    r"\bportugal\b", r"\bbelgium\b", r"\baustria\b", r"\bfinland\b",
    r"\bhyderabad\b", r"\bbangalore\b", r"\bbengaluru\b", r"\bmumbai\b",
    r"\bdelhi\b", r"\bchennai\b", r"\bpune\b", r"\btoronto\b", r"\bvancouver\b",
    r"\blondon\b", r"\bberlin\b", r"\bparis\b", r"\bbarcelona\b", r"\bmadrid\b",
    r"\bamsterdam\b", r"\bzurich\b", r"\bsydney\b", r"\bmelbourne\b",
    r"\btokyo\b", r"\bseoul\b", r"\btel aviv\b", r"\bwarsaw\b",
)

VIRTUAL_MARKERS = (r"\bvirtual\b", r"\bonline\b", r"\bremote\b", r"\banywhere\b")


def _any(patterns, text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def matched_event_keyword(text: str) -> Optional[Tuple[str, str]]:
    """Return (keyword, implied_event_type) for the first event keyword found."""
    low = (text or "").lower()
    # Longest keywords first so "code for good" beats a bare "code".
    for kw in sorted(EVENT_KEYWORDS, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(kw)}(?![a-z])", low):
            return kw, EVENT_KEYWORDS[kw]
    return None


def looks_like_job_posting(title: str) -> bool:
    """True when the title reads as a standard role rather than an event."""
    t = title or ""
    if _any(SENIORITY_MARKERS, t) and _any(JOB_TITLE_MARKERS, t):
        return True
    if _any(EMPLOYMENT_MARKERS, t):
        return True

    # The head of the title (before the first comma or dash) carries the role.
    head = re.split(r"[,\-–:|(]", t, maxsplit=1)[0]
    if _any(JOB_TITLE_MARKERS, head) and matched_event_keyword(head) is None:
        return True

    if _any(JOB_TITLE_MARKERS, t):
        kw = matched_event_keyword(t)
        if kw is None:
            return True
        # Hackathon is a strong enough event noun to win over a discipline word.
        if kw[1] not in {"hackathon"}:
            return True
    return False


def prefilter(cand: Candidate) -> Tuple[bool, str]:
    """Cheap gate deciding whether a candidate earns an LLM call.

    Returns (keep, reason). Biased toward recall, the LLM adjudicates the rest.
    """
    title = cand.title or ""
    if not title.strip():
        return False, "empty title"
    if not cand.url:
        return False, "no url"

    kw = matched_event_keyword(title)
    if kw is None:
        # Some sources (Devpost, MLH) are event-only feeds; their candidates do
        # not need an event keyword in the title to qualify.
        if cand.extra.get("source_is_event_feed"):
            return True, "event-only source"
        return False, "no event keyword in title"

    if looks_like_job_posting(title):
        return False, f"reads as job posting (kw={kw[0]})"

    return True, f"event keyword: {kw[0]}"


def is_us_or_virtual(location: Optional[str]) -> bool:
    """US-based or virtual. Unknown location is kept, never silently dropped."""
    loc = (location or "").strip()
    if not loc:
        return True
    if _any(VIRTUAL_MARKERS, loc):
        return True

    # Multi-location postings are common, one US option is enough to qualify.
    if _has_us_signal(loc):
        return True
    if _any(NON_US_MARKERS, loc):
        return False
    # No US signal and no non-US signal: keep it, flag downstream.
    return True


def _has_us_signal(loc: str) -> bool:
    if re.search(r"\b(usa|u\.s\.a?\.?|united states)\b", loc, re.I):
        return True
    # ", TX" / ", California" style, across every comma or semicolon segment.
    for p in (p.strip() for p in re.split(r"[,;/|]", loc)):
        if not p:
            continue
        if p.upper() in US_STATE_ABBR or p.lower() in US_STATE_NAMES:
            return True
    return False


def is_tech_related(text: str) -> bool:
    return _any(TECH_MARKERS, text or "")


# Tech by construction, so no explicit tech noun is required in the title.
TECH_BY_CONSTRUCTION_SOURCES = {"greenhouse", "lever", "ashby", "devpost", "mlh"}


def is_pre_college(text: str) -> bool:
    """True when the audience reads as high school or younger."""
    return _any(PRE_COLLEGE_MARKERS, text or "")


def looks_non_us(text: str) -> bool:
    """True when the organizer or framing reads as non-US."""
    return _any(NON_US_ORG_MARKERS, text or "") or _any(NON_US_MARKERS, text or "")


# Open submission feeds accept organizers worldwide, so an in-person venue there
# has to show a positive US signal. ATS boards are US company boards already,
# and MLH sets a real country code, so those keep the benefit of the doubt.
_STRICT_US_SOURCES = {"devpost", "discovery"}


def has_us_location_signal(location: Optional[str]) -> bool:
    """True when the location names a recognizable US place."""
    loc = (location or "").strip()
    return bool(loc) and _has_us_signal(loc)


def passes_hard_filters(ev: Event) -> Tuple[bool, str]:
    """Post-extraction enforcement of the hard filters."""
    if not is_us_or_virtual(ev.location_city_state):
        return False, f"not US and not virtual: {ev.location_city_state}"

    blob = " ".join(
        filter(None, [ev.event_name, ev.company, ev.event_type, ev.url])
    )
    if not is_tech_related(blob) and ev.source not in TECH_BY_CONSTRUCTION_SOURCES:
        return False, "not tech related"

    if looks_like_job_posting(ev.event_name or ""):
        return False, "reads as a standard job posting, not an event"

    # Audience: college upperclassmen, so pre-college events are out.
    audience = " ".join(filter(None, [ev.event_name, ev.company]))
    if is_pre_college(audience):
        return False, "pre-college audience"

    # A virtual event's location says "Online" and hides the organizer's
    # country, so check the name and company for a non-US signal instead.
    if ev.is_virtual() and looks_non_us(audience):
        return False, "virtual event with a non-US organizer"

    # On open submission feeds an unrecognized venue ("VITM, Indore") is more
    # often foreign than a US place the state list happens to miss.
    if (
        ev.source in _STRICT_US_SOURCES
        and not ev.is_virtual()
        and not has_us_location_signal(ev.location_city_state)
    ):
        return False, f"no US location signal: {ev.location_city_state or 'unknown'}"

    return True, "ok"
