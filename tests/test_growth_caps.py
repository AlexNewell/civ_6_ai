"""Issue 215: citizens off food at a housing or amenity cap, behind a setting."""

import asyncio
import logging
from dataclasses import replace

from civ6_ai.policies.growth import GrowthPolicy, GrowthWeights
from civ6_ai.policies.growth_caps import (
    decide_cap_focus,
    hold_cap_focus,
    most_needed_yield,
    settler_delay_note,
)
from civ6_ai.policies.production import CityProductionPolicy
from civ6_ai.state import read_empire
from civ6_ai.state.reader import build_city
from fixtures import civ_state as fx
from test_policy_production import FocusGame


def city(**changes):
    buildings = changes.pop("buildings", ["MONUMENT"])
    return build_city(fx.city_info(buildings=buildings, **changes), (), set())


def run(coro):
    return asyncio.run(coro)


def test_off_is_a_no_op():
    c = city(population=7, housing=7.0)
    assert decide_cap_focus(c).focus is None
    assert decide_cap_focus(c, enabled=False, amenity_net=-3).focus is None


def test_at_housing_cap_moves_to_production_and_logs_the_reason():
    c = city(population=7, housing=7.0, currently_building="BUILDING_LIBRARY")
    v = decide_cap_focus(c, enabled=True)
    assert v.focus == "production" and v.at_cap
    assert "housing cap 7/7" in v.reason and "production" in v.reason


def test_below_the_cap_leaves_it_growing():
    v = decide_cap_focus(city(population=5, housing=7.0), enabled=True)
    assert v.focus is None and not v.at_cap


def test_amenity_negative_counts_as_the_cap_even_with_housing_room():
    c = city(population=4, housing=9.0)
    assert decide_cap_focus(c, enabled=True, amenity_net=2).focus is None
    v = decide_cap_focus(c, enabled=True, amenity_net=-1)
    assert v.focus is not None and "amenities -1" in v.reason


def test_starving_city_is_left_to_the_food_rule():
    c = city(population=7, housing=7.0, food_surplus=-1.0)
    v = decide_cap_focus(c, enabled=True, amenity_net=-2)
    assert v.focus is None and "food-focus" in v.reason


def test_idle_city_gets_the_empires_lowest_weighted_yield():
    empire = run(read_empire(fx.FakeGame()))
    empire = replace(empire, science=30.0, culture=2.0, gold_per_turn=20.0, faith=9.0)
    idle = city(population=7, housing=7.0, currently_building="nothing")
    assert most_needed_yield(idle, empire) == "culture"
    assert decide_cap_focus(idle, empire, enabled=True).focus == "culture"


def test_settler_delay_note_is_log_only_and_windowed():
    near = city(currently_building="UNIT_SETTLER", production_turns_left=2, turns_to_grow=4)
    assert "delaying it 2t" in settler_delay_note(near)
    assert settler_delay_note(replace(near, build_turns_left=4)) is None  # Same turn or later.
    assert settler_delay_note(replace(near, build_turns_left=-1)) is None
    far = city(currently_building="UNIT_SETTLER", production_turns_left=1, turns_to_grow=20)
    assert settler_delay_note(far) is None
    assert settler_delay_note(city(currently_building="BUILDING_LIBRARY")) is None


def _policy(**kw):
    policy = CityProductionPolicy()
    policy.growth = GrowthPolicy(weights=GrowthWeights(**kw))
    return policy


BUSY = dict(currently_building="BUILDING_LIBRARY", production_turns_left=5)


def at_turn(game, turn):
    game.data["overview"] = fx.overview(turn=turn, num_cities=1)


def test_policy_hook_sets_the_focus_once_and_resets_below_the_cap():
    game = FocusGame(cities=[fx.city_info(population=7, housing=7.0, **BUSY)])
    policy = _policy(cap_focus=True)
    for turn in range(40, 43):
        at_turn(game, turn)
        run(policy.assign(game))
    assert game.focus_calls == [(65536, "production")]
    game.data["cities"] = [fx.city_info(population=7, housing=10.0, **BUSY)]
    at_turn(game, 43)
    run(policy.assign(game))
    at_turn(game, 44)
    run(policy.assign(game))
    assert game.focus_calls == [(65536, "production"), (65536, "default")]


def test_policy_hook_off_makes_no_call():
    game = FocusGame(cities=[fx.city_info(population=7, housing=7.0, **BUSY)])
    run(CityProductionPolicy().assign(game))
    assert game.focus_calls == []


def test_policy_hook_leaves_a_starving_city_to_the_food_rule():
    game = FocusGame(cities=[fx.city_info(population=7, housing=7.0, food_surplus=-1.0, **BUSY)])
    run(_policy(cap_focus=True).assign(game))
    assert game.focus_calls == [(65536, "food")]


def test_policy_logs_the_settler_note(caplog):
    game = FocusGame(
        cities=[
            fx.city_info(
                currently_building="UNIT_SETTLER", production_turns_left=2, turns_to_grow=4
            )
        ]
    )
    with caplog.at_level(logging.INFO):
        run(_policy(settler_note=True).assign(game))
    assert any("delaying it" in r.message for r in caplog.records)


def test_faith_balance_is_never_compared_with_income():
    empire = run(read_empire(fx.FakeGame()))
    empire = replace(empire, science=30.0, culture=20.0, gold_per_turn=25.0, faith=0.0)
    idle = city(population=7, housing=7.0, currently_building="nothing")
    assert most_needed_yield(idle, empire) != "faith"


def test_policy_hook_updates_the_held_focus_when_the_desired_yield_changes():
    idle = dict(population=7, housing=7.0, currently_building="nothing")
    game = FocusGame(cities=[fx.city_info(**idle)])
    policy = _policy(cap_focus=True, cap_min_hold=3)
    at_turn(game, 40)
    run(policy.assign(game))
    first = game.focus_calls[-1][1]
    assert first != "production"  # Idle city: an empire yield.
    game.data["cities"] = [fx.city_info(population=7, housing=7.0, **BUSY)]
    at_turn(game, 41)
    run(policy.assign(game))
    assert len(game.focus_calls) == 1  # Inside the minimum hold.
    at_turn(game, 43)
    run(policy.assign(game))
    assert game.focus_calls[-1] == (65536, "production")
    assert len(game.focus_calls) == 2
    at_turn(game, 44)
    run(policy.assign(game))
    assert len(game.focus_calls) == 2  # Unchanged: no repeat call.


def test_hold_keeps_the_focus_when_the_need_alternates_slightly():
    held = "gold"
    # Science is a little more needed than gold, inside the margin: keep gold.
    needs = {"science": 9.0, "culture": 20.0, "gold": 10.0}
    assert hold_cap_focus(held, 9, needs, margin=0.25, min_hold=5) == "gold"
    flipped = {"science": 10.0, "culture": 20.0, "gold": 9.0}
    assert hold_cap_focus(held, 9, flipped, margin=0.25, min_hold=5) == "gold"


def test_hold_switches_once_on_a_large_change_after_the_minimum_hold():
    needs = {"science": 2.0, "culture": 20.0, "gold": 10.0}
    assert hold_cap_focus("gold", 2, needs, margin=0.25, min_hold=5) == "gold"
    assert hold_cap_focus("gold", 5, needs, margin=0.25, min_hold=5) == "science"
    assert hold_cap_focus(None, 0, needs, margin=0.25, min_hold=5) == "science"


def test_hold_gives_up_a_focus_that_is_no_longer_a_candidate():
    needs = {"science": 5.0, "culture": 6.0, "gold": 7.0}
    assert hold_cap_focus("production", 5, needs, margin=0.25, min_hold=5) == "science"
    assert hold_cap_focus("production", 1, needs, margin=0.25, min_hold=5) == "production"


def test_policy_hold_flipping_build_state_makes_one_call_per_hold():
    idle = dict(currently_building="nothing")
    game = FocusGame(cities=[fx.city_info(population=7, housing=7.0, **idle)])
    policy = _policy(cap_focus=True)  # Default hold of 5 turns.
    for turn in range(40, 45):  # Build state alternates every turn.
        busy = turn % 2 == 1
        game.data["cities"] = [fx.city_info(population=7, housing=7.0, **(BUSY if busy else idle))]
        at_turn(game, turn)
        run(policy.assign(game))
    assert len(game.focus_calls) == 1


def test_policy_one_call_per_city_per_turn():
    game = FocusGame(cities=[fx.city_info(population=7, housing=7.0, **BUSY)])
    policy = _policy(cap_focus=True, cap_min_hold=0, cap_margin=0.0)
    at_turn(game, 40)
    run(policy.assign(game))
    game.data["cities"] = [fx.city_info(population=7, housing=7.0, currently_building="nothing")]
    run(policy.assign(game))  # Same game turn: the second change waits.
    assert len(game.focus_calls) == 1
    at_turn(game, 41)
    run(policy.assign(game))
    assert len(game.focus_calls) == 2
