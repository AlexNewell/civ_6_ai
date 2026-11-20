import asyncio
import logging

from civ6_ai.policies.production import (
    DEFAULT_RIVALS,
    CityProductionPolicy,
    ProductionContext,
    ProductionWeights,
    _raw_value,
    rank_production,
    rivals_from_rankings,
    score,
    wonder_alternative_value,
)
from civ6_ai.state import ProductionChoice
from civ6_ai.state.reader import build_city
from civ6_ai.strategy.wonders import (
    estimate_race,
    rival_win_probability,
    should_abandon,
)
from civ6_ai.world_rankings import CivScore, WorldRankings
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


# Issue 328: the estimator's real inputs.
def _empire(**overview_changes):
    from civ6_ai.state import read_empire

    game = fx.FakeGame(overview=fx.overview(**overview_changes))
    return asyncio.run(read_empire(game))


def test_the_empire_carries_the_overview_difficulty():
    assert _empire(difficulty="Prince").difficulty == "Prince"
    assert _empire().difficulty == ""  # Unread stays empty, which the estimator treats with care.


def test_prince_as_the_overview_names_it_is_read_as_prince():
    assert race(difficulty="Prince").win_probability == race(difficulty="PRINCE").win_probability


class _Holder:
    def __init__(self, last):
        self.last = last


def _rankings(*ids, ours=0):
    return WorldRankings(ours, tuple(CivScore(i, "", 100) for i in ids))


def test_rivals_are_the_living_majors_but_us_met_or_not():
    # Two unmet slots (-1), two met civs, and us: four rivals.
    holder = _Holder(_rankings(-1, -1, 5, 4, 0))
    assert rivals_from_rankings(holder) == (4, "rankings")
    assert rivals_from_rankings(_Holder(_rankings(0))) == (0, "rankings")  # Alone: no rival.


def test_unread_rankings_give_the_default_and_never_zero():
    assert rivals_from_rankings(None) == (DEFAULT_RIVALS, "default")
    assert rivals_from_rankings(_Holder(None)) == (DEFAULT_RIVALS, "default")
    assert rivals_from_rankings(_Holder(_rankings())) == (DEFAULT_RIVALS, "default")
    assert DEFAULT_RIVALS > 0


def _policy_context(weights, rankings, empire=None):
    policy = CityProductionPolicy(weights=weights, rankings=rankings)
    empire = empire or _empire(difficulty="Emperor")
    return policy._context(empire, [], [], [])


def test_on_the_context_gets_difficulty_and_rivals():
    ctx = _policy_context(ProductionWeights(wonder_races=True), _Holder(_rankings(1, 2, 0)))
    assert (ctx.difficulty, ctx.rivals, ctx.rivals_source) == ("Emperor", 2, "rankings")
    assert dict(ctx.rival_progress) == {}  # Not exposed by the game: left empty.


def test_off_reads_nothing_and_leaves_the_context_alone():
    class Tripwire:
        @property
        def last(self):
            raise AssertionError("the rankings were read with wonder_races off")

    ctx = _policy_context(ProductionWeights(), Tripwire())
    assert (ctx.difficulty, ctx.rivals, ctx.wonder_alternative) == ("", 0, 5.0)


def _alt_menu(*extra):
    return [_menu()[0], *extra]


def _alt_of(options, city=None, w=None, **kw):
    city = city or _city(production=8.0)
    w = w or ProductionWeights(wonder_races=True)
    return wonder_alternative_value(city, options, _ctx(), w, 5.0, **kw)


def test_the_alternative_is_the_best_district_or_building_score_here():
    campus = ProductionChoice("DISTRICT", "DISTRICT_CAMPUS", 54, 7)
    monument = ProductionChoice("BUILDING", "BUILDING_MONUMENT", 60, 7)
    unit = ProductionChoice("UNIT", "UNIT_WARRIOR", 40, 4)
    c, w, x = _city(production=8.0), ProductionWeights(wonder_races=True), _ctx()
    campus_value = _raw_value(campus, c, x, w)
    monument_value = _raw_value(monument, c, x, w)
    assert campus_value > 0 and monument_value > 0
    best = wonder_alternative_value(c, [*_menu(), campus, unit], x, w, 5.0)
    assert best == max(campus_value, monument_value)
    # Units and the wonder itself never count.
    assert wonder_alternative_value(c, [_menu()[0], unit], x, w, 5.0) == 5.0
    # An item the game or a refusal ruled out is no alternative.
    only = wonder_alternative_value(c, [campus, monument], x, w, 5.0, frozenset({campus.item}))
    assert only == monument_value


def test_with_nothing_to_build_the_fallback_is_kept():
    assert _alt_of([]) == 5.0
    assert _alt_of(_alt_menu()) == 5.0


def test_a_strong_alternative_turns_a_go_into_a_no_go_and_off_ignores_it(caplog):
    c = _city(production=8.0)
    library = ProductionChoice("BUILDING", "BUILDING_LIBRARY", 90, 11)
    menu = [_menu()[0], library]

    def verdict(w, turn):
        caplog.clear()
        with caplog.at_level(logging.INFO):
            rank_production(c, menu, _ctx(turn=turn), w)
        return caplog.text

    assert " GO (" in verdict(ProductionWeights(wonder_races=True), 20)
    # A Library worth 90 beats the wonder's expected 8 at any win chance: no race.
    rich = verdict(ProductionWeights(wonder_races=True, science=30.0), 21)
    assert "NO-GO" in rich and "alt 90.0" in rich and "alt=90.0" in rich
    # Off: no race log, and the ranking ignores the race inputs.
    off = ProductionWeights(science=30.0)
    assert "wonder race" not in verdict(off, 22)
    assert rank_production(c, menu, _ctx(), off) == rank_production(
        c, menu, _ctx(difficulty="DEITY", rivals=9), off
    )


def test_the_log_line_names_the_inputs_used(caplog):
    w = ProductionWeights(wonder_races=True)
    c = _city(production=8.0)
    ctx = _ctx(turn=60, rivals_source="rankings", rival_progress={WONDER: (0.25,)})
    with caplog.at_level(logging.INFO):
        score(_menu()[0], c, ctx, w)
    assert "difficulty=PRINCE" in caplog.text
    assert "rivals=3 (rankings)" in caplog.text
    assert "rival progress=0.25" in caplog.text
    assert "alt=5.0" in caplog.text
