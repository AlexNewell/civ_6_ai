"""Short of defenders by the threat map, and resuming a build a defence switch replaced (issue 264).

The fixtures are numbers from Run 19 (STD_T1_03): each case is the city's own-tile cover and the
hostile units the map held, as (unit type, x, y, strength). The map here spreads by tile distance.
"""

import asyncio

from civ_mcp import lua as lq

from civ6_ai.policies.defence import CityThreat, DefenceState
from civ6_ai.policies.production import CityProductionPolicy, _should_switch, is_pressed
from civ6_ai.state.reader import build_city, build_unit
from civ6_ai.threat_map import (
    DEFAULT_COMBAT_HEAL,
    UNIT,
    Ally,
    CombatHeal,
    Source,
    ThreatMap,
    ThreatMapSettings,
    parse_combat_heal,
    read_combat_heal,
)
from fixtures import civ_state as fx
from test_policy_defence import ALPHA, ProductionGame, mid, prod_city

TURN = 50

# City tile, cover on it, hostile units near (type, x, y, strength).
NOT_SHORT = {
    "t53 Constantinople": (
        (59, 10), 85.5,
        [("UNIT_SPEARMAN", 57, 7, 25), ("UNIT_SCOUT", 59, 9, 10), ("UNIT_WARRIOR", 55, 7, 20)],
    ),
    "t68 Trebizond": (
        (56, 13), 60.6,
        [("UNIT_BARBARIAN_HORSEMAN", 58, 12, 20), ("UNIT_BARBARIAN_HORSEMAN", 60, 8, 20),
         ("UNIT_BARBARIAN_HORSE_ARCHER", 60, 9, 15), ("UNIT_BARBARIAN_HORSE_ARCHER", 60, 10, 15)],
    ),
    "t89 Amaseia": (
        (55, 9), 168.0,
        [("UNIT_BARBARIAN_HORSE_ARCHER", 57, 12, 15), ("UNIT_HORSEMAN", 58, 8, 36),
         ("UNIT_HORSEMAN", 60, 8, 36), ("UNIT_BARBARIAN_HORSE_ARCHER", 60, 10, 15)],
    ),
    "t106 Adrianople": ((62, 12), 90.0, [("UNIT_HEAVY_CHARIOT", 61, 9, 28)]),
}  # fmt: skip
# Turn 17 in Run 19: three Warriors, two Slingers and two Scouts around a capital held by one
# Warrior with one beside it. A real raid.
RAID = (
    (59, 10), 36.6,
    [("UNIT_WARRIOR", 55, 11, 20), ("UNIT_WARRIOR", 57, 11, 20), ("UNIT_WARRIOR", 58, 10, 20),
     ("UNIT_SLINGER", 57, 7, 15), ("UNIT_SLINGER", 57, 10, 15),
     ("UNIT_SCOUT", 57, 12, 10), ("UNIT_SCOUT", 60, 13, 10)],
)  # fmt: skip


def make_map(tile, cover, units, walls=False):
    m = ThreatMap(ThreatMapSettings(map_width=None))
    m.allies = [Ally("city", *tile, cover, walls=walls)]
    for kind, x, y, strength in units:
        m.sources.append(Source(UNIT, x, y, float(strength), TURN, TURN, unit_type=kind))
    return m


def threat_for(m, tile, units, inside=1, beside=0):
    return CityThreat(
        ALPHA, "City", *tile, (f"hostile_units:{len(units)}",), len(units), inside, beside,
        reading=m.city_reading(*tile, TURN), margin=m.settings.short_margin,
        hostile_tiles=tuple((x, y) for _, x, y, _ in units),
    )  # fmt: skip


def test_the_four_run_19_cities_are_not_short():
    for name, (tile, cover, units) in NOT_SHORT.items():
        found = threat_for(make_map(tile, cover, units), tile, units)
        assert not found.short, name
        # The head count alone called each of these short, as it still does with no map.
        assert found.defenders < found.wanted, name


def test_a_real_raid_is_short():
    tile, cover, units = RAID
    assert threat_for(make_map(tile, cover, units), tile, units).short


def test_without_a_map_the_head_count_decides():
    found = CityThreat(ALPHA, "City", 5, 5, ("hostile_units:1",), 1, 1, 0)
    assert found.reading is None and found.short


def test_a_war_with_no_unit_near_keeps_the_head_count_even_with_a_map():
    m = make_map((5, 5), 50.0, [])
    found = CityThreat(ALPHA, "City", 5, 5, ("war:Rome",), 0, 1, 0, reading=m.city_reading(5, 5, 1))
    assert found.short  # One defender, two wanted.


def test_two_weak_units_cannot_out_damage_the_citys_healing_but_a_stack_can():
    weak = [("UNIT_SCOUT", 5, 6, 10), ("UNIT_SCOUT", 6, 6, 10)]
    stack = [("UNIT_WARRIOR", x, 6, 20) for x in (4, 5, 6)] + [("UNIT_WARRIOR", 5, 7, 20)]
    quiet = make_map((5, 5), 30.0, weak).city_reading(5, 5, TURN)
    assert quiet.net == 0
    raid = make_map((5, 5), 30.0, stack).city_reading(5, 5, TURN)
    assert raid.net > 0 and raid.damage > quiet.damage


def test_a_stronger_defence_lowers_the_damage_taken():
    units = [("UNIT_WARRIOR", 5, 6, 20)] * 3
    soft = make_map((5, 5), 20.0, units).city_reading(5, 5, TURN)
    hard = make_map((5, 5), 60.0, units).city_reading(5, 5, TURN)
    assert hard.damage < soft.damage


def test_an_unwalled_city_covers_only_its_own_tile_and_a_walled_one_two_tiles_out():
    def covered(walls):
        m = ThreatMap(ThreatMapSettings(map_width=None))
        info = fx.city_info(
            x=5,
            y=5,
            defense_strength=20,
            wall_hp=50 if walls else 0,
            wall_max_hp=50 if walls else 0,
        )
        m.see_own(TURN, [], [build_city(info, (), set(), 74)])
        return m

    bare, walled = covered(False), covered(True)
    assert bare.cover_at(5, 5) == 20
    assert bare.cover_at(6, 5) == 0 and bare.cover_at(7, 5) == 0
    assert walled.cover_at(5, 5) > 20  # Walls add to the city's own strength.
    assert walled.cover_at(6, 5) > 0 and walled.cover_at(7, 5) > 0
    assert walled.cover_at(8, 5) == 0


def test_a_units_cover_still_spreads_beside_it():
    m = ThreatMap(ThreatMapSettings(map_width=None))
    unit = fx.unit_info(x=5, y=5, unit_type="UNIT_WARRIOR", combat_strength=20)
    m.see_own(TURN, [build_unit(unit)], [])
    assert m.cover_at(5, 5) > 0 and m.cover_at(6, 5) > 0


def test_a_killed_unit_stops_counting_before_production_runs():
    tile, cover, units = NOT_SHORT["t53 Constantinople"]
    m = make_map(tile, cover, units)
    state = DefenceState(map=m)
    state.reset(TURN, {ALPHA: threat_for(m, tile, units)})
    before = state.cities[ALPHA].reading.damage
    m.forget_units_at([(59, 9)])
    state.forget_killed([(59, 9)])
    after = state.cities[ALPHA]
    assert after.hostile_units == len(units) - 1
    assert after.reading.damage < before


def test_a_covered_city_keeps_its_build_and_is_not_pressed():
    tile, cover, units = NOT_SHORT["t53 Constantinople"]
    m = make_map(tile, cover, units)
    state = DefenceState(map=m)
    state.reset(TURN, {ALPHA: threat_for(m, tile, units)})
    assert state.short_ids == frozenset() and state.mapped_ids == {ALPHA}
    city = prod_city(currently_building="BUILDING_LIBRARY", production_turns_left=8)
    assert not _should_switch(city, state.threatened_ids, state.short_ids)
    ctx = mid(threatened=state.threatened_ids, short=state.short_ids, mapped=state.mapped_ids)
    assert not is_pressed(prod_city(), ctx)


def _state(short: bool) -> DefenceState:
    state = DefenceState()
    held = CityThreat(ALPHA, "Alpha", 10, 10, ("war:Rome",), 0, 0 if short else 2, 0)
    state.reset(TURN, {ALPHA: held})
    return state


OPTIONS = [
    lq.ProductionOption("BUILDING", "BUILDING_WALLS", 80, 8, 160),
    lq.ProductionOption("BUILDING", "BUILDING_LIBRARY", 90, 8, 180),
    lq.ProductionOption("UNIT", "UNIT_BUILDER", 50, 3, 80),
]


def test_an_interrupted_build_is_put_first_again_once_the_city_is_covered():
    policy = CityProductionPolicy(defence=_state(short=True))
    game = ProductionGame(
        overview=fx.overview(num_cities=1),
        cities=[fx.city_info(currently_building="BUILDING_LIBRARY", production_turns_left=8)],
        production=OPTIONS,
    )
    asyncio.run(policy.run(game))
    assert game.set and game.set[0][2] != "BUILDING_LIBRARY"
    assert policy._replaced == {ALPHA: "BUILDING_LIBRARY"}
    # Later the city is idle and covered: the Library goes first, whatever it would score.
    later = _state(short=False)
    policy.defence = later
    game = ProductionGame(
        overview=fx.overview(num_cities=1),
        cities=[fx.city_info(currently_building=None)],
        production=OPTIONS,
    )
    asyncio.run(policy.run(game))
    assert game.set == [(ALPHA, "BUILDING", "BUILDING_LIBRARY")]
    assert policy._replaced == {}
    assert later.notes[ALPHA] == ["production: resumed BUILDING_LIBRARY"]


def test_a_build_is_not_resumed_while_the_city_is_still_short():
    policy = CityProductionPolicy(defence=_state(short=True))
    policy._replaced[ALPHA] = "BUILDING_LIBRARY"
    idle = prod_city(currently_building=None)
    assert not policy._can_resume(idle, frozenset({ALPHA}), frozenset({ALPHA}))
    assert policy._can_resume(idle, frozenset({ALPHA}), frozenset())


# ----- the game's healing rules -----


def test_the_combat_heal_read_is_parsed():
    reply = (
        "HEAL|COMBAT_HEAL_CITY_GARRISON|25\nHEAL|COMBAT_HEAL_CITY_OUTER_DEFENSES|10\n"
        "HEAL|COMBAT_HEAL_OUTER_DEFENSES_COOLDOWN|2\nHEALDONE\n"
    )
    heal = parse_combat_heal(reply)
    assert (heal.city_garrison, heal.outer_defences, heal.outer_defence_cooldown) == (25, 10, 2)


def test_a_missing_parameter_keeps_its_default_but_a_missing_garrison_value_is_refused():
    heal = parse_combat_heal("HEAL|COMBAT_HEAL_CITY_GARRISON|20\nHEALDONE\n")
    assert heal == DEFAULT_COMBAT_HEAL
    for bad in ("HEAL|COMBAT_HEAL_CITY_GARRISON|20\n", "HEALDONE\n", "HEAL|X|1\nHEALDONE"):
        try:
            parse_combat_heal(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_a_failed_read_falls_back_to_the_games_table_values():
    class NoLua:
        async def execute_lua(self, code, context="gamecore"):
            raise RuntimeError("no lua")

    class Partial:
        async def execute_lua(self, code, context="gamecore"):
            return "HEAL|COMBAT_HEAL_CITY_GARRISON|20"  # No end marker.

    assert DEFAULT_COMBAT_HEAL.city_garrison == 20  # COMBAT_HEAL_CITY_GARRISON
    assert asyncio.run(read_combat_heal(NoLua())) == DEFAULT_COMBAT_HEAL
    assert asyncio.run(read_combat_heal(Partial())) == DEFAULT_COMBAT_HEAL


def test_the_map_judges_by_the_heal_it_was_given():
    tile, cover, units = RAID
    m = make_map(tile, cover, units)
    assert m.city_reading(*tile, TURN).net > 0
    m.heal = CombatHeal(city_garrison=500.0)
    assert m.city_reading(*tile, TURN).net == 0


def test_only_a_rivals_units_near_keeps_the_head_count():
    """Rival units at peace are not sources on the map, so the map cannot judge them."""
    m = make_map((5, 5), 50.0, [])
    found = CityThreat(
        ALPHA, "City", 5, 5, ("rival_units:1",), 1, 1, 0, reading=m.city_reading(5, 5, TURN)
    )
    assert found.short  # One defender, two wanted.


def test_killing_the_last_threat_removes_the_city_unless_something_else_marks_it():
    tile, cover, units = ((5, 5), 50.0, [("UNIT_SCOUT", 5, 6, 10)])
    m = make_map(tile, cover, units)
    state = DefenceState(map=m)
    state.reset(TURN, {ALPHA: threat_for(m, tile, units)})
    state.forget_killed([(5, 6)])
    assert not state.any
    at_war = CityThreat(
        ALPHA, "City", 5, 5, ("war:Rome", "hostile_units:1"), 1, 1, 0,
        reading=m.city_reading(5, 5, TURN), hostile_tiles=((5, 6),),
    )  # fmt: skip
    state.reset(TURN, {ALPHA: at_war})
    state.forget_killed([(5, 6)])
    assert state.cities[ALPHA].reasons == ("war:Rome",)


def test_a_unit_that_moved_into_the_city_adds_its_cover_after_the_map_is_read_again():
    tile = (5, 5)
    m = ThreatMap(ThreatMapSettings(map_width=None))
    ours = build_unit(fx.unit_info(x=8, y=8, unit_type="UNIT_WARRIOR", combat_strength=40))
    m.see_own(TURN, [ours], [])
    m.allies.append(Ally("city", *tile, 10.0))
    m.sources.append(Source(UNIT, 5, 6, 40.0, TURN, TURN, unit_type="UNIT_WARRIOR"))
    state = DefenceState(map=m)
    state.reset(TURN, {ALPHA: threat_for(m, tile, [("UNIT_WARRIOR", 5, 6, 40)])})
    before = state.cities[ALPHA].reading
    m.relocate({ours.unit_id: tile})
    state.refresh()
    assert state.cities[ALPHA].reading.cover > before.cover
    assert state.cities[ALPHA].reading.damage < before.damage


def test_a_resume_that_fails_is_tried_again_and_one_no_longer_offered_is_dropped():
    policy = CityProductionPolicy(defence=_state(short=False))
    policy._replaced[ALPHA] = "BUILDING_LIBRARY"
    game = ProductionGame(
        overview=fx.overview(num_cities=1),
        cities=[fx.city_info(currently_building=None)],
        production=[lq.ProductionOption("BUILDING", "BUILDING_WALLS", 80, 8, 160)],
    )
    asyncio.run(policy.run(game))
    assert policy._replaced == {}  # Not on offer any more.


def test_a_unit_that_took_damage_gives_less_cover_after_the_map_is_updated():
    m = ThreatMap(ThreatMapSettings(map_width=None))
    ours = build_unit(fx.unit_info(x=5, y=5, unit_type="UNIT_WARRIOR", combat_strength=40))
    m.see_own(TURN, [ours], [])
    whole = m.cover_at(5, 5)
    m.relocate({}, {ours.unit_id: 0.5})
    assert m.cover_at(5, 5) == whole / 2


def test_only_a_kill_the_game_confirmed_leaves_the_defence_state():
    from civ6_ai.policies.military import MilitaryPolicy, _Turn

    t = _Turn(TURN, (), (), (), [], frozenset())
    policy = MilitaryPolicy()
    policy._record_hit(t, (1, 1), "Dealt about 40 damage (estimate)", 40, 30)  # Estimate only.
    policy._record_hit(t, (2, 2), "Target killed", 40, 30)
    assert t.killed == {(1, 1), (2, 2)}
    assert t.confirmed == {(2, 2)}
