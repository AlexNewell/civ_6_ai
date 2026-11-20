"""Match a dictated Wispr Flow Notetaker transcript to the turns of an observed human game.

The transcript is the markdown copied from Notetaker. Each line has an elapsed time from the
start of the recording, `[1:47] Speaker 1: text`, so given the wall-clock time the recording
started, every line has an absolute time. The observer logs when it saw each turn begin
(`observed_at` in `<run>_observed.jsonl`), and a note belongs to the newest turn begun by then.

Pause and resume: elapsed times restart after a resume, and the only anchor is the clock time on
the `-- Resumed --` marker, which has minute precision. Lines in a resumed segment are marked
`precision: "minute"` (about 60 seconds either way). One unbroken recording is more exact.
"""

import re
from bisect import bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

_LINE = re.compile(r"^\[(?:(\d+):)?(\d+):(\d{2})\]\s*([^:\n]+?):\s*(.*)$")
_MARKER = re.compile(r"^\[(\d{1,2}):(\d{2})\s*([ap]m)\]\s*--\s*(Paused|Resumed)\s*--\s*$", re.I)


@dataclass(frozen=True)
class Note:
    at: datetime  # UTC
    offset_seconds: int  # Into its recording segment.
    speaker: str
    text: str
    precision: str  # "second" for the first segment, "minute" after a resume.


def _clock_on(after: datetime, hour: int, minute: int, meridiem: str, zone: tzinfo) -> datetime:
    """The first `hour:minute` clock time not before `after`, so a resume past midnight lands on
    the next day. The clock time is floored to the minute, so it may sit up to a minute before
    the true time and still count as the same day."""
    hour = hour % 12 + (12 if meridiem.lower() == "pm" else 0)
    local_day = after.astimezone(zone)
    clock = local_day.replace(hour=hour, minute=minute, second=0, microsecond=0)
    while clock + timedelta(minutes=1) < local_day:
        clock += timedelta(days=1)
    return clock.astimezone(UTC)


def parse_transcript(text: str, start: datetime, zone: tzinfo | None = None) -> list[Note]:
    """Turn pasted Notetaker markdown into notes with absolute times.

    `start` is when the recording began (Wispr's note start time; must carry a timezone).
    `zone` is the zone of the `1:40pm` clock times, default the machine's local zone.
    """
    if start.tzinfo is None:
        raise ValueError("start needs a timezone, e.g. 2026-11-19T18:37:12Z")
    zone = zone or start.astimezone().tzinfo
    assert zone is not None
    segment_start = start.astimezone(UTC)
    latest = segment_start  # The newest time so far; a resume can't be earlier than it.
    precision = "second"
    notes: list[Note] = []
    for raw in text.splitlines():
        line = raw.strip()
        marker = _MARKER.match(line)
        if marker:
            if marker.group(4).lower() == "resumed":
                hour, minute, meridiem = int(marker.group(1)), int(marker.group(2)), marker.group(3)
                segment_start = _clock_on(latest, hour, minute, meridiem, zone)
                latest = max(latest, segment_start)
                precision = "minute"
            continue
        match = _LINE.match(line)
        if not match:
            continue
        hours, minutes, seconds, speaker, body = match.groups()
        offset = int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)
        at = segment_start + timedelta(seconds=offset)
        latest = max(latest, at)
        notes.append(Note(at, offset, speaker.strip(), body.strip(), precision))
    return notes


def turn_at(turns: list[tuple[datetime, int]], at: datetime) -> tuple[int, float] | None:
    """The turn in play at `at` and the seconds since it began, or None before the first turn.

    `turns` is (when the observer saw the turn begin, turn number), sorted by time.
    """
    index = bisect_right([when for when, _ in turns], at) - 1
    if index < 0:
        return None
    when, turn = turns[index]
    return turn, (at - when).total_seconds()


def match_notes(notes: list[Note], observations: list[dict]) -> list[dict]:
    """Add the turn to each note. `observations` are rows of `<run>_observed.jsonl`."""
    turns = sorted(
        (datetime.fromisoformat(row["observed_at"]), row["turn"])
        for row in observations
        if row.get("observed_at")
    )
    rows = []
    for note in notes:
        found = turn_at(turns, note.at)
        rows.append(
            {
                "at": note.at.isoformat(),
                "turn": found[0] if found else None,
                "seconds_into_turn": round(found[1]) if found else None,
                "precision": note.precision,
                "speaker": note.speaker,
                "text": note.text,
            }
        )
    return rows


def render_markdown(rows: list[dict]) -> str:
    """A readable list: the turn each note was said on, then the text."""
    lines = []
    for row in rows:
        turn = f"T{row['turn']} (+{row['seconds_into_turn']}s)" if row["turn"] else "before T1"
        mark = " ~" if row["precision"] == "minute" else ""
        lines.append(f"- {turn}{mark}: {row['text']}")
    return "\n".join(lines) + "\n"
