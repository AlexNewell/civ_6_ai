"""City growth targets (1.47): target size, stance, and how production scoring reads them."""

import asyncio
import logging
from dataclasses import replace

from civ6_ai.policies.growth import (
    GrowthPolicy,
    GrowthWeights,
    Stance,
    food_multiplier,
    housing_multiplier,
    plan_city,
)
from civ6_ai.policies.production import ProductionContext, ProductionWeights, score
from civ6_ai.state import ProductionChoice, read_empire
from civ6_ai.state.reader import build_city
from fixtures import civ_state as fx


def city(**changes):
    # The fixture's PALACE makes a capital. Tests want an ordinary city unless they ask.
    buildings = changes.pop("buildings", ["MONUMENT"])
    return build_city(fx.city_info(buildings=buildings, **changes), (), set())


def plan(c, turn=100, max_turns=330, num_cities=6, target=6, **kw):
    return plan_city(c, turn, max_turns, num_cities, target, **kw)


def test_housing_cap_limits_target_and_counts_the_gap():
    c = city(population=6, housing=6.0, food_surplus=4.0, turns_to_grow=8)
    p = plan(c)
    assert p.target == 6
    assert p.stance is Stance.HOLD
    assert p.housing_limit == 6
    assert p.housing_gap > 0
    assert p.food_gap == 0
    assert housing_multiplier(p) > 1.5


def test_housing_far_away_is_not_urgent():
    p = plan(city(population=3, housing=9.0, food_surplus=4.0))
    assert p.housing_gap == 0
    assert housing_multiplier(p) == GrowthWeights().housing_idle


def test_food_limited_city_wants_food_not_housing():
    p = plan(city(population=5, housing=10.0, food_surplus=0.4, turns_to_grow=0))
    assert p.food_limit == 5
    assert p.target == 5
    assert p.food_gap > 0 and p.housing_gap == 0
    assert food_multiplier(p) > 1.0


def test_starving_city_is_fed_and_never_planned_below_its_size():
    p = plan(city(population=7, housing=9.0, food_surplus=-1.5))
    assert p.stance is Stance.FEED
    assert p.target >= 7
    assert any("losing" in r for r in p.reasons)


def test_pump_city_plateaus_then_holds():
    c = city(population=4, housing=9.0, food_surplus=3.0, turns_to_grow=8)
    p = plan(c, turn=60, num_cities=3, target=6)
    assert p.target == 4
    assert p.stance is Stance.HOLD
    assert food_multiplier(p) == GrowthWeights().food_hold


def test_capital_is_not_capped_by_the_pump():
    c = city(population=4, housing=9.0, food_surplus=3.0, districts=["DISTRICT_CITY_CENTER@10,10"])
    p = plan(replace(c, is_capital=True), turn=60, num_cities=3, target=6)
    assert p.target > 4
    assert p.stance is Stance.GROW


def test_pump_plateau_rises_to_unlock_a_district():
    districts = [
        "DISTRICT_CITY_CENTER@10,10",
        "DISTRICT_CAMPUS@11,10",
    ]
    c = city(population=4, housing=12.0, food_surplus=3.0, districts=districts)
    p = plan(c, turn=60, num_cities=3, target=6)  # 1 specialty, 4//3 + 1 = 2 allowed: no cap
    assert p.target == 4
    two = districts + ["DISTRICT_HOLY_SITE@12,10"]
    p = plan(city(population=4, housing=12.0, food_surplus=3.0, districts=two), 60, 330, 3, 6)
    assert p.target == 6
    assert any("district cap" in r for r in p.reasons)


def test_loyalty_pressure_overrides_the_plateau():
    c = city(
        population=4,
        housing=12.0,
        food_surplus=3.0,
        loyalty=60.0,
        loyalty_per_turn=-2.0,
    )
    assert plan(c, turn=60, num_cities=3, target=6).target > 4


def test_slow_growth_is_accepted():
    p = plan(city(population=9, housing=14.0, food_surplus=0.5, turns_to_grow=60))
    assert p.target == 9
    assert any("trickle" in r for r in p.reasons)


def test_late_game_holds():
    p = plan(city(population=9, housing=14.0, food_surplus=4.0, turns_to_grow=5), turn=320)
    assert p.target == 9
    assert p.stance is Stance.HOLD


def test_late_deficit_covered_by_stored_food_is_tolerated():
    c = city(population=9, housing=14.0, food_surplus=-1.0, food_stored=40.0)
    assert plan(c, turn=320).stance is Stance.SHRINK_OK
    assert plan(c, turn=100).stance is Stance.FEED


def test_amenity_limit_is_optional_and_binds_when_given():
    c = city(population=6, housing=12.0, food_surplus=4.0)
    assert plan(c).amenity_gap == 0
    p = plan(c, amenity_limit=6)
    assert p.target == 6 and p.amenity_gap > 0


def test_plan_is_deterministic():
    c = city()
    assert plan(c) == plan(c)


def option(category, item, turns=5):
    return ProductionChoice(category, item, 100, turns)


def ctx_with(c, p=None):
    base = ProductionContext(turn=100, num_cities=6, builders=6, recon=1, army=9)
    return replace(base, growth={c.city_id: p} if p else {})


def test_aqueduct_scores_by_the_housing_gap_and_keeps_the_old_rule_without_a_plan():
    aq = option("DISTRICT", "DISTRICT_AQUEDUCT")
    capped = city(population=6, housing=6.0, food_surplus=4.0)
    roomy = city(population=3, housing=12.0, food_surplus=4.0)
    w = GrowthWeights()
    pw = ProductionWeights()
    s_capped = score(aq, capped, ctx_with(capped, plan(capped, weights=w)), pw)
    s_roomy = score(aq, roomy, ctx_with(roomy, plan(roomy, weights=w)), pw)
    assert s_capped > 3 * s_roomy
    assert score(aq, capped, ctx_with(capped), pw) > score(aq, roomy, ctx_with(roomy), pw)


def test_food_building_worth_more_when_food_limits_and_less_at_target():
    pw = ProductionWeights()
    harbor = option("BUILDING", "BUILDING_GRANARY")
    flat = city(population=5, housing=10.0, food_surplus=0.4, turns_to_grow=0)
    settled = city(population=9, housing=9.0, food_surplus=3.0, turns_to_grow=30)
    flat_plan, settled_plan = plan(flat), plan(settled)
    assert flat_plan.food_gap > 0
    assert score(harbor, flat, ctx_with(flat, flat_plan), pw) > score(
        harbor, settled, ctx_with(settled, settled_plan), pw
    )


def test_a_food_building_is_worthless_when_the_deficit_is_tolerated():
    c = city(population=9, housing=14.0, food_surplus=-1.0, food_stored=40.0)
    p = plan(c, turn=320)
    assert food_multiplier(p) == 0.0


def test_policy_logs_changes_once_and_every_n_turns(caplog):
    policy = GrowthPolicy()
    c = city()
    empire = fx_empire(turn=100)
    with caplog.at_level(logging.INFO, logger="civ6_ai.policies.growth"):
        policy.update([c], empire, 6)
        policy.update([c], replace(empire, turn=101), 6)
        assert len(caplog.records) == 1
        policy.update([replace(c, pop=c.pop + 1)], replace(empire, turn=102), 6)
        assert len(caplog.records) == 2
        policy.update([c], replace(empire, turn=130), 6)
        assert len(caplog.records) == 3
    assert policy.plan_for(c.city_id) is not None


def fx_empire(turn):
    return asyncio.run(read_empire(fx.FakeGame(overview=fx.overview(turn=turn))))


def test_hold_granary_keeps_a_positive_value_with_high_surplus():
    pw = ProductionWeights()
    granary = option("BUILDING", "BUILDING_GRANARY")
    c = city(population=9, housing=9.0, food_surplus=4.0, turns_to_grow=30)
    p = plan(c)
    assert p.stance is Stance.HOLD
    assert score(granary, c, ctx_with(c, p), pw) > 0


def test_tolerated_deficit_gets_no_starving_bonus():
    pw = ProductionWeights()
    granary = option("BUILDING", "BUILDING_GRANARY")
    c = city(population=9, housing=14.0, food_surplus=-1.0, food_stored=40.0, turns_to_grow=0)
    tolerated = plan(c, turn=320)
    assert tolerated.stance is Stance.SHRINK_OK
    fed = plan(c, turn=100)
    assert score(granary, c, ctx_with(c, tolerated), pw) < score(granary, c, ctx_with(c, fed), pw)


def test_the_policy_uses_the_speeds_last_turn_when_the_game_sets_no_limit():
    # Run 13: GAME_MAX_TURNS is unset, so max_turns was 0 and the late-game rules never applied.
    # A Quick game ends at turn 330.
    policy = GrowthPolicy()
    c = city(population=9, housing=14.0, food_surplus=4.0, turns_to_grow=5)
    empire = replace(fx_empire(turn=320), max_turns=0, game_speed="GAMESPEED_QUICK")
    policy.update([c], empire, 6)
    assert policy.plan_for(c.city_id).stance is Stance.HOLD


# --- Food capacity from the tile read (issue 215, Run 13) ---

from civ6_ai.city_tiles import CITY_TILES_LUA, CityTiles, parse_city_tiles  # noqa: E402
from civ6_ai.policies.growth import food_capacity  # noqa: E402


def tiles_of(pop, worked=(), land=(), water=()):
    return CityTiles(1, pop, tuple(worked), tuple(land), tuple(water))


def test_parse_city_tiles_needs_its_own_end_marker():
    text = "CT|7|9|2,2,1|3,1|\nCT|8|4||2|1\n"
    try:
        parse_city_tiles(text)
        raise AssertionError("partial read accepted")
    except ValueError:
        pass
    got = parse_city_tiles(text + "CTDONE\n")
    assert got[7].worked == (2, 2, 1) and got[7].free_land == (3, 1)
    assert got[8].free_water == (1,) and got[8].specialists == 4
    assert "CTDONE" in CITY_TILES_LUA


def test_capacity_with_no_free_tiles_counts_specialists_at_minus_two():
    # The Run 13 capital: all tiles worked, surplus +4: two specialists fit, then it starves.
    t = tiles_of(18, worked=[2] * 12)
    assert food_capacity(18, 4.0, t) == (20, 0)


def test_capacity_uses_free_tiles_first():
    t = tiles_of(5, worked=[2] * 5, land=[3, 2, 1])
    # +3 -> take the 3 (net +1) -> +4... 3: gain 1, left 4; 2: gain 0; 1: gain -1 left 3; then -2.
    assert food_capacity(5, 3.0, t)[0] == 5 + 3 + 1


def test_starving_city_sheds_specialists_then_weak_tiles():
    # The Run 13 Quirigua: pop 9, 1 specialist, 8 worked tiles, -2.6 food.
    t = tiles_of(9, worked=[3, 2, 2, 1, 1, 1, 1, 1])
    size, over = food_capacity(9, -2.6, t)
    assert (size, over) == (7, 2)


def test_over_capacity_without_a_cure_accepts_the_shrink():
    c = city(population=9, housing=10.0, food_surplus=-2.6, turns_to_grow=0)
    t = tiles_of(9, worked=[3, 2, 2, 1, 1, 1, 1, 1])
    p = plan(c, tiles=t)
    assert p.stance is Stance.SHRINK_OK
    assert p.over_capacity == 2 and p.food_limit == 7
    assert food_multiplier(p) == 0.0


def test_over_capacity_with_a_free_tile_is_still_fed():
    c = city(population=9, housing=10.0, food_surplus=-2.6, turns_to_grow=0)
    t = tiles_of(9, worked=[1, 1, 1, 1, 1, 1, 1, 1], land=[3])
    assert plan(c, tiles=t).stance is Stance.FEED


def test_target_respects_food_capacity():
    c = city(population=18, housing=25.0, food_surplus=4.0, turns_to_grow=5)
    old = plan(c, turn=250)
    new = plan(c, turn=250, tiles=tiles_of(18, worked=[2] * 12))
    assert old.target == 21 and new.target == 20 and new.food_limit == 20


def test_focus_gain_needs_a_better_free_tile():
    assert tiles_of(5, worked=[1, 2], land=[3]).focus_gain() == 2
    assert tiles_of(5, worked=[2, 2], land=[2, 1]).focus_gain() == 0


def test_no_read_keeps_the_old_estimate():
    c = city(population=5, housing=10.0, food_surplus=2.0)
    assert plan(c).food_limit == 7
