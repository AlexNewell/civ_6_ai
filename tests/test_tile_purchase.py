"""Tile purchases valued by the owner's rules (issue 87)."""

import asyncio
import logging

from civ_mcp import lua as lq

from civ6_ai.policies import gold
from civ6_ai.policies import valuation as v
from civ6_ai.policies.gold import GoldPolicy, reserve_for
from civ6_ai.policies.production import ProductionWeights
from civ6_ai.state.reader import build_city
from fixtures import civ_state as fx
from test_policy_gold import GoldGame, option, upgradable

W = ProductionWeights()


def make_city(food_surplus=3.2, production=8.5, **changes):
    info = fx.city_info(food_surplus=food_surplus, production=production, **changes)
    return build_city(info, (), set())


def tile(terrain="Grass", cost=60, resource=None, cls=None, **flags):
    return v.TileFacts(5, 5, cost, terrain, resource, cls, **flags)


def test_a_food_tile_for_a_city_without_food_is_valued_and_a_fed_city_gets_nothing():
    starving = v.value_tile_purchase(tile("Grass"), make_city(food_surplus=0.5), W)
    fed = v.value_tile_purchase(tile("Grass"), make_city(food_surplus=4.0), W)
    assert starving is not None and starving.priority == 1
    assert "lacks food" in starving.reason
    assert fed is None  # A plain tile the city does not need is not worth gold.


def test_a_one_food_tile_does_not_fix_a_food_lack():
    assert v.value_tile_purchase(tile("Tundra"), make_city(food_surplus=0.0), W) is None


def test_an_improvable_resource_tile_is_valued_and_an_unimprovable_one_is_not():
    city = make_city(food_surplus=4.0)
    yes = v.value_tile_purchase(tile("Plains", resource="WINE", cls="luxury"), city, W)
    no = v.value_tile_purchase(
        tile("Plains", resource="WINE", cls="luxury", improvable=False), city, W
    )
    assert yes is not None and yes.priority == 2 and "can be improved" in yes.reason
    assert no is None
    strategic = v.value_tile_purchase(tile("Plains", resource="IRON", cls="strategic"), city, W)
    assert strategic is not None and strategic.valuation.rate > 0


def test_a_contested_tile_is_worth_less_than_a_needed_one():
    city = make_city(food_surplus=4.0)
    contested = v.value_tile_purchase(tile("Plains", contested=True), city, W)
    needed = v.value_tile_purchase(tile("Grass"), make_city(food_surplus=0.0), W)
    assert contested is not None and contested.priority == 3
    assert contested.valuation.rate < needed.valuation.rate
    assert v.value_tile_purchase(tile("Desert", contested=True), city, W) is None


def test_a_boost_tile_adds_a_fixed_value_and_the_best_priority():
    got = v.value_tile_purchase(tile("Coast", boost_value=40.0), make_city(food_surplus=4.0), W)
    assert got is not None and got.priority == 0 and got.valuation.fixed == 40.0


def test_a_tile_costing_too_much_has_a_roi_under_the_floor():
    worth = v.value_tile_purchase(tile("Grass"), make_city(food_surplus=0.0), W)
    cheap = v.price("c", 60, worth.valuation, 100, 330)
    dear = v.price("d", 3000, worth.valuation, 100, 330)
    assert cheap.roi >= v.ROI_FLOOR > dear.roi


def ptile(x, cost, terrain="Grass", resource=None, cls=None):
    return lq.PurchasableTile(x, 10, cost, terrain, resource, cls)


def run(game, **kwargs):
    asyncio.run(GoldPolicy(**kwargs).run(game))


def tiles_bought(game):
    return [a for a in game.actions if a[0] == "tile"]


def starving_game(gold_amount=500, **kw):
    return GoldGame(
        gold_amount=gold_amount,
        production=[],
        units=[],
        tiles={65536: [ptile(1, 60)]},
        cities=[fx.city_info(food_surplus=0.0), fx.city_info(city_id=65537, name="B", x=30, y=20)],
        **kw,
    )


def test_policy_buys_the_food_tile_when_on_and_logs_the_reason(caplog):
    caplog.set_level(logging.INFO, logger=gold.log.name)
    game = starving_game()
    run(game, tile_valuation=True)
    assert tiles_bought(game) == [("tile", 65536, 1, 10)]
    assert any("tile_buy" in r.message and "lacks food" in r.message for r in caplog.records)


def test_off_changes_nothing_and_matches_the_default():
    default, off = starving_game(), starving_game()
    run(default)
    run(off, tile_valuation=False)
    assert default.actions == off.actions
    # Off keeps the old order: a plain tile is bought only when it costs half the budget or less.
    assert tiles_bought(default) == [("tile", 65536, 1, 10)]
    fed = GoldGame(gold_amount=500, production=[], units=[], tiles={65536: [ptile(1, 60)]})
    run(fed, tile_valuation=True)
    assert tiles_bought(fed) == []  # On is stricter: no rule applies to a plain tile.


def test_reserve_still_wins():
    reserve = reserve_for(2, 5.0)
    game = starving_game(gold_amount=reserve + 30)  # 60 gold tile, 30 to spend.
    run(game, tile_valuation=True)
    assert game.actions == []
    at = starving_game(gold_amount=reserve)
    run(at, tile_valuation=True)
    assert at.actions == []


def test_a_saving_target_and_defence_still_dominate():
    game = GoldGame(
        gold_amount=300,
        gold_per_turn=60.0,
        units=[upgradable(1, 400)],  # The saving target: tiles wait for it.
        production=[],
        tiles={65536: [ptile(1, 250)]},
        cities=[fx.city_info(food_surplus=0.0), fx.city_info(city_id=65537, name="B", x=30, y=20)],
    )
    run(game, tile_valuation=True)
    assert tiles_bought(game) == []

    from civ6_ai.policies.defence import CityThreat, DefenceState

    defence = DefenceState()
    defence.cities = {65536: CityThreat.__new__(CityThreat)}
    threatened = GoldGame(
        gold_amount=300,
        gold_per_turn=60.0,
        units=[],
        production=[option("UNIT_ARCHER", 40, category="UNIT")],
        tiles={65536: [ptile(1, 280)]},
        cities=[fx.city_info(food_surplus=0.0)],
    )
    run(threatened, tile_valuation=True, defence=defence)
    kinds = [a[0] for a in threatened.actions]
    assert "buy" in kinds and "tile" not in kinds


def test_contested_and_boost_hooks_reach_the_policy():
    game = GoldGame(
        gold_amount=500,
        production=[],
        units=[],
        tiles={65536: [ptile(1, 60, "Plains"), ptile(2, 60, "Plains")]},
        cities=[fx.city_info(), fx.city_info(city_id=65537, name="B", x=30, y=20)],
    )
    policy = GoldPolicy(tile_valuation=True)
    policy.boost_values = {(2, 10): 60.0}
    asyncio.run(policy.run(game))
    assert tiles_bought(game) == [("tile", 65536, 2, 10)]
