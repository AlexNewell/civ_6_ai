"""Record a game you are playing yourself, without touching it.

Read-only: it never ends a turn, dismisses a popup, loads a save or moves the camera. Civ must be
running with the tuner on, and nothing else may be connected to the tuner. Writes
runs/human_<label>_<UTC>*.jsonl, which the scoring scripts read like an AI run.
"""

import argparse
import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path

from civ_mcp.connection import GameConnection
from civ_mcp.game_state import GameState

from civ6_ai.observe import Observer

ROOT = Path(__file__).parent.parent


async def run(label: str, until_turn: int | None, resume: str | None) -> int:
    connection = GameConnection()
    await connection.connect()
    game = GameState(connection)
    name = resume or f"human_{label}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    observer = Observer(game, ROOT / "runs", name)
    verb = f"Resuming from turn {observer.last_turn}" if resume else "Observing"
    print(f"{verb} (read-only). Writing runs/{name}*.jsonl. Ctrl+C to stop.")
    with contextlib.suppress(KeyboardInterrupt):
        await observer.watch(until_turn)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", default="game", help="short name, e.g. the save or civ")
    parser.add_argument("--until-turn", type=int, help="stop after recording this turn")
    parser.add_argument("--resume", help="run name to carry on, e.g. human_rome_20261119T193113Z")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.label, args.until_turn, args.resume))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
