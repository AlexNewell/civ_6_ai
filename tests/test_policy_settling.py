import asyncio
import json
import logging

from civ_mcp import lua as lq

from civ6_ai.orchestrator import Handler
from civ6_ai.policies.settling import (
    SettlingPolicy,
    SettlingSettings,
    frontier_points,
    hex_line,
    neighbours,
    score_site,
)
from civ6_ai.state.models import SettleSite
from civ6_ai.state.reader import build_exploration
from fixtures.civ_state import BARBARIANS, FakeGame, city_info, overview, threat_info, unit_info

SETTLER = dict(unit_type="UNIT_SETTLER", name="Settler", combat_strength=0)
SCOUT = dict(unit_type="UNIT_SCOUT", name="Scout", combat_strength=0, max_moves=3.0)
GOOD = dict(food=20, prod=10, water="fresh")  # Scores 65.


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
