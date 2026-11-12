"""Take one screenshot of the open Civ VI game for triage, within the game's cap of 20.

Used while a stopped game is still open: the automatic shots are taken at the moment of a stop, and
this adds one more. It finds the game from the newest turn log in `runs/`, so it uses the same
count as the run. Files go to `runs/screenshots/`, which is git-ignored.
"""

import argparse
import json
import sys
from pathlib import Path

from civ6_ai.metrics.costs import game_id_of_log
from civ6_ai.screenshots import Screenshotter
from civ6_ai.triage import newest_turn_log

ROOT = Path(__file__).parent.parent
RUNS = ROOT / "runs"


def last_turn(log: Path) -> int | None:
    for line in reversed(log.read_text(encoding="utf-8").splitlines()):
        if line.strip():
            return int(json.loads(line)["turn"])
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reason", help="a few words for the file name, e.g. blocked-popup")
    parser.add_argument("--log", type=Path, help="the turn log to use (default: the newest)")
    parser.add_argument("--stop", action="store_true", help="may use the slots kept for stops")
    args = parser.parse_args()

    log = args.log or newest_turn_log(RUNS)
    if log is None:
        print("No turn log in runs/, so there is no game to attach the screenshot to")
        return 1
    game_id = game_id_of_log(RUNS, log)
    turn = last_turn(log)
    if game_id is None or turn is None:
        print(f"{log.name} has no finished turn yet, so it has no game id")
        return 1
    shots = Screenshotter(RUNS / "screenshots", game_id)
    path = shots.take(turn, args.reason, stop=args.stop)
    if path is None:
        print(f"No screenshot taken ({shots.count()} of the game's cap used; see the bot log)")
        return 1
    print(path)
    print(f"Game {game_id}, turn {turn}, from {log.name}. Pass --log if that isn't the open game.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
