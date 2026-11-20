from datetime import UTC, datetime, timedelta, timezone

from civ6_ai.notes import match_notes, parse_transcript, render_markdown, turn_at

EASTERN = timezone(timedelta(hours=-5))
START = datetime(2026, 11, 19, 18, 37, 12, tzinfo=UTC)
TRANSCRIPT = """# Civ 6 Game Testing 12345

## Transcript
[0:06] Speaker 1: Testing, 1, 2, 3, 4.
[0:08] Speaker 1:  This is for a Civ 6 game.
[1:47] Speaker 1:  And now see if there's a different timestamp associated with this line.
[2:32] Speaker 1:  Closing out the transcript now.
[1:39pm] -- Paused --
[1:40pm] -- Resumed --
[0:05] Speaker 2: Testing resuming the note as a way to get a new timestamp.
[1:40pm] -- Paused --
"""


def test_first_segment_lines_use_offset_from_start():
    notes = parse_transcript(TRANSCRIPT, START, EASTERN)
    assert len(notes) == 5
    assert notes[2].at == datetime(2026, 11, 19, 18, 38, 59, tzinfo=UTC)
    assert notes[3].at == datetime(2026, 11, 19, 18, 39, 44, tzinfo=UTC)
    assert {n.precision for n in notes[:4]} == {"second"}
    assert notes[2].text.startswith("And now see")


def test_resumed_segment_is_anchored_on_the_marker_clock_time():
    notes = parse_transcript(TRANSCRIPT, START, EASTERN)
    # 1:40pm at UTC-5 is 18:40:00Z, plus the 5 second offset.
    assert notes[4].at == datetime(2026, 11, 19, 18, 40, 5, tzinfo=UTC)
    assert (notes[4].speaker, notes[4].precision) == ("Speaker 2", "minute")


def test_hour_offsets_parse():
    notes = parse_transcript("[1:02:03] Speaker 1: long", START, EASTERN)
    assert notes[0].at == START + timedelta(hours=1, minutes=2, seconds=3)


def test_turn_at_picks_newest_turn_started():
    turns = [(START, 10), (START + timedelta(seconds=60), 11)]
    assert turn_at(turns, START - timedelta(seconds=1)) is None
    assert turn_at(turns, START + timedelta(seconds=59)) == (10, 59.0)
    assert turn_at(turns, START + timedelta(seconds=75)) == (11, 15.0)


def test_match_and_render():
    observations = [
        {"turn": 36, "observed_at": "2026-11-19T18:37:00+00:00"},
        {"turn": 37, "observed_at": "2026-11-19T18:38:30+00:00"},
    ]
    rows = match_notes(parse_transcript(TRANSCRIPT, START, EASTERN), observations)
    assert [r["turn"] for r in rows] == [36, 36, 37, 37, 37]
    text = render_markdown(rows)
    assert "- T37 (+29s): And now see" in text
    assert "- T37 (+95s) ~: Testing resuming" in text


def test_resume_after_midnight_lands_on_the_next_day():
    late = datetime(2026, 11, 20, 4, 50, 0, tzinfo=UTC)  # 11:50pm at UTC-5
    text = (
        "[0:10] Speaker 1: before\n[11:55pm] -- Paused --\n[12:05am] -- Resumed --\n[0:05] S: after"
    )
    notes = parse_transcript(text, late, EASTERN)
    assert notes[1].at == datetime(2026, 11, 20, 5, 5, 5, tzinfo=UTC)  # 12:05:05am on the 20th
