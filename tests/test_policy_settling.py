import asyncio
import json
import logging

import pytest
from civ_mcp import lua as lq

from civ6_ai.orchestrator import Handler
from civ6_ai.policies.expansion import ExpansionState
from civ6_ai.policies.settling import (
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
from civ6_ai.state.models import SettleSite
from civ6_ai.state.reader import build_exploration
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
    return city_info(x=10, y=10)


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


def test_a_held_settler_gives_up_waiting_after_a_bounded_number_of_turns():
    settings = SettlingSettings(threat_radius=2, max_hold_turns=3)
    policy = SettlingPolicy(settings)
    sites = [site(14, 6, food=40)]
    for turn in (1, 2, 3):
        game = make_game(
            [settler(y=14)], cities=[alpha()], threats=[barbarian(13, 10)], sites=sites, turn=turn
        )
        run(policy, game)
        assert game.actions == []
    final = make_game(
        [settler(y=14)], cities=[alpha()], threats=[barbarian(13, 10)], sites=sites, turn=4
    )
    run(policy, final)
    assert final.actions == [("move", 7, 14, 6)]
    assert "without an escort" in policy.decisions[-1].reason


def test_an_escort_that_cannot_move_does_not_stall_the_pair_for_good():
    settings = SettlingSettings(threat_radius=2, max_hold_turns=2)
    policy = SettlingPolicy(settings)
    sites = [site(14, 6, food=40)]
    escort = unit_info(unit_id=9, unit_index=9, x=10, y=14)
    for turn in (1, 2):
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
    last = make_game(
        [settler(y=14), escort], cities=[alpha()], threats=[barbarian(13, 10)], sites=sites, turn=3
    )
    run(policy, last)
    assert last.actions == [("move", 7, 14, 6)]


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
    barbs = [barbarian(17, 10)]
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
    policy = SettlingPolicy(SettlingSettings(loyalty_new_city_drag=DRAG))
    run(policy, game)
    by_site = {(c.x, c.y): c for c in policy.decisions[0].candidates}
    s = SettlingSettings(loyalty_new_city_drag=DRAG)
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
    policy = SettlingPolicy(SettlingSettings(loyalty_new_city_drag=DRAG))
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
