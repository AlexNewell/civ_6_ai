"""Tests for the siege estimator and city-state target ranking (issue 303)."""

from civ6_ai.strategy.conquest_targets import CityStateCandidate, rank_targets
from civ6_ai.strategy.siege import (
    Force,
    GarrisonUnit,
    TargetCity,
    estimate_force,
    expected_damage,
    units_needed,
    win_probability,
)

# Anshan, around turn 60: no walls, a modest garrison. 2 Legions and a ram took it.
ANSHAN = TargetCity(strength=22, garrison=(GarrisonUnit("slinger", 15, ranged=True),))
# Bandar Brunei: walls and a bigger garrison. 4 Legions, a ram and a warrior took it.
BRUNEI = TargetCity(
    strength=24,
    has_walls=True,
    garrison=(GarrisonUnit("spearman", 25), GarrisonUnit("archer", 20, ranged=True)),
)


def test_damage_rises_with_strength_difference() -> None:
    assert expected_damage(40, 20) > expected_damage(30, 20) > expected_damage(20, 20)
    assert 28 < expected_damage(20, 20) < 32  # about 30 on equal strength


def test_unwalled_city_needs_no_ram() -> None:
    est = estimate_force(ANSHAN)
    assert est.feasible
    assert "battering_ram" not in est.required
    assert any("a ram is not needed" in r for r in est.reasons)


def test_walled_city_needs_siege_or_more_force() -> None:
    est = estimate_force(BRUNEI)
    assert est.feasible
    assert est.required.get("battering_ram", 0) >= 1
    assert any("walls" in r for r in est.reasons)


def test_ram_matters_only_behind_walls() -> None:
    legions = Force.of({"legion": 3})
    with_ram = Force.of({"legion": 3, "battering_ram": 1})
    walled = TargetCity(strength=24, has_walls=True)
    gain_walls = win_probability(walled, with_ram)[0] - win_probability(walled, legions)[0]
    open_city = TargetCity(strength=24)
    gain_open = win_probability(open_city, with_ram)[0] - win_probability(open_city, legions)[0]
    assert gain_walls > 0.3
    assert gain_open < gain_walls


def test_stronger_garrison_needs_more() -> None:
    weak = TargetCity(strength=15)
    strong = TargetCity(strength=15, garrison=tuple(GarrisonUnit("spearman", 30) for _ in range(3)))
    assert sum(estimate_force(strong).required.values()) > sum(
        estimate_force(weak).required.values()
    )


def test_higher_confidence_never_needs_less() -> None:
    low = estimate_force(BRUNEI, confidence=0.5)
    high = estimate_force(BRUNEI, confidence=0.95)
    assert sum(high.required.values()) >= sum(low.required.values())


def test_deterministic() -> None:
    assert estimate_force(BRUNEI) == estimate_force(BRUNEI)
    force = Force.of({"legion": 2, "battering_ram": 1})
    assert win_probability(ANSHAN, force) == win_probability(ANSHAN, force)


def test_owner_forces_win_often() -> None:
    anshan_force = Force.of({"legion": 2, "battering_ram": 1})
    assert win_probability(ANSHAN, anshan_force)[0] >= 0.8
    brunei_force = Force.of({"legion": 4, "battering_ram": 1, "warrior": 1})
    assert win_probability(BRUNEI, brunei_force)[0] >= 0.8


def test_hopeless_city_is_infeasible() -> None:
    fortress = TargetCity(strength=200, has_walls=True)
    est = estimate_force(fortress)
    assert not est.feasible
    assert est.required == {}


def test_current_force_probability_and_missing_units() -> None:
    est = estimate_force(BRUNEI, current={"legion": 1})
    assert est.win_probability < 0.5
    missing = units_needed(est, {"legion": 1})
    assert missing.get("battering_ram", 0) >= 1


def test_no_melee_cannot_capture() -> None:
    assert win_probability(ANSHAN, Force.of({"catapult": 3}))[0] == 0.0


def test_ranking_drops_allies_and_orders_by_score() -> None:
    cands = [
        CityStateCandidate("Ally", True, False, 0, 3, blocks_settling=True),
        CityStateCandidate("Walled", False, True, 2, 6),
        CityStateCandidate("Blocker", False, False, 1, 6, blocks_settling=True, resources_nearby=2),
        CityStateCandidate("Far", False, False, 0, 40),
    ]
    ranked = rank_targets(cands)
    assert [r.name for r in ranked][0] == "Blocker"
    assert "Ally" not in [r.name for r in ranked]
    assert "Far" not in [r.name for r in ranked]
    assert all(r.reasons for r in ranked)


def test_envoys_lower_score() -> None:
    base = CityStateCandidate("A", False, False, 0, 5)
    with_envoys = CityStateCandidate("B", False, False, 0, 5, our_envoys=2)
    ranked = rank_targets([with_envoys, base])
    assert ranked[0].name == "A"
