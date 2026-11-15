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
