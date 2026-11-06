import asyncio
import json
import logging
from dataclasses import replace

import pytest

from civ6_ai import bot
from civ6_ai.monitors import EventBus
from civ6_ai.orchestrator import StopReason, TurnLog
from civ6_ai.policies.settling import SettlingPolicy
from civ6_ai.state.reader import IncompleteRead
from fixtures import civ_state as fx
from test_blockers import FakeGame as BlockerFake
from test_blockers import unit


class WiredGame(BlockerFake):
    """The blocker fake plus the reads the monitors and policies make, counting the shared ones."""

    def __init__(self):
        super().__init__()
        self.reads = []
        self.threat_reads = 0
        self.overview_cities = None  # Overrides the city count the overview reports.

    async def get_cities(self):
        self.reads.append("get_cities")
        return await super().get_cities()

    async def get_game_overview(self):
        self.reads.append("get_game_overview")
        count = len(self.cities) if self.overview_cities is None else self.overview_cities
        return fx.overview(turn=self.turn, num_cities=count)

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
        self.threat_reads += 1
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
        before = game.reads.count("get_game_overview")
        await shared.end_turn()
        await shared.get_game_overview()
        assert game.reads.count("get_game_overview") == before + 1

    asyncio.run(go())


def test_a_failed_shared_read_is_not_kept():
    class Flaky:
        calls = 0

        async def get_threat_scan(self):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("boom")
            return ["unit"]

    shared = bot.SharedReads(Flaky())

    async def go():
        with pytest.raises(RuntimeError):
            await shared.get_threat_scan()
        assert await shared.get_threat_scan() == ["unit"]

    asyncio.run(go())


def test_an_empty_read_is_not_kept_because_it_may_be_a_timeout():
    game = WiredGame()
    game.cities = []
    shared = bot.SharedReads(game)

    async def go():
        await shared.get_threat_scan()
        await shared.get_threat_scan()
        assert game.threat_reads == 2

    asyncio.run(go())


def test_a_short_city_read_is_neither_kept_nor_passed_on():
    game = WiredGame()
    full = game.cities
    game.overview_cities = 1
    game.cities = []  # The overview says 1 city, the read comes back empty.
    shared = bot.SharedReads(game)

    async def go():
        with pytest.raises(IncompleteRead):
            await shared.get_cities()
        game.cities = full
        cities, _ = await shared.get_cities()  # The next ask reads again, and is believed.
        assert len(cities) == 1

    asyncio.run(go())


def test_settling_does_not_act_on_an_empty_city_read(tmp_path):
    game = WiredGame()
    game.units = [replace(unit(), unit_type="UNIT_SETTLER")]
    game.cities = []
    game.overview_cities = 1
    shared = bot.SharedReads(game)
    with pytest.raises(IncompleteRead):
        asyncio.run(SettlingPolicy().run(shared))
    assert not game.called("found_city") and not game.called("move_unit")


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


def test_a_prefixed_blocked_report_is_retried(tmp_path):
    game = WiredGame()
    game.reports = ["Turn 5 -> 6\nCannot end turn: diplomacy encounter pending with Egypt."]
    reason, _ = play_one_turn(game, tmp_path)
    assert reason is StopReason.TURN_LIMIT
    assert len(game.called("end_turn")) == 2
