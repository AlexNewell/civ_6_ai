"""The threat state and what the military, production and gold policies do with it (1.35, 1.36)."""

import asyncio
import logging

from civ_mcp import lua as lq

from civ6_ai.policies.defence import (
    CityThreat,
    DefencePolicy,
    DefenceReport,
    DefenceState,
    assess,
    count_defenders,
)
from civ6_ai.policies.gold import GoldPolicy, reserve_for, threat_reserve
from civ6_ai.policies.military import MilitaryPolicy
from civ6_ai.policies.production import (
    CityProductionPolicy,
    ProductionContext,
    ProductionWeights,
    _should_switch,
    build_context,
    drop_dominated,
    is_obsolete_melee,
    is_pressed,
    rank_production,
)
from civ6_ai.state import ProductionChoice, Threat, read_cities, read_units
from civ6_ai.state.reader import build_city, build_unit
from fixtures import civ_state as fx
from test_policy_gold import GoldGame, option, upgradable
from test_policy_military import MilitaryGame, barbarian, warrior

ALPHA = 65536
BETA = 65537


def marked(*ids, inside=1, beside=0, reasons=("war:Rome",)):
    """A threat state marking the given city ids."""
    state = DefenceState()
    cities = {
        i: CityThreat(i, "Alpha" if i == ALPHA else "Beta", 10, 10, reasons, 0, inside, beside)
        for i in ids
    }
    state.reset(20, cities)
    return state


def foreign(owner_id=fx.ROME, x=12, y=10, hostile=False):
    return Threat(owner_id, "Rome", "UNIT_HORSEMAN", x, y, 100, 100, 36, 0, 2, hostile)


def read(game):
    cities = asyncio.run(read_cities(game))
    units = asyncio.run(read_units(game))
    return list(cities), list(units)


# ----- the threat state -----


def test_a_war_with_a_major_civ_marks_every_city():
    game = fx.FakeGame(civs=[fx.civ_info(is_at_war=True)], threats=[])
    cities, units = read(game)
    found = assess(cities, units, [], asyncio.run(game.get_diplomacy()))
    assert set(found) == {ALPHA, BETA}
    assert found[ALPHA].reasons == ("war:Rome",)


def test_an_unmet_civ_at_war_does_not_count():
    civ = fx.civ_info(is_at_war=True, has_met=False)
    cities, units = read(fx.FakeGame(civs=[civ], threats=[]))
    assert assess(cities, units, [], [civ]) == {}


def test_hostile_units_within_three_tiles_mark_the_city_but_far_ones_do_not():
    game = fx.FakeGame(threats=[], civs=[fx.civ_info()])
    cities, units = read(game)
    civs = [fx.civ_info()]
    near = Threat(fx.BARBARIANS, "Barbarians", "UNIT_WARRIOR", 13, 10, 100, 100, 8, 0, 3, True)
    far = Threat(fx.BARBARIANS, "Barbarians", "UNIT_WARRIOR", 14, 10, 100, 100, 8, 0, 4, True)
    assert set(assess(cities, units, [near], civs)) == {ALPHA}
    assert assess(cities, units, [far], civs) == {}


def test_a_denouncing_rivals_unit_within_three_tiles_marks_the_city():
    cities, units = read(fx.FakeGame(threats=[]))
    denounced = [fx.civ_info(diplomatic_state="DENOUNCED")]
    found = assess(cities, units, [foreign()], denounced)
    assert set(found) == {ALPHA}
    assert found[ALPHA].reasons == ("rival_units:1",)
    assert found[ALPHA].hostile_units == 1


def test_a_friendly_rivals_unit_does_not_mark_the_city():
    cities, units = read(fx.FakeGame(threats=[]))
    friendly = [fx.civ_info(diplomatic_state="FRIENDLY")]
    assert assess(cities, units, [foreign()], friendly) == {}


def test_a_denouncing_rivals_far_unit_does_not_mark_the_city():
    cities, units = read(fx.FakeGame(threats=[]))
    denounced = [fx.civ_info(diplomatic_state="DENOUNCED")]
    assert assess(cities, units, [foreign(x=20)], denounced) == {}


def test_defenders_are_counted_in_the_city_and_beside_it():
    units = [
        build_unit(fx.unit_info(unit_id=1, unit_index=1, x=10, y=10)),
        build_unit(fx.unit_info(unit_id=2, unit_index=2, x=11, y=10)),
        build_unit(fx.unit_info(unit_id=3, unit_index=3, x=14, y=10)),
        build_unit(fx.unit_info(unit_id=4, unit_index=4, unit_type="UNIT_BUILDER", x=9, y=10,
                                combat_strength=0)),
    ]  # fmt: skip
    city = build_city(fx.city_info(), (), set())
    assert count_defenders(city, units) == (1, 1)
    assert not CityThreat(ALPHA, "Alpha", 10, 10, (), 0, 1, 1).short
    assert CityThreat(ALPHA, "Alpha", 10, 10, (), 0, 1, 0).short


def test_the_policy_makes_no_city_or_unit_read_on_a_quiet_turn():
    game = fx.FakeGame(threats=[], civs=[fx.civ_info()])
    state = DefenceState()
    asyncio.run(DefencePolicy(state).run(game))
    assert not state.any
    assert "get_cities" not in game.calls and "get_units" not in game.calls


def test_the_policy_marks_cities_at_war_and_clears_them_when_it_ends():
    game = fx.FakeGame(civs=[fx.civ_info(is_at_war=True)], threats=[])
    state = DefenceState()
    asyncio.run(DefencePolicy(state).run(game))
    assert state.threatened_ids == {ALPHA, BETA}
    game.data["civs"] = [fx.civ_info()]
    asyncio.run(DefencePolicy(state).run(game))
    assert not state.any


def test_one_line_per_threatened_city_says_why_defenders_and_actions(caplog):
    state = marked(ALPHA, inside=1, beside=0)
    state.note(ALPHA, "gold: bought UNIT_ARCHER for 90")
    with caplog.at_level(logging.INFO, logger="civ6_ai.policies.defence"):
        asyncio.run(DefenceReport(state).run(None))
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("defence turn")]
    assert len(lines) == 1
    assert "Alpha (10,10)" in lines[0] and "threat=war:Rome" in lines[0]
    assert "defenders=1 (1 in city, 0 beside) SHORT" in lines[0]
    assert "gold: bought UNIT_ARCHER for 90" in lines[0]


def test_a_city_with_no_action_says_none(caplog):
    with caplog.at_level(logging.INFO, logger="civ6_ai.policies.defence"):
        asyncio.run(DefenceReport(marked(ALPHA, inside=1, beside=1)).run(None))
    assert "actions: none" in caplog.text and "SHORT" not in caplog.text


# ----- gold -----


def gold_run(game, state):
    asyncio.run(GoldPolicy(defence=state).run(game))


def test_the_reserve_drops_under_threat_and_keeps_a_deficit_cushion():
    assert threat_reserve(5.0) == 0
    assert threat_reserve(-10.0) == 20
    assert threat_reserve(-10.0) < reserve_for(2, -10.0)


def test_gold_at_the_normal_reserve_is_spent_when_a_city_is_threatened():
    reserve = reserve_for(2, 5.0)
    products = [option("UNIT_ARCHER", 40, "UNIT", turns=3)]
    quiet = GoldGame(gold_amount=reserve, production=products, units=[])
    gold_run(quiet, DefenceState())
    assert quiet.actions == []
    war = GoldGame(gold_amount=reserve, production=products, units=[])
    gold_run(war, marked(ALPHA))
    assert ("buy", ALPHA, "UNIT", "UNIT_ARCHER") in war.actions


def test_a_threatened_city_buys_only_defenders_and_walls():
    game = GoldGame(gold_amount=500, production=[option("BUILDING_LIBRARY", 40)], units=[])
    gold_run(game, marked(ALPHA))
    assert not [a for a in game.actions if a[0] == "buy" and a[1] == ALPHA]


def test_walls_are_bought_for_a_threatened_city():
    game = GoldGame(
        gold_amount=500,
        production=[option("BUILDING_LIBRARY", 40), option("BUILDING_WALLS", 60)],
        units=[],
    )
    gold_run(game, marked(ALPHA, inside=1, beside=1))
    assert ("buy", ALPHA, "BUILDING", "BUILDING_WALLS") in game.actions


def test_units_in_a_threatened_city_upgrade_before_units_nearer_a_hostile_unit():
    guarding = upgradable(1, x=10, y=11)
    nearer = upgradable(2, x=40, y=30)
    game = GoldGame(
        units=[nearer, guarding], threats=[fx.threat_info(x=41, y=30, distance=1)], production=[]
    )
    gold_run(game, marked(ALPHA))
    assert game.actions[0] == ("upgrade", 1)


def test_gold_spent_for_a_threatened_city_is_noted_for_the_report():
    state = marked(ALPHA)
    game = GoldGame(
        gold_amount=500,
        production=[option("UNIT_ARCHER", 40, "UNIT", turns=3)],
        units=[upgradable(1, x=10, y=11)],
    )
    gold_run(game, state)
    notes = " ".join(state.notes[ALPHA])
    assert "gold: bought UNIT_ARCHER for 40" in notes
    assert "gold: upgraded UNIT_WARRIOR to UNIT_SWORDSMAN" in notes


def test_the_gold_log_says_the_reserve_was_dropped(caplog):
    game = GoldGame(gold_amount=500, production=[], units=[])
    with caplog.at_level(logging.INFO, logger="civ6_ai.policies.gold"):
        gold_run(game, marked(ALPHA))
    assert "defence: 1 threatened cities, so the gold reserve is 0" in caplog.text


# ----- production -----


def prod_city(**changes):
    return build_city(fx.city_info(**changes), (), set())


def choice(category, item, turns=5):
    return ProductionChoice(category, item, 50, turns)


MENU = [
    choice("UNIT", "UNIT_WARRIOR", 3),
    choice("UNIT", "UNIT_SWORDSMAN", 5),
    choice("UNIT", "UNIT_CROSSBOWMAN", 5),
    choice("UNIT", "UNIT_BUILDER", 4),
    choice("UNIT", "UNIT_SETTLER", 8),
    choice("BUILDING", "BUILDING_WALLS", 8),
    choice("BUILDING", "BUILDING_LIBRARY", 8),
]


def mid(**changes):
    base = dict(turn=120, num_cities=3, builders=3, recon=1, army=5)
    return ProductionContext(**{**base, **changes})


def test_walls_come_first_in_a_marked_city_that_has_its_two_defenders():
    ctx = mid(threatened=frozenset({ALPHA}))
    ranked = rank_production(prod_city(), MENU, ctx, ProductionWeights())
    assert ranked[0].item == "BUILDING_WALLS"


def test_walls_are_not_wanted_in_an_unmarked_quiet_city():
    ranked = rank_production(prod_city(), MENU, mid(), ProductionWeights())
    assert ranked[0].item != "BUILDING_WALLS"


def test_a_marked_city_short_of_defenders_builds_one_before_a_settler():
    ctx = mid(threatened=frozenset({ALPHA}), short=frozenset({ALPHA}))
    city = prod_city()
    assert is_pressed(city, ctx)
    ranked = rank_production(city, MENU, ctx, ProductionWeights())
    assert ranked[0].category == "UNIT" and ranked[0].item not in ("UNIT_SETTLER", "UNIT_BUILDER")
    assert ranked.index(next(o for o in ranked if o.item == "BUILDING_WALLS")) < ranked.index(
        next(o for o in ranked if o.item == "UNIT_SETTLER")
    )


def test_a_ranged_unit_is_lifted_in_a_marked_city():
    menu = [choice("UNIT", "UNIT_SWORDSMAN"), choice("UNIT", "UNIT_CROSSBOWMAN")]
    ctx = mid(threatened=frozenset({ALPHA}), short=frozenset({ALPHA}))
    ranked = rank_production(prod_city(), menu, ctx, ProductionWeights())
    assert ranked[0].item == "UNIT_CROSSBOWMAN"


def test_build_context_reads_the_threat_state():
    empire = asyncio.run(_empire())
    state = marked(ALPHA, inside=1, beside=0)
    ctx = build_context(empire, [], (), (), state)
    assert ctx.threatened == {ALPHA} and ctx.short == {ALPHA}


async def _empire():
    from civ6_ai.state import read_empire

    return await read_empire(fx.FakeGame())


def test_warriors_are_obsolete_once_a_better_melee_unit_is_offered():
    assert is_obsolete_melee("UNIT_WARRIOR", ["UNIT_WARRIOR", "UNIT_SPEARMAN"])
    assert is_obsolete_melee("UNIT_WARRIOR", ["UNIT_MAN_AT_ARMS"])
    assert not is_obsolete_melee("UNIT_WARRIOR", ["UNIT_WARRIOR", "UNIT_ARCHER", "UNIT_SCOUT"])
    assert not is_obsolete_melee("UNIT_ARCHER", ["UNIT_MAN_AT_ARMS"])
    assert not is_obsolete_melee("UNIT_MAN_AT_ARMS", ["UNIT_SWORDSMAN"])


def test_an_obsolete_warrior_is_not_offered_for_ranking_in_any_city():
    menu = [choice("UNIT", "UNIT_WARRIOR"), choice("UNIT", "UNIT_SPEARMAN")]
    kept, _ = drop_dominated(menu, prod_city(), mid())
    assert [o.item for o in kept] == ["UNIT_SPEARMAN"]


def test_a_marked_city_building_something_else_is_switched_to_defence():
    city = prod_city(currently_building="BUILDING_LIBRARY", production_turns_left=8)
    assert not _should_switch(city)
    assert _should_switch(city, frozenset({ALPHA}))
    assert not _should_switch(city, frozenset({BETA}))


def test_the_production_policy_switches_a_marked_city_and_notes_it():
    state = marked(ALPHA, inside=1, beside=1)
    game = ProductionGame(
        overview=fx.overview(num_cities=1),
        cities=[fx.city_info(currently_building="BUILDING_LIBRARY", production_turns_left=8)],
        production=[
            lq.ProductionOption("BUILDING", "BUILDING_WALLS", 80, 8, 160),
            lq.ProductionOption("BUILDING", "BUILDING_LIBRARY", 90, 8, 180),
        ],
    )
    asyncio.run(CityProductionPolicy(defence=state).run(game))
    assert game.set == [(ALPHA, "BUILDING", "BUILDING_WALLS")]
    assert state.notes[ALPHA] == ["production: switched to BUILDING_WALLS"]


class ProductionGame(fx.FakeGame):
    def __init__(self, **data):
        data.setdefault("threats", [])
        super().__init__(**data)
        self.set: list[tuple] = []

    async def set_city_production(self, city_id, item_type, item_name, x=None, y=None):
        self.set.append((city_id, item_type, item_name))
        return "OK"


# ----- military -----


def mil_run(game, state):
    asyncio.run(MilitaryPolicy(defence=state).run(game))


def moves(game):
    return [(a[1], a[2], a[3]) for a in game.orders("move")]


def adjacent(x, y):
    return hex_distance(10, 10, x, y) == 1


def hex_distance(*args):
    from civ6_ai.state.reader import hex_distance as distance

    return distance(*args)


def test_a_second_defender_is_sent_to_a_tile_beside_a_threatened_city():
    game = MilitaryGame([warrior(uid=1), warrior(uid=2, x=13, y=10)], [])
    mil_run(game, marked(ALPHA))
    ordered = moves(game)
    assert [m[0] for m in ordered] == [2]
    assert adjacent(ordered[0][1], ordered[0][2])


def test_no_unit_is_sent_beside_a_city_with_two_defenders_already():
    game = MilitaryGame(
        [warrior(uid=1), warrior(uid=2, x=11, y=10), warrior(uid=3, x=13, y=10)], []
    )
    mil_run(game, marked(ALPHA))
    assert moves(game) == []


def test_without_a_threat_state_no_second_defender_is_sent():
    game = MilitaryGame([warrior(uid=1), warrior(uid=2, x=13, y=10)], [])
    asyncio.run(MilitaryPolicy().run(game))
    assert moves(game) == []


def test_the_second_defender_never_comes_from_a_city_that_needs_it():
    cities = [fx.city_info(), fx.city_info(city_id=BETA, name="Beta", x=13, y=10)]
    game = MilitaryGame([warrior(uid=1), warrior(uid=2, x=13, y=10)], [], cities=cities)
    mil_run(game, marked(ALPHA))
    assert moves(game) == []


def test_a_tile_the_game_calls_full_is_not_tried_again_that_turn():
    full = (
        "Error: STACKING_CONFLICT|Friendly UNIT_MILITARY_ENGINEER already on (11,10). Cannot stack"
    )
    game = MilitaryGame(
        [warrior(uid=1), warrior(uid=2, x=14, y=10), warrior(uid=3, x=14, y=11)], []
    )
    game.replies["move"] = full
    mil_run(game, marked(ALPHA))
    assert [m[1:] for m in moves(game)].count((11, 10)) == 1


def test_a_weaker_holder_steps_aside_and_a_stronger_unit_takes_the_city_tile():
    holder = warrior(uid=1)
    newcomer = warrior(uid=2, x=11, y=10, unit_type="UNIT_MAN_AT_ARMS", combat_strength=45)
    game = MilitaryGame([holder, newcomer], [])
    state = marked(ALPHA, inside=1, beside=1)
    mil_run(game, state)
    ordered = moves(game)
    assert [m[0] for m in ordered] == [1, 2]
    assert adjacent(ordered[0][1], ordered[0][2]) and ordered[0][1:] != (11, 10)
    assert ordered[1][1:] == (10, 10)
    assert any(n.startswith("swap:") for n in state.notes[ALPHA])
    # The holder is beside the city and the newcomer inside it: two defenders.
    assert (state.cities[ALPHA].defenders_in, state.cities[ALPHA].defenders_beside) == (1, 1)


def test_a_military_engineer_on_the_city_tile_is_swapped_out_for_a_defender():
    engineer = fx.unit_info(
        unit_id=5, unit_index=5, unit_type="UNIT_MILITARY_ENGINEER", combat_strength=0, x=10, y=10
    )
    game = MilitaryGame([engineer, warrior(uid=2, x=11, y=10)], [])
    mil_run(game, marked(ALPHA, inside=0, beside=1))
    ordered = moves(game)
    assert [m[0] for m in ordered] == [5, 2]
    assert ordered[1][1:] == (10, 10)


def test_an_equal_holder_is_not_swapped():
    game = MilitaryGame([warrior(uid=1), warrior(uid=2, x=11, y=10)], [])
    mil_run(game, marked(ALPHA, inside=1, beside=1))
    assert moves(game) == []


def test_a_garrison_is_not_sent_onto_a_tile_an_engineer_holds_and_is_not_refused():
    # Run 7: "Friendly UNIT_MILITARY_ENGINEER already on (64,28)", five garrison moves refused.
    engineer = fx.unit_info(
        unit_id=5, unit_index=5, unit_type="UNIT_MILITARY_ENGINEER", combat_strength=0, x=10, y=10
    )
    game = MilitaryGame([engineer, warrior(uid=2, x=12, y=10)], [barbarian(x=11, y=11)])
    asyncio.run(MilitaryPolicy().run(game))
    assert (10, 10) not in [m[1:] for m in moves(game)]


def test_a_covering_unit_digs_in_and_is_not_called_home():
    game = MilitaryGame([warrior(uid=1), warrior(uid=2, x=11, y=10)], [barbarian(x=13, y=11, cs=8)])
    policy = MilitaryPolicy(defence=marked(ALPHA, inside=1, beside=1))
    asyncio.run(policy.run(game))
    assert moves(game) == []
    assert any(d.reason == "covering a threatened city" for d in policy.decisions)


def test_the_counts_after_the_turn_are_handed_to_the_threat_state():
    state = marked(ALPHA, inside=1, beside=0)
    game = MilitaryGame([warrior(uid=1), warrior(uid=2, x=13, y=10)], [])
    mil_run(game, state)
    assert (state.cities[ALPHA].defenders_in, state.cities[ALPHA].defenders_beside) == (1, 1)
    assert not state.cities[ALPHA].short
    assert any(n.startswith("cover:") for n in state.notes[ALPHA])


def test_a_cover_unit_that_stops_short_is_not_counted_beside_but_no_other_is_sent():
    state = marked(ALPHA, inside=1, beside=0)
    game = MilitaryGame(
        [warrior(uid=1), warrior(uid=2, x=14, y=10), warrior(uid=3, x=14, y=11)], []
    )
    game.replies["move"] = "MOVING_TO|11,10|from:14,10|now_at:13,10|STOPPED_MID_PATH"
    mil_run(game, state)
    assert len(moves(game)) == 1
    assert state.cities[ALPHA].defenders_beside == 0
    assert state.cities[ALPHA].short
