import asyncio
import logging
from types import SimpleNamespace

from civ6_ai.era import (
    EraClock,
    EraHolder,
    EraSettings,
    district_nudge,
    eligible_districts,
    log_outlook,
    outlook,
    outlook_from_standing,
    turns_left_in_era,
)
from civ6_ai.policies.production import (
    CityProductionPolicy,
    ProductionContext,
    ProductionWeights,
    _with_era_districts,
    score,
)
from civ6_ai.state import ProductionChoice
from civ6_ai.state.reader import build_city
from fixtures import civ_state as fx


def test_turns_left_scales_with_speed():
    s = EraSettings()
    # Standard: eras begin on turns 1, 41, 81 (observed), 40 turns each.
    assert turns_left_in_era(1, "GAMESPEED_STANDARD", s) == 40
    assert turns_left_in_era(40, "GAMESPEED_STANDARD", s) == 1
    assert turns_left_in_era(41, "GAMESPEED_STANDARD", s) == 40
    assert turns_left_in_era(72, "GAMESPEED_STANDARD", s) == 9
    # Quick: 0.67 * 40 = 27 turns an era.
    assert turns_left_in_era(20, "GAMESPEED_QUICK", s) == 8


def test_observed_start_overrides_the_grid():
    s = EraSettings()
    assert turns_left_in_era(50, "GAMESPEED_STANDARD", s, era_start=45) == 35


def test_clock_follows_era_changes():
    clock = EraClock()
    assert clock.observe(1, "ERA_ANCIENT") == 1
    assert clock.observe(40, "ERA_ANCIENT") == 1
    assert clock.observe(44, "ERA_CLASSICAL") == 44  # A late era change self-corrects.
    assert clock.observe(60, "ERA_CLASSICAL") == 44
    assert clock.observe(60, None) == 44


def test_clock_loaded_mid_era_has_no_start_until_a_change():
    clock = EraClock()
    assert clock.observe(30, "ERA_ANCIENT") is None
    assert clock.observe(41, "ERA_CLASSICAL") == 41


def test_push_inside_window_and_reach():
    result = outlook(34, score=1, dark=10, golden=30, game_speed="GAMESPEED_STANDARD")
    assert (result.target, result.gap, result.turns_left, result.push) == ("dark", 9, 7, True)


def test_no_push_before_window_or_out_of_reach():
    assert not outlook(15, 1, 10, 30, "GAMESPEED_STANDARD").push  # 26 turns left
    assert not outlook(35, 1, 20, 40, "GAMESPEED_STANDARD").push  # 19 points short of Dark


def test_golden_target_when_above_dark():
    near = outlook(35, 19, 10, 22, "GAMESPEED_STANDARD")
    assert (near.target, near.gap, near.push) == ("golden", 3, True)
    far = outlook(35, 12, 10, 40, "GAMESPEED_STANDARD")
    assert far.target == "golden" and not far.push


def test_golden_reached_means_no_target():
    result = outlook(35, 36, 10, 22, "GAMESPEED_STANDARD")
    assert (result.target, result.gap, result.push) == ("", 0, False)


def test_window_zero_and_last_turn_turn_it_off():
    assert not outlook(35, 1, 10, 30, "", EraSettings(window=0)).push
    assert not outlook(35, 1, 10, 30, "", EraSettings(last_turn=30)).push


def test_from_standing_needs_the_fields():
    assert outlook_from_standing(5, {"score": 3}) is None
    got = outlook_from_standing(35, {"era_score": 1, "era_dark": 10, "era_golden": 30})
    assert got is not None and got.push


def test_log_line(caplog):
    with caplog.at_level(logging.INFO, logger="civ6_ai.era"):
        log_outlook(outlook(35, 1, 10, 30))
        log_outlook(None)
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1 and lines[0].startswith("era_outlook ") and '"push": true' in lines[0]


def test_eligible_needs_adjacency_and_an_unclaimed_kind():
    settings = EraSettings()
    got = eligible_districts(
        {"CAMPUS": 3, "THEATER": 2, "HOLY_SITE": 5}, frozenset({"HOLY_SITE"}), settings
    )
    assert got == frozenset({"CAMPUS"})


def test_district_nudge_needs_eligibility_and_time():
    ok = frozenset({"CAMPUS"})
    assert district_nudge("CAMPUS", ok, 5, 8, 3.0) == 3.0
    assert district_nudge("CAMPUS", frozenset(), 5, 8, 3.0) == 0.0  # Not eligible.
    assert district_nudge("CAMPUS", ok, 9, 8, 3.0) == 0.0  # Cannot finish before the boundary.
    assert district_nudge("CAMPUS", ok, 5, None, 3.0) == 0.0
    assert district_nudge("CAMPUS", ok, 5, 8, 0.0) == 0.0


def _score_campus(eligible, turns=5, left=8):
    option = ProductionChoice("DISTRICT", "DISTRICT_CAMPUS", 54, turns)
    ctx = ProductionContext(
        turn=35, num_cities=2, era_push=True, era_turns_left=left, era_districts=eligible
    )
    return score(option, build_city(fx.city_info(), (), set()), ctx, ProductionWeights())


def test_push_lifts_only_an_eligible_district_that_finishes_in_time():
    base = _score_campus(frozenset())
    assert _score_campus(frozenset({"CAMPUS"})) > base
    assert _score_campus(frozenset({"CAMPUS"}), turns=20, left=3) < _score_campus(
        frozenset({"CAMPUS"}), turns=20, left=30
    )
    assert _score_campus(frozenset({"CAMPUS"}), turns=9, left=8) == _score_campus(
        frozenset(), turns=9, left=8
    )


class _Advisor:
    def __init__(self, adjacency):
        self.adjacency = adjacency

    async def get_district_advisor(self, city_id, district_type):
        return [SimpleNamespace(x=1, y=1, total_adjacency=self.adjacency)]


def _moment_context(adjacency, claimed=frozenset()):
    ctx = ProductionContext(turn=35, num_cities=2, era_push=True, era_claimed=claimed)
    options = [ProductionChoice("DISTRICT", "DISTRICT_CAMPUS", 54, 5)]
    city = build_city(fx.city_info(), (), set())
    return asyncio.run(_with_era_districts(_Advisor(adjacency), city, options, ctx))


def test_advisor_adjacency_gates_the_moment():
    assert _moment_context(2).era_districts == frozenset()
    assert _moment_context(3).era_districts == frozenset({"CAMPUS"})
    assert _moment_context(4, claimed=frozenset({"CAMPUS"})).era_districts == frozenset()


def _empire(turn, era):
    return SimpleNamespace(
        turn=turn,
        era=era,
        era_score=1,
        dark_threshold=10,
        golden_threshold=30,
        game_speed="GAMESPEED_STANDARD",
    )


def test_policy_uses_the_turn_loops_outlook_across_an_era_change():
    holder = EraHolder()
    clock = holder.clock
    for turn in range(38, 52):  # The turn loop sees every turn; the policy sees one in 10.
        era = "ERA_ANCIENT" if turn < 41 else "ERA_CLASSICAL"
        holder.last = outlook_from_standing(
            turn,
            {"era": era, "era_score": 1, "era_dark": 10, "era_golden": 30},
            "GAMESPEED_STANDARD",
            clock=clock,
        )
    policy = CityProductionPolicy(era=holder)
    got = policy._era_outlook(_empire(51, "ERA_CLASSICAL"))
    assert got is holder.last and got.turns_left == 30  # Era began T41, not T51.
    # Without a holder, the policy's own clock still works.
    alone = CityProductionPolicy()
    assert alone._era_outlook(_empire(35, "ERA_ANCIENT")).turns_left == 6
