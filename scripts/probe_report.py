"""List a game's probe screenshots with their sidecar data, as a markdown table. No game calls.

    python scripts/probe_report.py [GAME_ID] [--dir runs/screenshots]

With no game id, every game's probe shots are listed. Screenshots can show player names, so keep
the output out of the repo.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
COLUMNS = ("turn", "kind", "city", "item", "tile", "reason_class", "targets", "seconds", "image")


def load(directory: Path, game_id: str = "") -> list[dict]:
    """Sidecars of probe shots in `directory`, oldest turn first. Unreadable ones are skipped."""
    rows = []
    for path in sorted(directory.glob(f"{game_id}*_probe-*.json")):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(rows, key=lambda row: row.get("turn", 0))


def _cell(row: dict, column: str) -> str:
    if column == "tile":
        tile = row.get("tile")
        return f"{tile[0]},{tile[1]}" if tile else f"city {row.get('x')},{row.get('y')}"
    if column == "targets":
        count = row.get("target_count")
        return "" if count is None else str(count)
    return str(row.get(column, "")).replace("|", "/")


def markdown(rows: list[dict]) -> str:
    if not rows:
        return "No probe screenshots found."
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    lines += ["| " + " | ".join(_cell(row, c) for c in COLUMNS) + " |" for row in rows]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("game_id", nargs="?", default="")
    parser.add_argument("--dir", type=Path, default=ROOT / "runs" / "screenshots")
    args = parser.parse_args()
    print(markdown(load(args.dir, args.game_id)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
