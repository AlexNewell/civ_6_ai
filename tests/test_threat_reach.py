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
