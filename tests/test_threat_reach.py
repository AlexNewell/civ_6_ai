"""Threat reads by reach (epic #399, slice 4): the threat map's hostile reach is the movement
service's `threat_reach`, and the defence and production reads follow it, not a hex radius."""

import asyncio

from civ6_ai.movement import Movement
from civ6_ai.policies.defence import DefenceState, assess
from civ6_ai.policies.production import ProductionContext, build_context, hostiles_near
from civ6_ai.state import Threat
from civ6_ai.state.reader import build_city
from civ6_ai.threat_map import ThreatMap, ThreatMapSettings
from fixtures import civ_state as fx
from test_movement import store
from test_policy_defence import _empire

RIDGE = {(12, y): "m" for y in range(0, 17)}


def barb(x, y, unit_type="UNIT_WARRIOR"):
    return Threat(fx.BARBARIANS, "Barbarians", unit_type, x, y, 100, 100, 8, 0, 2, True)


def city(x=10, y=10):
    return build_city(fx.city_info(x=x, y=y), (), set(), None)


def reach_map(terrain=None):
    return ThreatMap(ThreatMapSettings(map_width=None), terrain=terrain)


def near(threat, terrain, at=(10, 10)):
    found = assess([city(*at)], [], [threat], [], threat_map=reach_map(terrain), map_width=None)
    return found.get(city(*at).city_id)


def test_open_ground_reach_is_the_services_threat_reach():
    m = reach_map(store())
    walk = Movement(store())
    assert m.hostile_reach("UNIT_WARRIOR", 13, 10) == walk.threat_reach((13, 10), 2.0, 1)
    assert m.hostile_reach("UNIT_ARCHER", 14, 10) == walk.threat_reach((14, 10), 2.0, 2)
    assert m.hostile_reach("UNIT_HORSEMAN", 15, 10) == walk.threat_reach((15, 10), 4.0, 1)


def test_open_ground_counts_what_the_hex_radius_did():
    close = near(barb(13, 10), store())  # Three tiles: a warrior reaches the city next turn.
    far = near(barb(15, 10), store())  # Five tiles: it does not.
    assert close is not None and close.hostile_units == 1
    assert far is None


def test_a_ridge_keeps_a_near_unit_from_the_city():
    ridge = store(RIDGE)
    assert near(barb(13, 10), ridge) is None  # Three tiles by hex, but the long way round.
    assert near(barb(13, 10), store()) is not None


def test_a_ranged_unit_and_a_horse_reach_further_than_a_walker():
    assert near(barb(14, 10, "UNIT_ARCHER"), store()) is not None
    assert near(barb(14, 10), store()) is None
    assert near(barb(14, 10, "UNIT_HORSEMAN"), store()) is not None


def test_with_no_map_the_hex_radius_stands():
    found = assess([city()], [], [barb(13, 10)], [], map_width=None)
    assert found[city().city_id].hostile_units == 1


def test_production_counts_the_reach_aware_hostiles_with_a_map():
    state = DefenceState()
    state.map = reach_map(store(RIDGE))
    c = build_city(fx.city_info(), (barb(13, 10),), set(), None)  # The scan's radius sees it.
    assert len(c.threats) == 1
    state.reset(20, assess([c], [], [barb(13, 10)], [], threat_map=state.map, map_width=None))
    ctx = build_context(asyncio.run(_empire()), [], cities=[c], defence=state)
    assert ctx.reach_aware and hostiles_near(c, ctx) == 0
    plain = build_context(asyncio.run(_empire()), [], cities=[c], defence=None)
    assert not plain.reach_aware and hostiles_near(c, plain) == 1


def test_a_context_defaults_to_the_scan():
    assert not ProductionContext(turn=1, num_cities=1).reach_aware


def test_the_maps_own_field_follows_the_same_walk():
    from civ6_ai.threat_map import UNIT, Source

    for rows in ({}, RIDGE):
        m = reach_map(store(rows))
        src = Source(UNIT, 13, 10, 50.0, 1, 1, unit_type="UNIT_WARRIOR")
        one_turn = {
            t for t in m._field(src) if m.terrain.reach(13, 10, 2.0, False, 1.0, 1).get(t, 9) <= 1
        }
        assert one_turn == m.hostile_reach("UNIT_WARRIOR", 13, 10)


# ----- live regressions (STD_T1_04, 60 turns) -----
# T18: a new city with a barbarian four tiles off by hex but out of reach. T47: a camp plan
# wanting a unit. The reach-aware count must lower pressure only where the map says the city
# holds, and must not touch the camp demand.


def _scores(ctx, city, items=("UNIT_WARRIOR", "UNIT_SLINGER", "UNIT_BUILDER")):
    from civ6_ai.policies.production import ProductionWeights, _unit_score
    from civ6_ai.state import ProductionChoice

    w = ProductionWeights()
    return {
        i: round(_unit_score(ProductionChoice("UNIT", i, 50, 5), city, ctx, w), 2) for i in items
    }


def _scan_city():
    return build_city(fx.city_info(garrison_unit=""), (barb(12, 12),), set(), None)


def test_t18_a_hostile_the_map_cannot_reach_does_not_press_the_city():
    c = _scan_city()
    assert len(c.threats) == 1
    held = ProductionContext(turn=18, num_cities=2, builders=1, recon=1, army=1, reach_aware=True)
    scan = ProductionContext(turn=18, num_cities=2, builders=1, recon=1, army=1)
    quiet = build_city(fx.city_info(garrison_unit=""), (), set(), None)
    assert _scores(held, c) == _scores(held, quiet) == _scores(scan, quiet)
    assert _scores(scan, c) != _scores(
        held, c
    )  # The scan alone pressed it: no Builder, higher units.


def test_t18_a_hostile_the_map_can_reach_still_presses_the_city():
    c = _scan_city()
    reached = ProductionContext(
        turn=18, num_cities=2, builders=1, recon=1, army=1, reach_aware=True, near={c.city_id: 1}
    )
    scan = ProductionContext(turn=18, num_cities=2, builders=1, recon=1, army=1)
    assert _scores(reached, c) == _scores(scan, c)


def test_t47_camp_demand_does_not_depend_on_the_reach_reading():
    def camp(**kw):
        return ProductionContext(
            turn=47, num_cities=3, builders=2, recon=1, army=3, camp_units=1, camp_wants="melee",
            camp_target=(14, 10), **kw,
        )  # fmt: skip

    c = build_city(fx.city_info(garrison_unit="UNIT_WARRIOR"), (barb(12, 12),), set(), None)
    items = ("UNIT_SPEARMAN", "UNIT_ARCHER", "UNIT_BUILDER")
    quiet = build_city(fx.city_info(garrison_unit="UNIT_WARRIOR"), (), set(), None)
    held = _scores(camp(reach_aware=True), c, items)
    assert held == _scores(camp(), quiet, items)  # Held by the map: the plan's demand alone.
    assert held["UNIT_SPEARMAN"] > 10.0 > 0.0 and held["UNIT_BUILDER"] == 8.0


# ----- review fixes -----


def test_a_ship_reaches_the_land_beside_the_water_it_can_stand_on():
    sea = store({(x, 10): "w" for x in range(11, 20)})
    m = reach_map(sea)
    assert (10, 10) in m.hostile_reach("UNIT_GALLEY", 12, 10)  # The coast tile next to the water.
    assert near(barb(12, 10, "UNIT_GALLEY"), sea) is not None
    assert (9, 10) not in m.hostile_reach("UNIT_GALLEY", 12, 10)  # Nothing further inland.


def test_multi_turn_warning_resets_movement_each_turn():
    from civ6_ai.movement import attack_tiles

    # Corridor row 10 of flat, hill, flat, hill, flat; mountains above and below.
    rows = {(x, y): "m" for x in range(10, 20) for y in (9, 11)}
    rows |= {(11, 10): "h", (13, 10): "h", (10, 10): "m", (16, 10): "m"}
    t = store(rows)
    one = attack_tiles(t, (10 + 1, 10), 2.0, 0, False, None, 1)
    two = attack_tiles(t, (10 + 1, 10), 2.0, 0, False, None, 2)
    assert (15, 10) in two and (15, 10) not in one
    # Two turns of 2 moves: hill(2) ends turn 1, flat, hill(2)... not one turn of 4 points.
    assert (13, 10) in two
