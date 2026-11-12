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

    async def fortify_unit(self, unit_index):
        self.calls.append(("fortify_unit", unit_index))
        return "FORTIFIED"

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
        "diplomacy",
        "wars",
        "trade_offers",
        "settling",
        "military",
        "research",
        "government",
        "city_production",
        "gold",
        "builder",
        "secondary",
        "faith",
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


@pytest.mark.parametrize("attribute", ["MilitaryPolicy", "DiplomacyPolicy"])
def test_military_and_diplomacy_are_one_instance_each_shared_by_handler_and_blockers(
    tmp_path, monkeypatch, attribute
):
    made = []
    original = getattr(bot, attribute)

    class Counting(original):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(bot, attribute, Counting)
    orchestrator = bot.build_orchestrator(WiredGame(), EventBus(), TurnLog(tmp_path / "g.jsonl"))
    assert len(made) == 1
    handlers = [h for h in orchestrator._handlers if h is made[0]]
    assert len(handlers) == 1


def test_a_policy_claiming_a_blocker_another_owns_is_refused(tmp_path, monkeypatch):
    class Greedy(bot.DiplomacyPolicy):
        def blocker_policies(self):
            return {**super().blocker_policies(), "ENDTURN_BLOCKING_UNIT_PROMOTION": None}

    monkeypatch.setattr(bot, "DiplomacyPolicy", Greedy)
    with pytest.raises(ValueError, match="same blocker"):
        bot.build_orchestrator(WiredGame(), EventBus(), TurnLog(tmp_path / "g.jsonl"))


def test_an_open_proposal_is_answered_by_the_diplomacy_policy_before_the_turn_ends(tmp_path):
    game = WiredGame()
    game.sessions = [
        lq.DiplomacySession(
            session_id=1,
            other_player_id=1,
            other_civ_name="Rome",
            other_leader_name="Trajan",
            choices=[],
            dialogue_text="Greetings",
        )
    ]
    reason, record = play_one_turn(game, tmp_path)
    assert reason is StopReason.TURN_LIMIT
    assert game.called("diplomacy_respond")[0][2] == "POSITIVE"
    assert not record.handler_errors


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
        "attack_unit",
        "fortify_unit",
        "heal_unit",
        "promote_unit",
        "diplomacy_respond",
        "respond_to_deal",
        "vote_world_congress",
        "execute_lua",
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
        "get_unit_promotions",
        "get_diplomacy_sessions",
        "get_pending_deals",
        "get_world_congress",
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


class _CountingGame:
    """Any read returns a non-empty answer, and each call is counted."""

    def __init__(self):
        self.reads = []

    def __getattr__(self, name):
        async def call(*args):
            self.reads.append(name)
            return [name]

        return call


def test_units_policies_and_governors_are_read_once_until_something_changes():
    game = _CountingGame()
    shared = bot.SharedReads(game)
    names = [
        "get_units",
        "get_tech_civics",
        "get_policies",
        "get_governors",
        "get_trade_routes",
        "get_pantheon_status",
        "get_city_states",
    ]

    async def go():
        for name in names:
            await getattr(shared, name)()
            await getattr(shared, name)()
        assert [game.reads.count(name) for name in names] == [1] * len(names)
        await shared.move_unit(1, 2, 3)  # a change
        await shared.get_units()
        assert game.reads.count("get_units") == 2

    asyncio.run(go())


def test_reads_the_end_turn_watcher_polls_are_never_kept():
    game = _CountingGame()
    shared = bot.SharedReads(game)

    async def go():
        for name in ("get_diplomacy_sessions", "get_pending_deals", "get_notifications"):
            await getattr(shared, name)()
            await getattr(shared, name)()
            assert game.reads.count(name) == 2

    asyncio.run(go())


def test_a_turn_log_line_counts_each_handlers_game_calls(tmp_path):
    reason, record = play_one_turn(WiredGame(), tmp_path)
    assert record.handler_calls["research"]["get_tech_civics"][0] == 1
    count, seconds = record.handler_calls["monitors"]["get_diplomacy"]
    assert count >= 1 and seconds >= 0
    line = json.loads((tmp_path / "game.jsonl").read_text(encoding="utf-8"))
    assert line["handler_calls"]["research"]["get_tech_civics"][0] == 1


NOISE = "Overview response has 1 fields, expected >=14: Omitting feature label: CHOCOLATE HILLS"
NOISE_BULK_HIDE = (
    "Overview response has 1 fields, expected >=14: Request to BulkHide( true, Diplomacy ), "
    "Show on 0 = 1"
)


class NoisyGame:
    """Raises the given errors, one per call, then answers."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    async def _answer(self, value):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return value

    async def get_game_overview(self):
        return await self._answer(["overview"])

    async def get_notifications(self, owner=None):
        return await self._answer(["note", owner])


@pytest.fixture
def no_pause(monkeypatch):
    async def instant(_seconds):
        return None

    monkeypatch.setattr(bot.asyncio, "sleep", instant)


def test_log_noise_is_recognised_only_in_the_real_shape():
    assert bot.is_log_noise(ValueError(NOISE))
    assert bot.is_log_noise(ValueError(NOISE_BULK_HIDE))
    assert not bot.is_log_noise(ValueError("Overview response has 3 fields, expected >=14: a|b|c"))
    assert not bot.is_log_noise(RuntimeError(NOISE))


def test_a_shared_read_retries_log_noise_once_logged_and_keeps_the_answer(no_pause, caplog):
    game = NoisyGame([ValueError(NOISE)])
    shared = bot.SharedReads(game)

    async def go():
        with caplog.at_level(logging.WARNING, logger="civ6_ai.bot"):
            assert await shared.get_game_overview() == ["overview"]
        assert await shared.get_game_overview() == ["overview"]  # Kept after the good read.

    asyncio.run(go())
    assert game.calls == 2
    assert sum("stray log line" in r.getMessage() for r in caplog.records) == 1


def test_a_read_that_keeps_returning_noise_gives_up_after_two_retries_and_is_not_kept(no_pause):
    game = NoisyGame([ValueError(NOISE)] * 3)
    shared = bot.SharedReads(game)

    async def go():
        with pytest.raises(ValueError, match="Omitting feature label"):
            await shared.get_game_overview()
        assert await shared.get_game_overview() == ["overview"]

    asyncio.run(go())
    assert game.calls == 4


def test_a_real_parse_error_is_not_retried(no_pause):
    game = NoisyGame([ValueError("Overview response has 3 fields, expected >=14: a|b|c")])
    shared = bot.SharedReads(game)

    async def go():
        with pytest.raises(ValueError):
            await shared.get_game_overview()

    asyncio.run(go())
    assert game.calls == 1


def test_plain_reads_with_arguments_also_retry_log_noise(no_pause):
    game = NoisyGame([ValueError(NOISE)])
    shared = bot.SharedReads(game)

    async def go():
        assert await shared.get_notifications(owner=3) == ["note", 3]

    asyncio.run(go())
    assert game.calls == 2


def test_the_second_noise_string_is_retried_too(no_pause):
    game = NoisyGame([ValueError(NOISE_BULK_HIDE)])
    shared = bot.SharedReads(game)

    async def go():
        assert await shared.get_game_overview() == ["overview"]

    asyncio.run(go())
    assert game.calls == 2
