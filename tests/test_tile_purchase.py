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
    yes = v.value_tile_purchase(
        tile("Plains", resource="WINE", cls="luxury", improvable=True), city, W
    )
    no = v.value_tile_purchase(
        tile("Plains", resource="WINE", cls="luxury", improvable=False), city, W
    )
    assert yes is not None and yes.priority == 2 and "can be improved" in yes.reason
    assert no is None
    strategic = v.value_tile_purchase(
        tile("Plains", resource="IRON", cls="strategic", improvable=True), city, W
    )
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


def builder_unit():
    return fx.unit_info(unit_id=7, unit_index=7, unit_type="UNIT_BUILDER", name="Builder")


def resource_game(resource="WINE", units=None):
    # The research fixture leaves Mining open and Pottery and Animal Husbandry researched.
    return GoldGame(
        gold_amount=500,
        production=[],
        units=[builder_unit()] if units is None else units,
        tiles={65536: [ptile(1, 60, "Plains", resource, "luxury")]},
        cities=[fx.city_info(), fx.city_info(city_id=65537, name="B", x=30, y=20)],
    )


def test_plantation_resource_needs_irrigation_not_pottery():
    # `open_techs` lists what is still to research.
    irrigation_open = frozenset({"IRRIGATION"})  # Pottery done, Irrigation not.
    pottery_open = frozenset({"POTTERY"})  # Irrigation done, Pottery not.
    for resource in ("WINE", "TEA", "SUGAR", "COTTON", "DYES", "SILK", "SPICES", "INCENSE"):
        assert not v.resource_improvable(resource, irrigation_open, True)
        assert v.resource_improvable(resource, pottery_open, True)
    assert v.resource_improvable("WHEAT", irrigation_open, True)
    assert not v.resource_improvable("WHEAT", pottery_open, True)
    for unsure in ("DEER", "FURS", "TRUFFLES", "IVORY", "NITER", "COAL"):
        assert not v.resource_improvable(unsure, frozenset(), True)


def test_resource_tile_with_the_tech_researched_is_bought_for_its_improvement():
    game = resource_game("HORSES")  # Pasture needs Animal Husbandry, which is done.
    run(game, tile_valuation=True)
    assert tiles_bought(game) == [("tile", 65536, 1, 10)]


def test_resource_tile_whose_tech_is_missing_is_not_bought():
    game = resource_game("IRON")  # Iron needs Mining, still to research in the fixture.
    run(game, tile_valuation=True)
    assert tiles_bought(game) == []


def test_resource_tile_without_a_builder_or_an_unknown_resource_is_not_bought():
    no_builder = resource_game("WINE", units=[])
    run(no_builder, tile_valuation=True)
    assert tiles_bought(no_builder) == []
    unknown = resource_game("MYSTERY")
    run(unknown, tile_valuation=True)
    assert tiles_bought(unknown) == []


def test_resource_improvable_is_conservative_when_the_research_read_is_missing():
    assert not v.resource_improvable("WINE", None, True)
    assert v.resource_improvable("RESOURCE_WINE", frozenset({"MINING"}), True)
    assert not v.resource_improvable("IRON", frozenset({"MINING"}), True)


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


# Issue 338: the contested-tile source, the improvable refinement and the report.
from civ6_ai.placement import SavingTarget  # noqa: E402
from civ6_ai.policies.settling import SettlingPolicy  # noqa: E402
from civ6_ai.tile_report import format_rows, report_game, tile_rows  # noqa: E402


def test_is_contested_needs_a_foreign_city_near_and_something_worth_taking():
    foreign = [(10, 10)]
    near = v.CONTESTED_RADIUS
    assert v.is_contested(10 + near, 10, "TERRAIN_GRASS", None, foreign)  # Exactly at the radius.
    assert not v.is_contested(10 + near + 1, 10, "TERRAIN_GRASS", None, foreign)
    assert not v.is_contested(11, 10, "TERRAIN_DESERT", None, foreign)  # Nothing to take.
    assert not v.is_contested(11, 10, "TERRAIN_TUNDRA", None, foreign)  # One yield is too little.
    assert v.is_contested(11, 10, "TERRAIN_DESERT", "IRON", foreign)  # A resource is enough.
    assert not v.is_contested(11, 10, "TERRAIN_GRASS", "IRON", [])  # No foreign city known.


def test_is_contested_wraps_around_the_map_edge():
    assert v.is_contested(1, 10, "TERRAIN_GRASS", None, [(v.STANDARD_MAP_WIDTH - 2, 10)])
    assert not v.is_contested(1, 10, "TERRAIN_GRASS", None, [(v.STANDARD_MAP_WIDTH - 2, 10)], 2)
    assert v.is_contested(40, 10, "TERRAIN_GRASS", None, [(10, 10)], 30, None)


def test_tile_improvable_needs_a_free_tile():
    done = frozenset({"MINING"})  # Pottery and Animal Husbandry are researched.
    assert v.tile_improvable("HORSES", done, True)
    assert not v.tile_improvable("HORSES", done, True, occupied=True)
    assert not v.tile_improvable("HORSES", done, True, reserved=True)
    assert not v.tile_improvable("HORSES", done, False)
    assert not v.tile_improvable(None, done, True)


def test_the_settling_survey_lists_every_known_foreign_city_but_no_phantoms():
    settling = SettlingPolicy()
    settling._rival_memory = {(30, 12): (1, 4), (5, 5): (60, 3)}
    settling._minor_owners = {60}  # City-states exert no loyalty pressure but do take land.
    settling._phantoms = [(9, 9, 2)]
    assert settling.foreign_city_tiles() == [(5, 5), (30, 12)]
    assert (5, 5) not in [c[:2] for c in settling._pressure_sources()]


def contest_game(area=None, tiles=None):
    game = GoldGame(
        gold_amount=500,
        production=[],
        units=[builder_unit()],
        tiles={65536: tiles or [ptile(1, 60, "Plains")]},
        cities=[fx.city_info(), fx.city_info(city_id=65537, name="B", x=30, y=20)],
    )
    game.data["tiles"] = area or {}
    return game


def test_a_tile_near_a_foreign_city_is_bought_as_contested_land():
    game = contest_game()
    policy = GoldPolicy(tile_valuation=True)
    policy.foreign_cities = lambda: [(5, 10)]  # Four tiles from (1, 10).
    asyncio.run(policy.run(game))
    assert tiles_bought(game) == [("tile", 65536, 1, 10)]
    far = contest_game()
    policy = GoldPolicy(tile_valuation=True)
    policy.foreign_cities = lambda: [(20, 10)]
    asyncio.run(policy.run(far))
    assert tiles_bought(far) == []
    none = contest_game()
    asyncio.run(GoldPolicy(tile_valuation=True).run(none))
    assert tiles_bought(none) == []


def test_foreign_cities_change_nothing_while_the_valuation_is_off():
    plain, with_foreign = contest_game(), contest_game()
    run(plain)
    policy = GoldPolicy()
    policy.foreign_cities = lambda: [(5, 10)]
    asyncio.run(policy.run(with_foreign))
    assert plain.actions == with_foreign.actions


def test_a_resource_tile_already_improved_or_planned_for_a_district_is_not_bought():
    horses = [ptile(1, 60, "Plains", "HORSES", "strategic")]
    free = contest_game(tiles=horses)
    run(free, tile_valuation=True)
    assert tiles_bought(free) == [("tile", 65536, 1, 10)]
    built = contest_game(tiles=horses, area={(10, 10): [fx.tile(x=1, y=10, district=None)]})
    run(built, tile_valuation=True)
    assert tiles_bought(built) == []  # The improvement (a mine in the fixture) stands there.
    district = contest_game(
        tiles=horses, area={(10, 10): [fx.tile(x=1, y=10, improvement=None, district="D")]}
    )
    run(district, tile_valuation=True)
    assert tiles_bought(district) == []
    bare = contest_game(
        tiles=horses, area={(10, 10): [fx.tile(x=1, y=10, improvement=None, district=None)]}
    )
    run(bare, tile_valuation=True)
    assert tiles_bought(bare) == [("tile", 65536, 1, 10)]
    planned = contest_game(tiles=horses)
    policy = GoldPolicy(tile_valuation=True)
    policy.plans.add(SavingTarget(65536, "Alpha", "DISTRICT_CAMPUS", 1, 10, 9999, 42))
    asyncio.run(policy.run(planned))
    assert tiles_bought(planned) == []


def test_the_bot_wires_the_valuation_only_when_asked():
    import tempfile
    from pathlib import Path

    from civ6_ai import bot
    from civ6_ai.monitors import EventBus
    from civ6_ai.orchestrator import TurnLog

    def gold_of(**kw):
        with tempfile.TemporaryDirectory() as tmp:
            orchestrator = bot.build_orchestrator(
                GoldGame(), EventBus(), TurnLog(Path(tmp) / "g.jsonl"), **kw
            )
            return next(h for h in orchestrator._handlers if isinstance(h, GoldPolicy))

    off, on = gold_of(), gold_of(tile_valuation=True)
    assert not off.tile_valuation and on.tile_valuation
    assert off.foreign_cities is not None  # The source is wired; it is read only when on.


def test_report_rows_show_price_yields_value_and_reason():
    city = make_city(food_surplus=0.0)
    tiles = [
        ptile(1, 60, "Grass"),
        ptile(2, 40, "Desert"),
        ptile(3, 80, "Plains", "HORSES", "strategic"),
        ptile(4, 0, "Grass"),  # Not for sale.
    ]
    rows = tile_rows(city, tiles, W, 100, 330, frozenset(), True)
    assert [r.x for r in rows] == [1, 3, 2]  # Lacking food, then the resource, then no rule.
    grass, horses, desert = rows
    assert grass.priority == 1 and grass.yields == (2, 0, 0) and grass.cost == 60
    assert horses.priority == 2 and "HORSES" in horses.reason and horses.roi > 0
    assert desert.value is None and desert.reason == "no rule applies"
    without = tile_rows(city, tiles[2:3], W, 100, 330, frozenset({"ANIMAL_HUSBANDRY"}), True)
    assert without[0].value is None and "cannot be improved" in without[0].reason
    near = tile_rows(city, [ptile(1, 60, "Plains")], W, 100, 330, foreign_cities=[(3, 10)])
    assert near[0].priority == 3 and "contested" in near[0].reason
    text = format_rows(city, rows)
    assert "Alpha" in text and "60g" in text and "lacks food" in text and "HORSES" in text


def test_report_game_reads_the_open_game_without_buying():
    game = contest_game(tiles=[ptile(1, 60, "Grass"), ptile(2, 70, "Plains", "WINE", "luxury")])
    game.data["cities"] = [fx.city_info(food_surplus=0.0), game.data["cities"][1]]
    text = asyncio.run(report_game(game, [(5, 10)]))
    assert "Alpha" in text and "WINE" in text and "contested" in text
    assert game.actions == []


def test_the_configured_map_width_reaches_the_contested_distance():
    # Width 56: (1, 10) and (54, 10) are 3 tiles apart, but 53 on the standard 74-wide map.
    assert v.is_contested(1, 10, "TERRAIN_GRASS", None, [(54, 10)], map_width=56)
    assert not v.is_contested(1, 10, "TERRAIN_GRASS", None, [(54, 10)])
    spot = ptile(1, 60, "Plains")
    assert v.tile_facts(spot, None, False, foreign_cities=[(54, 10)], map_width=56).contested
    assert not v.tile_facts(spot, None, False, foreign_cities=[(54, 10)]).contested
    rows = tile_rows(make_city(), [spot], W, 100, 330, foreign_cities=[(54, 10)], map_width=56)
    assert rows[0].priority == 3
    game = contest_game()
    policy = GoldPolicy(tile_valuation=True)
    policy.foreign_cities = lambda: [(54, 10)]
    policy.map_width = 56
    asyncio.run(policy.run(game))
    assert tiles_bought(game) == [("tile", 65536, 1, 10)]
    wide = contest_game()
    policy = GoldPolicy(tile_valuation=True)
    policy.foreign_cities = lambda: [(54, 10)]
    asyncio.run(policy.run(wide))
    assert tiles_bought(wide) == []


def test_the_bot_gives_gold_the_settling_map_width():
    import tempfile
    from pathlib import Path

    from civ6_ai import bot
    from civ6_ai.monitors import EventBus
    from civ6_ai.orchestrator import TurnLog

    with tempfile.TemporaryDirectory() as tmp:
        orchestrator = bot.build_orchestrator(
            GoldGame(), EventBus(), TurnLog(Path(tmp) / "g.jsonl")
        )
    handlers = orchestrator._handlers
    gold_policy = next(h for h in handlers if isinstance(h, GoldPolicy))
    settling = next(h for h in handlers if isinstance(h, SettlingPolicy))
    assert gold_policy.map_width == settling.settings.map_width
