"""Record a game you are playing yourself, without touching it.

Read-only, except one start save (`--no-start-save` skips it): it never ends a turn, dismisses a
popup, loads a save or moves the camera. Civ must be running with the tuner on, and nothing else
may be connected to the tuner. Writes runs/human_<label>_<UTC>*.jsonl, which the scoring scripts
read like an AI run. `--screenshots` adds passive shots at turns 25 to 150.
"""

import argparse
import asyncio
import contextlib
from datetime import UTC, datetime
from pathlib import Path

from civ_mcp.connection import GameConnection
from civ_mcp.game_state import GameState

from civ6_ai.observe import Observer, write_header
from civ6_ai.runner.loader import save_checkpoint
from civ6_ai.screenshots import Screenshotter

ROOT = Path(__file__).parent.parent


async def run(
    label: str,
    until_turn: int | None,
    resume: str | None,
    screenshots: bool,
    start_save: bool,
) -> int:
    connection = GameConnection()
    await connection.connect()
    game = GameState(connection)
    name = resume or f"human_{label}_{datetime.now(UTC):%Y%m%dT%H%M%SZ}"
    shots = None
    if screenshots:
        taker = Screenshotter(ROOT / "runs" / "screenshots", name, cap=100)

        def shots(turn: int) -> None:
            taker.take(turn, "observed", stop=True)

    observer = Observer(game, ROOT / "runs", name, shots=shots)
    started = datetime.now(UTC)
    turn = (await game.get_game_overview()).turn
    save_name = None
    if start_save:
        wanted = f"HUMAN_{label}_T{turn}"
        # The observer's one write: a save of the game as it stands.
        if await save_checkpoint(connection.execute_write, wanted):
            save_name = wanted
    if not resume:
        await write_header(
            ROOT / "runs" / f"{name}_header.json",
            game,
            connection.execute_write,
            save_name,
            started,
        )
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
    parser.add_argument("--screenshots", action="store_true", help="shots at turns 25 to 150")
    saving = parser.add_mutually_exclusive_group()
    saving.add_argument("--no-start-save", action="store_true", help="skip the start save")
    saving.add_argument("--start-save", action="store_true", help="save at start even on --resume")
    args = parser.parse_args()
    start_save = args.start_save or not (args.no_start_save or args.resume)
    try:
        return asyncio.run(
            run(args.label, args.until_turn, args.resume, args.screenshots, start_save)
        )
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
