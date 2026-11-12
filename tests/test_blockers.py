import asyncio
import inspect
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
from civ_mcp import end_turn as civ_mcp_end_turn
from civ_mcp import lua as lq
from civ_mcp.game_state import GameState

from civ6_ai import blockers
from civ6_ai.blockers import (
    DEFAULT_RESOLVERS,
    MAX_END_TURN_RETRIES,
    Blocker,
    BlockerGame,
    BlockerResolver,
    BlockerType,
    ResolvingGame,
    UnresolvedBlockers,
)
from civ6_ai.orchestrator import Orchestrator, StopReason, TurnLog

ROOT = Path(__file__).parent.parent
ADVANCED = "Turn 1 -> 2"


@dataclass
class Overview:
    turn: int


def unit(unit_id=1, x=5, y=5, combat=20, moves=2.0):
    return lq.UnitInfo(
        unit_id=unit_id,
        unit_index=unit_id,
        name="Warrior",
        unit_type="UNIT_WARRIOR",
        x=x,
        y=y,
        moves_remaining=moves,
        max_moves=2.0,
        health=100,
        max_health=100,
        combat_strength=combat,
    )


def city(city_id=1, building="nothing"):
    return lq.CityInfo(
        city_id=city_id,
        name="Rome",
        x=1,
        y=1,
        population=1,
        food=0.0,
        production=0.0,
        gold=0.0,
        science=0.0,
        culture=0.0,
        faith=0.0,
        housing=0.0,
        amenities=0,
        turns_to_grow=1,
        currently_building=building,
    )


def session(player=2, at_war=False, deal=""):
    return lq.DiplomacySession(
        session_id=1,
        other_player_id=player,
        other_civ_name="Egypt",
        other_leader_name="Cleopatra",
        choices=[],
        deal_summary=deal,
        is_at_war=at_war,
    )


def deal(player=2):
    return lq.PendingDeal(other_player_id=player, other_player_name="Egypt", other_leader_name="C")


def congress(turns=5, in_session=False, resolutions=()):
    return lq.WorldCongressStatus(
        is_in_session=in_session,
        turns_until_next=turns,
        favor=0,
        max_votes=5,
        favor_costs=[],
        resolutions=list(resolutions),
        proposals=[],
    )


def resolution(hash_=7, passed=False):
    return lq.CongressResolution(
        resolution_type="WC_RES",
        resolution_hash=hash_,
        name="Mercenaries",
        target_kind="YIELD",
        effect_a="",
        effect_b="",
        possible_targets=[],
        is_passed=passed,
    )


def tech(name, turns):
    return lq.TechOption(name, name, 10, 0, turns, False, "", "")


def civic(name, turns):
    return lq.CivicOption(name, name, 10, 0, turns, False, "")


def tech_status(research="None", civic_name="None", techs=(), civics=()):
    return lq.TechCivicStatus(research, 1, civic_name, 1, list(techs), list(civics))


def option(category, name, cost, repair=False):
    return lq.ProductionOption(category, name, cost, is_repair=repair)


def slot(index, slot_type, policy=None):
    return lq.PolicySlot(index, slot_type, policy, policy)


def policy(name, slot_type):
    return lq.PolicyInfo(name, name, "", slot_type)


def governor_status(points=1, can_appoint=True, available=("GOV_A",), appointed=()):
    infos = [lq.GovernorInfo(g, g, "") for g in available]
    return lq.GovernorStatus(points, 0, can_appoint, list(appointed), infos)


def promotions(*names):
    options = [lq.PromotionOption(name, name, "") for name in names]
    return lq.UnitPromotionStatus(1, 1, "UNIT_WARRIOR", options)


def person(individual_id, can_recruit):
    return lq.GreatPersonInfo(
        "Great Scientist",
        "Hypatia",
        "Classical",
        60,
        "Unclaimed",
        60,
        can_recruit=can_recruit,
        individual_id=individual_id,
    )


class FakeGame:
    """A game whose open blockers are cleared by the same calls that clear them in civ6-mcp."""

    def __init__(self, open_types=(), reports=()):
        self.turn = 1
        self.open = list(open_types)
        self.reports = list(reports)
        self.calls = []
        self.lua = []
        self.sessions = []
        self.deals = []
        self.notifications = []
        self.congress = congress()
        self.techs = tech_status(techs=[tech("TECH_A", 3)], civics=[civic("CIVIC_A", 3)])
        self.cities = [city()]
        self.production = [option("BUILDING", "BUILDING_MONUMENT", 40)]
        self.policies = lq.GovernmentStatus(
            "Chiefdom",
            "GOVERNMENT_CHIEFDOM",
            [slot(0, "SLOT_MILITARY")],
            [policy("POLICY_AGOGE", "SLOT_MILITARY")],
        )
        self.governors = governor_status()
        self.units = [unit()]
        self.unit_promotions = {1: promotions("PROMOTION_A")}
        self.pantheon = lq.PantheonStatus(
            False, None, None, 25.0, [lq.BeliefInfo("BELIEF_A", "A", "")]
        )
        self.dedications = lq.DedicationStatus(
            "Normal", 1, 0, 0, 0, 1, [], [lq.DedicationChoice(3, "A", "", "", "")]
        )
        self.envoys = lq.EnvoyStatus(1, [lq.CityStateInfo(9, "Geneva", "Sci", 0, -1, "None", True)])
        self.people = [person(4, True)]
        self.result = "OK"  # What every action returns, unless a test changes it.

    def clear(self, *types):
        self.open = [found for found in self.open if found not in types]

    def did(self, name, *args):
        self.calls.append((name, *args))
        return self.result

    def called(self, name):
        return [call for call in self.calls if call[0] == name]

    async def check_game_over(self):
        return None

    async def get_game_overview(self):
        return Overview(self.turn)

    async def end_turn(self):
        self.calls.append(("end_turn",))
        if self.reports:
            return self.reports.pop(0)
        if self.sessions or self.deals:
            return "Cannot end turn: diplomacy encounter pending with Egypt."
        self.clear(*[t for t, resolve in DEFAULT_RESOLVERS.items() if _clears_itself(resolve)])
        if self.open:
            return f"Cannot end turn — resolve these blockers:\n  - {self.open[0]}"
        self.turn += 1
        return f"Turn {self.turn - 1} -> {self.turn}"

    async def execute_lua(self, code, context="gamecore"):
        self.calls.append(("execute_lua", context))
        self.lua.append(code)
        return "\n".join(f"BLOCKING|{found}|message" for found in self.open) or "NONE"

    async def get_notifications(self):
        return self.notifications

    async def get_diplomacy_sessions(self):
        return self.sessions

    async def diplomacy_respond(self, other_player_id, response):
        self.sessions = [s for s in self.sessions if s.other_player_id != other_player_id]
        return self.did("diplomacy_respond", other_player_id, response)

    async def get_pending_deals(self):
        return self.deals

    async def respond_to_deal(self, other_player_id, accept):
        self.deals = [d for d in self.deals if d.other_player_id != other_player_id]
        self.sessions = [s for s in self.sessions if s.other_player_id != other_player_id]
        return self.did("respond_to_deal", other_player_id, accept)

    async def get_world_congress(self):
        return self.congress

    async def queue_wc_votes(self, votes):
        self.congress = congress()
        self.clear(BlockerType.WORLD_CONGRESS_SESSION)
        return self.did("queue_wc_votes", votes)

    async def vote_world_congress(self, resolution_hash, option, target_index, num_votes):
        self.clear(BlockerType.WORLD_CONGRESS_SESSION)
        return self.did("vote_world_congress", resolution_hash, option, target_index, num_votes)

    async def get_tech_civics(self):
        return self.techs

    async def set_research(self, tech_name):
        self.clear(BlockerType.RESEARCH)
        return self.did("set_research", tech_name)

    async def set_civic(self, civic_name):
        self.clear(BlockerType.CIVIC)
        return self.did("set_civic", civic_name)

    async def get_cities(self):
        return self.cities, []

    async def list_city_production(self, city_id):
        return self.production

    async def set_city_production(self, city_id, item_type, item_name):
        if not self.result.startswith("Error"):
            self.clear(BlockerType.PRODUCTION)
        return self.did("set_city_production", city_id, item_type, item_name)

    async def get_policies(self):
        return self.policies

    async def set_policies(self, assignments):
        self.clear(BlockerType.FILL_CIVIC_SLOT)
        return self.did("set_policies", assignments)

    async def get_governors(self):
        return self.governors

    async def appoint_governor(self, governor_type):
        self.clear(BlockerType.GOVERNOR_APPOINTMENT)
        self.clear(BlockerType.GOVERNOR_OPPORTUNITY)
        return self.did("appoint_governor", governor_type)

    async def promote_governor(self, governor_type, promotion_type):
        self.clear(BlockerType.GOVERNOR_APPOINTMENT)
        self.clear(BlockerType.GOVERNOR_OPPORTUNITY)
        return self.did("promote_governor", governor_type, promotion_type)

    async def get_units(self):
        return self.units

    async def get_unit_promotions(self, unit_id):
        status = self.unit_promotions.get(unit_id, promotions())
        if isinstance(status, Exception):
            raise status
        return status

    async def promote_unit(self, unit_id, promotion_type):
        self.clear(BlockerType.UNIT_PROMOTION)
        return self.did("promote_unit", unit_id, promotion_type)

    async def move_unit(self, unit_index, target_x, target_y):
        self.clear(BlockerType.STACKED_UNITS)
        return self.did("move_unit", unit_index, target_x, target_y)

    async def skip_remaining_units(self):
        self.clear(BlockerType.UNITS)
        return self.did("skip_remaining_units")

    async def get_pantheon_status(self):
        return self.pantheon

    async def choose_pantheon(self, belief_type):
        self.clear(BlockerType.PANTHEON)
        return self.did("choose_pantheon", belief_type)

    async def get_dedications(self):
        return self.dedications

    async def choose_dedication(self, dedication_index):
        self.clear(BlockerType.COMMEMORATION_AVAILABLE)
        return self.did("choose_dedication", dedication_index)

    async def get_city_states(self):
        return self.envoys

    async def send_envoy(self, city_state_player_id):
        self.clear(BlockerType.GIVE_INFLUENCE_TOKEN)
        return self.did("send_envoy", city_state_player_id)

    async def get_great_people(self):
        return self.people

    async def recruit_great_person(self, individual_id):
        return self.did("recruit_great_person", individual_id)


def _clears_itself(resolve):
    return resolve is blockers.leave_to_end_turn


def resolve(game, resolver=None, diplomacy_only=False):
    return asyncio.run((resolver or BlockerResolver()).resolve(game, diplomacy_only))


def play(game, tmp_path, turns=1, resolver=None):
    resolver = resolver or BlockerResolver()
    turn_log = TurnLog(tmp_path / "game.jsonl")
    loop = Orchestrator(ResolvingGame(game, resolver), [resolver], turn_log)
    return asyncio.run(loop.play(turns)), turn_log


def game_types():
    return [found for found in BlockerType if found.startswith("ENDTURN_BLOCKING_")]


def test_catalogue_covers_every_blocker_civ6_mcp_names():
    named = set(lq.BLOCKING_TOOL_MAP) | set(
        re.findall(r"ENDTURN_BLOCKING_[A-Z_]*[A-Z]", inspect.getsource(civ_mcp_end_turn))
    )
    assert named <= set(game_types())


def test_every_type_has_a_handler_and_a_catalogue_row():
    catalogue = (ROOT / "docs" / "blockers.md").read_text(encoding="utf-8")
    for found in BlockerType:
        assert found in DEFAULT_RESOLVERS
        assert f"`{found}`" in catalogue


@pytest.mark.parametrize("name", [n for n in vars(BlockerGame) if not n.startswith("_")])
def test_protocol_matches_game_state(name):
    def parameters(method):
        return list(inspect.signature(method).parameters)[:]

    ours = parameters(getattr(BlockerGame, name))
    theirs = parameters(getattr(GameState, name))
    assert theirs[: len(ours)] == ours  # Extra optional parameters on theirs are fine.


def test_reports_still_match_what_end_turn_returns():
    source = inspect.getsource(civ_mcp_end_turn)
    for report in blockers._DIPLOMACY_REPORTS + blockers._BLOCKED_REPORTS:
        assert f'"{report}' in source


def test_congress_lua_ends_with_the_sentinel():
    assert blockers._SUBMIT_CONGRESS_LUA.strip().endswith(f'print("{lq.SENTINEL}")')
    assert "ACTION_ENDTURN" not in blockers._SUBMIT_CONGRESS_LUA


@pytest.mark.parametrize("found", game_types())
def test_each_game_blocker_is_cleared_in_a_played_turn(found, tmp_path):
    game = FakeGame([found])
    if found == BlockerType.WORLD_CONGRESS_SESSION:
        game.congress = congress(in_session=True, resolutions=[resolution()])
    if found == BlockerType.STACKED_UNITS:
        game.units = [unit(1), unit(2)]

    reason, turn_log = play(game, tmp_path)

    assert reason == StopReason.TURN_LIMIT
    assert game.turn == 2
    assert turn_log.records[0].handler_errors == {}


def test_a_turn_with_nothing_open_changes_nothing(tmp_path):
    game = FakeGame()

    reason, _ = play(game, tmp_path, turns=3)

    assert reason == StopReason.TURN_LIMIT
    assert {call[0] for call in game.calls} == {"execute_lua", "end_turn"}


def test_blocking_query_runs_in_the_ingame_context():
    game = FakeGame()
    resolve(game)
    assert game.called("execute_lua")[0] == ("execute_lua", "ingame")


def test_a_blocker_revealed_by_clearing_another_is_handled():
    game = FakeGame([BlockerType.RESEARCH])
    real = game.set_research

    async def reveals_civic(tech_name):
        game.open.append(BlockerType.CIVIC)
        return await real(tech_name)

    game.set_research = reveals_civic

    assert resolve(game) == []
    assert game.called("set_civic")


def test_units_are_skipped_after_everything_else():
    game = FakeGame([BlockerType.UNITS, BlockerType.UNIT_PROMOTION])
    resolve(game)
    actions = [call[0] for call in game.calls if call[0] != "execute_lua"]
    assert actions == ["promote_unit", "skip_remaining_units"]


def test_settled_type_is_not_acted_on_twice():
    """A notification that outlives its action is left to `end_turn` to clear."""
    game = FakeGame([BlockerType.RESEARCH])
    game.clear = lambda *types: None  # The notification stays up.

    assert resolve(game) == []
    assert len(game.called("set_research")) == 1


def test_unknown_blocker_is_recorded_and_the_turn_is_still_tried(tmp_path):
    game = FakeGame(["ENDTURN_BLOCKING_NEW_THING"])

    reason, turn_log = play(game, tmp_path)

    assert reason == StopReason.BLOCKED
    assert "ENDTURN_BLOCKING_NEW_THING" in turn_log.records[0].handler_errors["blockers"]
    assert game.called("end_turn")


def test_run_raises_for_what_is_left_open():
    game = FakeGame(["ENDTURN_BLOCKING_NEW_THING"])
    with pytest.raises(UnresolvedBlockers, match="NEW_THING"):
        asyncio.run(BlockerResolver().run(game))


def test_policy_takes_over_a_type():
    calls = []

    async def my_research(game, blocker):
        calls.append(blocker)
        game.clear(blocker.type)
        return True

    game = FakeGame([BlockerType.RESEARCH])
    resolver = BlockerResolver({BlockerType.RESEARCH: my_research})

    assert resolve(game, resolver) == []
    assert calls == [Blocker(BlockerType.RESEARCH, "message")]
    assert not game.called("set_research")


def test_policy_can_handle_a_type_left_to_end_turn():
    calls = []

    async def raze(game, blocker):
        calls.append(blocker.type)
        return True

    game = FakeGame([BlockerType.CONSIDER_RAZE_CITY])
    resolve(game, BlockerResolver({BlockerType.CONSIDER_RAZE_CITY: raze}))
    assert calls == [BlockerType.CONSIDER_RAZE_CITY]


@pytest.mark.parametrize("outcome", [False, RuntimeError("bug")])
def test_default_steps_in_when_a_policy_fails(outcome):
    async def broken(game, blocker):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    game = FakeGame([BlockerType.RESEARCH])

    assert resolve(game, BlockerResolver({BlockerType.RESEARCH: broken})) == []
    assert game.called("set_research")


def test_a_failed_read_leaves_the_others_working():
    game = FakeGame([BlockerType.RESEARCH])

    async def broken():
        raise RuntimeError("Lua error")

    game.get_world_congress = broken

    assert resolve(game) == []
    assert game.called("set_research")


def test_lost_connection_is_not_swallowed():
    game = FakeGame([BlockerType.RESEARCH])

    async def gone(tech_name):
        raise ConnectionError

    game.set_research = gone
    with pytest.raises(ConnectionError):
        resolve(game)


def test_friendly_answer_repeats_until_the_session_closes():
    game = FakeGame()
    game.sessions = [session()]
    replies = ["OK:RESPONDED|POSITIVE|SESSION_CONTINUES", "OK:RESPONDED|POSITIVE|SESSION_CLOSED"]

    async def respond(player, response):
        game.calls.append(("diplomacy_respond", player, response))
        reply = replies.pop(0)
        if "CLOSED" in reply:
            game.sessions = []
        return reply

    game.diplomacy_respond = respond

    assert resolve(game) == []
    assert game.called("diplomacy_respond") == [("diplomacy_respond", 2, "POSITIVE")] * 2


def test_session_that_never_closes_is_forced_shut():
    game = FakeGame()
    game.sessions = [session()]

    async def respond(player, response):
        game.calls.append(("diplomacy_respond", player, response))
        if response == "EXIT":
            game.sessions = []
            return "SESSION_CLOSED"
        return "OK:RESPONDED|POSITIVE|SESSION_CONTINUES"

    game.diplomacy_respond = respond

    assert resolve(game) == []
    assert game.called("diplomacy_respond")[-1] == ("diplomacy_respond", 2, "EXIT")


def test_war_declaration_is_closed_and_deal_session_is_refused():
    game = FakeGame()
    game.sessions = [session(2, at_war=True), session(3, deal="They offer: gold")]

    assert resolve(game) == []
    assert game.called("diplomacy_respond") == [("diplomacy_respond", 2, "EXIT")]
    assert game.called("respond_to_deal") == [("respond_to_deal", 3, False)]


def test_pending_deal_is_refused():
    game = FakeGame()
    game.deals = [deal(4)]

    assert resolve(game) == []
    assert game.called("respond_to_deal") == [("respond_to_deal", 4, False)]


@pytest.mark.parametrize(
    "status",
    [congress(turns=5), congress(turns=-1), congress(turns=0)],
    ids=["far off", "no congress yet", "no resolutions"],
)
def test_congress_with_nothing_to_vote_on_is_left_alone(status):
    game = FakeGame()
    game.congress = status
    resolve(game)
    assert not game.called("queue_wc_votes")


@pytest.mark.parametrize(
    "status",
    [congress(turns=0, resolutions=[resolution()]), congress(turns=3, in_session=True)],
    ids=["fires this turn", "in session"],
)
def test_congress_about_to_fire_gets_the_default_voter(status):
    game = FakeGame()
    game.congress = status

    assert resolve(game) == []
    assert game.called("queue_wc_votes") == [("queue_wc_votes", [])]


def test_open_session_gets_a_free_vote_on_each_open_resolution_and_a_submit():
    game = FakeGame([BlockerType.WORLD_CONGRESS_SESSION])
    game.congress = congress(
        in_session=True, resolutions=[resolution(7), resolution(8, passed=True)]
    )

    resolve(game)

    assert game.called("vote_world_congress") == [("vote_world_congress", 7, 1, 0, 1)]
    assert any("WORLD_CONGRESS_SUBMIT_TURN" in code for code in game.lua)
    # end_turn refuses an open session unless a voter is registered, even after the submit.
    assert game.called("queue_wc_votes") == [("queue_wc_votes", [])]


def test_the_default_open_session_vote_is_logged(caplog):
    game = FakeGame([BlockerType.WORLD_CONGRESS_SESSION])
    game.congress = congress(in_session=True, resolutions=[resolution(7)])

    with caplog.at_level("INFO", logger=blockers.__name__):
        resolve(game)

    assert any("congress (default voter)" in r.getMessage() for r in caplog.records)


def test_research_and_civic_pick_the_fastest():
    game = FakeGame([BlockerType.RESEARCH, BlockerType.CIVIC])
    game.techs = tech_status(
        techs=[tech("SLOW", 9), tech("FAST", 2)], civics=[civic("SLOW", 9), civic("FAST", 2)]
    )

    resolve(game)

    assert game.called("set_research") == [("set_research", "FAST")]
    assert game.called("set_civic") == [("set_civic", "FAST")]


def test_stale_research_notification_leaves_the_choice_alone():
    game = FakeGame([BlockerType.RESEARCH])
    game.techs = tech_status(research="Pottery", techs=[tech("TECH_A", 1)])

    assert resolve(game) == []
    assert not game.called("set_research")


def test_nothing_to_research_stays_unresolved():
    game = FakeGame([BlockerType.RESEARCH])
    game.techs = tech_status()
    assert [found.type for found in resolve(game)] == [BlockerType.RESEARCH]


def test_production_prefers_cheap_non_units_and_only_touches_idle_cities():
    game = FakeGame([BlockerType.PRODUCTION])
    game.cities = [city(1), city(2, building="UNIT_SCOUT")]
    game.production = [
        option("UNIT", "UNIT_SCOUT", 10),
        option("DISTRICT", "DISTRICT_CAMPUS", 1),
        option("DISTRICT", "DISTRICT_HOLY_SITE", 1, repair=True),
        option("BUILDING", "BUILDING_WALLS", 50),
        option("BUILDING", "BUILDING_MONUMENT", 30),
    ]

    resolve(game)

    assert game.called("set_city_production") == [
        ("set_city_production", 1, "BUILDING", "BUILDING_MONUMENT")
    ]


def test_production_gets_past_many_refused_wonders_to_a_unit():
    game = FakeGame([BlockerType.PRODUCTION])
    game.production = [option("BUILDING", f"BUILDING_WONDER_{i}", 100 + i) for i in range(12)] + [
        option("UNIT", "UNIT_SCOUT", 30)
    ]
    real = game.set_city_production

    async def picky(city_id, item_type, item_name):
        if item_name.startswith("BUILDING_WONDER"):
            game.calls.append(("set_city_production", city_id, item_type, item_name))
            return "ERR:MISSING_COORDS|needs a tile"
        return await real(city_id, item_type, item_name)

    game.set_city_production = picky

    assert resolve(game) == []
    assert game.called("set_city_production")[-1][-1] == "UNIT_SCOUT"


def test_production_moves_on_from_a_rejected_item():
    game = FakeGame([BlockerType.PRODUCTION])
    game.production = [option("BUILDING", "WONDER", 10), option("UNIT", "UNIT_SCOUT", 20)]
    real = game.set_city_production

    async def picky(city_id, item_type, item_name):
        game.result = "Error: CANNOT_START|needs a tile" if item_name == "WONDER" else "OK"
        return await real(city_id, item_type, item_name)

    game.set_city_production = picky

    assert resolve(game) == []
    assert game.called("set_city_production")[-1][-1] == "UNIT_SCOUT"


def test_policies_fill_empty_slots_without_repeating_one():
    game = FakeGame([BlockerType.FILL_CIVIC_SLOT])
    game.policies = lq.GovernmentStatus(
        "Oligarchy",
        "GOVERNMENT_OLIGARCHY",
        [
            slot(0, "SLOT_MILITARY", "POLICY_USED"),
            slot(1, "SLOT_ECONOMIC"),
            slot(2, "SLOT_WILDCARD"),
            slot(3, "SLOT_DIPLOMATIC"),
        ],
        [
            policy("POLICY_USED", "SLOT_MILITARY"),
            policy("POLICY_ECON", "SLOT_ECONOMIC"),
            policy("POLICY_MIL", "SLOT_MILITARY"),
        ],
    )

    resolve(game)

    assert game.called("set_policies") == [
        ("set_policies", {1: "POLICY_ECON", 2: "POLICY_MIL"}),
    ]


def test_policy_change_the_engine_ignored_counts_as_failed():
    game = FakeGame([BlockerType.FILL_CIVIC_SLOT])
    game.clear = lambda *types: None
    game.result = "POLICIES_SET\nWARN:SILENT_FAILURE — engine rejected: slot 0"

    assert [found.type for found in resolve(game)] == [BlockerType.FILL_CIVIC_SLOT]


def test_governor_point_promotes_when_nobody_can_be_appointed():
    promotion = lq.GovernorPromotion("PROMO_A", "A", "")
    appointed = lq.AppointedGovernor("GOV_B", "B", -1, "Unassigned", False, 0, [promotion])
    game = FakeGame([BlockerType.GOVERNOR_APPOINTMENT])
    game.governors = governor_status(can_appoint=False, available=(), appointed=[appointed])

    resolve(game)

    assert game.called("promote_governor") == [("promote_governor", "GOV_B", "PROMO_A")]


def test_a_governor_title_opportunity_is_spent_like_an_appointment():
    # Live, turn 62: "New Governor Title Available" listed ENDTURN_BLOCKING_GOVERNOR_OPPORTUNITY
    # with a point to spend and one governor unassigned with promotions on offer.
    promotion = lq.GovernorPromotion("PROMO_A", "A", "")
    appointed = lq.AppointedGovernor("GOV_B", "B", -1, "Unassigned", False, 0, [promotion])
    game = FakeGame([BlockerType.GOVERNOR_OPPORTUNITY])
    game.governors = governor_status(can_appoint=False, available=(), appointed=[appointed])

    assert resolve(game) == []
    assert game.called("promote_governor") == [("promote_governor", "GOV_B", "PROMO_A")]


def test_promotions_go_to_units_with_options_and_skip_the_unreadable():
    game = FakeGame([BlockerType.UNIT_PROMOTION])
    game.units = [unit(1), unit(2), unit(3), unit(4, combat=0)]
    game.unit_promotions = {1: promotions(), 2: ValueError("no experience"), 3: promotions("P")}

    assert resolve(game) == []
    assert game.called("promote_unit") == [("promote_unit", 3, "P")]


def test_every_envoy_goes_to_one_city_state():
    game = FakeGame([BlockerType.GIVE_INFLUENCE_TOKEN])
    closed = lq.CityStateInfo(8, "Closed", "Sci", 0, -1, "None", False)
    open_city = lq.CityStateInfo(9, "Geneva", "Sci", 0, -1, "None", True)
    game.envoys = lq.EnvoyStatus(2, [closed, open_city])

    resolve(game)

    assert game.called("send_envoy") == [("send_envoy", 9)] * 2


def test_envoy_refused_by_one_city_state_goes_to_the_next():
    game = FakeGame([BlockerType.GIVE_INFLUENCE_TOKEN])
    first = lq.CityStateInfo(8, "Full", "Sci", 0, -1, "None", True)
    second = lq.CityStateInfo(9, "Geneva", "Sci", 0, -1, "None", True)
    game.envoys = lq.EnvoyStatus(1, [first, second])
    real = game.send_envoy

    async def picky(player_id):
        if player_id == 8:
            game.calls.append(("send_envoy", 8))
            return "Error: cannot send"
        return await real(player_id)

    game.send_envoy = picky

    assert resolve(game) == []
    assert game.called("send_envoy") == [("send_envoy", 8), ("send_envoy", 9)]


def test_governor_refused_once_tries_the_next_candidate():
    game = FakeGame([BlockerType.GOVERNOR_APPOINTMENT])
    game.governors = governor_status(available=("GOV_A", "GOV_B"))
    real = game.appoint_governor

    async def picky(governor_type):
        if governor_type == "GOV_A":
            game.calls.append(("appoint_governor", "GOV_A"))
            return "Error: cannot appoint"
        return await real(governor_type)

    game.appoint_governor = picky

    assert resolve(game) == []
    assert game.called("appoint_governor")[-1] == ("appoint_governor", "GOV_B")


def test_stale_envoy_notification_sends_nothing():
    game = FakeGame([BlockerType.GIVE_INFLUENCE_TOKEN])
    game.envoys = lq.EnvoyStatus(0, [])

    assert resolve(game) == []
    assert not game.called("send_envoy")


def test_only_the_extra_unit_on_a_tile_moves():
    game = FakeGame([BlockerType.STACKED_UNITS])
    game.units = [unit(1), unit(2, combat=0), unit(3), unit(4, x=9)]

    resolve(game)

    assert game.called("move_unit") == [("move_unit", 3, 6, 5)]


def test_stacked_unit_tries_other_tiles_when_blocked():
    game = FakeGame([BlockerType.STACKED_UNITS])
    game.units = [unit(1), unit(2)]
    replies = ["Error: CANNOT_MOVE", "MOVING_TO|4,5|BLOCKED (no path)", "MOVING_TO|5,6"]

    async def move(unit_index, x, y):
        game.calls.append(("move_unit", x, y))
        return replies.pop(0)

    game.move_unit = move
    game.clear(BlockerType.STACKED_UNITS)  # Blocker gone once a move lands.

    asyncio.run(blockers.unstack_units(game, Blocker(BlockerType.STACKED_UNITS)))

    assert game.called("move_unit")[-1] == ("move_unit", 5, 6)


def test_stacked_unit_without_moves_stays_unresolved():
    game = FakeGame([BlockerType.STACKED_UNITS])
    game.units = [unit(1), unit(2, moves=0.0)]
    assert [found.type for found in resolve(game)] == [BlockerType.STACKED_UNITS]


def test_great_people_are_recruited_on_the_claim_notification():
    game = FakeGame()
    game.people = [person(4, True), person(5, False)]
    game.notifications = [lq.GameNotification("NOTIFICATION_CLAIM_GREAT_PERSON", "", 1, -1, -1)]

    resolve(game)

    assert game.called("recruit_great_person") == [("recruit_great_person", 4)]


def test_great_people_are_left_alone_without_the_notification():
    game = FakeGame()
    resolve(game)
    assert not game.called("recruit_great_person")


def test_failed_recruit_shows_in_the_raw_error_form():
    game = FakeGame()
    game.notifications = [lq.GameNotification("NOTIFICATION_CLAIM_GREAT_PERSON", "", 1, -1, -1)]
    game.result = "ERR:CANNOT_RECRUIT|Not enough GP points"
    assert [found.type for found in resolve(game)] == [BlockerType.GREAT_PERSON_CLAIM]


def test_mid_turn_proposal_is_answered_and_the_turn_finishes(tmp_path):
    game = FakeGame(reports=["Turn paused — AI diplomatic proposal from Egypt."])
    real = game.end_turn

    async def pause_then_finish():
        report = await real()
        if report.startswith("Turn paused"):
            game.sessions = [session()]
        return report

    game.end_turn = pause_then_finish

    reason, _ = play(game, tmp_path)

    assert reason == StopReason.TURN_LIMIT
    assert game.turn == 2
    assert game.called("diplomacy_respond")


def test_only_diplomacy_is_touched_while_the_ai_may_be_working():
    game = FakeGame([BlockerType.RESEARCH], reports=["Turn paused — proposal", ADVANCED])

    asyncio.run(ResolvingGame(game, BlockerResolver()).end_turn())

    assert [call[0] for call in game.calls] == ["end_turn", "end_turn"]


@pytest.mark.parametrize(
    "report",
    [
        "WAR DECLARED by Egypt! Session dismissed.\nTurn did not advance — call end_turn again.",
        "Cannot end turn: diplomacy encounter pending with Egypt.",
        "Cannot end turn: incoming trade deal pending.\n",
    ],
)
def test_diplomacy_reports_retry_without_a_full_pass(report):
    game = FakeGame(reports=[report, ADVANCED])

    assert asyncio.run(ResolvingGame(game, BlockerResolver()).end_turn()) == ADVANCED
    assert not game.called("execute_lua")


@pytest.mark.parametrize(
    "report",
    [
        "Cannot end turn — resolve these blockers:\n  - Research",
        "World Congress fires this turn (1 resolution(s), 0 favor).",
        "End turn blocked (turn 5): Blocker: Research",
    ],
)
def test_blocked_reports_get_a_full_pass_and_a_retry(report):
    game = FakeGame([BlockerType.RESEARCH], reports=[report, ADVANCED])

    assert asyncio.run(ResolvingGame(game, BlockerResolver()).end_turn()) == ADVANCED
    assert game.called("set_research")


@pytest.mark.parametrize(
    "report",
    [
        ADVANCED,
        "GAME OVER — DEFEAT. Cleopatra of Egypt won a Science victory.",
        "HANG:5:0_MCP_0005|End turn requested (turn is still 5).",
        "RUN ABORTED — save scumming threshold exceeded.",
        "CRITICAL: Turn regressed from 9 to 5.",
    ],
)
def test_other_reports_go_straight_to_the_turn_loop(report):
    game = FakeGame(reports=[report])

    assert asyncio.run(ResolvingGame(game, BlockerResolver()).end_turn()) == report
    assert len(game.called("end_turn")) == 1


def test_retries_stop_so_a_stuck_turn_reaches_the_turn_loop(tmp_path):
    report = "Cannot end turn — resolve these blockers:\n  - Unknown"
    game = FakeGame(reports=[report] * 100)

    reason, turn_log = play(game, tmp_path)

    assert reason == StopReason.BLOCKED
    assert len(game.called("end_turn")) == MAX_END_TURN_RETRIES + 1
    assert turn_log.records[0].detail.startswith("Cannot end turn")


def test_a_failure_while_clearing_still_retries():
    game = FakeGame(reports=["Cannot end turn — blocked", ADVANCED])
    resolver = BlockerResolver()

    async def broken(*args):
        raise RuntimeError("bug")

    resolver.resolve = broken

    assert asyncio.run(ResolvingGame(game, resolver).end_turn()) == ADVANCED


def test_lost_connection_while_clearing_is_not_swallowed():
    game = FakeGame(reports=["Cannot end turn — blocked"])
    resolver = BlockerResolver()

    async def gone(*args):
        raise ConnectionError

    resolver.resolve = gone

    with pytest.raises(ConnectionError):
        asyncio.run(ResolvingGame(game, resolver).end_turn())


def test_wrapper_passes_everything_else_to_the_game():
    game = FakeGame()
    wrapped = ResolvingGame(game, BlockerResolver())
    assert asyncio.run(wrapped.get_game_overview()).turn == 1


def test_a_long_game_with_a_blocker_every_turn_never_stops(tmp_path):
    types = [found for found in game_types() if found != BlockerType.WORLD_CONGRESS_SESSION]
    game = FakeGame()
    real = game.end_turn

    async def next_blocker():
        report = await real()
        if report.startswith("Turn"):
            game.open = [types[game.turn % len(types)]]
            if game.open[0] == BlockerType.STACKED_UNITS:
                game.units = [unit(1), unit(2)]
        return report

    game.end_turn = next_blocker

    reason, turn_log = play(game, tmp_path, turns=len(types) * 3)

    assert reason == StopReason.TURN_LIMIT
    assert game.turn == len(types) * 3 + 1
    assert all(record.handler_errors == {} for record in turn_log.records)
