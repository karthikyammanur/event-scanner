"""README events board rendering tests."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import board  # noqa: E402


def _rec(**kw):
    base = {
        "emailed_at": "2026-08-08T05:00:00+00:00",
        "company": "JPMorgan",
        "event_name": "Code for Good Hackathon",
        "event_type": "hackathon",
        "url": "https://careers.jpmorgan.com/cfg",
        "source": "greenhouse",
        "start_date": None,
        "application_deadline": "2026-09-01",
        "date_posted": "2026-07-30",
        "location_city_state": "Plano, TX",
        "travel_credit_mentioned": None,
    }
    base.update(kw)
    return base


def test_table_has_event_details_and_link():
    table = board.render_table({"a": _rec()})
    assert "Code for Good Hackathon" in table
    assert "JPMorgan" in table
    assert "2026-07-30" in table
    assert "[Apply](https://careers.jpmorgan.com/cfg)" in table


def test_pipe_in_name_cannot_break_the_table():
    table = board.render_table({"a": _rec(event_name="Hack | Summit")})
    row = [ln for ln in table.splitlines() if "Hack" in ln][0]
    assert "\\|" in row, "literal pipe must be escaped"
    # Removing the escaped pipe leaves only real column separators.
    assert row.replace("\\|", "").count("|") == 8


def test_newline_in_field_is_flattened():
    table = board.render_table({"a": _rec(company="Acme\nCorp")})
    assert "Acme Corp" in table


def test_out_of_state_row_is_flagged():
    table = board.render_table({"a": _rec(location_city_state="New York, NY")})
    row = [ln for ln in table.splitlines() if "Code for Good" in ln][0]
    assert "⚑" in row


def test_texas_row_not_flagged():
    table = board.render_table({"a": _rec(location_city_state="Plano, TX")})
    row = [ln for ln in table.splitlines() if "Code for Good" in ln][0]
    assert "⚑" not in row  # the legend line always mentions the flag


def test_newest_first():
    seen = {
        "old": _rec(event_name="Older", emailed_at="2026-01-01T00:00:00+00:00"),
        "new": _rec(event_name="Newer", emailed_at="2026-08-08T00:00:00+00:00"),
    }
    table = board.render_table(seen)
    assert table.index("Newer") < table.index("Older")


def test_empty_state_renders_placeholder():
    assert "No events found yet" in board.render_table({})


def test_legacy_hash_only_entries_are_skipped():
    """Old anonymized entries have no event_name and must not render blank rows."""
    seen = {"abc": {"emailed_at": "2026-01-01T00:00:00+00:00"}, "b": _rec()}
    table = board.render_table(seen)
    assert table.count("| [Apply]") <= 1
    assert "Code for Good Hackathon" in table


def test_update_readme_replaces_between_markers(tmp_path):
    p = tmp_path / "README.md"
    p.write_text(
        f"# Title\n\nintro\n\n{board.START}\n\nold table\n\n{board.END}\n\n## Setup\n",
        encoding="utf-8",
    )
    assert board.update_readme({"a": _rec()}, str(p)) is True
    out = p.read_text(encoding="utf-8")
    assert "old table" not in out
    assert "Code for Good Hackathon" in out
    assert "# Title" in out and "## Setup" in out, "content outside markers preserved"


def test_update_readme_without_markers_is_a_noop(tmp_path):
    p = tmp_path / "README.md"
    p.write_text("# No markers here\n", encoding="utf-8")
    assert board.update_readme({"a": _rec()}, str(p)) is False
    assert p.read_text(encoding="utf-8") == "# No markers here\n"


def test_no_em_dash_in_table():
    table = board.render_table({"a": _rec(event_name="Summit — Fall")})
    assert "—" not in table
