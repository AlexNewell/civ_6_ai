"""Attach turn numbers to dictated notes pasted from Wispr Flow Notetaker.

Copy the note's markdown into a file, then:
    python scripts/import_notes.py runs/human_<label>_<time> \
        --note notes.md 2026-11-19T18:37:12Z --note part2.md 2026-11-19T21:41:52Z

Each `--note` is a file and the time its recording began (Wispr's note start time, in UTC). Give
one per recording; they are merged in time order. The run is the name the observer printed,
without a suffix. Writes <run>_notes.jsonl and <run>_notes.md beside it.
"""

import argparse
import json
from datetime import datetime
from pathlib import Path

from civ6_ai.notes import match_notes, parse_transcript, render_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path, help="run path without suffix, e.g. runs/human_x_2026...")
    parser.add_argument(
        "--note",
        nargs=2,
        action="append",
        required=True,
        metavar=("FILE", "START"),
        help="pasted markdown file and the recording start (ISO, with timezone); repeatable",
    )
    args = parser.parse_args()

    observed = Path(f"{args.run}_observed.jsonl")
    observations = [json.loads(line) for line in observed.read_text("utf-8").splitlines() if line]
    if not any(row.get("observed_at") for row in observations):
        print(f"{observed.name} has no observed_at times; it was made by an older observer.")
        return 1
    notes = []
    for file, start_text in args.note:
        start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        notes += parse_transcript(Path(file).read_text("utf-8"), start)
    rows = match_notes(sorted(notes, key=lambda note: note.at), observations)
    Path(f"{args.run}_notes.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    Path(f"{args.run}_notes.md").write_text(render_markdown(rows), encoding="utf-8")
    print(f"Matched {len(rows)} notes to turns -> {args.run}_notes.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
