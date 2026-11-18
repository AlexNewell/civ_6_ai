"""Short of defenders by the threat map, and resuming a build a defence switch replaced (issues
264 and 267).

The fixtures are numbers from Runs 19 and 20 (STD_T1_03): the city's own strength, its garrison
and the hostile units the map held, as (unit type, x, y, strength[, hit points over most]). Our
units are (unit type, x, y, combat strength, ranged strength). The map here spreads by tile
distance. Where a log did not carry a city's own strength or garrison it is a plausible value,
noted on the case.
"""

import asyncio
import random

from civ_mcp import lua as lq

from civ6_ai.policies.defence import CityThreat, DefenceState
from civ6_ai.policies.production import CityProductionPolicy, _should_switch, is_pressed
from civ6_ai.state.models import Threat
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
CITY_HP = 200  # A city's hit points in the game.

# Each case: city tile, the city's strength, our units in or near it, hostile units near.
# Run 19 turn 53: a 9-hit-point Scout beside the capital and a Spearman three tiles off. Held by
# a Warrior with a Slinger beside it (the log shows the Slinger sent to (58,10) on turn 51).
T53 = (
    (59, 10), 24.0,
    [("UNIT_WARRIOR", 59, 10, 20, 0), ("UNIT_SLINGER", 58, 10, 5, 15)],
    [("UNIT_SPEARMAN", 57, 7, 25), ("UNIT_SCOUT", 59, 9, 10, 0.09), ("UNIT_WARRIOR", 55, 7, 20)],
)  # fmt: skip
NOT_SHORT = {
    "t53 Constantinople": T53,
    "t68 Trebizond": (
        (56, 13), 24.0,
        [("UNIT_SPEARMAN", 56, 13, 25, 0), ("UNIT_ARCHER", 55, 13, 15, 25)],
        [("UNIT_BARBARIAN_HORSEMAN", 58, 12, 20), ("UNIT_BARBARIAN_HORSEMAN", 60, 8, 20),
         ("UNIT_BARBARIAN_HORSE_ARCHER", 60, 9, 15), ("UNIT_BARBARIAN_HORSE_ARCHER", 60, 10, 15)],
    ),
    "t106 Adrianople": (
        (62, 12), 30.0,
        [("UNIT_SPEARMAN", 62, 12, 25, 0), ("UNIT_ARCHER", 61, 12, 15, 25)],
        [("UNIT_HEAVY_CHARIOT", 61, 9, 28)],
    ),
}  # fmt: skip
# Turns 14 to 17 in Run 19: three Warriors, two Slingers and two Scouts around a capital held by
# one Warrior with one beside it. A real raid.
RAID = (
    (59, 10), 24.0,
    [("UNIT_WARRIOR", 59, 10, 20, 0), ("UNIT_WARRIOR", 60, 10, 20, 0)],
    [("UNIT_WARRIOR", 55, 11, 20), ("UNIT_WARRIOR", 57, 11, 20), ("UNIT_WARRIOR", 58, 10, 20),
     ("UNIT_SLINGER", 57, 7, 15), ("UNIT_SLINGER", 57, 10, 15),
     ("UNIT_SCOUT", 57, 12, 10), ("UNIT_SCOUT", 60, 13, 10)],
)  # fmt: skip
# Run 20 turn 118, Thessalonica (55,11): held by a Horseman (strength 36, as the log's combat
# estimate shows), the city's strike at about 40, and Barbarian Man-at-Arms, a Trebuchet and a
# Crossbowman around it. Lost soon after, with the first reading calling it not short.
T118 = (
    (55, 11), 40.0,
    [("UNIT_HORSEMAN", 55, 11, 36, 0)],
    [("UNIT_SCOUT", 53, 9, 10), ("UNIT_MAN_AT_ARMS", 54, 9, 45), ("UNIT_MAN_AT_ARMS", 54, 11, 45),
     ("UNIT_TREBUCHET", 55, 9, 35), ("UNIT_MAN_AT_ARMS", 55, 10, 45),
     ("UNIT_CROSSBOWMAN", 58, 8, 40), ("UNIT_MAN_AT_ARMS", 58, 9, 45)],
)  # fmt: skip


def make_map(tile, city_strength, units, ours=(), wall_hp=0.0, city_hp=CITY_HP):
    m = ThreatMap(ThreatMapSettings(map_width=None))
    m.allies = [
        Ally(
            "city",
            *tile,
            city_strength,
            walls=wall_hp > 0,
            hp=city_hp,
            max_hp=CITY_HP,
            wall_hp=wall_hp,
            wall_max_hp=wall_hp,
        )
    ]
    for i, (kind, x, y, combat, ranged) in enumerate(ours):
        top = float(max(combat, ranged))
        m.allies.append(
            Ally("unit", x, y, top, kind, base=top, unit_id=i + 1, defence_base=float(combat))
        )
    for kind, x, y, strength, *hp in units:
        m.sources.append(
            Source(
                UNIT, x, y, float(strength), TURN, TURN, unit_type=kind, hp_frac=(hp or [1.0])[0]
            )
        )
    return m


def threat_for(m, tile, units, inside=1, beside=0):
    return CityThreat(
        ALPHA, "City", *tile, (f"hostile_units:{len(units)}",), len(units), inside, beside,
        reading=m.city_reading(*tile, TURN), margin=m.settings.short_margin,
        hostile_tiles=tuple((u[1], u[2]) for u in units),
    )  # fmt: skip


def case_map(case, wall_hp=0.0):
    tile, strength, ours, units = case
    return make_map(tile, strength, units, ours, wall_hp), tile, units


def test_the_run_19_cities_are_not_short():
    for name, case in NOT_SHORT.items():
        m, tile, units = case_map(case)
        found = threat_for(m, tile, units)
        assert not found.short, (name, m.city_reading(*tile, TURN))
        # The head count alone called each of these short, as it still does with no map.
        assert found.defenders < found.wanted, name


def test_a_real_raid_is_short():
    m, tile, units = case_map(RAID)
    assert threat_for(m, tile, units).short


def test_run_20_thessalonica_on_turn_118_is_short_with_or_without_walls():
    for walls in (0.0, 100.0):  # None, and Ancient Walls (100 outer hit points).
        m, tile, units = case_map(T118, walls)
        reading = m.city_reading(*tile, TURN)
        assert reading.defender == 36  # The Horseman, not the sum of all our cover.
        assert threat_for(m, tile, units).short, (walls, reading)


def test_the_fixture_numbers_are_printed():
    for name, case in {**dict(NOT_SHORT), "raid": RAID, "t118": T118}.items():
        for walls in (0.0, 100.0):
            m, tile, _ = case_map(case, walls)
            print(name, walls, m.city_reading(*tile, TURN))


def test_walls_take_the_blows_and_their_hit_points_do_not_heal():
    bare = case_map(T118)[0].city_reading(55, 11, TURN)
    walled = case_map(T118, 100.0)[0].city_reading(55, 11, TURN)
    assert walled.net == walled.wall_damage < bare.net  # Walls take a share, melee least.
    assert walled.turns == 100.0 / walled.wall_damage


def test_sustained_wall_damage_over_a_few_turns_is_a_threat_though_the_city_out_heals_it():
    # Two Trebuchets hit walls in full: 60 wall hit points go in a couple of turns.
    units = [("UNIT_TREBUCHET", 5, 7, 35), ("UNIT_TREBUCHET", 6, 7, 35)]
    m = make_map((5, 5), 60.0, units, [("UNIT_SPEARMAN", 5, 5, 25, 0)], wall_hp=60.0)
    reading = m.city_reading(5, 5, TURN)
    assert reading.damage < m.heal.city_garrison + 30  # Not a rout of the city itself.
    assert reading.pressed()


def test_our_units_cut_the_attack_instead_of_adding_to_the_defender():
    units = [("UNIT_MAN_AT_ARMS", 5, 6, 45), ("UNIT_MAN_AT_ARMS", 6, 6, 45)]
    alone = make_map((5, 5), 30.0, units, [("UNIT_WARRIOR", 5, 5, 20, 0)])
    helped = make_map(
        (5, 5), 30.0, units,
        [("UNIT_WARRIOR", 5, 5, 20, 0), ("UNIT_CROSSBOWMAN", 4, 5, 30, 40)],
    )  # fmt: skip
    a, b = alone.city_reading(5, 5, TURN), helped.city_reading(5, 5, TURN)
    assert a.defender == b.defender == 20
    assert b.damage < a.damage


def test_adding_attackers_or_raising_their_strength_never_lowers_the_damage():
    rng = random.Random(267)
    kinds = ["UNIT_WARRIOR", "UNIT_MAN_AT_ARMS", "UNIT_TREBUCHET", "UNIT_CROSSBOWMAN", "UNIT_SCOUT"]
    spots = [(x, y) for x in range(3, 8) for y in range(3, 8) if (x, y) != (5, 5)]
    for _ in range(300):
        ours = [
            (
                rng.choice(["UNIT_WARRIOR", "UNIT_ARCHER", "UNIT_SPEARMAN"]),
                *rng.choice(spots),
                20,
                25,
            )
            for _ in range(rng.randint(0, 3))
        ] + [("UNIT_WARRIOR", 5, 5, 20, 0)]
        walls = rng.choice([0.0, 50.0])
        units = [
            (rng.choice(kinds), *rng.choice(spots), float(rng.randint(5, 60)), rng.uniform(0.1, 1))
            for _ in range(rng.randint(0, 5))
        ]

        def read(group, ours=ours, walls=walls):
            return make_map((5, 5), 25.0, group, ours, walls).city_reading(5, 5, TURN)

        base = read(units)
        extra = (rng.choice(kinds), *rng.choice(spots), float(rng.randint(5, 60)))
        more = read([*units, extra])
        assert more.damage >= base.damage - 1e-9
        assert more.wall_damage >= base.wall_damage - 1e-9
        if units:
            i = rng.randrange(len(units))
            kind, x, y, strength, hp = units[i]
            raised = [*units[:i], (kind, x, y, strength + rng.randint(1, 30), hp), *units[i + 1 :]]
            stronger = read(raised)
            assert stronger.damage >= base.damage - 1e-9
            assert stronger.wall_damage >= base.wall_damage - 1e-9


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
    assert not quiet.pressed()  # It out-damages the heal, but would take many turns.
    raid = make_map((5, 5), 30.0, stack).city_reading(5, 5, TURN)
    assert raid.pressed() and raid.damage > quiet.damage


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
    assert walled.cover_at(5, 5) == 20  # Walls are hit points, not strength.
    assert walled.cover_at(6, 5) > 0 and walled.cover_at(7, 5) > 0
    assert walled.cover_at(8, 5) == 0


def test_a_units_cover_still_spreads_beside_it():
    m = ThreatMap(ThreatMapSettings(map_width=None))
    unit = fx.unit_info(x=5, y=5, unit_type="UNIT_WARRIOR", combat_strength=20)
    m.see_own(TURN, [build_unit(unit)], [])
    assert m.cover_at(5, 5) > 0 and m.cover_at(6, 5) > 0


def test_a_killed_unit_stops_counting_before_production_runs():
    m, tile, units = case_map(T53)
    state = DefenceState(map=m)
    state.reset(TURN, {ALPHA: threat_for(m, tile, units)})
    before = state.cities[ALPHA].reading.damage
    m.forget_units_at([(57, 7)])
    state.forget_killed([(57, 7)])
    after = state.cities[ALPHA]
    assert after.hostile_units == len(units) - 1
    assert after.reading.damage < before


def test_a_covered_city_keeps_its_build_and_is_not_pressed():
    m, tile, units = case_map(T53)
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


def test_the_heal_says_where_its_values_came_from():
    class Reads:
        async def execute_lua(self, code, context="gamecore"):
            return "HEAL|COMBAT_HEAL_CITY_GARRISON|20\nHEALDONE"

    class NoLua:
        async def execute_lua(self, code, context="gamecore"):
            raise RuntimeError("no lua")

    assert asyncio.run(read_combat_heal(Reads())).source == "game"
    failed = asyncio.run(read_combat_heal(NoLua())).source
    assert failed.startswith("fallback (") and "no lua" in failed


def test_the_map_judges_by_the_heal_it_was_given():
    m, tile, units = case_map(RAID)
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


def test_a_resume_drops_the_cancelled_defence_unit_from_the_armys_count():
    policy = CityProductionPolicy(defence=DefenceState())  # The threat is gone.
    policy._replaced[ALPHA] = "BUILDING_LIBRARY"
    game = ProductionGame(
        overview=fx.overview(num_cities=1),
        cities=[fx.city_info(currently_building="UNIT_WARRIOR", production_turns_left=5)],
        production=OPTIONS,
    )
    seen = []
    original = policy._context

    def spy(empire, units, in_progress, cities):
        in_progress = list(in_progress)
        seen.append(in_progress)
        return original(empire, units, in_progress, cities)

    policy._context = spy
    asyncio.run(policy.run(game))
    assert game.set == [(ALPHA, "BUILDING", "BUILDING_LIBRARY")]
    assert seen[-1] == ["BUILDING_LIBRARY"]


def test_below_the_army_floor_a_military_unit_outranks_a_building_with_nothing_short():
    from civ6_ai.policies.production import ProductionWeights, rank_production
    from test_policy_defence import MENU

    city = prod_city()  # Garrisoned, quiet.
    floor = ProductionWeights().army_per_city
    assert floor == 1.5
    low = rank_production(city, MENU, mid(num_cities=4, army=3), ProductionWeights())
    assert low[0].category == "UNIT" and low[0].item != "UNIT_BUILDER"
    met = rank_production(city, MENU, mid(num_cities=4, army=6), ProductionWeights())
    assert met[0].item not in ("UNIT_WARRIOR", "UNIT_SWORDSMAN", "UNIT_CROSSBOWMAN")


def test_a_blow_must_reach_the_attacker_and_a_ranged_attacker_defends_with_its_combat_strength():
    # Our Archer stands west of the city, the enemy Archer east of it: too far apart to trade.
    mine = [("UNIT_WARRIOR", 5, 5, 20, 0), ("UNIT_ARCHER", 3, 5, 15, 25)]
    far = make_map((5, 5), 30.0, [("UNIT_ARCHER", 8, 5, 25, 1.0)], mine)
    near = make_map((5, 5), 30.0, [("UNIT_ARCHER", 4, 5, 25, 1.0)], mine)
    assert near.city_reading(5, 5, TURN).damage < far.city_reading(5, 5, TURN).damage
    # The hostile Archer defends at its combat strength (15), not its ranged one (25).
    assert far.sources[0].defence == 0  # Built by hand: unknown, so its attack strength stands in.
    m = ThreatMap(ThreatMapSettings(map_width=None))
    archer = Threat(63, "Barbarians", "UNIT_ARCHER", 6, 5, 100, 100, 15, 25, 1, True)
    m.see_threats(TURN, [archer])
    assert m.sources[0].defence == 15 and m.sources[0].strength == 25
