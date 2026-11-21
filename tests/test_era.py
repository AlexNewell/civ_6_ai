import logging

from civ6_ai.era import (
    EraSettings,
    district_nudge,
    log_outlook,
    outlook,
    outlook_from_standing,
    turns_left_in_era,
)
from civ6_ai.policies.production import ProductionContext, ProductionWeights, score
from civ6_ai.state import ProductionChoice
from civ6_ai.state.reader import build_city
from fixtures import civ_state as fx


def test_turns_left_scales_with_speed():
    s = EraSettings()
    assert turns_left_in_era(41, "GAMESPEED_STANDARD", s) == 9
    assert turns_left_in_era(0, "GAMESPEED_STANDARD", s) == 50
    # Quick: 0.67 * 50 = 34 turns an era.
    assert turns_left_in_era(30, "GAMESPEED_QUICK", s) == 4


def test_push_inside_window_and_reach():
    result = outlook(44, score=1, dark=10, golden=30, game_speed="GAMESPEED_STANDARD")
    assert (result.target, result.gap, result.turns_left, result.push) == ("dark", 9, 6, True)


def test_no_push_before_window_or_out_of_reach():
    assert not outlook(30, 1, 10, 30, "GAMESPEED_STANDARD").push  # 20 turns left
    assert not outlook(45, 1, 20, 40, "GAMESPEED_STANDARD").push  # 19 points short of Dark


def test_golden_target_when_above_dark():
    near = outlook(45, 19, 10, 22, "GAMESPEED_STANDARD")
    assert (near.target, near.gap, near.push) == ("golden", 3, True)
    far = outlook(45, 12, 10, 40, "GAMESPEED_STANDARD")
    assert far.target == "golden" and not far.push


def test_golden_reached_means_no_target():
    result = outlook(45, 36, 10, 22, "GAMESPEED_STANDARD")
    assert (result.target, result.gap, result.push) == ("", 0, False)


def test_window_zero_and_last_turn_turn_it_off():
    assert not outlook(45, 1, 10, 30, "", EraSettings(window=0)).push
    assert not outlook(45, 1, 10, 30, "", EraSettings(last_turn=40)).push


def test_from_standing_needs_the_fields():
    assert outlook_from_standing(5, {"score": 3}) is None
    got = outlook_from_standing(45, {"era_score": 1, "era_dark": 10, "era_golden": 30})
    assert got is not None and got.push


def test_log_line(caplog):
    with caplog.at_level(logging.INFO, logger="civ6_ai.era"):
        log_outlook(outlook(45, 1, 10, 30))
        log_outlook(None)
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1 and lines[0].startswith("era_outlook ") and '"push": true' in lines[0]


def test_district_nudge_only_first_of_kind():
    assert district_nudge("CAMPUS", frozenset(), True, 3.0) == 3.0
    assert district_nudge("CAMPUS", frozenset({"CAMPUS"}), True, 3.0) == 0.0
    assert district_nudge("CAMPUS", frozenset(), False, 3.0) == 0.0
    assert district_nudge("CAMPUS", frozenset(), True, 0.0) == 0.0


def _score_campus(push, built=frozenset()):
    option = ProductionChoice("DISTRICT", "DISTRICT_CAMPUS", 54, 5)
    ctx = ProductionContext(turn=45, num_cities=2, districts=built, era_push=push)
    return score(option, build_city(fx.city_info(), (), set()), ctx, ProductionWeights())


def test_push_lifts_first_district_only():
    base = _score_campus(False)
    assert _score_campus(True) > base
    assert _score_campus(True, frozenset({"CAMPUS"})) == base
