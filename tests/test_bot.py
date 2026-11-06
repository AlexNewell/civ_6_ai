import asyncio
import json
import logging

import pytest

from civ6_ai import bot
from civ6_ai.monitors import EventBus
from civ6_ai.orchestrator import StopReason, TurnLog
from fixtures import civ_state as fx
from test_blockers import FakeGame as BlockerFake


class WiredGame(BlockerFake):
    """The blocker fake plus the reads the monitors and policies make, counting the shared ones."""

    def __init__(self):
        super().__init__()
        self.reads = []

    async def get_cities(self):
        self.reads.append("get_cities")
        return await super().get_cities()

    async def get_game_overview(self):
        self.reads.append("get_game_overview")
        return fx.overview(turn=self.turn, num_cities=len(self.cities))

    async def get_empire_resources(self):
        self.reads.append("get_empire_resources")
        return fx.resources()

    async def get_diplomacy(self):
        return [fx.civ_info(), fx.unmet_civ()]

    async def get_victory_progress(self):
        return fx.victory()

    async def get_trade_routes(self):
        return fx.trade()

    async def get_threat_scan(self):
        return []


def play_one_turn(game, tmp_path):
    turn_log = TurnLog(tmp_path / "game.jsonl")
    orchestrator = bot.build_orchestrator(game, EventBus(), turn_log)
    reason = asyncio.run(orchestrator.play(1))
    return reason, turn_log.records[0]


def test_one_turn_runs_handlers_in_order_and_acts(tmp_path):
    game = WiredGame()
    reason, record = play_one_turn(game, tmp_path)
    assert reason is StopReason.TURN_LIMIT
    assert list(record.handler_seconds) == [
        "monitors",
        "settling",
        "research",
        "city_production",
        "blockers",
    ]
    assert game.called("set_research") and game.called("set_civic")
    assert game.called("set_city_production")
    for name in ("settling", "research", "city_production", "blockers"):
        assert name not in record.handler_errors


def test_production_blocker_goes_to_the_policy_and_the_turn_ends(tmp_path):
    game = WiredGame()
    game.open = ["ENDTURN_BLOCKING_PRODUCTION"]
    reason, _ = play_one_turn(game, tmp_path)
    assert reason is StopReason.TURN_LIMIT
    assert game.called("set_city_production")


def test_shared_reads_are_made_once_until_something_changes():
    game = WiredGame()
    shared = bot.SharedReads(game)

    async def go():
        await shared.get_game_overview()
        await shared.get_game_overview()
        await shared.get_cities()
        await shared.get_cities()
        assert game.reads == ["get_game_overview", "get_cities"]
        await shared.set_research("TECH_A")  # a change
        await shared.get_cities()
        await shared.get_units()  # a plain read that isn't shared doesn't clear anything
        await shared.get_cities()
        assert game.reads.count("get_cities") == 2
        await shared.end_turn()
        await shared.get_game_overview()
        assert game.reads.count("get_game_overview") == 2

    asyncio.run(go())


def test_a_failed_shared_read_is_not_kept():
    class Flaky:
        calls = 0

        async def get_cities(self):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("boom")
            return "ok"

    shared = bot.SharedReads(Flaky())

    async def go():
        with pytest.raises(RuntimeError):
            await shared.get_cities()
        assert await shared.get_cities() == "ok"

    asyncio.run(go())


def test_settle_decisions_are_written_one_json_object_per_line(tmp_path):
    path = tmp_path / "runs" / "settle.jsonl"
    decisions = bot.SettleDecisionLog(path)
    logger = logging.getLogger(bot.SETTLING_LOGGER)
    level_before = logger.level
    decisions.attach()
    try:
        logger.info("settle_decision %s", json.dumps({"turn": 4, "action": "wait"}))
        logger.info("something else")
    finally:
        decisions.detach()
    assert logger.level == level_before
    assert decisions not in logger.handlers
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line) for line in lines] == [{"action": "wait", "turn": 4}]
