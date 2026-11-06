"""Play turns in the open Civ VI game with the turn loop.

Plays on from whatever turn the game is on, and writes per-turn timing to `runs/`. It only runs on
a standard save, which it recognises by seed, because other games would give meaningless results.
Civ must be running with the tuner on and one of those saves open. It doesn't load saves.
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from civ_mcp.connection import GameConnection
from civ_mcp.game_state import GameState

from civ6_ai.bot import SettleDecisionLog, build_orchestrator
from civ6_ai.metrics.costs import game_id_of_log
from civ6_ai.monitors import EventBus, EventLog
from civ6_ai.orchestrator import StopReason, TurnLog
from civ6_ai.saves import use_real_save_dirs

ROOT = Path(__file__).parent.parent
MANIFEST = ROOT / "config" / "standard_saves.json"


def identify_save(manifest: dict, game_seed: int) -> str | None:
    """Return the name of the standard save with this seed, if there is one."""
    for save in manifest["saves"]:
        if save["recorded"]["game_seed"] == str(game_seed):
            return save["name"]
    return None


async def run(turns: int) -> int:
    use_real_save_dirs()
    connection = GameConnection()
    await connection.connect()
    game = GameState(connection)

    _, seed = await game.get_game_identity()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    name = identify_save(manifest, seed)
    if name is None:
        print("FAILED: the open game isn't a standard save")
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    turn_log = TurnLog(ROOT / "runs" / f"{name}_{stamp}.jsonl")
    bus = EventBus()
    bus.subscribe(EventLog(ROOT / "runs" / f"{name}_{stamp}_events.jsonl").write)
    # Policy decisions are logged at INFO. Keep them beside the turn log, since they explain it.
    file_log = logging.FileHandler(ROOT / "runs" / f"{name}_{stamp}_bot.log", encoding="utf-8")
    file_log.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logging.getLogger("civ6_ai").addHandler(file_log)
    logging.getLogger("civ6_ai").setLevel(logging.INFO)
    decisions = SettleDecisionLog(ROOT / "runs" / f"{name}_{stamp}_settle.jsonl")
    decisions.attach()
    try:
        reason = await build_orchestrator(game, bus, turn_log).play(turns)
    finally:
        decisions.detach()
        logging.getLogger("civ6_ai").removeHandler(file_log)
        file_log.close()

    records = turn_log.records
    total = sum(record.total_seconds for record in records)
    print(f"{name}: played {len(records)} turns in {total:.0f}s, stopped: {reason}")
    if records:
        print(f"Slowest turn: {max(record.total_seconds for record in records):.1f}s")
        if reason != StopReason.TURN_LIMIT:
            print(f"Last result: {records[-1].detail}")
    print(f"Log: {turn_log.path.relative_to(ROOT)}")
    # The id the cost report gives this game, and the ledger's game_id must be, across sessions.
    print(f"Game: {game_id_of_log(ROOT / 'runs', turn_log.path) or '(no turns logged)'}")
    print(f"Settling decisions: {decisions.path.relative_to(ROOT)}")
    return 0 if reason == StopReason.TURN_LIMIT else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=20, help="turns to play")
    return asyncio.run(run(parser.parse_args().turns))


if __name__ == "__main__":
    sys.exit(main())
