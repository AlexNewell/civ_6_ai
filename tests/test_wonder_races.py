import logging

from civ6_ai.policies.production import (
    ProductionContext,
    ProductionWeights,
    rank_production,
    score,
)
from civ6_ai.state import ProductionChoice
from civ6_ai.state.reader import build_city
from civ6_ai.strategy.wonders import (
    estimate_race,
    rival_win_probability,
    should_abandon,
)
from fixtures import civ_state as fx

WONDER = "BUILDING_STONEHENGE"


def race(**changes):
    args = dict(
        wonder=WONDER,
        cost=180,
        production=8.0,
        turn=30,
        difficulty="PRINCE",
        rivals=3,
        alternative=5.0,
    )
    return estimate_race(**{**args, **changes})


def test_prince_is_better_than_a_higher_difficulty():
    prince = race(difficulty="PRINCE")
    deity = race(difficulty="DEITY")
    assert prince.win_probability > deity.win_probability
    assert prince.go and not deity.go


def test_a_fast_city_wins_more_often_than_a_slow_one():
    fast = race(production=12.0)
    slow = race(production=3.0)
    assert fast.our_turn < slow.our_turn
    assert fast.win_probability > slow.win_probability
    assert not slow.go


def test_observed_rival_progress_lowers_the_chance_and_triggers_abandon():
    none = race()
    ahead = race(progress=(0.8,))
    assert ahead.win_probability < none.win_probability
    assert not ahead.go
    assert should_abandon(ahead.win_probability, (0.8,), 0.2)
    assert not should_abandon(ahead.win_probability, (0.8,), 0.95)


def test_a_race_in_progress_is_kept_longer_than_one_is_started():
    middling = race(difficulty="KING", production=6.0)
    assert 0.35 < middling.win_probability < 0.6
    assert not middling.go
    assert race(difficulty="KING", production=6.0, already_started=True, alternative=1.0).go


def test_no_rivals_means_certain_win_and_unknown_difficulty_is_careful():
    assert rival_win_probability(60, 30, 180, "PRINCE", 0) == 0.0
    assert race(difficulty="").win_probability < race(difficulty="PRINCE").win_probability


def test_the_log_line_names_the_estimate_and_the_choice():
    line = race().log_line("Alpha")
    assert "wonder race Alpha BUILDING_STONEHENGE" in line and "GO" in line and "win " in line


def _city(**changes):
    return build_city(fx.city_info(**changes), (), set())


def _menu():
    return [
        ProductionChoice("BUILDING", WONDER, 180, 20),
        ProductionChoice("BUILDING", "BUILDING_MONUMENT", 60, 7),
        ProductionChoice("UNIT", "UNIT_BUILDER", 50, 6),
    ]


def _ctx(**changes):
    base = dict(turn=30, num_cities=2, difficulty="PRINCE", rivals=3, army=4, builders=2)
    return ProductionContext(**{**base, **changes})


def test_off_ranks_identically_whatever_the_race_signals():
    c = _city(production=8.0)
    plain = rank_production(c, _menu(), _ctx(), ProductionWeights())
    odd = _ctx(difficulty="DEITY", rivals=9, rival_progress={WONDER: (0.9,)})
    assert rank_production(c, _menu(), odd, ProductionWeights()) == plain
    flat = score(_menu()[0], c, _ctx(), ProductionWeights())
    assert flat == score(_menu()[0], c, odd, ProductionWeights()) > 0


def test_on_scores_a_wonder_only_when_the_estimate_says_go(caplog):
    w = ProductionWeights(wonder_races=True)
    c = _city(production=8.0)
    with caplog.at_level(logging.INFO):
        good = score(_menu()[0], c, _ctx(), w)
    assert good > 0 and "wonder race" in caplog.text
    assert score(_menu()[0], c, _ctx(difficulty="DEITY", turn=31), w) == 0.0
    assert score(_menu()[0], c, _ctx(rival_progress={WONDER: (0.9,)}, turn=32), w) == 0.0


def test_on_a_threatened_or_expanding_city_does_not_race():
    w = ProductionWeights(wonder_races=True)
    c = _city(production=8.0)
    assert score(_menu()[0], c, _ctx(turn=40, settlers=1), w) == 0.0
    assert score(_menu()[0], c, _ctx(turn=41, threatened=frozenset({c.city_id})), w) == 0.0
    assert score(_menu()[0], c, _ctx(turn=42, threatened=frozenset({99})), w) == 0.0
