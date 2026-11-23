"""A camp unit is worth what the camp plan's readiness makes it (issue 416, slice 2).

The numbers are the plan lines of two live 90-turn runs: STD_T1_01 (camp units built on T40 to T59
with the plan at need 19, have 3) and STD_T1_04 (T26 at need 10, have 6).
"""

import asyncio
import logging
from dataclasses import replace

import pytest

from civ6_ai.policies.defence import DefenceState
from civ6_ai.policies.gold import GoldPolicy
from civ6_ai.policies.military import CampPlan, MilitaryWeights
from civ6_ai.policies.production import (
    ProductionContext,
    ProductionWeights,
    camp_readiness,
    camp_scale,
    score,
)
from fixtures import civ_state as fx
from test_policy_gold import GoldGame, option
from test_policy_production import best, city
from test_policy_production import option as menu_option

W = ProductionWeights()
OFF = ProductionWeights(camp_readiness_power=0.0)


def ctx(needed, available, building=0, **over):
    base = dict(turn=60, num_cities=3, builders=3, recon=1, army=6, camp_units=2,
                camp_wants="any", camp_needed=needed, camp_available=available,
                camp_building=building)  # fmt: skip
    return ProductionContext(**{**base, **over})


def test_a_unit_attack_is_the_plans_scale():
    assert W.camp_unit_attacks == MilitaryWeights().camp_strike_turns


@pytest.mark.parametrize(
    ("label", "needed", "available", "building", "ready"),
    [
        ("STD_T1_01 T40", 19, 3.0, 0, 0.32),  # Four builds over T40-T58; available never rose.
        ("STD_T1_01 T52", 10, 3.0, 0, 0.60),
        ("STD_T1_01 T58", 13, 3.0, 1, 0.69),
        ("STD_T1_01 T59", 10, 6.0, 1, 1.00),  # The build that joined the force that cleared T83.
        ("STD_T1_04 T26", 10, 6.0, 0, 0.90),
        ("STD_T1_04 T31", 7, 6.0, 0, 1.00),
    ],
)
def test_readiness_on_the_logged_plans(label, needed, available, building, ready):
    assert camp_readiness(ctx(needed, available, building), W) == pytest.approx(ready, abs=0.01)
    garrisoned = city()
    assert camp_scale(garrisoned, ctx(needed, available, building), W) == pytest.approx(
        ready**2, abs=0.02
    )


def test_a_slinger_for_a_plan_that_is_far_from_acting_scores_a_tenth():
    garrisoned = city()
    slinger = menu_option("UNIT", "UNIT_SLINGER", 4)
    far = ctx(19, 3.0)
    flat = score(slinger, garrisoned, replace(far), OFF)
    scaled = score(slinger, garrisoned, far, W)
    assert scaled == pytest.approx(flat * 0.1, rel=0.05)


def test_the_pick_changes_where_the_logged_pick_was_a_camp_unit_that_could_not_act():
    garrisoned = city(buildings=["BUILDING_MONUMENT"])
    menu = [menu_option("UNIT", "UNIT_SLINGER", 4), menu_option("BUILDING", "BUILDING_LIBRARY", 4)]
    far = ctx(19, 3.0)
    assert best(garrisoned, menu, far, OFF) == "UNIT_SLINGER"  # Today.
    assert best(garrisoned, menu, far, W) == "BUILDING_LIBRARY"
    ready = ctx(
        10, 6.0, 1, num_cities=4
    )  # STD_T1_01 T59: the plan can act, so the unit still wins.
    assert best(garrisoned, menu, ready, W) == "UNIT_SLINGER"


def test_an_unknown_need_keeps_todays_score():
    garrisoned = city()
    slinger = menu_option("UNIT", "UNIT_SLINGER", 4)
    unknown = ctx(0.0, 0.0)
    assert camp_readiness(unknown, W) is None
    assert score(slinger, garrisoned, unknown, W) == score(slinger, garrisoned, unknown, OFF)


def test_a_camp_that_blocks_expansion_keeps_the_full_worth():
    garrisoned = city()
    blocked = ctx(19, 3.0, held_settlers=1)
    assert blocked.camp_blocks
    assert camp_scale(garrisoned, blocked, W) == 1.0


def test_a_city_with_no_garrison_or_under_threat_is_unchanged():
    far = ctx(19, 3.0)
    empty = city(garrison_unit="")
    slinger = menu_option("UNIT", "UNIT_SLINGER", 4)
    assert camp_scale(empty, far, W) == 1.0
    assert score(slinger, empty, far, W) == score(slinger, empty, far, OFF)
    pressed = replace(far, threatened=frozenset({65536}), short=frozenset({65536}))
    assert camp_scale(city(), pressed, W) == 1.0
    assert score(slinger, city(), pressed, W) == score(slinger, city(), pressed, OFF)


def test_power_zero_restores_the_old_worth():
    garrisoned = city()
    slinger = menu_option("UNIT", "UNIT_SLINGER", 4)
    far = ctx(19, 3.0)
    assert camp_scale(garrisoned, far, OFF) == 1.0
    assert score(slinger, garrisoned, far, OFF) > 5 * score(slinger, garrisoned, far, W)


def test_a_threatened_defender_build_does_not_fall():
    # The threatened city's unit score comes from the threat term, not the camp's.
    garrisoned = city(threats=[])
    far = ctx(19, 3.0, threatened=frozenset({65536}), short=frozenset({65536}))
    spearman = menu_option("UNIT", "UNIT_SPEARMAN", 4)
    assert score(spearman, garrisoned, far, W) == score(spearman, garrisoned, far, OFF)


def gold_buys(needed, available, caplog):
    state = DefenceState()
    state.camp_plan = CampPlan(
        (13, 10), 3, 1, float(needed), float(available), 2, 2, "any", 10, 0.1, defender=45.0
    )
    game = GoldGame(
        gold_amount=500,
        cities=[fx.city_info(currently_building="BUILDING_MONUMENT", garrison_unit="UNIT_WARRIOR")],
        production=[option("UNIT_ARCHER", 130, "UNIT", cost=60)],
        units=[fx.unit_info(unit_type="UNIT_WARRIOR"), fx.unit_info(unit_id=2, unit_index=2)],
    )
    caplog.clear()
    with caplog.at_level(logging.INFO):
        asyncio.run(GoldPolicy(defence=state).run(game))
    return any(
        "camp force" in r.getMessage() and "Bought" in r.getMessage() for r in caplog.records
    )


def test_gold_buys_for_a_plan_close_to_acting_and_not_for_one_far_from_it(caplog):
    assert gold_buys(10, 6.0, caplog)  # STD_T1_04 T26: readiness 0.9.
    assert not gold_buys(19, 3.0, caplog)  # STD_T1_01 T40: readiness 0.32.
