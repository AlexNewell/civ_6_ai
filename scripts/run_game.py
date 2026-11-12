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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from civ_mcp.connection import GameConnection
from civ_mcp.game_state import GameState

from civ6_ai.bot import SettleDecisionLog, build_orchestrator
from civ6_ai.city_attack import fix_city_attack
from civ6_ai.fastcalls import narrow_popup_scan, shorten_call_waits
from civ6_ai.gp_advisor import fix_gp_advisor
from civ6_ai.metrics.costs import game_id_for_new_session, game_id_of_log
from civ6_ai.metrics.speed import summarise as summarise_speed
from civ6_ai.monitors import EventBus, EventLog
from civ6_ai.orchestrator import StopReason, TurnLog, TurnRecord
from civ6_ai.saves import disable_mcp_saves, use_real_save_dirs
from civ6_ai.screenshots import Screenshotter

ROOT = Path(__file__).parent.parent
# Off since Run 4 (turn 105): civ6-mcp's propose_trade accepts whatever counter-offer the AI
# makes, and Mapuche's added Joint War, Open Borders and 30 favor before the game's AI turn
# stalled for good. The policy now asks test_trade first and sends only an offer the AI takes as
# written (unit-tested only). Switch back on after a live check that test_trade reads correctly.
TRADE_OFFERS = False
MANIFEST = ROOT / "config" / "standard_saves.json"


def identify_save(manifest: dict, game_seed: int) -> str | None:
    """Return the name of the standard save with this seed, if there is one."""
    for save in manifest["saves"]:
        if save["recorded"]["game_seed"] == str(game_seed):
            return save["name"]
    return None


@dataclass
class GameRun:
    name: str
    reason: StopReason
    turn_log: TurnLog
    decisions: SettleDecisionLog


class _NotifyingTurnLog(TurnLog):
    """A turn log that tells the runner's watchdog about each finished turn."""

    on_turn: Callable[[TurnRecord], None] | None = None

    def write(self, record: TurnRecord) -> None:
        super().write(record)
        if self.on_turn is not None:
            self.on_turn(record)


async def play_game(
    turns: int,
    expect_save: str | None = None,
    on_turn: Callable[[TurnRecord], None] | None = None,
) -> GameRun | None:
    """Play `turns` turns in the open game. None if it is not a standard save (or `expect_save`)."""
    use_real_save_dirs()
    disable_mcp_saves()
    narrow_popup_scan()
    shorten_call_waits()
    fix_gp_advisor()
    fix_city_attack()
    connection = GameConnection()
    await connection.connect()
    game = GameState(connection)

    _, seed = await game.get_game_identity()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    name = identify_save(manifest, seed)
    if name is None:
        print("FAILED: the open game isn't a standard save")
        return None
    if expect_save is not None and name != expect_save:
        print(f"FAILED: the open game is {name}, not {expect_save}")
        return None

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    turn_log = _NotifyingTurnLog(ROOT / "runs" / f"{name}_{stamp}.jsonl")
    turn_log.on_turn = on_turn
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
        first_turn = (await game.get_game_overview()).turn
        game_id = game_id_for_new_session(ROOT / "runs", name, first_turn, f"{name}_{stamp}")
        shots = Screenshotter(ROOT / "runs" / "screenshots", game_id)
        orchestrator = build_orchestrator(
            game,
            bus,
            turn_log,
            trade_offers=TRADE_OFFERS,
            screenshots=shots,
            wars_state=ROOT / "runs" / f"{name}_wars.json",  # Same for every restart of this game.
        )
        reason = await orchestrator.play(turns)
    finally:
        decisions.detach()
        logging.getLogger("civ6_ai").removeHandler(file_log)
        file_log.close()
    return GameRun(name, reason, turn_log, decisions)


async def run(turns: int) -> int:
    played = await play_game(turns)
    if played is None:
        return 1
    name, reason, turn_log, decisions = (
        played.name,
        played.reason,
        played.turn_log,
        played.decisions,
    )
    records = turn_log.records
    total = sum(record.total_seconds for record in records)
    print(f"{name}: played {len(records)} turns in {total:.0f}s, stopped: {reason}")
    if records:
        print(f"Slowest turn: {max(record.total_seconds for record in records):.1f}s")
        speed = summarise_speed((r.total_seconds, r.end_turn_seconds) for r in records)
        for line in speed.lines() if speed else []:
            print(line)
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
