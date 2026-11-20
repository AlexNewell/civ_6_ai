import asyncio
import json
import logging

import pytest
from civ_mcp import lua as lq

from civ6_ai.orchestrator import Handler
from civ6_ai.policies.expansion import ExpansionState
from civ6_ai.policies.settling import (
    FOG_PENDING,
    NEEDS_LOOK,
    SettlingPolicy,
    SettlingSettings,
    frontier_points,
    hex_line,
    loyalty_margin,
    loyalty_rate,
    neighbours,
    safe_loyalty_rate,
    score_site,
)
from civ6_ai.state.models import Exploration, SettleSite
from civ6_ai.state.reader import FogCount, build_exploration, hex_distance
from fixtures.civ_state import (
    BARBARIANS,
    FakeGame,
    city_info,
    overview,
    threat_info,
    tile,
    unit_info,
)

SETTLER = dict(unit_type="UNIT_SETTLER", name="Settler", combat_strength=0)
SCOUT = dict(unit_type="UNIT_SCOUT", name="Scout", combat_strength=0, max_moves=3.0)
GOOD = dict(food=20, prod=10, water="fresh")  # Scores 65.


@pytest.fixture(autouse=True)
def _no_safe_rate(monkeypatch):
    """Older tests place sites near the fixture's rival; they test other rules, so the safe rate
    reads as clear for them. Tests of the safe rate itself ask for `real_safe_rate`."""
    monkeypatch.setattr(
        "civ6_ai.policies.settling.safe_loyalty_rate",
        lambda *a, **k: SettlingSettings().loyalty_rate_cap,
    )


@pytest.fixture
def real_safe_rate(monkeypatch):
    monkeypatch.setattr("civ6_ai.policies.settling.safe_loyalty_rate", safe_loyalty_rate)


class ActionGame(FakeGame):
    """A fake game that records the actions a policy takes."""

    def __init__(self, **overrides):
        super().__init__(**overrides)
        self.actions: list[tuple] = []
        self.failing: set[str] = set()

    async def move_unit(self, unit_index, target_x, target_y):
        self.actions.append(("move", unit_index, target_x, target_y))
        return (
            "Error: CANNOT_MOVE" if "move" in self.failing else f"MOVING_TO|{target_x},{target_y}"
        )

    async def found_city(self, unit_index):
        self.actions.append(("found", unit_index))
        return "Error: CANNOT_FOUND" if "found" in self.failing else "FOUNDED|10,10"

    async def automate_explore(self, unit_index):
        self.actions.append(("explore", unit_index))
        return "AUTOMATED|0,0"


def site(x, y, **changes):
    base = {**GOOD, "resources": []}
    base.update(changes)
    return lq.SettleCandidate(
        x, y, 0.0, base["food"], base["prod"], base["water"], base["resources"]
    )


def settler(x=10, y=10, **changes):
    return unit_info(unit_id=7, unit_index=7, x=x, y=y, **{**SETTLER, **changes})


def make_game(units, cities=(), threats=(), sites=(), turn=1, strategic=None):
    return ActionGame(
        units=list(units),
        cities=list(cities),
        threats=list(threats),
        settle=list(sites),
        overview=overview(turn=turn, num_cities=len(cities)),
        strategic=strategic or lq.StrategicMapData([], []),
    )


def run(policy, game):
    asyncio.run(policy.run(game))


def barbarian(x, y):
    return threat_info(x=x, y=y, owner_id=BARBARIANS)


def alpha():
    return city_info(x=10, y=10, buildings=[])


def test_policy_is_a_turn_handler():
    policy: Handler = SettlingPolicy()
    assert policy.name == "settling"


def test_site_score_uses_visible_fields_only():
    settings = SettlingSettings()
    plain = SettleSite(1, 1, 20, 10, "none", (), 0, 0, 0)
    assert score_site(plain, settings) == 60
    rich = SettleSite(1, 1, 20, 10, "fresh", ("L:SILK", "S:IRON"), 3, 1, 1)
    assert score_site(rich, settings) == 60 + 5 + 4 + 3 + 3


def test_default_founds_the_first_city_where_it_stands():
    game = make_game([settler()], sites=[site(14, 10, food=40)])
    policy = SettlingPolicy()
    run(policy, game)
    assert game.actions == [("found", 7)]
    decision = policy.decisions[0]
    assert decision.action == "found"
    assert decision.first_city
    assert decision.delay_turns == 0


def test_decision_logs_candidates_and_threats(caplog):
    game = make_game(
        [settler()], threats=[barbarian(13, 10)], sites=[site(14, 10), site(10, 16, food=30)]
    )
    policy = SettlingPolicy()
    with caplog.at_level(logging.INFO, logger="civ6_ai.policies.settling"):
        run(policy, game)
    decision = policy.decisions[0]
    assert {(c.x, c.y) for c in decision.candidates} == {(14, 10), (10, 16)}
    assert all(c.site_score > 0 for c in decision.candidates)
    assert [(t.x, t.y) for t in decision.threats] == [(13, 10)]
    line = next(r.getMessage() for r in caplog.records if "settle_decision" in r.getMessage())
    payload = json.loads(line.split("settle_decision ", 1)[1])
    assert payload["candidates"][0]["site_score"] > 0
    assert payload["threats"][0]["unit_type"] == "UNIT_WARRIOR"


def test_delay_walks_to_the_best_site_within_reach():
    near, far = site(14, 10, food=40), site(10, 30, food=80)
    game = make_game([settler()], sites=[near, far])
    run(SettlingPolicy(SettlingSettings(first_city_delay_turns=3)), game)
    assert game.actions == [("move", 7, 14, 10)]  # 4 tiles is within 3 turns, 20 is not.


def test_delay_with_no_better_site_founds_at_once():
    game = make_game([settler()], sites=[site(10, 30, food=80)])
    policy = SettlingPolicy(SettlingSettings(first_city_delay_turns=3))
    run(policy, game)
    assert game.actions == [("found", 7)]
    assert policy.decisions[0].reason == "no better site within reach"


def test_settle_now_score_ends_the_wait():
    sites = [site(10, 10, food=30), site(14, 10, food=40)]
    settings = SettlingSettings(first_city_delay_turns=3, settle_now_score=60)
    game = make_game([settler()], sites=sites)
    run(SettlingPolicy(settings), game)
    assert game.actions == [("found", 7)]
    # A higher bar keeps the settler walking.
    game = make_game([settler()], sites=sites)
    run(SettlingPolicy(SettlingSettings(first_city_delay_turns=3, settle_now_score=500)), game)
    assert game.actions == [("move", 7, 14, 10)]


def test_wait_ends_when_the_delay_runs_out():
    policy = SettlingPolicy(SettlingSettings(first_city_delay_turns=2))
    sites = [site(14, 10, food=40)]
    first = make_game([settler()], sites=sites, turn=5)
    run(policy, first)
    assert first.actions == [("move", 7, 14, 10)]
    last = make_game([settler(x=12)], sites=sites, turn=7)
    run(policy, last)
    assert last.actions == [("found", 7)]
    assert policy.decisions[-1].waited_turns == 2


def test_failed_founding_blacklists_the_tile_and_moves_on():
    game = make_game([settler()], sites=[site(10, 10), site(15, 10)])
    game.failing = {"found"}
    policy = SettlingPolicy()
    run(policy, game)
    assert game.actions == [("found", 7), ("move", 7, 15, 10)]
    assert [d.action for d in policy.decisions] == ["found_failed", "move"]


def test_later_city_goes_to_the_best_acceptable_site():
    sites = [
        site(12, 10, food=40),  # Too close to Alpha.
        site(15, 10, food=5, prod=5, water="none"),  # Score 20, below the minimum.
        site(10, 30, food=40),  # Too far from our cities.
        site(14, 10, food=25),  # Acceptable.
        site(10, 15, food=20),  # Acceptable, but worse.
    ]
    game = make_game([settler()], cities=[alpha()], sites=sites)
    policy = SettlingPolicy()
    run(policy, game)
    assert game.actions == [("move", 7, 14, 10)]
    rejected = {(c.x, c.y): c.rejected for c in policy.decisions[0].candidates}
    assert rejected[12, 10] == "too close to a city"
    assert rejected[15, 10] == "below minimum site score"
    assert rejected[10, 30] == "too far from our cities"
    assert rejected[14, 10] is None


def test_later_city_founds_when_standing_on_an_acceptable_site():
    game = make_game([settler(x=14, y=10)], cities=[alpha()], sites=[site(14, 10)])
    run(SettlingPolicy(), game)
    assert game.actions == [("found", 7)]


def test_settler_avoids_sites_near_known_threats():
    sites = [site(14, 10, food=40), site(10, 14, food=25)]
    game = make_game([settler()], cities=[alpha()], threats=[barbarian(15, 10)], sites=sites)
    policy = SettlingPolicy()
    run(policy, game)
    assert game.actions == [("move", 7, 10, 14)]
    rejected = {(c.x, c.y): c.rejected for c in policy.decisions[0].candidates}
    assert rejected[14, 10] == "hostile unit near the site"


def test_settler_avoids_sites_near_rival_cities():
    game = make_game([settler()], cities=[alpha()], sites=[site(18, 10, food=40)])
    policy = SettlingPolicy()
    run(policy, game)  # Rome's visible city is at 20,10.
    assert policy.decisions[0].candidates[0].rejected == "too close to a city"
    assert policy.decisions[0].action == "no_site"


def test_peaceful_units_are_not_threats():
    peace = threat_info(x=15, y=10, owner_id=1, owner_name="Rome")
    game = make_game([settler()], cities=[alpha()], threats=[peace], sites=[site(14, 10, food=40)])
    run(SettlingPolicy(), game)
    assert game.actions == [("move", 7, 14, 10)]


def test_threatened_route_holds_without_an_escort():
    game = make_game(
        [settler(y=14)],
        cities=[alpha()],
        threats=[barbarian(13, 10)],
        sites=[site(14, 6, food=40)],
    )
    policy = SettlingPolicy(SettlingSettings(threat_radius=2))
    run(policy, game)
    assert not [a for a in game.actions if a[0] in ("move", "found")]
    assert policy.decisions[0].action == "hold"


def test_threatened_route_gathers_then_moves_with_an_escort():
    escort = unit_info(unit_id=9, unit_index=9, x=10, y=13)
    sites = [site(14, 6, food=40)]
    settings = SettlingSettings(threat_radius=2)
    policy = SettlingPolicy(settings)
    first = make_game(
        [settler(y=14), escort], cities=[alpha()], threats=[barbarian(13, 10)], sites=sites
    )
    run(policy, first)
    assert first.actions == [("move", 9, 10, 14)]  # The escort joins the settler first.
    together = unit_info(unit_id=9, unit_index=9, x=10, y=14)
    second = make_game(
        [settler(y=14), together], cities=[alpha()], threats=[barbarian(13, 10)], sites=sites
    )
    run(policy, second)
    escort_move, settler_move = second.actions
    assert escort_move[0] == settler_move[0] == "move"
    assert escort_move[1:2] == (9,) and settler_move[1] == 7
    assert escort_move[2:] == settler_move[2:]  # Same step, so they stay stacked.
    assert policy.decisions[-1].action == "escort_move"


def test_sole_garrison_is_not_used_as_an_escort():
    garrison = unit_info(unit_id=9, unit_index=9, x=10, y=10)
    game = make_game(
        [settler(y=14), garrison],
        cities=[alpha()],
        threats=[barbarian(13, 10)],
        sites=[site(14, 6, food=40)],
    )
    run(SettlingPolicy(SettlingSettings(threat_radius=2)), game)
    assert not game.actions


def test_settler_runs_to_a_city_when_a_hostile_unit_is_close():
    game = make_game(
        [settler(x=13, y=10)],
        cities=[alpha()],
        threats=[barbarian(15, 10)],
        sites=[site(16, 10, food=40)],
    )
    policy = SettlingPolicy()
    run(policy, game)
    assert game.actions == [("move", 7, 10, 10)]
    assert policy.decisions[0].action == "flee"


def test_scout_heads_for_the_nearest_frontier_and_keeps_it():
    fog = lq.StrategicMapData([lq.FogBoundary("Alpha", 10, 10, [-1, 5, -1, 4, -1, -1])], [])
    scout = unit_info(unit_id=3, unit_index=3, x=10, y=12, **SCOUT)
    policy = SettlingPolicy()
    game = make_game([scout], cities=[alpha()], strategic=fog)
    run(policy, game)
    # Rays: east is (1,-1) at 5 -> (14,6); south is (0,1) at 4 -> (10,13).
    assert game.actions == [("move", 3, 10, 13)]


def test_scout_skips_frontiers_near_threats():
    fog = lq.StrategicMapData([lq.FogBoundary("Alpha", 10, 10, [-1, 5, -1, 4, -1, -1])], [])
    scout = unit_info(unit_id=3, unit_index=3, x=10, y=12, **SCOUT)
    game = make_game([scout], cities=[alpha()], strategic=fog, threats=[barbarian(10, 14)])
    run(SettlingPolicy(), game)
    assert game.actions == [("move", 3, 14, 6)]


def test_two_scouts_take_different_frontiers():
    fog = lq.StrategicMapData([lq.FogBoundary("Alpha", 10, 10, [-1, 5, -1, 4, -1, -1])], [])
    scouts = [
        unit_info(unit_id=3, unit_index=3, x=10, y=12, **SCOUT),
        unit_info(unit_id=4, unit_index=4, x=10, y=12, **SCOUT),
    ]
    game = make_game(scouts, cities=[alpha()], strategic=fog)
    run(SettlingPolicy(), game)
    assert game.actions == [("move", 3, 10, 13), ("move", 4, 14, 6)]


def test_scout_with_nothing_to_chase_automates_once():
    scout = unit_info(unit_id=3, unit_index=3, x=10, y=12, **SCOUT)
    policy = SettlingPolicy()
    first = make_game([scout])
    run(policy, first)
    second = make_game([scout])
    run(policy, second)
    assert first.actions == [("explore", 3)]
    assert second.actions == []


def test_same_state_gives_the_same_choice():
    def play():
        game = make_game(
            [settler()],
            cities=[alpha()],
            threats=[barbarian(15, 10)],
            sites=[site(14, 10, food=40), site(10, 14, food=25), site(10, 15, food=25)],
        )
        policy = SettlingPolicy()
        run(policy, game)
        return game.actions, [json.dumps(d, default=vars, sort_keys=True) for d in policy.decisions]

    assert play() == play()


def test_nothing_to_move_makes_no_extra_reads():
    game = make_game([unit_info()])
    run(SettlingPolicy(), game)
    assert "get_global_settle_scan" not in game.calls
    assert game.actions == []


def test_hex_line_steps_one_tile_at_a_time():
    line = hex_line((10, 10), (14, 10))
    assert line[-1] == (14, 10)
    assert len(line) == 4
    previous = (10, 10)
    for point in line:
        assert point in neighbours(*previous)
        previous = point
    assert hex_line((5, 5), (5, 5)) == []


def test_hex_line_wraps_the_east_west_edge():
    line = hex_line((72, 10), (2, 10))
    assert line[-1] == (2, 10)
    assert len(line) == 4


def test_frontier_points_step_back_from_the_unrevealed_tile():
    fog = lq.StrategicMapData([lq.FogBoundary("Alpha", 10, 10, [-1, 5, -1, 4, -1, 3])], [])
    points = frontier_points(build_exploration(fog, []))
    assert points == [(14, 6), (10, 13), (8, 8)]


FOG = lq.StrategicMapData([lq.FogBoundary("Alpha", 10, 10, [-1, 5, -1, 4, -1, -1])], [])


def test_a_threat_near_a_scout_route_only_blocks_that_turn():
    scout = unit_info(unit_id=3, unit_index=3, x=10, y=12, **SCOUT)
    policy = SettlingPolicy()
    blocked = make_game(
        [scout], cities=[alpha()], strategic=FOG, threats=[barbarian(10, 15)], turn=5
    )
    run(policy, blocked)
    assert blocked.actions == [("move", 3, 14, 6)]  # The other frontier is still open.
    only = lq.StrategicMapData([lq.FogBoundary("Alpha", 10, 10, [-1, -1, -1, 4, -1, -1])], [])
    waiting = make_game(
        [scout], cities=[alpha()], strategic=only, threats=[barbarian(10, 15)], turn=6
    )
    run(policy, waiting)
    assert waiting.actions == []  # Waits instead of blacklisting the frontier or auto-exploring.
    clear = make_game([scout], cities=[alpha()], strategic=only, turn=7)
    run(policy, clear)
    assert clear.actions == [("move", 3, 10, 13)]


def test_a_scout_blocked_by_a_threat_goes_anyway_after_waiting():
    scout = unit_info(unit_id=3, unit_index=3, x=10, y=12, **SCOUT)
    only = lq.StrategicMapData([lq.FogBoundary("Alpha", 10, 10, [-1, -1, -1, 4, -1, -1])], [])
    policy = SettlingPolicy()
    waits = SettlingPolicy().settings.scout_wait_turns
    for turn in range(5, 5 + waits):
        game = make_game(
            [scout], cities=[alpha()], strategic=only, threats=[barbarian(10, 15)], turn=turn
        )
        run(policy, game)
        assert game.actions == []
    game = make_game(
        [scout], cities=[alpha()], strategic=only, threats=[barbarian(10, 15)], turn=5 + waits
    )
    run(policy, game)
    assert game.actions == [("move", 3, 10, 13)]  # Stuck in the city for ever otherwise.


def test_refused_scout_moves_set_a_frontier_aside_only_for_a_while():
    scout = unit_info(unit_id=3, unit_index=3, x=10, y=12, **SCOUT)
    only = lq.StrategicMapData([lq.FogBoundary("Alpha", 10, 10, [-1, -1, -1, 4, -1, -1])], [])
    policy = SettlingPolicy(SettlingSettings(retry_turns=5, automate_retry_turns=3))
    refused = make_game([scout], cities=[alpha()], strategic=only, turn=1)
    refused.failing = {"move"}
    run(policy, refused)
    assert refused.actions == [("move", 3, 10, 13), ("explore", 3)]
    later = make_game([scout], cities=[alpha()], strategic=only, turn=3)
    run(policy, later)
    assert later.actions == []  # Set aside, and automation was only just tried.
    again = make_game([scout], cities=[alpha()], strategic=only, turn=4)
    run(policy, again)
    assert again.actions == [("explore", 3)]  # An idle scout is handed over again.
    open_again = make_game([scout], cities=[alpha()], strategic=only, turn=7)
    run(policy, open_again)
    assert open_again.actions == [("move", 3, 10, 13)]


def test_refused_moves_set_a_site_aside_for_a_while_not_for_good():
    settings = SettlingSettings(max_move_failures=2, retry_turns=5)
    policy = SettlingPolicy(settings)
    sites = [site(14, 10, food=40)]
    for turn in (1, 2):
        game = make_game([settler()], cities=[alpha()], sites=sites, turn=turn)
        game.failing = {"move"}
        run(policy, game)
        assert game.actions == [("move", 7, 14, 10)]
    skipped = make_game([settler()], cities=[alpha()], sites=sites, turn=3)
    run(policy, skipped)
    assert skipped.actions == []
    assert policy.decisions[-1].candidates[0].rejected == "recent moves to it were refused"
    retry = make_game([settler()], cities=[alpha()], sites=sites, turn=8)
    run(policy, retry)
    assert retry.actions == [("move", 7, 14, 10)]


def test_two_settlers_do_not_target_the_same_site():
    other = unit_info(unit_id=8, unit_index=8, x=10, y=11, **SETTLER)
    sites = [site(14, 10, food=40), site(15, 10, food=39), site(10, 15, food=20)]
    game = make_game([settler(), other], cities=[alpha()], sites=sites)
    policy = SettlingPolicy()
    run(policy, game)
    first, second = game.actions
    assert first == ("move", 7, 14, 10)
    assert second == ("move", 8, 10, 15)  # 15,10 is within 4 tiles of the reserved 14,10.
    reasons = {(c.x, c.y): c.rejected for c in policy.decisions[1].candidates}
    assert reasons[15, 10] == "reserved by another settler"


def test_a_city_founded_this_turn_blocks_nearby_sites_for_the_next_settler():
    on_site = settler(x=14, y=10)
    other = unit_info(unit_id=8, unit_index=8, x=14, y=11, **SETTLER)
    sites = [site(14, 10, food=40), site(15, 10, food=39), site(10, 15, food=20)]
    game = make_game([on_site, other], cities=[alpha()], sites=sites)
    policy = SettlingPolicy()
    run(policy, game)
    assert game.actions == [("found", 7), ("move", 8, 10, 15)]


def test_an_escort_that_cannot_move_keeps_the_settler_waiting_not_walking_alone():
    policy = SettlingPolicy(SettlingSettings(threat_radius=2))
    sites = [site(14, 6, food=40)]
    escort = unit_info(unit_id=9, unit_index=9, x=10, y=14)
    for turn in range(1, 6):
        game = make_game(
            [settler(y=14), escort],
            cities=[alpha()],
            threats=[barbarian(13, 10)],
            sites=sites,
            turn=turn,
        )
        game.failing = {"move"}
        run(policy, game)
        assert policy.decisions[-1].action == "hold"
        assert not [a for a in game.actions if a[1] == 7]


def test_a_blocked_move_counts_as_a_failure():
    settings = SettlingSettings(max_move_failures=1, retry_turns=5)
    policy = SettlingPolicy(settings)
    game = make_game([settler()], cities=[alpha()], sites=[site(14, 10, food=40)], turn=1)

    async def blocked(unit_index, x, y):
        game.actions.append(("move", unit_index, x, y))
        return f"MOVING_TO|{x},{y}|from:10,10|now_at:10,10|BLOCKED (water)"

    game.move_unit = blocked
    run(policy, game)
    assert policy.decisions[-1].reason == "move refused, trying again later"
    again = make_game([settler()], cities=[alpha()], sites=[site(14, 10, food=40)], turn=2)
    run(policy, again)
    assert again.actions == []
    assert policy.decisions[-1].candidates[0].rejected == "recent moves to it were refused"


def test_a_blocked_scout_move_sets_the_frontier_aside():
    scout = unit_info(unit_id=3, unit_index=3, x=10, y=12, **SCOUT)
    game = make_game([scout], cities=[alpha()], strategic=FOG, turn=1)

    async def blocked(unit_index, x, y):
        game.actions.append(("move", unit_index, x, y))
        return f"MOVING_TO|{x},{y}|BLOCKED (no path)"

    game.move_unit = blocked
    policy = SettlingPolicy()
    run(policy, game)
    assert [a[0] for a in game.actions].count("move") == 2  # Both frontiers were tried.
    assert policy._bad_frontiers


def test_a_failed_automation_is_tried_again_next_turn():
    scout = unit_info(unit_id=3, unit_index=3, x=10, y=12, **SCOUT)
    policy = SettlingPolicy()
    first = make_game([scout], turn=1)

    async def refused(unit_index):
        first.actions.append(("explore", unit_index))
        return "Error: CANNOT_AUTOMATE|Unit cannot auto-explore"

    first.automate_explore = refused
    run(policy, first)
    second = make_game([scout], turn=2)
    run(policy, second)
    assert second.actions == [("explore", 3)]


def test_dead_scouts_release_their_frontier():
    scout = unit_info(unit_id=3, unit_index=3, x=10, y=12, **SCOUT)
    policy = SettlingPolicy()
    only = lq.StrategicMapData([lq.FogBoundary("Alpha", 10, 10, [-1, -1, -1, 4, -1, -1])], [])
    run(policy, make_game([scout], cities=[alpha()], strategic=only, turn=1))
    assert policy._scout_targets
    other = unit_info(unit_id=4, unit_index=4, x=10, y=12, **SCOUT)
    game = make_game([other], cities=[alpha()], strategic=only, turn=2)
    run(policy, game)
    assert game.actions == [("move", 4, 10, 13)]
    assert list(policy._scout_targets) == [4]


def test_claims_survive_a_settler_that_has_no_moves_left():
    policy = SettlingPolicy()
    run(policy, make_game([settler()], cities=[alpha()], sites=[site(14, 10, food=40)], turn=1))
    assert policy._site_claims
    resting = settler(moves_remaining=0.0)
    run(policy, make_game([resting], cities=[alpha()], sites=[site(14, 10, food=40)], turn=2))
    assert policy._site_claims


def test_first_city_does_not_retry_a_tile_where_founding_failed():
    policy = SettlingPolicy()
    sites = [site(15, 10, food=30)]
    first = make_game([settler()], sites=sites, turn=1)
    first.failing = {"found"}
    run(policy, first)
    assert [a[0] for a in first.actions] == ["found", "move"]
    second = make_game([settler()], sites=sites, turn=2)
    run(policy, second)
    assert second.actions == [("move", 7, 15, 10)]
    skipped = [d for d in policy.decisions if d.action == "found_skipped"]
    assert skipped
    assert "already failed" in skipped[0].reason


def test_a_scout_stops_waiting_for_a_camp_that_never_leaves():
    # Live: barbarians sat beside the capital for 30 turns, every route out passed within their
    # radius, and the scout never left.
    scout = unit_info(unit_id=3, unit_index=3, x=10, y=12, **SCOUT)
    only = lq.StrategicMapData([lq.FogBoundary("Alpha", 10, 10, [-1, 5, -1, -1, -1, -1])], [])
    policy = SettlingPolicy()
    for turn in (6, 7):
        waiting = make_game(
            [scout], cities=[alpha()], strategic=only, threats=[barbarian(9, 13)], turn=turn
        )
        run(policy, waiting)
        assert waiting.actions == []
    going = make_game([scout], cities=[alpha()], strategic=only, threats=[barbarian(9, 13)], turn=8)
    run(policy, going)
    assert going.actions == [("move", 3, 14, 6)]


def test_an_escorted_settler_founds_though_a_hostile_unit_shadows_it():
    # Live, turn 56: the settler and its escort stood on the best site (score 242) while
    # barbarians sat three tiles away, so every site read "hostile unit near the site".
    escort = unit_info(unit_id=9, unit_index=9, x=14, y=10)
    sites = [site(14, 10, food=40)]
    barbs = [barbarian(17, 10), barbarian(17, 11), barbarian(16, 9)]  # Too many to found alone.
    alone = make_game([settler(x=14)], cities=[alpha()], threats=barbs, sites=sites)
    run(SettlingPolicy(), alone)
    assert not [a for a in alone.actions if a[0] == "found"]
    together = make_game([settler(x=14), escort], cities=[alpha()], threats=barbs, sites=sites)
    run(SettlingPolicy(), together)
    assert ("found", 7) in together.actions


# ----- loyalty (1.37) -----


def test_loyalty_margin_counts_both_sides_and_ignores_far_cities():
    settings = SettlingSettings()
    own, rival = [(10, 10, 5)], [(20, 10, 6)]
    assert loyalty_margin(14, 10, own, rival, settings) == 42 - 32
    assert loyalty_margin(16, 10, own, rival, settings) == 28 - 48
    assert loyalty_margin(10, 30, own, rival, settings) == 0  # Both out of range.
    # A bigger rival pulls harder, and a second city of ours adds to our pull.
    bigger = [(20, 10, 12)]
    assert loyalty_margin(16, 10, own, bigger, settings) < loyalty_margin(
        16, 10, own, rival, settings
    )
    more = own + [(13, 10, 3)]
    assert loyalty_margin(16, 10, more, rival, settings) > loyalty_margin(
        16, 10, own, rival, settings
    )


def test_site_likely_to_flip_is_rejected():
    # Rome has a visible city at 20,10 (pop 6). A site 4 tiles from it and 6 from ours leans to it.
    game = make_game([settler()], cities=[alpha()], sites=[site(16, 10, food=40)])
    policy = SettlingPolicy()
    run(policy, game)
    candidate = policy.decisions[0].candidates[0]
    assert candidate.rejected == "likely to flip to a rival (loyalty)"
    assert candidate.loyalty_margin < -12
    assert policy.decisions[0].action == "no_site"


def test_marginal_site_loses_score_but_a_safe_one_does_not():
    game = make_game(
        [settler()], cities=[alpha()], sites=[site(15, 10, food=40), site(13, 10, food=40)]
    )
    policy = SettlingPolicy()
    run(policy, game)
    by_site = {(c.x, c.y): c for c in policy.decisions[0].candidates}
    assert by_site[15, 10].loyalty_penalty == 5 and by_site[15, 10].rejected is None
    assert by_site[13, 10].loyalty_penalty == 0
    assert by_site[15, 10].site_score == by_site[13, 10].site_score - 5


# Run 9 (STD_T1_01): our cities when Chichen Itza (57,26) was founded, with assumed populations.
RUN9_OWN = [(66, 25, 9), (64, 22, 6), (67, 21, 5), (63, 27, 5), (60, 25, 4), (60, 20, 4)]


def test_rate_saturates_and_follows_the_ratio_not_the_difference():
    settings = SettlingSettings()
    # A rival twice as strong as us reads far worse than the same gap between two big sides.
    small = loyalty_rate(14, 10, [(10, 10, 2)], [(18, 10, 4)], settings)
    big = loyalty_rate(14, 10, [(10, 10, 20)], [(18, 10, 22)], settings)
    assert small < big < 0
    # No rivals is the cap, and a crowd of rivals is the cap the other way.
    assert loyalty_rate(14, 10, [(10, 10, 5)], [], settings) == settings.loyalty_rate_cap
    crowd = [(18, 10, 30), (17, 12, 30)]
    assert loyalty_rate(14, 10, [(10, 10, 1)], crowd, settings) == -settings.loyalty_rate_cap


def test_run9_chichen_itza_site_is_refused_once_the_rival_city_is_counted():
    # The linear margin passed the site (14.0) and the game then said -24 a turn. Rival cities
    # to the west (positions and sizes assumed: the log kept no rival cities) pulls the rate past
    # the limit.
    settings = SettlingSettings()
    rivals = [(52, 26, 12), (53, 23, 10), (54, 29, 8)]
    assert loyalty_rate(57, 26, RUN9_OWN, rivals, settings) < settings.loyalty_reject_rate
    # With no rival counted (the fog), the same site looks safe, which is how it was founded.
    assert loyalty_rate(57, 26, RUN9_OWN, [], settings) > 0


def test_a_rival_city_that_leaves_view_still_counts():
    # Run 9: the margin at one site read 82, 16, 92, 96 as the rival's tile came and went.
    game = make_game([settler()], cities=[alpha()], sites=[site(16, 10, food=40)])
    policy = SettlingPolicy()
    run(policy, game)
    seen = policy.decisions[0].candidates[0]
    game.data["tiles"][20, 10] = [tile(visibility="revealed")]  # Roma now in the fog.
    run(policy, game)
    after = policy.decisions[-1].candidates[0]
    assert after.loyalty_margin == seen.loyalty_margin < 0
    assert after.rejected == "likely to flip to a rival (loyalty)"


def test_sightings_are_remembered_on_a_turn_no_settler_can_move():
    game = make_game([settler(moves_remaining=0.0)], cities=[alpha()], sites=[site(16, 10)])
    policy = SettlingPolicy()
    run(policy, game)
    assert policy._rival_memory == {(20, 10): (policy._rival_memory[20, 10][0], 6)}
    assert policy._rival_cities == [(20, 10, 6)]


def test_a_remembered_city_that_flipped_to_us_stops_counting_as_a_rival():
    game = make_game([settler()], cities=[alpha()], sites=[site(16, 10)])
    policy = SettlingPolicy()
    run(policy, game)
    assert policy._rival_cities == [(20, 10, 6)]
    game.data["cities"].append(city_info(city_id=9, name="Roma", x=20, y=10))
    game.data["overview"] = overview(num_cities=2)
    game.data["civs"][0].visible_cities = []  # Now ours, so no longer listed under Rome.
    run(policy, game)
    assert policy._rival_cities == []


def test_a_remembered_city_that_changed_owner_takes_the_new_owner():
    game = make_game([settler()], cities=[alpha()], sites=[site(16, 10)])
    policy = SettlingPolicy()
    run(policy, game)
    old_owner = policy._rival_memory[20, 10][0]
    game.data["civs"][0].player_id = old_owner + 5
    run(policy, game)
    assert policy._rival_memory[20, 10][0] == old_owner + 5


def test_a_falling_estimated_rate_costs_score():
    game = make_game([settler()], cities=[alpha()], sites=[site(16, 10, food=40)])
    policy = SettlingPolicy()
    run(policy, game)
    candidate = policy.decisions[0].candidates[0]
    assert candidate.loyalty_rate < 0 and candidate.loyalty_penalty > -candidate.loyalty_margin


# Run 10 (STD_T1_01): Palenque at (59,29) was estimated +2.3 a turn and the game said -27.
# Positions and populations are assumed (the log kept the rates, not the cities).
RUN10_OWN = [(66, 25, 9), (64, 22, 6), (67, 21, 5), (63, 27, 5)]
RUN10_RIVALS = [(54, 27, 6), (55, 31, 5)]


def test_safe_rate_is_below_the_snapshot_rate_by_drag_growth_and_fog():
    settings = SettlingSettings()
    raw = loyalty_rate(59, 29, RUN10_OWN, RUN10_RIVALS, settings)
    safe = safe_loyalty_rate(59, 29, RUN10_OWN, RUN10_RIVALS, settings)
    assert safe < raw - settings.loyalty_new_city_drag
    # No rival in range: only the drag applies, and the site stays comfortable.
    quiet = safe_loyalty_rate(66, 28, RUN10_OWN, [], settings)
    assert quiet == settings.loyalty_rate_cap - settings.loyalty_new_city_drag
    assert quiet > settings.loyalty_comfort_rate


DRAG = 1.0  # Lighter than the default so a site 4 tiles out still clears the floor.


def test_a_site_below_the_safe_floor_is_refused_and_one_under_comfort_is_marked_down(
    real_safe_rate,
):
    # Own city (10,10) pop 5, Roma (20,10) pop 6. The raw estimate at (15,10) is +1.4, positive,
    # but after growth, fog and drag it is under zero: refused. (13,10) clears the floor but not
    # the comfort rate and loses score; (4,10) is out of Roma's range and loses none.
    game = make_game(
        [settler()],
        cities=[alpha()],
        sites=[site(15, 10, food=40), site(14, 10, food=40), site(4, 10, food=40)],
    )
    policy = SettlingPolicy(SettlingSettings(loyalty_new_city_drag=DRAG, loyalty_reject_safe=0.0))
    run(policy, game)
    by_site = {(c.x, c.y): c for c in policy.decisions[0].candidates}
    s = SettlingSettings(loyalty_new_city_drag=DRAG, loyalty_reject_safe=0.0)
    refused = by_site[15, 10]
    assert refused.loyalty_rate > 0 > refused.safe_rate
    assert refused.rejected == "likely to flip to a rival (loyalty)"
    mid = by_site[14, 10]
    assert mid.rejected is None and 0 <= mid.safe_rate < s.loyalty_comfort_rate
    assert mid.loyalty_penalty == (s.loyalty_comfort_rate - mid.safe_rate) * (
        s.loyalty_safe_penalty_weight
    )
    safe = by_site[4, 10]
    assert safe.loyalty_penalty == 0 and safe.safe_rate == s.loyalty_rate_cap - DRAG


def test_a_few_site_points_do_not_beat_a_materially_safer_site(real_safe_rate):
    # Run 10, turn 99: 17 site points took Palenque over a site 3.4 rate points safer. Here the
    # nearer site is a few points better on paper, and the comfort penalty reverses the order.
    game = make_game(
        [settler()],
        cities=[alpha()],
        sites=[site(14, 10, food=40), site(4, 10, food=36)],
    )
    policy = SettlingPolicy(SettlingSettings(loyalty_new_city_drag=DRAG, loyalty_reject_safe=0.0))
    run(policy, game)
    by_site = {(c.x, c.y): c for c in policy.decisions[0].candidates}
    near, far = by_site[14, 10], by_site[4, 10]
    assert near.rejected is None and far.rejected is None
    assert near.site_score + near.loyalty_penalty > far.site_score  # Better before the penalty.
    assert policy.decisions[0].target == (4, 10)


def test_fog_phantom_only_appears_near_a_seen_rival():
    settings = SettlingSettings()
    near = safe_loyalty_rate(14, 10, [(10, 10, 5)], [(20, 10, 6)], settings)
    # A phantom at the range edge adds no pull, so it is the same site without the fog term.
    no_fog = SettlingSettings(loyalty_fog_distance=settings.loyalty_range + 1)
    assert near < safe_loyalty_rate(14, 10, [(10, 10, 5)], [(20, 10, 6)], no_fog)
    # With no rival in range there is no phantom: only the drag.
    assert (
        safe_loyalty_rate(14, 10, [(10, 10, 5)], [(60, 10, 6)], settings)
        == settings.loyalty_rate_cap - settings.loyalty_new_city_drag
    )


def test_first_city_ignores_loyalty():
    game = make_game([settler()], sites=[site(16, 10, food=40)])
    policy = SettlingPolicy()
    run(policy, game)
    assert policy.decisions[0].candidates[0].loyalty_penalty == 0


# ----- an escort already on the tile and the expansion survey -----


def test_a_unit_already_on_the_settler_tile_is_the_escort_and_nothing_is_gathered():
    companion = unit_info(unit_id=9, unit_index=9, x=10, y=14)
    far = unit_info(unit_id=11, unit_index=11, x=10, y=13)
    game = make_game(
        [settler(y=14), companion, far],
        cities=[alpha()],
        threats=[barbarian(13, 10)],
        sites=[site(14, 6, food=40)],
    )
    run(SettlingPolicy(SettlingSettings(threat_radius=2)), game)
    assert all(a[1] != 11 for a in game.actions)  # The unit one tile away never moves.
    assert {a[1] for a in game.actions} == {9, 7}


def test_a_companion_with_no_moves_makes_the_settler_wait_not_stack_another():
    companion = unit_info(unit_id=9, unit_index=9, x=10, y=14, moves_remaining=0.0)
    far = unit_info(unit_id=11, unit_index=11, x=10, y=13)
    game = make_game(
        [settler(y=14), companion, far],
        cities=[alpha()],
        threats=[barbarian(13, 10)],
        sites=[site(14, 6, food=40)],
    )
    policy = SettlingPolicy(SettlingSettings(threat_radius=2))
    run(policy, game)
    assert not [a for a in game.actions if a[0] == "move"]
    assert policy.decisions[0].action == "hold"


def test_a_garrison_in_the_city_is_not_taken_as_the_companion():
    garrison = unit_info(unit_id=9, unit_index=9, x=10, y=10)
    game = make_game(
        [settler(y=10), garrison],
        cities=[alpha()],
        threats=[barbarian(13, 10)],
        sites=[site(14, 6, food=40)],
    )
    run(SettlingPolicy(SettlingSettings(threat_radius=2)), game)
    assert all(a[1] != 9 for a in game.actions)


def test_survey_records_safe_sites_and_the_best_score():
    game = make_game(
        [settler()], cities=[alpha()], sites=[site(14, 10, food=40), site(10, 16, food=30)]
    )
    policy = SettlingPolicy()
    run(policy, game)
    state = policy.expansion
    assert state.survey_turn == 1 and state.safe_sites == 2
    assert state.best_site_score == max(c.site_score for c in policy.decisions[0].candidates)
    assert state.settlers_on_map == 1


def test_survey_with_a_scout_only_still_counts_sites():
    scout = unit_info(unit_id=3, unit_index=3, x=10, y=10, **SCOUT)
    game = make_game([scout], cities=[alpha()], sites=[site(14, 10, food=40)])
    policy = SettlingPolicy()
    run(policy, game)
    assert policy.expansion.safe_sites == 1 and policy.expansion.settlers_on_map == 0


def test_survey_updates_the_shared_state_in_place():
    shared = ExpansionState()
    game = make_game([settler()], cities=[alpha()], sites=[site(14, 10, food=40)])
    run(SettlingPolicy(expansion=shared), game)
    assert shared.survey_turn == 1 and shared.safe_sites == 1


def test_settlers_on_map_does_not_count_a_settler_that_just_founded():
    # Run 8: the count stayed at 1 for 160 turns after the last settler founded its city.
    state = ExpansionState()
    game = make_game([settler()], sites=[site(10, 10)])  # The first city, founded on the spot.
    run(SettlingPolicy(expansion=state), game)
    assert ("found", 7) in game.actions
    assert state.settlers_on_map == 0


def test_settlers_on_map_is_refreshed_on_turns_with_no_settler_to_move():
    state = ExpansionState(settlers_on_map=1)
    game = make_game(
        [unit_info(unit_id=9, unit_index=9, unit_type="UNIT_WARRIOR")], cities=[alpha()]
    )
    run(SettlingPolicy(expansion=state), game)
    assert state.settlers_on_map == 0


def test_a_settler_with_no_site_for_many_turns_is_disbanded():
    class DeletingGame(ActionGame):
        async def delete_unit(self, unit_index):
            self.actions.append(("delete", unit_index))
            return "OK"

    policy = SettlingPolicy(settings=SettlingSettings(no_site_disband_turns=3))
    for turn in range(1, 4):
        game = DeletingGame(
            units=[settler()],
            cities=[alpha()],
            settle=[],
            overview=overview(turn=turn, num_cities=1),
            strategic=lq.StrategicMapData([], []),
        )
        run(policy, game)
        expected = [("delete", 7)] if turn == 3 else []
        assert game.actions == expected
    assert policy.decisions[-1].action == "disband"


def test_a_held_settler_with_a_site_resets_the_no_site_streak():
    game = make_game(
        [settler(y=14)],
        cities=[alpha()],
        threats=[barbarian(13, 10)],
        sites=[site(14, 6, food=40)],
    )
    policy = SettlingPolicy(SettlingSettings(threat_radius=2))
    policy._no_site_turns[7] = 19
    run(policy, game)
    assert policy.decisions[0].action == "hold"
    assert policy._no_site_turns.get(7, 0) == 0


def test_far_site_is_taken_when_safe_and_marked_down():
    # Run 11: sites 9 to 14 tiles out with no rival near were refused as too far.
    far = site(10, 19, food=40)  # 9 tiles south of Alpha at (10, 10)
    game = make_game([settler()], cities=[alpha()], sites=[far])
    policy = SettlingPolicy()
    run(policy, game)
    cand = policy.decisions[0].candidates[0]
    assert cand.rejected is None
    s = policy.settings
    base = 40 * s.food_weight + 10 * s.production_weight + s.fresh_water_bonus
    assert cand.site_score == base - s.far_penalty * (9 - s.max_city_distance)


def test_far_site_beyond_the_far_limit_stays_rejected():
    game = make_game([settler()], cities=[alpha()], sites=[site(10, 26, food=40)])
    policy = SettlingPolicy()
    run(policy, game)
    assert policy.decisions[0].candidates[0].rejected == "too far from our cities"


def test_far_site_needs_a_safe_loyalty_rate(monkeypatch):
    monkeypatch.setattr("civ6_ai.policies.settling.safe_loyalty_rate", lambda *a, **k: 5.0)
    game = make_game([settler()], cities=[alpha()], sites=[site(10, 19, food=40)])
    policy = SettlingPolicy()
    run(policy, game)
    assert policy.decisions[0].candidates[0].rejected == "too far from our cities"


def test_survey_runs_without_settlers_while_below_the_city_target():
    state = ExpansionState(target_cities=6)
    game = make_game([], cities=[alpha()], sites=[site(14, 10, food=25)])
    policy = SettlingPolicy(expansion=state)
    run(policy, game)
    assert state.survey_turn is not None
    assert state.safe_sites == 1
    full = ExpansionState(target_cities=1)
    policy = SettlingPolicy(expansion=full)
    run(policy, make_game([], cities=[alpha()], sites=[site(14, 10, food=25)]))
    assert full.survey_turn is None


# Run 12 (STD_T1_01): Tikal at (61,20), the game's breakdown on turn 84. Pressure -13.28, from
# these sources (owner, x, y, pop, capital).
TIKAL = (61, 20)
TIKAL_OWN = [(64, 22, 3), (63, 27, 2), (66, 25, 3, True)]
TIKAL_RIVALS = [
    (56, 19, 7, True),
    (59, 14, 2),
    (55, 15, 4),
    (53, 18, 2),
    (63, 11, 13, True),
]


def test_tikal_pressure_matches_the_game_breakdown():
    s = SettlingSettings(map_width=84)
    # The game said -13.28 on turn 84 (the new city counted as pop 1 on our side).
    assert loyalty_rate(*TIKAL, TIKAL_OWN, TIKAL_RIVALS, s) == pytest.approx(-13.28, abs=0.2)


def test_the_capital_weight_matters():
    s = SettlingSettings(map_width=84)
    plain = [(c[0], c[1], c[2]) for c in TIKAL_RIVALS]
    assert loyalty_rate(*TIKAL, TIKAL_OWN, plain, s) > loyalty_rate(
        *TIKAL, TIKAL_OWN, TIKAL_RIVALS, s
    )


def test_tikal_is_refused_when_the_rivals_are_known_and_passed_when_they_are_not():
    s = SettlingSettings(map_width=84)
    own = [(66, 25, 3, True), (64, 22, 3), (63, 27, 1)]
    assert safe_loyalty_rate(*TIKAL, own, [], s) >= 9  # What the bot saw: +17.
    assert safe_loyalty_rate(*TIKAL, own, TIKAL_RIVALS, s) < s.loyalty_reject_safe


KNOWN_LUA_OUTPUT = """KCITY|5|56|19|7|1|1
KCITY|5|59|14|2|0|1
BORDER|5|57,19;58,19;59,15
BORDER|9|70,5;71,5
VISIBLE|
KDONE"""


def test_known_city_output_is_parsed():
    from civ6_ai.state.reader import parse_known_cities

    known = parse_known_cities(KNOWN_LUA_OUTPUT)
    assert [(c.owner, c.x, c.y, c.pop, c.capital) for c in known.cities] == [
        (5, 56, 19, 7, True),
        (5, 59, 14, 2, False),
    ]
    assert (9, 71, 5) in known.border_tiles and len(known.border_tiles) == 5


def test_a_partial_known_city_read_is_an_error():
    from civ6_ai.state.reader import parse_known_cities

    with pytest.raises(ValueError):
        parse_known_cities("KCITY|5|56|19|7|1|1\n")
    with pytest.raises(ValueError):
        parse_known_cities("")


class LuaGame(ActionGame):
    def __init__(self, output, **overrides):
        super().__init__(**overrides)
        self.output = output
        self.lua_calls = 0
        self.lua_code: list[str] = []

    async def execute_lua(self, code, context="gamecore"):
        self.lua_calls += 1
        self.lua_code.append(code)
        return self.output


def lua_game(output, **overrides):
    return LuaGame(
        output,
        units=overrides.pop("units", [settler()]),
        cities=[alpha()],
        settle=[site(16, 10, food=40)],
        overview=overview(turn=1, num_cities=1),
        strategic=lq.StrategicMapData([], []),
        **overrides,
    )


def test_visible_cities_capitals_and_borders_feed_the_estimate():
    game = lua_game(KNOWN_LUA_OUTPUT)
    policy = SettlingPolicy()
    run(policy, game)
    assert game.lua_calls == 1
    assert sorted(c for c in policy._rival_cities if c[0] > 50) == [(56, 19, 7, True), (59, 14, 2)]
    # The unexplained Sumerian-free border (owner 9) is a phantom; owner 5's are explained.
    assert policy._phantoms == [(69, 4, 3)]


def test_phantoms_are_not_cities_for_spacing_or_the_log():
    game = lua_game(KNOWN_LUA_OUTPUT)
    policy = SettlingPolicy()
    run(policy, game)
    assert (69, 4) not in [(c[0], c[1]) for c in policy._rival_cities]
    assert (69, 4, 3) in policy._pressure_sources()
    decision = policy.decisions[0]
    assert (69, 4, 3) not in decision.rival_cities
    assert decision.phantom_cities == ((69, 4, 3),)


def test_a_failed_or_partial_read_keeps_the_memory():
    policy = SettlingPolicy()
    run(policy, lua_game(KNOWN_LUA_OUTPUT))
    before = list(policy._rival_cities), list(policy._phantoms)
    run(policy, lua_game("KCITY|5|56|19|7|1|1\n"))  # No end marker.
    assert (list(policy._rival_cities), list(policy._phantoms)) == before
    run(policy, lua_game("KDONE"))  # Complete but sees nothing: nothing is seen to be gone.
    assert sorted(policy._rival_cities) == sorted(before[0])


def test_a_remembered_city_is_dropped_only_when_its_plot_is_seen_empty():
    policy = SettlingPolicy()
    run(policy, lua_game(KNOWN_LUA_OUTPUT))
    # Out of sight: the memory (and the live game state) is not consulted for it.
    run(policy, lua_game("VISIBLE|59,14\nKDONE"))  # (59,14) visible and city-free
    assert [c for c in policy._rival_cities if c[0] > 50] == [(56, 19, 7, True)]
    # Only visible tiles are watched: Lua is told what we remember.
    game = lua_game("KDONE")
    run(policy, game)
    assert "56,19" in game.lua_code[0] and "59,14" not in game.lua_code[0]


def test_a_remembered_city_updates_only_when_visible_again():
    policy = SettlingPolicy()
    run(policy, lua_game(KNOWN_LUA_OUTPUT))
    run(policy, lua_game("KCITY|7|56|19|9|0|1\nKDONE"))
    assert policy._rival_memory[56, 19] == (7, 9)
    assert (56, 19, 9) in policy._rival_cities  # No longer a capital, new owner.


def test_no_lua_read_when_nothing_needs_the_rivals():
    game = LuaGame(
        KNOWN_LUA_OUTPUT,
        units=[],
        cities=[alpha()],
        overview=overview(turn=1, num_cities=1),
        strategic=lq.StrategicMapData([], []),
    )
    policy = SettlingPolicy()
    policy.expansion.target_cities = 1
    run(policy, game)
    assert game.lua_calls == 0


def _capital_pair(*others, settler_at=(10, 10)):
    return [settler(x=settler_at[0], y=settler_at[1]), *others]


def test_the_garrison_already_stacked_with_the_settler_is_its_escort():
    # Run 12: two combat units and the settler in the capital; an escort sent onto the tile was
    # refused every turn (STACKING_CONFLICT). One of the two on the tile leads, none is sent.
    slinger = unit_info(unit_id=8, unit_index=8, x=10, y=10)
    warrior = unit_info(unit_id=9, unit_index=9, x=10, y=10)
    game = make_game(
        _capital_pair(slinger, warrior),
        cities=[alpha()],
        threats=[barbarian(13, 10)],
        sites=[site(14, 6, food=40)],
    )
    policy = SettlingPolicy(SettlingSettings(threat_radius=2))
    run(policy, game)
    assert policy.decisions[-1].action == "escort_move"
    assert [a[1] for a in game.actions] == [8, 7]  # The slinger leads, the settler joins it.
    assert game.actions[0][2:] == game.actions[1][2:]


def test_a_lone_garrison_stays_and_another_escort_meets_the_settler_on_the_route():
    garrison = unit_info(unit_id=9, unit_index=9, x=10, y=10)
    escort = unit_info(unit_id=8, unit_index=8, x=10, y=9)
    sites = [site(14, 6, food=40)]
    settings = SettlingSettings(threat_radius=2)
    policy = SettlingPolicy(settings)
    first = make_game(
        _capital_pair(garrison, escort),
        cities=[alpha()],
        threats=[barbarian(13, 10)],
        sites=sites,
    )
    run(policy, first)
    assert first.actions and first.actions[0][1] == 8
    meet = first.actions[0][2:]
    assert meet != (10, 10)  # Not onto the garrison's tile, which would be refused.
    assert policy.decisions[-1].action == "escort_gather"
    waiting = unit_info(unit_id=8, unit_index=8, x=meet[0], y=meet[1])
    second = make_game(
        _capital_pair(garrison, waiting),
        cities=[alpha()],
        threats=[barbarian(13, 10)],
        sites=sites,
    )
    run(policy, second)
    assert second.actions == [("move", 7, *meet)]  # The settler joins the escort.


def test_a_refused_join_is_not_repeated_while_nothing_changes():
    class Refusing(ActionGame):
        async def move_unit(self, unit_index, x, y):
            self.actions.append(("move", unit_index, x, y))
            return "Error: STACKING_CONFLICT|Friendly UNIT_SLINGER already on (10,9)"

    garrison = unit_info(unit_id=9, unit_index=9, x=10, y=10)
    escort = unit_info(unit_id=8, unit_index=8, x=10, y=9)

    def play(policy):
        game = Refusing(
            units=_capital_pair(garrison, escort),
            cities=[alpha()],
            threats=[barbarian(13, 10)],
            settle=[site(14, 6, food=40)],
            overview=overview(turn=1, num_cities=1),
            strategic=lq.StrategicMapData([], []),
        )
        run(policy, game)
        return game.actions

    policy = SettlingPolicy(SettlingSettings(threat_radius=2))
    assert len(play(policy)) == 1
    assert play(policy) == []  # Same order, same blockers: not sent again.


def test_a_settler_never_goes_alone_down_a_route_the_threat_read_calls_threatened():
    # Run 12: "held 4 turns, so going without an escort" sent the settler into barbarians.
    policy = SettlingPolicy(SettlingSettings(threat_radius=2))
    for turn in range(1, 9):
        game = make_game(
            [settler(y=14)],
            cities=[alpha()],
            threats=[barbarian(13, 10)],
            sites=[site(14, 6, food=40)],
            turn=turn,
        )
        run(policy, game)
        assert not [a for a in game.actions if a[0] == "move"]
        assert policy.decisions[-1].action == "hold"


def test_an_escorted_settler_does_not_flee_back_to_the_city():
    escort = unit_info(unit_id=9, unit_index=9, x=13, y=10)
    game = make_game(
        [settler(x=13, y=10), escort],
        cities=[alpha()],
        threats=[barbarian(15, 10)],
        sites=[site(16, 10, food=40)],
    )
    policy = SettlingPolicy()
    run(policy, game)
    assert policy.decisions[0].action != "flee"


def test_an_escorted_settler_flees_when_outnumbered():
    # Issue 283: two settlers were taken with an escort beside them and two or more hostile
    # units close.
    escort = unit_info(unit_id=9, unit_index=9, x=13, y=10)
    game = make_game(
        [settler(x=13, y=10), escort],
        cities=[alpha()],
        threats=[barbarian(15, 10), barbarian(13, 12)],
        sites=[site(16, 10, food=40)],
    )
    policy = SettlingPolicy()
    run(policy, game)
    assert policy.decisions[0].action == "flee"


def test_a_settler_runs_from_cavalry_beyond_the_foot_soldier_radius():
    horse = threat_info(unit_type="UNIT_COURSER", x=17, y=10, owner_id=BARBARIANS)
    game = make_game(
        [settler(x=13, y=10)], cities=[alpha()], threats=[horse], sites=[site(16, 10, food=40)]
    )
    policy = SettlingPolicy()
    run(policy, game)
    assert policy.decisions[0].action == "flee"
    foot = make_game(
        [settler(x=13, y=10)],
        cities=[alpha()],
        threats=[barbarian(17, 10)],
        sites=[site(16, 10, food=40)],
    )
    foot_policy = SettlingPolicy()
    run(foot_policy, foot)
    assert foot_policy.decisions[0].action != "flee"


def test_a_refused_flee_is_logged_not_silent():
    game = make_game(
        [settler(x=13, y=10)],
        cities=[alpha()],
        threats=[barbarian(15, 10)],
        sites=[site(16, 10, food=40)],
    )
    game.failing = {"move"}
    policy = SettlingPolicy()
    run(policy, game)
    refused = [d for d in policy.decisions if "refused" in d.reason]
    assert refused and "fleeing" in refused[0].reason


def test_a_ship_in_a_coastal_city_is_not_the_settlers_companion():
    ship = unit_info(unit_id=8, unit_index=8, x=10, y=10, unit_type="UNIT_GALLEY", name="Galley")
    warrior = unit_info(unit_id=9, unit_index=9, x=10, y=10)
    game = make_game(
        _capital_pair(ship, warrior),
        cities=[alpha()],
        threats=[barbarian(13, 10)],
        sites=[site(14, 6, food=40)],
    )
    policy = SettlingPolicy(SettlingSettings(threat_radius=2))
    run(policy, game)
    assert all(a[1] not in (8, 9) for a in game.actions)  # Lone land unit stays; ship never moves.
    assert policy.decisions[-1].action == "hold"


def test_a_refused_join_clears_when_an_engineer_blocking_the_tile_leaves():
    class Refusing(ActionGame):
        async def move_unit(self, unit_index, x, y):
            self.actions.append(("move", unit_index, x, y))
            return "Error: STACKING_CONFLICT|already on"

    garrison = unit_info(unit_id=9, unit_index=9, x=10, y=10)
    escort = unit_info(unit_id=8, unit_index=8, x=10, y=9)
    engineer = unit_info(
        unit_id=5, unit_index=5, x=11, y=9, unit_type="UNIT_MILITARY_ENGINEER", combat_strength=0
    )

    def play(policy, extra):
        game = Refusing(
            units=_capital_pair(garrison, escort, *extra),
            cities=[alpha()],
            threats=[barbarian(13, 10)],
            settle=[site(14, 6, food=40)],
            overview=overview(turn=1, num_cities=1),
            strategic=lq.StrategicMapData([], []),
        )
        run(policy, game)
        return game.actions

    policy = SettlingPolicy(SettlingSettings(threat_radius=2))
    first = play(policy, [engineer])
    assert len(first) == 1
    assert play(policy, [engineer]) == []
    assert len(play(policy, [])) == 1  # The blocker left, so the order is tried again.


def test_the_known_city_read_does_not_rely_on_the_stripped_end_marker():
    # civ6-mcp's execute_lua strips ---END--- before returning (Run 12 live check), so the read's
    # own KDONE line is the completeness check.
    from civ6_ai.state.reader import _KNOWN_CITIES_HEAD, parse_known_cities

    assert 'print("KDONE")' in _KNOWN_CITIES_HEAD
    known = parse_known_cities("KCITY|5|61|20|13|0|1\nVISIBLE|61,20\nKDONE")
    assert known.cities[0].x == 61 and (61, 20) in known.visible_watched
    with pytest.raises(ValueError):
        parse_known_cities("KCITY|5|61|20|13|0|1\n---END---")


# ----- a city's last defender escorting when the threat near it is limited -----


class _War:
    def __init__(self, at_war=False):
        self.at_war = at_war


def _t(unit_type, x, y, strength=0, **changes):
    return threat_info(unit_type=unit_type, x=x, y=y, combat_strength=strength, **changes)


def _stand_in_city(strength=12):
    from types import SimpleNamespace

    return SimpleNamespace(x=10, y=10, defense_strength=strength)


def _state_threat(unit_type, x, y, hostile=True, strength=0):
    from civ6_ai.state.models import Threat

    return Threat(BARBARIANS, "Barbarians", unit_type, x, y, 100, 100, strength, 0, 3, hostile)


@pytest.mark.parametrize(
    ("threats", "at_war", "expected"),
    [
        ([], False, True),
        ([("UNIT_SCOUT", 14, 10)], False, True),
        ([("UNIT_WARRIOR", 13, 10), ("UNIT_SLINGER", 12, 12)], False, True),
        ([("UNIT_BARBARIAN_HORSEMAN", 12, 10), ("UNIT_WARRIOR", 13, 10)], False, True),
        ([("UNIT_WARRIOR", 13, 10)], True, False),  # A civ at war: every city is a target.
        ([("UNIT_WARRIOR", 13, 10)] * 3, False, False),  # Three is a raid, not a stray.
        ([("UNIT_CATAPULT", 14, 10)], False, False),  # Siege takes a city a warrior cannot.
        ([("UNIT_SWORDSMAN", 13, 10)], False, False),  # Stronger than the city holds.
        ([("UNIT_SPEARMAN", 13, 10), ("UNIT_SPEARMAN", 12, 10)], False, False),  # 50 > 2 x 20.
        ([("UNIT_WARRIOR", 18, 10)] * 4, False, True),  # Out of the weighing radius.
    ],
)
def test_garrison_may_leave_weighs_count_strength_siege_and_war(threats, at_war, expected):
    from civ6_ai.policies.settling import garrison_may_leave

    near = [_state_threat(*t) for t in threats]
    assert garrison_may_leave(_stand_in_city(), near, at_war) is expected


def test_a_rival_at_peace_is_not_a_threat_to_the_garrison():
    from civ6_ai.policies.settling import garrison_may_leave

    peaceful = [_state_threat("UNIT_SWORDSMAN", 11, 10, hostile=False)] * 4
    assert garrison_may_leave(_stand_in_city(), peaceful, False)


def _lone_garrison_game(threats, turn=1, units_extra=()):
    garrison = unit_info(unit_id=9, unit_index=9, x=10, y=10)
    return make_game(
        [settler(), garrison, *units_extra],
        cities=[alpha()],
        threats=threats,
        sites=[site(14, 6, food=40)],
        turn=turn,
    )


def _policy(at_war=False):
    return SettlingPolicy(SettlingSettings(threat_radius=2), defence=_War(at_war))


def test_a_lone_garrison_leads_the_settler_past_one_barbarian_scout():
    # Run 12 turns 49-51: a lone scout 3 tiles away held the settler for three turns.
    game = _lone_garrison_game([_t("UNIT_SCOUT", 13, 10, 10)])
    policy = _policy()
    run(policy, game)
    assert [a[1] for a in game.actions] == [9, 7]
    assert policy.decisions[-1].action == "escort_move"
    assert policy._stripped == {(10, 10): 9}


@pytest.mark.parametrize(
    "threats",
    [
        # Run 12 turns 29-40 around the capital: two slingers, two warriors and a scout.
        [
            _t("UNIT_SLINGER", 11, 10, 5),
            _t("UNIT_SLINGER", 11, 11, 5),
            _t("UNIT_WARRIOR", 12, 10, 20),
            _t("UNIT_WARRIOR", 13, 10, 20),
            _t("UNIT_SCOUT", 13, 11, 10),
        ],
        [_t("UNIT_CATAPULT", 13, 10, 25)],
    ],
)
def test_the_lone_garrison_stays_when_the_threat_is_real(threats):
    game = _lone_garrison_game(threats)
    policy = _policy()
    run(policy, game)
    assert game.actions == []
    assert policy.decisions[-1].action == "hold"
    assert policy._stripped == {}


def test_the_lone_garrison_stays_at_war_or_without_the_threat_state():
    for policy in (_policy(at_war=True), SettlingPolicy(SettlingSettings(threat_radius=2))):
        game = _lone_garrison_game([_t("UNIT_SCOUT", 13, 10, 10)])
        run(policy, game)
        assert game.actions == []


def test_the_escort_and_settler_go_back_when_the_stripped_city_is_threatened_again():
    policy = _policy()
    run(policy, _lone_garrison_game([_t("UNIT_SCOUT", 13, 10, 10)]))
    assert policy._stripped == {(10, 10): 9}
    # Next turn both stand one step out and a war party shows up beside the city.
    out = [(11, 9)]
    garrison = unit_info(unit_id=9, unit_index=9, x=out[0][0], y=out[0][1])
    game = make_game(
        [settler(x=out[0][0], y=out[0][1]), garrison],
        cities=[alpha()],
        threats=[_t("UNIT_WARRIOR", 12, 9, 20) for _ in range(3)],
        sites=[site(14, 6, food=40)],
        turn=2,
    )
    run(policy, game)
    assert game.actions == [("move", 9, 10, 10), ("move", 7, 10, 10)]


def test_no_recall_while_the_threat_stays_limited_and_the_record_clears_when_it_is_held():
    policy = _policy()
    run(policy, _lone_garrison_game([_t("UNIT_SCOUT", 13, 10, 10)]))
    garrison = unit_info(unit_id=9, unit_index=9, x=11, y=9)
    game = make_game(
        [settler(x=11, y=9), garrison],
        cities=[alpha()],
        threats=[_t("UNIT_SCOUT", 13, 10, 10)],
        sites=[site(14, 6, food=40)],
        turn=2,
    )
    run(policy, game)
    assert not any(a[2:] == (10, 10) for a in game.actions)
    # A new defender arrives in the city: the record is dropped.
    back = unit_info(unit_id=11, unit_index=11, x=10, y=10)
    run(policy, make_game([settler(x=11, y=9), garrison, back], cities=[alpha()], turn=3))
    assert policy._stripped == {}


def test_a_spare_unit_is_chosen_before_the_lone_garrison():
    from civ6_ai.policies.settling import find_escort

    garrison = unit_info(unit_id=9, unit_index=9, x=10, y=10)
    other = unit_info(unit_id=8, unit_index=8, x=11, y=11)
    from civ6_ai.state import read_units

    units = asyncio.run(read_units(make_game([settler(), garrison, other])))
    settler_unit = next(u for u in units if u.unit_id == 7)
    pick = find_escort(settler_unit, units, [(10, 10)], set(), 5, lone_ok={(10, 10)})
    assert pick is not None and pick.unit_id == 8


def test_a_garrison_that_walks_out_to_join_the_settler_is_recorded_for_recall():
    policy = _policy()
    garrison = unit_info(unit_id=9, unit_index=9, x=10, y=10)
    game = make_game(
        [settler(x=10, y=13), garrison],
        cities=[alpha()],
        threats=[_t("UNIT_SCOUT", 13, 13, 10)],
        sites=[site(14, 6, food=40)],
    )
    run(policy, game)
    assert policy._stripped == {(10, 10): 9}


def test_a_barbarian_near_the_sites_does_not_zero_the_survey():
    # Run 13: barbarians shadowed the capital, the survey judged every site as an unescorted
    # settler would ("hostile unit near the site"), safe_sites stayed 0 and no settler was built.
    state = ExpansionState(target_cities=6)
    game = make_game(
        [], cities=[alpha()], threats=[barbarian(14, 11)], sites=[site(14, 10, food=25)]
    )
    run(SettlingPolicy(expansion=state), game)
    assert state.safe_sites == 1
    assert state.rejected == {}


def test_the_expansion_line_names_why_sites_were_refused():
    from civ6_ai.policies.expansion import expansion_line

    state = ExpansionState(survey_turn=5, safe_sites=0, rejected={"too far from our cities": 3})
    line = expansion_line(5, 1, 0, state)
    assert line.endswith("rejected=too far from our cities:3")
    assert "rejected" not in expansion_line(5, 1, 0, ExpansionState(survey_turn=5))


def test_a_site_near_a_hostile_unit_is_reached_with_an_escort_not_dropped():
    # Codex on PR 220: the survey counts a site that only needs an escort, so the settler must
    # be able to use it: gather an escort and go, never 'no_site' and never alone.
    lone = settler(x=12, y=10)
    escort = unit_info(unit_id=8, unit_index=8, x=11, y=10)
    game = make_game(
        [lone, escort], cities=[alpha()], threats=[barbarian(14, 11)], sites=[site(14, 10, food=40)]
    )
    policy = SettlingPolicy(SettlingSettings(threat_radius=2))
    run(policy, game)
    decision = policy.decisions[-1]
    assert decision.action != "no_site"
    assert decision.action in ("escort_gather", "escort_move", "hold")
    assert all(a[1] != 7 or decision.action == "escort_move" for a in game.actions)


def test_with_no_escort_free_a_settler_holds_for_a_site_near_a_hostile_unit():
    game = make_game(
        [settler(x=12, y=10)], cities=[alpha()], threats=[barbarian(14, 11)],
        sites=[site(14, 10, food=40)],
    )  # fmt: skip
    policy = SettlingPolicy(SettlingSettings(threat_radius=2))
    run(policy, game)
    assert policy.decisions[-1].action in ("hold", "flee")
    assert not any(a[1] == 7 and a[0] == "move" and a[2:] == (13, 10) for a in game.actions)


def test_a_phantom_stands_behind_the_border_not_on_it():
    # Run 13: a phantom on the nearest border tile refused every early site; the border is
    # seen from our side, so the unseen city stands further away.
    policy = SettlingPolicy()
    policy._border_memory = {(18, 10): 5, (18, 11): 5, (19, 9): 5}
    (x, y, pop) = policy._phantom_cities({(10, 10)})[0]
    assert x >= 20 and pop == policy.settings.rival_city_default_pop - 2
    nearest_border = min(hex_distance(10, 10, bx, by, 80) for bx, by in policy._border_memory)
    assert hex_distance(10, 10, x, y, 80) >= nearest_border + 2


def test_a_phantom_across_the_map_seam_stays_by_its_border():
    # Codex on PR 221: the map wraps east-west, so a cluster on both sides of the seam must not
    # average to the middle of the map, and the push must go the short way round.
    policy = SettlingPolicy()
    width = policy.settings.map_width
    policy._border_memory = {(width - 1, 10): 5, (0, 10): 5}
    (x, y, _) = policy._phantom_cities({(width - 6, 10)})[0]
    assert hex_distance(0, 10, x, y, width) <= 3
    assert hex_distance(width - 6, 10, x, y, width) >= 6
    policy._border_memory = {(0, 10): 5}
    (x, y, _) = policy._phantom_cities({(width - 4, 10)})[0]
    assert x in (1, 2, 3) and y == 10  # Pushed east, past the seam, away from us.


def test_default_floor_refuses_tikal_and_passes_run_12_palenque():
    # Run 13: a -6 drag and a +4 floor refused (67,21), Run 12's Palenque, which held all game
    # (raw +9.4 there in Run 13). With the rivals known, Tikal reads far below the floor.
    s = SettlingSettings()
    assert s.loyalty_reject_safe <= 9.4 - s.loyalty_new_city_drag - 4.0
    tikal = safe_loyalty_rate(*TIKAL, TIKAL_OWN, TIKAL_RIVALS, s)
    assert tikal < s.loyalty_reject_safe


# ----- fog: judge a site by what its far side may hide (Run 13: Uxmal) -----

UXMAL = (60, 29)
UXMAL_OWN = [(66, 25, 10, True), (64, 22, 9), (64, 28, 11), (69, 27, 3), (68, 21, 4)]
UXMAL_RIVALS = [
    (64, 18, 7, True),
    (66, 15, 2),
    (60, 24, 9),
    (67, 32, 5, True),
    (56, 19, 11, True),
    (55, 15, 9),
]


def test_robust_rate_flags_uxmal_but_passes_a_site_our_own_pull_holds():
    s = SettlingSettings(map_width=84)
    floor = s.loyalty_reject_safe
    assert safe_loyalty_rate(*UXMAL, UXMAL_OWN, UXMAL_RIVALS, s) >= floor  # What the bot saw.
    for d in (5, 6, 7):  # The Mapuche capital was 6 tiles off, unseen.
        assert safe_loyalty_rate(*UXMAL, UXMAL_OWN, UXMAL_RIVALS, s, unseen_distance=d) < floor
    # Run 12 Palenque (67,21), among four cities of ours: held even with a capital 5 tiles off.
    palenque_own = [(66, 25, 10, True), (64, 22, 9), (63, 27, 6), (61, 20, 4)]
    assert safe_loyalty_rate(67, 21, palenque_own, [], s, unseen_distance=5) >= floor
    # Run 13 Coba (64,28) with the capital and El Mirador: held once the fog is 9+ tiles away.
    coba_own = [(66, 25, 10, True), (64, 22, 9)]
    assert safe_loyalty_rate(64, 28, coba_own, [], s, unseen_distance=9) >= floor


def _fog_policy(site_xy, own, rivals, fog, scout_alive=True):
    policy = SettlingPolicy(SettlingSettings(map_width=84))
    policy._own_cities = own
    policy._rival_cities = rivals
    policy._fog = {site_xy: fog} if fog else {}
    policy._scout_alive = scout_alive
    exploration = Exploration((), (), (SettleSite(*site_xy, 30, 10, "fresh", (), 0, 0, 0),))
    unit = settler(x=site_xy[0], y=site_xy[1], max_moves=2)
    tiles = [(c[0], c[1]) for c in own]
    return policy._candidates(
        unit, exploration, tiles, [(c[0], c[1]) for c in rivals], _NoDanger(), False, 146, []
    )


class _NoDanger:
    threats: tuple = ()

    def near(self, *a):
        return False

    def on_path(self, *a):
        return False

    def recent_near(self, *a):
        return False


def test_a_fogged_rival_side_is_marked_down_not_refused(real_safe_rate):
    # Run 14: refusing these while a scout lived stalled expansion; the scout was on automatic
    # exploration and never looked. The site competes, marked down, and is a look point.
    fog = FogCount(*UXMAL, 120, 60, 5, -4, -2)
    (c,) = _fog_policy(UXMAL, UXMAL_OWN, UXMAL_RIVALS, fog)
    assert c.rejected != NEEDS_LOOK
    assert c.robust_rate < c.safe_rate and c.fog_share == 0.5


def test_a_needs_a_look_site_competes_with_a_penalty_scout_or_not(real_safe_rate):
    fog = FogCount(*UXMAL, 120, 60, 5, -4, -2)
    (looked,) = _fog_policy(UXMAL, UXMAL_OWN, UXMAL_RIVALS, fog, scout_alive=True)
    (blind,) = _fog_policy(UXMAL, UXMAL_OWN, UXMAL_RIVALS, fog, scout_alive=False)
    (seen,) = _fog_policy(UXMAL, UXMAL_OWN, UXMAL_RIVALS, None, scout_alive=False)
    assert blind.rejected is None and blind.loyalty_penalty > seen.loyalty_penalty
    assert looked.rejected is None and looked.loyalty_penalty == blind.loyalty_penalty


def test_a_revealed_far_side_or_a_small_fogged_share_needs_no_look(real_safe_rate):
    revealed = FogCount(*UXMAL, 120, 0, 99, 0, 0)
    assert _fog_policy(UXMAL, UXMAL_OWN, UXMAL_RIVALS, revealed)[0].rejected is None
    thin = FogCount(*UXMAL, 120, 20, 5, -4, -2)  # A sixth of it: not enough to wait for.
    assert _fog_policy(UXMAL, UXMAL_OWN, UXMAL_RIVALS, thin)[0].rejected is None


def test_a_site_in_our_own_pull_passes_even_with_a_fogged_far_side(real_safe_rate):
    own = [(66, 25, 10, True), (64, 22, 9), (63, 27, 6), (61, 20, 4)]
    fog = FogCount(67, 21, 100, 80, 6, 4, 0)
    (c,) = _fog_policy((67, 21), own, [], fog)
    assert c.rejected is None and c.robust_rate >= 0


def test_the_known_city_read_returns_fog_counts_and_asks_for_sites():
    from civ6_ai.state.reader import read_known_cities

    out = "FOG|16|10|40|12|6|3|-1\nKDONE"
    game = LuaGame(out)
    known = asyncio.run(read_known_cities(game, [(1, 2)], [(16, 10, 10, 10)], 9))
    assert known.fog == (FogCount(16, 10, 40, 12, 6, 3, -1),)
    assert game.lua_calls == 1 and '"16,10,10,10"' in game.lua_code[0]
    assert 'print("KDONE")' in game.lua_code[0]


def test_next_turn_reads_the_fog_of_the_sites_the_survey_found_and_a_scout_goes_to_look(
    real_safe_rate,
):
    out = "FOG|4|10|40|30|6|-5|0\nKDONE"
    scout = unit_info(unit_id=3, unit_index=3, x=10, y=12, **SCOUT)
    game = LuaGame(
        out,
        units=[scout],
        cities=[alpha()],
        settle=[site(4, 10, food=40)],
        overview=overview(turn=30, num_cities=1),
        strategic=lq.StrategicMapData([], []),
    )
    policy = SettlingPolicy(SettlingSettings(loyalty_reject_safe=15.0))
    run(policy, game)  # Survey: the site (4,10) is picked for the next read.
    assert policy._fog_sites and policy._fog_sites[0][:4] == (4, 10, 10, 10)
    run(policy, game)
    assert policy._fog[4, 10].unrevealed == 30
    assert policy._look_points == [(73, 10)]
    assert ("move", 3, 73, 10) in game.actions


def test_a_border_four_tiles_from_a_known_city_is_not_explained_by_it():
    # Run 13: a city 5 tiles off explained borders that belonged to three unseen cities.
    policy = SettlingPolicy()
    policy._rival_memory = {(60, 24): (4, 9)}
    policy._border_memory = {(57, 27): 4, (56, 27): 4}
    assert policy._phantom_cities({(66, 25)})
    policy._border_memory = {(60, 27): 4}  # Three tiles from the city: its own.
    assert not policy._phantom_cities({(66, 25)})


def test_early_sites_near_a_small_capital_need_no_look():
    # Every city is small early on: the unseen rival is sized like our median city, not a
    # pop-5 capital (as heavy as pop 10), which flagged every site near a pop-2 or pop-3 capital.
    s = SettlingSettings()
    for pop in (2, 3):
        for unseen in (5, 6, 7):
            rate = safe_loyalty_rate(62, 25, [(66, 25, pop, True)], [], s, unseen_distance=unseen)
            assert rate >= s.loyalty_reject_safe


def test_a_needed_look_outranks_a_scouts_previous_heading():
    # Codex on PR 228: `p != previous` sorted first, so a scout kept its old heading for ever.
    fog = lq.StrategicMapData([lq.FogBoundary("Alpha", 10, 10, [-1, 5, -1, 4, -1, -1])], [])
    scout = unit_info(unit_id=3, unit_index=3, x=10, y=12, **SCOUT)
    policy = SettlingPolicy()
    game = make_game([scout], cities=[alpha()], strategic=fog)
    run(policy, game)
    assert game.actions == [("move", 3, 10, 13)]  # Heading (10,13), kept next turn:
    game.actions.clear()
    run(policy, game)
    assert game.actions == [("move", 3, 10, 13)]
    # A site now needs the fog around (14,6) looked at: that beats the old heading.
    exploration = build_exploration(fog, [])
    game.actions.clear()
    asyncio.run(policy._explore(game, scout, exploration, _NoDanger(), 5, (14, 6)))
    assert game.actions[0][:2] == ("move", 3) and game.actions[0][2:] != (10, 13)


def test_a_site_whose_fog_is_not_measured_yet_is_not_founded_on(real_safe_rate):
    # Codex on PR 228: a site new to the scan had no fog record and passed as seen.
    out = "KDONE"  # The read works but brings no FOG line for the site yet.
    game = LuaGame(
        out,
        units=[settler(x=4, y=10)],
        cities=[alpha()],
        settle=[site(4, 10, food=40)],
        overview=overview(turn=30, num_cities=1),
        strategic=lq.StrategicMapData([], []),
    )
    policy = SettlingPolicy()
    run(policy, game)
    assert ("found", 7) not in game.actions
    assert policy.decisions[-1].action == "no_site"
    assert policy.decisions[-1].candidates[0].rejected == FOG_PENDING
    assert policy._fog_sites  # The survey asked for it, so the next read measures it.
    game.output = "FOG|4|10|40|0|99|0|0\nKDONE"  # Fully revealed: judged, and acceptable.
    run(policy, game)
    assert ("found", 7) in game.actions


def test_a_settlers_own_far_site_is_always_measured():
    # Founding waits for a fog measurement; a site outside the nearest few must still get one.
    policy = SettlingPolicy(SettlingSettings(fog_max_sites=1))
    near, far = site(12, 10), site(18, 10)
    exploration = type("E", (), {"settle_sites": [near, far]})()
    policy._site_claims = {7: (18, 10)}
    policy._choose_fog_sites(exploration, [(10, 10)])
    assert [spec[:2] for spec in policy._fog_sites] == [(18, 10)]
    policy._site_claims = {}
    policy._choose_fog_sites(exploration, [(10, 10)], [(18, 10)])
    assert [spec[:2] for spec in policy._fog_sites] == [(18, 10)]
    policy._choose_fog_sites(exploration, [(10, 10)])
    assert [spec[:2] for spec in policy._fog_sites] == [(12, 10)]


def test_run_14_el_mirador_reads_below_the_floor_once_the_unseen_city_is_sized_right(
    real_safe_rate,
):
    # Founded at (60,29) with 81% of its rival side unexplored; the unseen Mapuche capital (pop
    # 13, 6 tiles) pressed 10.4 and the city flipped 13 turns later. Sized like our median city
    # (pop 4) it read +11.9; sized like the largest known city (pop 8) and as a capital, below 0.
    own = [(66, 25, 5, True), (62, 25, 4), (69, 27, 2), (64, 29, 3)]
    rivals = [(64, 18, 7, True), (56, 19, 7, True), (60, 18, 4), (59, 14, 3), (55, 15, 5)]
    rivals += [(57, 9, 8, True), (61, 8, 2)]
    fog = FogCount(60, 29, 154, 125, 6, -5, 1)
    (c,) = _fog_policy((60, 29), own, rivals, fog)
    assert c.robust_rate < SettlingSettings().loyalty_reject_safe
    assert c.loyalty_penalty > 0


# ----- founding with a small threat near (the settler does not run from one barbarian) -----


def _found_with(threats, defence=None):
    game = make_game(
        [settler(x=14, y=10)], cities=[alpha()], threats=threats, sites=[site(14, 10, food=40)]
    )
    policy = SettlingPolicy(defence=defence)
    run(policy, game)
    return game, policy


def test_a_settler_on_a_good_site_founds_with_one_weak_barbarian_near():
    game, policy = _found_with([barbarian(16, 10)])
    assert ("found", 7) in game.actions
    assert policy.decisions[-1].reason.startswith("standing on a good site")


def test_a_settler_on_a_good_site_still_runs_from_a_strong_barbarian():
    strong = threat_info(x=16, y=10, owner_id=BARBARIANS, unit_type="UNIT_SWORDSMAN")
    game, policy = _found_with([strong])
    assert not [a for a in game.actions if a[0] == "found"]
    assert policy.decisions[-1].action == "flee"


def test_a_settler_on_a_good_site_still_runs_from_three_barbarians():
    game, _ = _found_with([barbarian(16, 10), barbarian(16, 11), barbarian(15, 9)])
    assert not [a for a in game.actions if a[0] == "found"]


def test_a_site_that_fails_another_rule_is_not_founded_on_just_for_a_small_threat():
    game = make_game(
        [settler(x=14, y=10)],
        cities=[alpha()],
        threats=[barbarian(16, 10)],
        sites=[site(14, 10, food=40)],
    )
    policy = SettlingPolicy(SettlingSettings(max_city_distance=1, far_city_distance=1))
    run(policy, game)  # Four tiles from the only city: too far, whatever the barbarian does.
    assert not [a for a in game.actions if a[0] == "found"]


def test_a_settler_does_not_found_under_threat_while_at_war_with_a_civ():
    game, _ = _found_with([barbarian(16, 10)], defence=_War(at_war=True))
    assert not [a for a in game.actions if a[0] == "found"]


def test_a_settler_off_the_site_still_runs_from_a_close_barbarian():
    game = make_game(
        [settler(x=13, y=10)],
        cities=[alpha()],
        threats=[barbarian(15, 10)],
        sites=[site(16, 10, food=40)],
    )
    policy = SettlingPolicy()
    run(policy, game)
    assert policy.decisions[0].action == "flee"


# ----- stepping onto a good site and founding in the same turn -----


def _step_game(ground, threats, settler_moves=2.0):
    from civ6_ai.state.reader import build_tile
    from civ6_ai.terrain import TerrainStore
    from fixtures.civ_state import tile

    game = make_game(
        [settler(x=13, y=10, moves_remaining=settler_moves)],
        cities=[alpha()],
        threats=threats,
        sites=[site(14, 10, food=40)],
    )

    async def arrives(unit_index, x, y):
        game.actions.append(("move", unit_index, x, y))
        return f"MOVING_TO|{x},{y}|from:13,10|now_at:{x},{y}"

    game.move_unit = arrives
    policy = SettlingPolicy()
    store = TerrainStore(map_width=74)
    if ground is not None:
        store.learn(
            [build_tile(tile(x=14, y=10, improvement=None, units=[], is_river=False, **ground))]
        )
    policy.terrain = store
    return game, policy


def test_a_settler_one_step_from_a_good_site_steps_on_and_founds_with_a_small_threat_near():
    game, policy = _step_game({}, [barbarian(16, 10)])  # Flat ground: one move left to found.
    run(policy, game)
    assert game.actions == [("move", 7, 14, 10), ("found", 7)]
    assert policy.decisions[-1].reason.startswith("stepped onto the site")


def test_a_settler_does_not_step_onto_hills_it_could_not_found_on_the_same_turn():
    game, policy = _step_game({"is_hills": True}, [barbarian(16, 10)])  # Hills cost both moves.
    run(policy, game)
    assert ("found", 7) not in game.actions
    assert ("move", 7, 14, 10) not in game.actions  # It does not walk into the barbarian's reach.


def test_a_settler_does_not_step_onto_a_tile_whose_cost_is_unknown():
    game, policy = _step_game(None, [barbarian(16, 10)])
    run(policy, game)
    assert ("found", 7) not in game.actions


def test_a_settler_does_not_step_and_found_with_a_larger_threat_near():
    game, policy = _step_game({}, [barbarian(16, 10), barbarian(16, 11), barbarian(15, 9)])
    run(policy, game)
    assert ("found", 7) not in game.actions


def test_a_founding_that_fails_right_after_the_step_does_not_blacklist_the_site():
    game, policy = _step_game({}, [barbarian(16, 10)])
    game.failing = {"found"}
    run(policy, game)
    assert [a[0] for a in game.actions] == ["move", "found"]
    assert (14, 10) not in policy._failed_sites


class _Sightings:
    """A threat map stand-in: hostile units last seen at the given tiles, `age` turns ago."""

    def __init__(self, *tiles, age=1):
        from civ6_ai.threat_map import UNIT, Source

        self.sources = [
            Source(UNIT, x, y, 30.0, 1, 100 - age, unit_type="UNIT_SKIRMISHER") for x, y in tiles
        ]


def _sighting_policy(sighted, fog_unrevealed, escort=False, age=1, y=14, turn=100):
    """A settler in the capital's reach, a site 4 tiles off, a Skirmisher seen 3 tiles from it."""
    policy = SettlingPolicy(SettlingSettings(map_width=84))
    policy.threat_map = _Sightings(*sighted, age=age)
    policy._fog = {(14, 10): FogCount(14, 10, 100, fog_unrevealed, 6, 3, 0)}
    units = [settler(x=10, y=10)]
    if escort:
        units.append(unit_info(unit_id=9, unit_index=9, x=10, y=10))
        units.append(unit_info(unit_id=10, unit_index=10, x=10, y=10))
    game = make_game(units, cities=[alpha()], sites=[site(14, 10, food=40)], turn=turn)
    # The turn the threat map was last updated is `last_seen`; `run` reads the game's turn.
    run(policy, game)
    return policy, game


def test_a_fogged_site_near_a_recent_sighting_is_not_walked_to_alone():
    # Issue 288: a Skirmisher seen 3 tiles from the site, then lost in the fog, was a camp's.
    policy, game = _sighting_policy([(14, 7)], fog_unrevealed=70)
    decision = policy.decisions[-1]
    assert decision.candidates[0].fog_risk
    assert decision.action == "hold" and "fogged site" in decision.reason
    assert not [a for a in game.actions if a[0] == "move"]


def test_a_fogged_site_with_a_recent_sighting_takes_an_escort():
    policy, game = _sighting_policy([(14, 7)], fog_unrevealed=70, escort=True)
    assert policy.decisions[-1].action in ("escort_move", "escort_gather")


def test_a_clear_or_old_or_far_sighting_does_not_hold_the_settler():
    for kwargs in (
        dict(sighted=[(14, 7)], fog_unrevealed=10),  # The surround is mostly seen.
        dict(sighted=[(14, 7)], fog_unrevealed=70, age=7),  # The sighting has aged out.
        dict(sighted=[(14, 1)], fog_unrevealed=70),  # Far from the site.
    ):
        policy, game = _sighting_policy(**kwargs)
        assert not policy.decisions[-1].candidates[0].fog_risk, kwargs
        assert any(a[0] == "move" and a[1] == 7 for a in game.actions), kwargs


def test_a_settler_stops_a_tile_short_of_a_site_with_a_recent_sighting_beside_it():
    # Issue 288: it would reach the site with no moves left and be captured there.
    policy = SettlingPolicy(SettlingSettings(map_width=84))
    policy.threat_map = _Sightings((14, 7))
    policy._fog = {}
    lone = settler(x=12, y=10, moves_remaining=2.0, max_moves=2.0)
    game = make_game([lone], cities=[alpha()], sites=[site(14, 10, food=40)], turn=100)
    run(policy, game)
    moves = [a for a in game.actions if a[0] == "move"]
    assert moves == [("move", 7, 13, 10)]


def test_a_settler_one_tile_short_steps_on_and_founds_in_one_turn():
    # Codex on PR 290: after stopping short, the next turn must found, not just step on.
    policy = SettlingPolicy(SettlingSettings(map_width=84))
    policy.threat_map = _Sightings((14, 7))
    policy.terrain = type("T", (), {"step_cost": lambda self, x, y, naval=False: 1})()
    lone = settler(x=13, y=10, moves_remaining=2.0, max_moves=2.0)
    game = make_game([lone], cities=[alpha()], sites=[site(14, 10, food=40)], turn=100)

    async def move(unit_index, x, y):
        game.actions.append(("move", unit_index, x, y))
        return f"MOVING_TO|{x},{y} now_at:{x},{y}"

    game.move_unit = move
    run(policy, game)
    assert game.actions == [("move", 7, 14, 10), ("found", 7)]


# ----- issue 291: escort strength, settlers in cities, backing off after a flee -----


def _courser(x, y):
    return _t("UNIT_COURSER", x, y, 46)


def test_settler_in_a_city_stays_with_two_coursers_adjacent_and_one_warrior():
    # Run 20 turn 142: the settler left Constantinople with a Warrior into two adjacent Coursers.
    warrior = unit_info(unit_id=9, unit_index=9, x=10, y=10, combat_strength=20)
    game = make_game(
        [settler(), warrior],
        cities=[alpha()],
        threats=[_courser(10, 11), _courser(11, 10)],
        sites=[site(14, 6, food=40)],
    )
    policy = _policy()
    run(policy, game)
    assert not game.actions
    assert policy.decisions[-1].action == "hold"


def _route_game(escort_type_strength, threats, escort_at=(10, 14)):
    escort = unit_info(
        unit_id=9,
        unit_index=9,
        x=escort_at[0],
        y=escort_at[1],
        combat_strength=escort_type_strength,
    )
    return make_game(
        [settler(y=14), escort],
        cities=[alpha()],
        threats=threats,
        sites=[site(14, 6, food=40)],
    )


def test_escort_that_cannot_hold_the_stop_tile_does_not_lead():
    # Two Coursers reach the stop tile (five tiles out is beyond a settler's flee radius).
    game = _route_game(20, [_courser(12, 9), _courser(12, 10)])
    policy = _policy()
    run(policy, game)
    assert not game.actions
    decision = policy.decisions[-1]
    assert decision.action == "hold" and "would not hold" in decision.reason


def test_escort_that_can_hold_the_stop_tile_still_leads():
    game = _route_game(36, [_courser(12, 9)])
    policy = _policy()
    run(policy, game)
    assert [a[1] for a in game.actions] == [9, 7]
    assert policy.decisions[-1].action == "escort_move"


def test_settler_that_fled_does_not_resume_while_the_hostile_is_near_the_route():
    policy = _policy()
    policy._fled[7] = (14, 6)
    game = _route_game(36, [_courser(12, 11)])
    run(policy, game)
    assert not game.actions
    assert "fled" in policy.decisions[-1].reason
    # The hostile is gone: it goes on, and the memory is cleared.
    clear = _route_game(36, [])
    run(policy, clear)
    assert clear.actions
    assert 7 not in policy._fled


def test_backing_off_one_route_leaves_another_site_open():
    policy = _policy()
    policy._fled[7] = (14, 6)
    game = make_game(
        [settler(y=14), unit_info(unit_id=9, unit_index=9, x=10, y=14, combat_strength=36)],
        cities=[alpha()],
        threats=[_courser(12, 11)],
        sites=[site(14, 6, food=40), site(4, 18, food=40)],
    )
    run(policy, game)
    assert game.actions
    assert all("fled" not in d.reason for d in policy.decisions)


# ----- city-states exert no loyalty pressure (issue 314) -----

# STD_T1_04 at turn 30: our capital (67,22) pop 4 and one revealed city, the city-state capital
# Jerusalem (63,11) pop 5. The human founded Ostia in the north (near (67,11)) and Aquileia
# (about (65,18)); both held 100 loyalty at +21 to +24 a turn.
STD04_OWN = [(67, 22, 4, True)]
STD04_CITY_STATE = [(63, 11, 5, True)]
STD04_SITES = {"Ostia": (67, 11), "Aquileia": (65, 18)}


def test_std_t1_04_city_state_counted_as_a_rival_refused_every_site():
    """The old model: the city-state as a rival capital made Ostia (4 tiles off it) flip."""
    s = SettlingSettings(map_width=74)
    assert (
        safe_loyalty_rate(*STD04_SITES["Ostia"], STD04_OWN, STD04_CITY_STATE, s)
        < s.loyalty_reject_safe
    )


def test_std_t1_04_sites_hold_with_no_city_state_pressure():
    s = SettlingSettings(map_width=74)
    for site in STD04_SITES.values():
        assert safe_loyalty_rate(*site, STD04_OWN, [], s) == pytest.approx(
            s.loyalty_rate_cap - s.loyalty_new_city_drag
        )  # +17, near the +21 to +24 the human saw.


def test_known_read_flags_city_state_cities_and_borders():
    from civ6_ai.state.reader import parse_known_cities

    known = parse_known_cities("KCITY|8|63|11|5|1|1|1\nBORDER|14|72,10|1\nBORDER|5|1,1|0\nKDONE")
    assert known.cities[0].minor and known.minors == {8, 14}


def test_city_state_cities_and_borders_do_not_press_but_still_space():
    output = "KCITY|8|63|11|5|1|1|1\nBORDER|14|70,5;71,5|1\nVISIBLE|\nKDONE"
    policy = SettlingPolicy()
    run(policy, lua_game(output))
    assert (63, 11, 5, True) in policy._rival_cities  # Kept for spacing.
    assert (63, 11, 5, True) not in policy._pressure_sources()  # No pressure.
    assert all(c[0] != 63 for c in policy._pressure_sources())
    assert policy._phantoms == []


# ----- Issue 319: a settler stops waiting after `max_wait_turns` -----


def _fog_wait_game(turn=100):
    """The issue 288 case: a site beside a sighting lost in the fog, so the settler holds."""
    return make_game(
        [settler(x=10, y=10)], cities=[alpha()], sites=[site(14, 10, food=40)], turn=turn
    )


def _fog_wait_policy():
    policy = SettlingPolicy(SettlingSettings(map_width=84))
    policy.threat_map = _Sightings((14, 7), age=1)
    policy._fog = {(14, 10): FogCount(14, 10, 100, 70, 6, 3, 0)}
    return policy


def test_waiting_turns_are_counted_per_settler_and_end_with_a_step():
    policy = _fog_wait_policy()
    for expected in (1, 2, 3):
        run(policy, _fog_wait_game())
        assert policy.decisions[-1].action == "hold"
        assert policy._waits[7] == expected
    game = _fog_wait_game()
    run(policy, game)  # The fourth turn: the cap is reached, the settler goes.
    assert policy.decisions[-1].action == "move"
    assert 7 not in policy._waits  # A step forward ends the run of waiting turns.


def test_after_the_cap_a_settler_goes_without_an_escort_when_no_hostile_can_reach_the_route():
    policy = _fog_wait_policy()
    for _ in range(3):
        run(policy, _fog_wait_game())
    game = _fog_wait_game()
    run(policy, game)
    decision = policy.decisions[-1]
    assert "waited 3 turns for an escort" in decision.reason
    assert [a for a in game.actions if a[0] == "move" and a[1] == 7]


def test_the_cap_is_the_setting():
    policy = _fog_wait_policy()
    policy.settings = SettlingSettings(map_width=84, max_wait_turns=1)
    run(policy, _fog_wait_game())
    run(policy, _fog_wait_game())
    assert policy.decisions[-1].action == "move"


def test_the_escort_that_cannot_move_stops_blocking_once_the_route_is_clear():
    policy = _fog_wait_policy()
    escort = unit_info(unit_id=9, unit_index=9, x=10, y=10)
    other = unit_info(unit_id=10, unit_index=10, x=10, y=10)
    for _ in range(3):
        game = make_game(
            [settler(x=10, y=10), escort, other],
            cities=[alpha()],
            sites=[site(14, 10, food=40)],
            turn=100,
        )
        game.failing = {"move"}
        run(policy, game)
    assert policy._waits[7] == 3
    game = make_game(
        [settler(x=10, y=10), escort, other],
        cities=[alpha()],
        sites=[site(14, 10, food=40)],
        turn=100,
    )
    run(policy, game)
    assert [a for a in game.actions if a[1] == 7]


def test_after_the_cap_a_threatened_far_site_gives_way_to_a_nearer_site_clear_of_hostiles():
    # The far site has a hostile unit in sight near its route; the nearer one only has a sighting
    # lost in the fog beside it (issue 288), which holds a settler for `max_wait_turns` turns.
    far, near = site(14, 6, food=40), site(10, 18, food=30)

    def play(waits):
        policy = SettlingPolicy(SettlingSettings(threat_radius=2, map_width=84))
        policy.threat_map = _Sightings((10, 20), age=1)
        policy._fog = {(10, 18): FogCount(10, 18, 100, 70, 6, 3, 0)}
        policy._waits[7] = waits
        game = make_game(
            [settler(y=14)],
            cities=[alpha()],
            threats=[barbarian(13, 10)],
            sites=[far, near],
            turn=100,
        )
        run(policy, game)
        return policy, game

    policy, game = play(0)
    assert policy.decisions[-1].action == "hold"
    policy, game = play(3)
    decision = policy.decisions[-1]
    assert decision.action == "move" and decision.target == (10, 18)
    assert "waited 3 turns for the far site" in decision.reason
    assert [a for a in game.actions if a[1] == 7]


def test_after_the_cap_a_settler_never_steps_into_a_visible_hostiles_reach():
    # Only site is past the hostile unit: no fallback, the settler keeps waiting (issues 283/291).
    for waits in (3, 9):
        policy = SettlingPolicy(SettlingSettings(threat_radius=2))
        policy._waits[7] = waits
        game = make_game(
            [settler(y=14)],
            cities=[alpha()],
            threats=[barbarian(13, 10)],
            sites=[site(14, 6, food=40)],
        )
        run(policy, game)
        assert policy.decisions[-1].action == "hold"
        assert not [a for a in game.actions if a[1] == 7]


def test_a_settler_does_not_walk_alone_past_a_mounted_hostile_after_the_cap():
    # A mounted unit reaches two tiles further, so a route five tiles off is still in its reach.
    policy = SettlingPolicy(SettlingSettings(threat_radius=2))
    policy._waits[7] = 5
    horse = threat_info(x=13, y=10, owner_id=BARBARIANS, unit_type="UNIT_HORSEMAN")
    game = make_game(
        [settler(y=14)], cities=[alpha()], threats=[horse], sites=[site(14, 6, food=40)]
    )
    run(policy, game)
    assert not [a for a in game.actions if a[1] == 7]


def test_a_flee_does_not_count_as_a_waiting_turn():
    policy = SettlingPolicy()
    policy._count_wait(7, "escort_gather", "")
    policy._wait_counted.clear()
    policy._count_wait(7, "flee", "MOVING_TO|1,1")
    policy._wait_counted.clear()
    assert policy._waits[7] == 1  # Neither counted nor reset.


def test_the_nearer_site_chosen_after_waiting_is_kept_on_the_next_turn():
    far, near = site(14, 6, food=40), site(10, 18, food=30)

    def play(policy):
        policy.threat_map = _Sightings((10, 20), age=1)
        policy._fog = {(10, 18): FogCount(10, 18, 100, 70, 6, 3, 0)}
        game = make_game(
            [settler(y=14)], cities=[alpha()], threats=[barbarian(13, 10)], sites=[far, near],
            turn=100,
        )  # fmt: skip
        run(policy, game)
        return policy.decisions[-1]

    policy = SettlingPolicy(SettlingSettings(threat_radius=2, map_width=84))
    policy._waits[7] = 3
    assert play(policy).target == (10, 18)
    assert policy._fallback_site[7] == (10, 18)
    assert 7 not in policy._waits  # The step ended the run of waits.
    again = play(policy)
    assert again.target == (10, 18) and "nearer site chosen after waiting" in again.reason


def test_the_kept_site_is_honoured_when_an_escort_appears_and_the_far_route_clears():
    # Codex on PR 320: a later turn must not undo the nearer site chosen after waiting.
    far, near = site(14, 6, food=40), site(10, 18, food=30)
    policy = SettlingPolicy(SettlingSettings(threat_radius=2, map_width=84))
    policy.threat_map = _Sightings((10, 20), age=1)
    policy._fog = {(10, 18): FogCount(10, 18, 100, 70, 6, 3, 0)}
    policy._waits[7] = 3
    first = make_game(
        [settler(y=14)], cities=[alpha()], threats=[barbarian(13, 10)], sites=[far, near],
        turn=100,
    )  # fmt: skip
    run(policy, first)
    assert policy._fallback_site[7] == (10, 18)
    escort = unit_info(unit_id=9, unit_index=9, x=10, y=14)
    second = make_game(
        [settler(y=14), escort], cities=[alpha()], sites=[far, near], turn=101
    )  # The hostile is gone and an escort stands on the settler.
    run(policy, second)
    decision = policy.decisions[-1]
    assert decision.target == (10, 18) and "nearer site chosen" in decision.reason
    assert [a for a in second.actions if a[1] == 7 and a[2:] == (10, 18)]


def test_a_flee_after_a_refused_first_city_leaves_the_wait_count_unchanged():
    # Bugbot on PR 320: the refused city records a hold, then the second city accepts the flee.
    policy = SettlingPolicy(SettlingSettings(map_width=84))
    policy._waits[7] = 2
    cities = [city_info(x=10, y=10, buildings=[]), city_info(x=14, y=14, buildings=[])]
    game = make_game(
        [settler(x=11, y=11)], cities=cities, threats=[barbarian(12, 12)],
        sites=[site(16, 10, food=40)], turn=100,
    )  # fmt: skip
    real = game.move_unit
    calls = []

    async def first_refused(unit_index, x, y):
        calls.append((x, y))
        if len(calls) == 1:
            return "Error: CANNOT_MOVE"
        return await real(unit_index, x, y)

    game.move_unit = first_refused
    run(policy, game)
    actions = [d.action for d in policy.decisions]
    assert actions[-2:] == ["hold", "flee"], actions
    assert policy._waits[7] == 2
    # With none counted before, the run stays empty.
    fresh = SettlingPolicy(SettlingSettings(map_width=84))
    calls.clear()
    game2 = make_game(
        [settler(x=11, y=11)], cities=cities, threats=[barbarian(12, 12)],
        sites=[site(16, 10, food=40)], turn=100,
    )  # fmt: skip
    real2 = game2.move_unit

    async def refused_first(unit_index, x, y):
        calls.append((x, y))
        return "Error: CANNOT_MOVE" if len(calls) == 1 else await real2(unit_index, x, y)

    game2.move_unit = refused_first
    run(fresh, game2)
    assert 7 not in fresh._waits
