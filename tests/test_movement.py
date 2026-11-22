"""The shared movement service (epic #399, slice 1): walking turns, cost fields, next steps,
route danger on the walk, threat reach, and the trip log, plus parity with the per-policy code it
replaced (builders and traders)."""

import asyncio
import json
import logging
import math

import pytest

import trip_report
from civ6_ai.movement import (
    Movement,
    MovementSettings,
    TripLog,
    straight_line,
    turn_field,
)
from civ6_ai.policies.builder import BuilderPolicy
from civ6_ai.policies.secondary import SecondaryPolicy, line_tiles
from civ6_ai.state.reader import build_tile, hex_distance
from civ6_ai.terrain import TerrainStore
from fixtures.civ_state import overview, tile
from test_policy_builder import ActionGame, job, task, worker, worker_unit
from test_policy_secondary import dest, trade_game, trader


def ground(x, y, code="g"):
    terrain = {"w": "TERRAIN_COAST", "m": "TERRAIN_GRASS_MOUNTAIN"}.get(code, "TERRAIN_GRASS")
    return build_tile(
        tile(
            x=x, y=y, terrain=terrain, feature="FEATURE_FOREST" if code == "f" else None,
            is_hills=code == "h", is_river=code == "r", improvement=None, units=[],
        )
    )  # fmt: skip


def store(rows=None, width=30, height=30, map_width=None, map_height=None):
    """Flat land over width x height; rows maps (x, y) to w water, m mountain, h hills, f forest,
    r river."""
    rows = rows or {}
    t = TerrainStore(map_width=map_width, map_height=map_height)
    t.learn(ground(x, y, rows.get((x, y), "g")) for x in range(width) for y in range(height))
    return t


class Map:
    """A threat map stand-in: raw danger by tile, with what `route_check` reads."""

    class settings:
        map_width = None

    def __init__(self, danger):
        self.danger = danger

    def raw_danger_at(self, x, y, turn=None):
        return self.danger.get((x, y), 0.0)

    def camps_reaching(self, x, y):
        return []

    def nearest_hostile(self, tiles, turn=None):
        return None


# ----- walking turns -----


def test_walk_turns_on_flat_ground_count_moves_per_turn():
    m = Movement(store())
    assert m.walk_turns((5, 10), (5, 10), 2) == 0
    assert m.walk_turns((5, 10), (7, 10), 2) == 1  # Two flat tiles on two moves.
    assert m.walk_turns((5, 10), (8, 10), 2) == 2
    assert m.walk_turns((5, 10), (10, 10), 2) == 3
    assert m.walk_turns((5, 10), (10, 10), 1) == 5


@pytest.mark.parametrize("moves", [1, 2, 3])
def test_open_ground_parity_with_the_old_builder_estimate(moves):
    """Where the old estimate (terrain cost over moves, rounded up) was right, the answer is the
    same: flat land with whole moves."""
    t = store(width=60, height=60)
    old = t.reach(30, 30, float(moves), False, 20)  # Turns, as fractions.
    new = turn_field(t, (30, 30), float(moves))
    near = [w for w in old if hex_distance(30, 30, *w, None) <= 15]  # Inside the known ground.
    assert len(near) > 500
    for where in near:
        assert new[where] == math.ceil(old[where]), where


def test_a_unit_may_enter_a_tile_while_it_has_any_movement_left():
    # Flat then hills: the old estimate said ceil(3 / 2) = 2 turns; the game lets the unit onto
    # the hill with one move left, so it is there this turn.
    assert Movement(store({(8, 10): "h"})).walk_turns((6, 10), (8, 10), 2) == 1
    # Hills first use both moves, so the flat tile after it is next turn.
    assert Movement(store({(7, 10): "h"})).walk_turns((6, 10), (8, 10), 2) == 2


def test_moves_already_spent_this_turn_count():
    m = Movement(store())
    assert m.turn_field((5, 10), 2, spent=1.0)[(7, 10)] == 2


def test_a_ridge_is_walked_round_not_crossed():
    ridge = {(12, y): "m" for y in range(0, 17)}
    m = Movement(store(ridge))
    open_ground = Movement(store())
    assert m.walk_turns((10, 10), (14, 10), 2) > open_ground.walk_turns((10, 10), (14, 10), 2)
    assert m.cost_field((14, 10))[(10, 10)] > open_ground.cost_field((14, 10))[(10, 10)]


def test_a_bay_costs_the_way_round():
    bay = {(x, y): "w" for x in (12, 13) for y in range(4, 16)}
    m = Movement(store(bay))
    field = m.cost_field((15, 10))
    assert field[(15, 10)] == 0
    assert field[(10, 10)] > 5  # Not the straight 5; round by the north.
    assert m.walk_turns((10, 10), (15, 10), 2) >= 4


def test_a_goal_in_the_water_or_past_the_horizon_has_no_walk():
    m = Movement(store({(9, 9): "w"}), settings=MovementSettings(horizon_turns=2))
    assert m.walk_turns((5, 5), (9, 9), 2) is None
    assert m.walk_turns((0, 0), (25, 25), 2) is None


def test_no_terrain_means_no_walk_and_a_hex_estimate():
    m = Movement(None, settings=MovementSettings(map_width=None))
    assert m.walk_turns((5, 5), (9, 5), 2) is None
    assert m.cost_field((9, 5)) is None
    assert m.estimate((5, 5), (9, 5), 2) == (2, "hex")
    assert m.estimate((5, 5), (5, 5), 2) == (0, "walk")  # Already there: no walk to judge.


def test_rows_past_the_map_height_are_not_walked_and_x_wraps():
    t = store(width=20, height=8, map_width=20, map_height=6)
    m = Movement(t)
    field = m.turn_field((10, 5), 2)
    assert all(y < 6 for _, y in field)
    assert m.walk_turns((19, 2), (1, 2), 2) == 1  # Over the seam: two tiles.
    assert m.walk_turns((19, 2), (3, 2), 2) == 2


def test_fields_are_reused_until_the_terrain_learns_something():
    t = store()
    m = Movement(t)
    first = m.turn_field((5, 5), 2)
    assert m.turn_field((5, 5), 2) is first
    t.learn([ground(40, 40)])
    assert m.turn_field((5, 5), 2) is not first


# ----- the next step -----


def test_next_step_follows_the_walk_and_avoids_held_and_refused_tiles():
    m = Movement(store())
    assert m.next_step((5, 10), (10, 10), 2) == (7, 10)
    assert m.next_step((5, 10), (10, 10), 2, avoid={(6, 10)}) != (7, 10)
    trip = m.trips.start(1, "builder", (5, 10), (10, 10), 3, 3)
    m.trips.refuse(1, (7, 10), "territory", 3)
    assert m.next_step((5, 10), (10, 10), 2, trip=trip, turn=4) != (7, 10)  # Kept for the trip.


def test_a_stacking_refusal_is_not_kept_across_turns():
    m = Movement(store())
    trip = m.trips.start(1, "builder", (5, 10), (10, 10), 3, 3)
    m.trips.refuse(1, (7, 10), "stacking", 3)
    assert m.next_step((5, 10), (10, 10), 2, trip=trip, turn=3) != (7, 10)
    assert m.next_step((5, 10), (10, 10), 2, trip=trip, turn=4) == (7, 10)


def test_next_step_is_none_with_no_terrain():
    assert Movement(None).next_step((5, 10), (10, 10), 2) is None


# ----- route danger on the walk -----


def test_straight_line_matches_the_old_trade_line_and_wraps_the_seam():
    assert straight_line(10, 10, 14, 10, 44) == line_tiles(10, 10, 14, 10, 44)
    assert straight_line(42, 10, 1, 10, 44) == line_tiles(42, 10, 1, 10, 44)
    assert straight_line(10, 10, 14, 10, 44)[0] == (10, 10)


def test_open_ground_route_danger_equals_the_straight_line():
    m = Movement(store(), Map({(12, 10): 4.0, (14, 10): 2.0}))
    assert m.route_tiles((10, 10), (14, 10)) == straight_line(10, 10, 14, 10, None)
    assert m.route_danger((10, 10), (14, 10), 5) == 4.0
    assert m.route_danger((10, 10), (14, 10), 5, danger_at=lambda x, y, t: 1.0) == 1.0
    assert Movement(store()).route_danger((10, 10), (14, 10), 5) == 0.0


def test_route_danger_can_sum():
    m = Movement(store(), Map({(12, 10): 4.0, (14, 10): 2.0}), MovementSettings(route_mode="sum"))
    assert m.route_danger((10, 10), (14, 10), 5) == 6.0


def test_danger_on_the_straight_line_but_across_a_bay_is_not_on_the_route():
    # The line from (10,10) to (16,10) crosses the bay at x=12..13; the walk goes round by row 3.
    bay = {(x, y): "w" for x in (12, 13) for y in range(4, 16)}
    camp_in_bay = Map({(13, 10): 8.0})
    walk = Movement(store(bay), camp_in_bay)
    line = Movement(None, camp_in_bay, MovementSettings(map_width=None))
    assert line.route_danger((10, 10), (16, 10), 5) == 8.0
    assert walk.route_danger((10, 10), (16, 10), 5) == 0.0
    assert (13, 10) not in walk.route_tiles((10, 10), (16, 10))


def test_danger_on_the_way_round_a_ridge_counts():
    ridge = {(12, y): "m" for y in range(0, 17)}
    on_detour = Map({(12, 17): 6.0})
    assert Movement(store(ridge), on_detour).route_danger((10, 10), (14, 10), 5) == 6.0
    line = Movement(None, on_detour, MovementSettings(map_width=None))
    assert line.route_danger((10, 10), (14, 10), 5) == 0.0


def test_a_route_with_no_walk_falls_back_to_the_straight_line():
    sea = {(x, y): "w" for x in range(12, 14) for y in range(0, 30)}
    m = Movement(store(sea, height=30, map_height=30), Map({(12, 10): 3.0}))
    assert m.route_tiles((10, 10), (16, 10)) == straight_line(10, 10, 16, 10, None)
    assert m.route_danger((10, 10), (16, 10), 5) == 3.0


# ----- threat reach -----


def test_melee_reach_is_moves_plus_one_and_ranged_is_moves_plus_range():
    m = Movement(store(width=40, height=40))
    melee = m.threat_reach((20, 20), 2)
    assert max(hex_distance(20, 20, x, y, None) for x, y in melee) == 3
    ranged = m.threat_reach((20, 20), 2, attack_range=2)
    assert max(hex_distance(20, 20, x, y, None) for x, y in ranged) == 4
    assert melee < ranged


def test_threat_reach_follows_the_terrain():
    hills = {(x, y): "h" for x in range(21, 25) for y in range(0, 40)}
    open_ground = Movement(store(width=40, height=40))
    assert (23, 20) in open_ground.threat_reach((20, 20), 2)
    # A hill band uses up its moves: one hill tile is entered, then no more this turn.
    assert (23, 20) not in Movement(store(hills, width=40, height=40)).threat_reach((20, 20), 2)
    mountains = {(21, y): "m" for y in range(0, 40)}
    wall = Movement(store(mountains, width=40, height=40)).threat_reach((20, 20), 2)
    assert (21, 20) in wall  # Attacked from beside; it can't be stood on.
    assert (23, 20) not in wall


def test_threat_reach_without_terrain_uses_hex_distance():
    reach = Movement(None, settings=MovementSettings(map_width=None)).threat_reach((20, 20), 2)
    assert max(hex_distance(20, 20, x, y, None) for x, y in reach) == 3


# ----- the trip log -----


def _rows(caplog):
    return [
        json.loads(r.getMessage().split("movement_trip ")[1])
        for r in caplog.records
        if "movement_trip" in r.getMessage()
    ]


def test_a_trip_logs_estimated_and_actual_turns_on_arrival(caplog):
    trips = TripLog()
    trips.start(7, "builder", (1, 1), (5, 1), 10, 2)
    with caplog.at_level(logging.INFO, logger="civ6_ai.movement"):
        trips.observe(7, (3, 1), 11)  # On the way: nothing yet.
        trips.observe(7, (5, 1), 12)
    rows = _rows(caplog)
    assert len(rows) == 1
    assert (rows[0]["estimated"], rows[0]["actual"], rows[0]["outcome"]) == (2, 3, "arrived")
    assert trips.trip(7) is None


def test_arrival_on_the_order_turn_is_one_turn():
    trips = TripLog()
    trips.start(7, "builder", (1, 1), (2, 1), 10, 1)
    trips.arrived(7, 10)
    assert trips.closed[0]["actual"] == 1


def test_the_same_goal_carries_on_and_a_new_goal_closes_the_old_trip():
    trips = TripLog()
    first = trips.start(7, "builder", (1, 1), (5, 1), 10, 2)
    assert trips.start(7, "builder", (2, 1), (5, 1), 11, 1) is first
    trips.start(7, "builder", (2, 1), (9, 9), 11, 4)
    assert [t["outcome"] for t in trips.closed] == ["retargeted"]


def test_lost_units_and_timeouts_close_their_trips():
    trips = TripLog(MovementSettings(trip_timeout_turns=5))
    trips.start(1, "builder", (1, 1), (9, 1), 10, 4)
    trips.start(2, "trader", (1, 1), (9, 1), 10, 4)
    trips.sweep({2}, 12, "builder")
    trips.observe(2, (3, 1), 15)
    assert [(t["unit_id"], t["outcome"]) for t in trips.closed] == [(1, "gone"), (2, "timeout")]


def test_refusals_are_logged_with_the_trip():
    trips = TripLog()
    trips.start(1, "builder", (1, 1), (9, 1), 10, 4)
    trips.refuse(1, (3, 1), "territory", 10)
    trips.abandon(1, 10, "refused")
    assert trips.closed[0]["refused"] == [[3, 1, "territory"]]


# ----- builders -----


def test_builder_route_danger_matches_the_old_line_on_open_ground():
    policy = BuilderPolicy(terrain=store(), threat_map=Map({(12, 10): 4.0}))
    target = job(x=14, y=10)
    old = max(policy.danger_at(x, y, 5) for x, y in line_tiles(10, 10, 14, 10, None))
    assert policy._route_danger(worker_unit(10, 10), target, 5) == old == 4.0


def test_builder_route_danger_follows_the_walk_round_a_ridge():
    ridge = {(12, y): "m" for y in range(0, 17)}
    policy = BuilderPolicy(terrain=store(ridge), threat_map=Map({(12, 17): 6.0}))
    assert policy._route_danger(worker_unit(10, 10), job(x=14, y=10), 5) == 6.0


def test_builder_route_danger_still_counts_a_lost_builder_and_is_capped():
    policy = BuilderPolicy(terrain=store(), threat_map=Map({(12, 10): 40.0}))
    assert (
        policy._route_danger(worker_unit(10, 10), job(x=14, y=10), 5) == policy.settings.danger_cap
    )
    policy.lost.append((12, 10, 4))
    quiet = BuilderPolicy(terrain=store())
    quiet.lost.append((12, 10, 4))
    assert (
        quiet._route_danger(worker_unit(10, 10), job(x=14, y=10), 5) == quiet.settings.lost_danger
    )


def test_builder_walk_uses_the_any_movement_left_rule():
    policy = BuilderPolicy(terrain=store({(8, 10): "h"}))
    reach = policy._reach(worker_unit(6, 10))  # A builder has 2 moves.
    assert reach[(8, 10)] == 1.0
    assert BuilderPolicy()._reach(worker_unit(6, 10)) is None


def test_builder_trip_is_logged_with_estimate_and_actual(caplog):
    policy = BuilderPolicy(terrain=store())
    with caplog.at_level(logging.INFO, logger="civ6_ai.movement"):
        asyncio.run(policy.run(ActionGame([task(x=14, y=10)], [worker(x=10, y=10)])))  # Turn 5.
        assert policy._movement().trips.trip(1).estimated == 2  # Four tiles at two moves.
        later = ActionGame([task(x=14, y=10)], [worker(x=14, y=10)])
        later.data["overview"] = overview(turn=7)
        asyncio.run(policy.run(later))
    (row,) = _rows(caplog)
    assert (row["kind"], row["goal"], row["outcome"]) == ("builder", [14, 10], "arrived")
    assert (row["estimated"], row["actual"]) == (2, 3)


def test_a_refused_builder_move_ends_the_trip_with_its_refusal():
    policy = BuilderPolicy(terrain=store())
    game = ActionGame([task(x=14, y=10)], [worker(x=10, y=10)])
    game.replies["move"] = "ERR:Cannot enter foreign territory"
    asyncio.run(policy.run(game))
    row = policy._movement().trips.closed[0]
    assert row["outcome"] == "refused" and row["refused"] == [[14, 10, "territory"]]


def test_a_builder_that_walks_the_whole_way_this_turn_arrives_on_turn_one():
    policy = BuilderPolicy(terrain=store())
    game = ActionGame([task(x=12, y=10)], [worker(x=10, y=10)])
    game.replies["move"] = "MOVED|now_at:12,10"
    asyncio.run(policy.run(game))
    row = policy._movement().trips.closed[0]
    assert (row["estimated"], row["actual"], row["outcome"]) == (1, 1, "arrived")


def test_a_lost_builder_closes_its_trip():
    policy = BuilderPolicy(terrain=store())
    asyncio.run(policy.run(ActionGame([task(x=20, y=10)], [worker(x=10, y=10)])))
    asyncio.run(policy.run(ActionGame([task(x=20, y=10)], [worker(2, x=30, y=10)])))
    assert policy._movement().trips.closed[0]["outcome"] == "gone"


# ----- traders -----


def test_trader_route_danger_follows_the_walk_not_the_line():
    bay = {(x, y): "w" for x in (12, 13) for y in range(4, 16)}
    camp = Map({(13, 10): 9.0})
    policy = SecondaryPolicy()
    policy.map = camp
    policy.movement = Movement(store(bay), camp)
    danger, _ = policy._route_danger(trader(x=10, y=10), dest("Far", x=16, y=10), 50)
    assert danger == 0.0
    straight = SecondaryPolicy()
    straight.map = camp
    danger, _ = straight._route_danger(trader(x=10, y=10), dest("Far", x=16, y=10), 50)
    assert danger == 9.0  # Without the service, the old line.


def test_trader_route_danger_matches_the_line_on_open_ground():
    camp = Map({(13, 10): 9.0})
    walked, line = SecondaryPolicy(), SecondaryPolicy()
    walked.map = line.map = camp
    walked.movement = Movement(store(), camp)
    args = (trader(x=10, y=10), dest("Far", x=16, y=10), 50)
    assert walked._route_danger(*args) == line._route_danger(*args) == (9.0, [])


def test_trader_trip_is_logged_when_it_stands_at_the_destination():
    policy = SecondaryPolicy()
    policy.movement = Movement(store())
    game = trade_game([trader(x=10, y=10)], dests={5: [dest("Far", x=16, y=10)]}, turn=50)
    asyncio.run(policy._trade(game, 50))
    open_trip = policy.movement.trips.trip(5)
    assert (open_trip.goal, open_trip.estimated) == ((16, 10), 3)  # Six tiles at 2 moves.
    arrived = trade_game([trader(x=16, y=10, on_route=True)], active=1, turn=53)
    asyncio.run(policy._trade(arrived, 53))
    row = policy.movement.trips.closed[0]
    assert (row["kind"], row["estimated"], row["actual"]) == ("trader", 3, 4)


def test_a_lost_trader_closes_its_trip():
    policy = SecondaryPolicy()
    policy.movement = Movement(store())
    game = trade_game([trader()], dests={5: [dest("Far", x=16, y=10)]})
    asyncio.run(policy._trade(game, 50))
    asyncio.run(policy._trade(trade_game([], turn=52), 52))
    assert policy.movement.trips.closed[0]["outcome"] == "gone"


# ----- the report script -----


def _line(kind, estimated, actual, outcome="arrived", basis="walk"):
    row = dict(kind=kind, estimated=estimated, actual=actual, outcome=outcome, basis=basis)
    return "2026-01-01 INFO civ6_ai.movement movement_trip " + json.dumps(row)


def test_trip_report_grades_arrived_trips_by_kind(tmp_path):
    log = tmp_path / "game_bot.log"
    log.write_text(
        "\n".join(
            [
                _line("builder", 2, 2),
                _line("builder", 2, 3),
                _line("builder", 2, 5),
                _line("builder", 2, 1, outcome="refused"),
                _line("trader", 4, 4),
                _line("trader", None, 4),
                "unrelated line",
                "movement_trip {broken",
            ]
        ),
        encoding="utf-8",
    )
    kinds = trip_report.summarise(trip_report.read_trips(trip_report.log_files(log)))
    b = kinds["builder"]
    assert (b["trips"], b["arrived"], b["graded"], b["within"], b["refused"]) == (4, 3, 3, 2, 1)
    assert b["mean_abs"] == pytest.approx(4 / 3)
    assert b["mean_signed"] == pytest.approx(4 / 3)
    assert kinds["trader"]["graded"] == 1 and kinds["trader"]["share_within"] == 1.0
    text = trip_report.format_report(kinds)
    assert "builder: 4 trips, 3 arrived, 1 refused" in text and "67%" in text
    assert trip_report.format_report({}) == "No movement_trip lines found."


def test_trip_report_finds_the_bot_log_from_a_turn_log_or_a_folder(tmp_path):
    (tmp_path / "game_bot.log").write_text(_line("builder", 1, 1), encoding="utf-8")
    assert trip_report.log_files(tmp_path / "game.jsonl") == [tmp_path / "game_bot.log"]
    assert trip_report.log_files(tmp_path) == [tmp_path / "game_bot.log"]
    assert len(trip_report.read_trips(trip_report.log_files(tmp_path))) == 1


# ----- the step follows the same rule as the estimate (review of #402) -----


def test_next_step_enters_a_hill_with_a_move_left_like_walk_turns():
    m = Movement(store({(8, 10): "h"}))
    assert m.walk_turns((6, 10), (8, 10), 2) == 1
    assert m.next_step((6, 10), (8, 10), 2) == (8, 10)  # Flat, then the hill, in one turn.


def test_next_step_is_always_a_tile_the_estimate_reaches_this_turn():
    t = store({(7, 10): "h", (9, 10): "f", (10, 11): "h"})
    m = Movement(t)
    for goal in [(12, 10), (11, 12), (14, 9)]:
        step = m.next_step((6, 10), goal, 2)
        assert m.turn_field((6, 10), 2)[step] == 1


def test_next_step_with_the_path_taken_picks_a_free_tile_reached_this_turn():
    m = Movement(store())
    step = m.next_step((5, 10), (10, 10), 2, occupied={(6, 10), (7, 10)})
    assert step is not None and step not in {(6, 10), (7, 10)}
    assert m.turn_field((5, 10), 2)[step] == 1


def test_next_step_stops_beside_the_goal():
    assert Movement(store()).next_step((5, 10), (7, 10), 2, stop_short=True) == (6, 10)


def test_a_builder_that_reached_its_goal_is_observed_before_a_new_job_is_picked(caplog):
    policy = BuilderPolicy(terrain=store())
    asyncio.run(policy.run(ActionGame([task(x=14, y=10)], [worker(x=10, y=10)])))
    # It walked on by its own order and stands on (14, 10), but a better job is offered elsewhere.
    later = ActionGame([task(x=14, y=10), task("urgent", x=20, y=10)], [worker(x=14, y=10)])
    later.data["overview"] = overview(turn=7)
    with caplog.at_level(logging.INFO, logger="civ6_ai.movement"):
        asyncio.run(policy.run(later))
    first = _rows(caplog)[0]
    assert (first["goal"], first["outcome"], first["actual"]) == ([14, 10], "arrived", 3)
