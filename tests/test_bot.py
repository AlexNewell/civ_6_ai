import asyncio
import json
import logging
from dataclasses import replace

import pytest
from civ_mcp import lua as lq

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
        self.tasks = []
        self.trade = fx.trade()

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

    async def get_threat_scan(self):
        self.threat_reads += 1
        return []

    async def get_religion_founding_status(self):
        return lq.ReligionFoundingStatus(False, None, None, -1, 50.0)

    async def get_builder_tasks(self):
        return self.tasks, []

    async def get_trade_routes(self):
        return self.trade

    async def get_trade_destinations(self, unit_index):
        return [
            lq.TradeDestination(
                city_name="Roma",
                owner_name="Rome",
                x=20,
                y=10,
                is_domestic=False,
                origin_yields="Gold:3",
            )
        ]

    async def get_gp_advisor(self, unit_index):
        return None

    async def change_government(self, government_type):
        self.calls.append(("change_government", government_type))
        return "ERR:GOV_LOCKED"

    async def move_unit(self, unit_index, target_x, target_y):
        self.calls.append(("move_unit", unit_index, target_x, target_y))
        return f"MOVING_TO|{target_x},{target_y}"

    async def improve_tile(self, unit_index, improvement_name):
        self.calls.append(("improve_tile", unit_index, improvement_name))
        return f"OK:IMPROVING|{improvement_name}|0,0"

    async def make_trade_route(self, unit_index, target_x, target_y):
        self.calls.append(("make_trade_route", unit_index, target_x, target_y))
        return "TRADE_ROUTE_STARTED|to Roma"


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
        "government",
        "city_production",
        "builder",
        "secondary",
        "blockers",
    ]
    assert game.called("set_research") and game.called("set_civic")
    assert game.called("set_city_production")
    assert not record.handler_errors


def test_one_turn_every_wired_policy_acts_on_its_own_units(tmp_path):
    game = WiredGame()
    game.units = [
        fx.unit_info(
            unit_id=1, unit_index=1, unit_type="UNIT_BUILDER", build_charges=3,
            combat_strength=0, x=10, y=10,
        ),
        fx.unit_info(
            unit_id=2, unit_index=2, unit_type="UNIT_TRADER", combat_strength=0, x=5, y=5
        ),
    ]  # fmt: skip
    game.tasks = [lq.BuilderTask("normal", 10, 10, "IMPROVEMENT_FARM", "", "none", "Alpha")]
    game.trade = lq.TradeRouteStatus(
        capacity=1, active_count=0, traders=[lq.TraderInfo(unit_id=2, x=5, y=5, has_moves=True)]
    )
    game.pantheon = lq.PantheonStatus(False, None, None, 25.0, [lq.BeliefInfo("BELIEF_A", "A", "")])
    _, record = play_one_turn(game, tmp_path)
    assert not record.handler_errors
    assert game.called("improve_tile") == [("improve_tile", 1, "IMPROVEMENT_FARM")]
    assert [c[1] for c in game.called("make_trade_route")] == [2]
    assert game.called("choose_pantheon")
    assert game.called("change_government") and game.called("set_policies")
    # No unit is ordered by two handlers.
    moved = [c[1] for c in game.called("move_unit")]
    assert len(moved) == len(set(moved))


def test_government_policy_is_one_instance_shared_by_handler_and_blockers(tmp_path, monkeypatch):
    made = []

    class Counting(bot.GovernmentPolicy):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(bot, "GovernmentPolicy", Counting)
    bot.build_orchestrator(WiredGame(), EventBus(), TurnLog(tmp_path / "game.jsonl"))
    assert len(made) == 1


def test_pantheon_and_great_person_blockers_go_to_the_secondary_policy(tmp_path):
    game = WiredGame()
    game.open = ["ENDTURN_BLOCKING_PANTHEON"]
    reason, record = play_one_turn(game, tmp_path)
    assert reason is StopReason.TURN_LIMIT
    assert game.called("choose_pantheon")


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


def test_a_mismatch_drops_the_overview_too_so_a_wrong_one_cannot_stick():
    game = WiredGame()
    game.overview_cities = 5  # A wrong count: the cities read is complete.
    shared = bot.SharedReads(game)

    async def go():
        with pytest.raises(IncompleteRead):
            await shared.get_cities()
        game.overview_cities = None  # The next overview read is right.
        cities, _ = await shared.get_cities()
        assert len(cities) == 1

    asyncio.run(go())


def test_cities_are_not_kept_without_a_real_overview():
    game = WiredGame()

    async def no_overview():
        return None

    game.get_game_overview = no_overview
    shared = bot.SharedReads(game)

    async def go():
        await shared.get_cities()
        await shared.get_cities()
        assert game.reads.count("get_cities") == 2

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


class _Recorder:
    def __init__(self):
        self.count = 0

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self.count += 1
            return [name, self.count]

        return call


@pytest.mark.parametrize(
    "action",
    [
        "improve_tile",
        "repair_improvement",
        "remove_feature",
        "change_government",
        "set_policies",
        "appoint_governor",
        "assign_governor",
        "promote_governor",
        "choose_pantheon",
        "found_religion",
        "recruit_great_person",
        "activate_great_person",
        "make_trade_route",
    ],
)
def test_the_new_policies_actions_clear_shared_reads(action):
    async def scenario():
        shared = bot.SharedReads(_Recorder())
        first = await shared.get_empire_resources()
        assert await shared.get_empire_resources() == first
        await getattr(shared, action)()
        return first, await shared.get_empire_resources()

    first, after = asyncio.run(scenario())
    assert after != first


@pytest.mark.parametrize(
    "read",
    [
        "get_builder_tasks",
        "get_policies",
        "get_governors",
        "get_tech_civics",
        "get_pantheon_status",
        "get_religion_founding_status",
        "get_great_people",
        "get_gp_advisor",
        "get_trade_routes",
        "get_trade_destinations",
        "get_city_states",
        "get_notifications",
    ],
)
def test_the_new_policies_plain_reads_do_not_clear_shared_reads(read):
    async def scenario():
        shared = bot.SharedReads(_Recorder())
        first = await shared.get_empire_resources()
        await getattr(shared, read)()
        return first, await shared.get_empire_resources()

    first, after = asyncio.run(scenario())
    assert after == first
