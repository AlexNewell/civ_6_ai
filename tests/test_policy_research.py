import asyncio
from dataclasses import replace

import pytest
from civ_mcp import lua as lq

from civ6_ai.blockers import Blocker, BlockerResolver, BlockerType
from civ6_ai.policies.research import (
    ResearchPolicy,
    ResearchWeights,
    blocker_policies,
    choose_civic,
    choose_tech,
    estimate_civic_progress,
    rank_options,
)
from civ6_ai.state import reader
from civ6_ai.state.models import LockedResearch
from fixtures import civ_state as fx

NO_PRIORITIES = ResearchWeights(priorities=())


def run(coro):
    return asyncio.run(coro)


def tech(name, turns=5, boosted=False, progress=0):
    return lq.TechOption(
        name,
        f"TECHNOLOGY_{name.upper().replace(' ', '_')}",
        50,
        progress,
        turns,
        boosted,
        "",
        "",
    )


def civic(name, turns=5, boosted=False):
    return lq.CivicOption(
        name, f"CIVIC_{name.upper().replace(' ', '_')}", 30, 0, turns, boosted, "", ""
    )


def status(techs=(), civics=(), locked_techs=(), current_tech="None", current_civic="None"):
    return replace(
        fx.research_status(),
        current_research=current_tech,
        current_civic=current_civic,
        available_techs=list(techs),
        available_civics=list(civics),
        locked_techs=list(locked_techs),
        locked_civics=[],
    )


def research(**kwargs):
    return reader.build_research(status(**kwargs))


def names(ranked):
    return [o.name for o in ranked]


class Game(fx.FakeGame):
    def __init__(self, refuse=(), **overrides):
        super().__init__(**overrides)
        self.refuse = set(refuse)
        self.started: list[str] = []

    async def set_research(self, tech_name):
        return await self._set(tech_name)

    async def set_civic(self, civic_name):
        return await self._set(civic_name)

    async def _set(self, item):
        if item in self.refuse:
            return "Error: refused"
        self.started.append(item)
        return "OK"


def test_quicker_option_wins_when_nothing_else_differs():
    r = research(techs=[tech("Alpha", 8), tech("Beta", 3)])
    assert names(choose_tech(r, NO_PRIORITIES)) == ["Beta", "Alpha"]


def test_boosted_option_beats_a_quicker_plain_one():
    r = research(techs=[tech("Alpha", 2), tech("Beta", 4, boosted=True)])
    assert names(choose_tech(r, NO_PRIORITIES))[0] == "Beta"


def test_eureka_weight_of_zero_ignores_boosts():
    r = research(techs=[tech("Alpha", 2), tech("Beta", 4, boosted=True)])
    weights = ResearchWeights(eureka=0.0, priorities=())
    assert names(choose_tech(r, weights))[0] == "Alpha"


def test_option_that_unlocks_more_is_preferred():
    locked = [
        lq.LockedTech("Gamma", "TECHNOLOGY_GAMMA", ["Beta"], "Ancient", False, ""),
        lq.LockedTech("Delta", "TECHNOLOGY_DELTA", ["Beta", "Zeta"], "Ancient", False, ""),
    ]
    r = research(techs=[tech("Alpha", 4), tech("Beta", 4)], locked_techs=locked)
    assert names(choose_tech(r, NO_PRIORITIES))[0] == "Beta"


def test_sole_missing_prerequisite_counts_more_than_a_shared_one():
    sole = [lq.LockedTech("Gamma", "TECHNOLOGY_GAMMA", ["Beta"], "Ancient", False, "")]
    shared = [lq.LockedTech("Delta", "TECHNOLOGY_DELTA", ["Alpha", "Zeta"], "Ancient", False, "")]
    r = research(techs=[tech("Alpha"), tech("Beta")], locked_techs=sole + shared)
    assert names(choose_tech(r, NO_PRIORITIES)) == ["Beta", "Alpha"]


def test_opening_a_boosted_locked_tech_adds_value():
    locked = [lq.LockedTech("Gamma", "TECHNOLOGY_GAMMA", ["Alpha"], "Ancient", True, "x")]
    r = research(techs=[tech("Alpha", 6), tech("Beta", 3)], locked_techs=locked)
    assert names(choose_tech(r, NO_PRIORITIES))[0] == "Alpha"


def test_priority_matches_the_name_without_its_prefix():
    r = research(techs=[tech("Mining", 3), tech("Writing", 5)])
    assert names(choose_tech(r, ResearchWeights()))[0] == "Writing"
    weights = ResearchWeights(priorities=(("MINING", 9.0),))
    assert names(choose_tech(r, weights))[0] == "Mining"


def test_progress_and_stickiness_keep_the_current_target():
    options = [tech("Alpha", 4), tech("Beta", 4)]
    assert names(choose_tech(research(techs=options), NO_PRIORITIES))[0] == "Alpha"
    r = research(techs=options, current_tech="Beta")
    assert names(choose_tech(r, NO_PRIORITIES))[0] == "Beta"
    r = research(techs=[tech("Alpha", 4), tech("Beta", 4, progress=60)])
    assert names(choose_tech(r, NO_PRIORITIES))[0] == "Beta"


def test_ties_break_by_item_type_and_ranking_is_repeatable():
    r = research(techs=[tech("Zed"), tech("Alpha"), tech("Mid")])
    first = rank_options(r.techs, r.locked_techs, NO_PRIORITIES)
    assert names(first) == ["Alpha", "Mid", "Zed"]
    assert first == rank_options(tuple(reversed(r.techs)), r.locked_techs, NO_PRIORITIES)


def test_unknown_turns_do_not_divide_by_zero():
    r = research(techs=[tech("Alpha", 0), tech("Beta", -1), tech("Gamma", 2)])
    assert names(choose_tech(r, NO_PRIORITIES))[0] == "Gamma"


def test_civics_use_their_own_options():
    r = research(civics=[civic("Slow", 9), civic("Fast", 2, boosted=True)])
    assert names(choose_civic(r, NO_PRIORITIES)) == ["Fast", "Slow"]


def test_target_changes_when_a_boost_is_earned():
    plain = research(techs=[tech("Alpha", 3), tech("Beta", 5)])
    earned = research(techs=[tech("Alpha", 3), tech("Beta", 5, boosted=True)])
    assert names(choose_tech(plain, NO_PRIORITIES))[0] == "Alpha"
    assert names(choose_tech(earned, NO_PRIORITIES))[0] == "Beta"


def test_policy_fills_an_empty_research_and_civic():
    game = Game(research=status(techs=[tech("Alpha", 3)], civics=[civic("Law", 2)]))
    run(ResearchPolicy().run(game))
    assert game.started == ["TECHNOLOGY_ALPHA", "CIVIC_LAW"]


def test_policy_leaves_the_best_current_choice_alone():
    game = Game(
        research=status(
            techs=[tech("Alpha", 3)],
            civics=[civic("Law", 2)],
            current_tech="Alpha",
            current_civic="Law",
        )
    )
    run(ResearchPolicy().run(game))
    assert game.started == []


def test_policy_switches_to_a_newly_boosted_option():
    game = Game(
        research=status(
            techs=[tech("Alpha", 3), tech("Beta", 6, boosted=True)], current_tech="Alpha"
        )
    )
    run(ResearchPolicy(NO_PRIORITIES).run(game))
    assert game.started == ["TECHNOLOGY_BETA"]


def test_policy_takes_new_weights():
    r = status(techs=[tech("Alpha", 3), tech("Beta", 6)])
    plain, favoured = Game(research=r), Game(research=r)
    run(ResearchPolicy(NO_PRIORITIES).run(plain))
    run(ResearchPolicy(ResearchWeights(priorities=(("BETA", 50.0),))).run(favoured))
    assert plain.started[0] == "TECHNOLOGY_ALPHA"
    assert favoured.started[0] == "TECHNOLOGY_BETA"


def test_policy_tries_the_next_option_when_one_is_refused():
    game = Game(
        refuse={"TECHNOLOGY_ALPHA"}, research=status(techs=[tech("Alpha", 2), tech("Beta", 5)])
    )
    run(ResearchPolicy(NO_PRIORITIES).run(game))
    assert game.started == ["TECHNOLOGY_BETA"]


def test_policy_raises_when_everything_is_refused():
    game = Game(refuse={"TECHNOLOGY_ALPHA"}, research=status(techs=[tech("Alpha")]))
    with pytest.raises(RuntimeError):
        run(ResearchPolicy().run(game))


def test_nothing_to_start_is_not_an_error():
    game = Game(research=status())
    run(ResearchPolicy().run(game))
    assert game.started == []


def test_blocker_policies_choose_for_the_resolver_and_ignore_stale_ones():
    game = Game(research=status(techs=[tech("Alpha")], civics=[civic("Law")]))
    policies = blocker_policies(NO_PRIORITIES)
    assert run(policies[BlockerType.RESEARCH](game, Blocker(BlockerType.RESEARCH))) is True
    assert run(policies[BlockerType.CIVIC](game, Blocker(BlockerType.CIVIC))) is True
    assert game.started == ["TECHNOLOGY_ALPHA", "CIVIC_LAW"]
    stale = Game(research=status(current_tech="Alpha", current_civic="Law"))
    assert run(policies[BlockerType.RESEARCH](stale, Blocker(BlockerType.RESEARCH))) is True
    assert stale.started == []


def test_blocker_policy_reports_false_when_no_option_exists():
    game = Game(research=status())
    policy = blocker_policies()[BlockerType.RESEARCH]
    assert run(policy(game, Blocker(BlockerType.RESEARCH))) is False


def test_blocker_policies_plug_into_the_resolver():
    assert BlockerResolver(blocker_policies())


def test_education_and_chemistry_carry_a_science_priority():
    from civ6_ai.policies.research import DEFAULT_PRIORITIES

    names = dict(DEFAULT_PRIORITIES)
    assert names["EDUCATION"] > 0 and names["CHEMISTRY"] > 0 and names["ASTRONOMY"] > 0


def test_boost_line_names_boosted_options_and_locked_items(caplog):
    import json
    import logging

    from civ6_ai.policies.research import log_boosts, research_snapshot
    from civ6_ai.state.models import ResearchOption

    class R:
        techs = (
            ResearchOption("TECH_MINING", "Mining", 50, 0, 5, True, "", (), ""),
            ResearchOption("TECH_POTTERY", "Pottery", 50, 0, 5, False, "", (), ""),
        )
        locked_techs = ()
        civics = ()
        locked_civics = ()

    assert research_snapshot(R.techs, R.locked_techs) == {
        "Mining": [50, 0, 1],
        "Pottery": [50, 0, 0],
    }
    with caplog.at_level(logging.INFO):
        log_boosts(R)
    line = next(
        r.getMessage() for r in caplog.records if r.getMessage().startswith("research_boosts")
    )
    assert json.loads(line.split(" ", 1)[1]) == {
        "civics": {},
        "techs": {"Mining": [50, 0, 1], "Pottery": [50, 0, 0]},
    }


def civic_research(turns_left):
    """Craftsmanship (cost 26) in progress; Foreign Trade is boosted and opens a locked civic."""
    craft = lq.CivicOption("Craftsmanship", "CIVIC_CRAFTSMANSHIP", 26, 0, turns_left, False, "", "")
    trade = lq.CivicOption("Foreign Trade", "CIVIC_FOREIGN_TRADE", 30, 0, 15, False, "", "")
    base = reader.build_research(status(civics=[craft, trade], current_civic="Craftsmanship"))
    unlock = LockedResearch("CIVIC_X", "X", ("Foreign Trade",), "", True, "")
    return replace(base, current_civic_turns=turns_left, locked_civics=(unlock,))


def test_estimate_keeps_the_civic_nearly_done():
    base = civic_research(2)
    assert names(choose_civic(base, NO_PRIORITIES))[0] == "Foreign Trade"  # Progress 0: dropped.
    fixed = estimate_civic_progress(base, 2.0)
    crafted = next(o for o in fixed.civics if o.name == "Craftsmanship")
    assert crafted.progress_pct == 84  # (26 - 2 * 2) / 26
    assert names(choose_civic(fixed, NO_PRIORITIES))[0] == "Craftsmanship"


def test_estimate_lets_a_civic_just_started_be_replaced():
    fixed = estimate_civic_progress(civic_research(13), 2.0)  # 26 - 13 * 2: nothing done
    crafted = next(o for o in fixed.civics if o.name == "Craftsmanship")
    assert crafted.progress_pct == 0
    assert names(choose_civic(fixed, NO_PRIORITIES))[0] == "Foreign Trade"


def test_estimate_needs_the_turns_left():
    base = civic_research(2)
    unknown = replace(base, current_civic_turns=None)
    assert estimate_civic_progress(unknown, 2.0) is unknown
    assert estimate_civic_progress(base, 0.0) is base
    assert estimate_civic_progress(replace(base, current_civic_turns=-1), 2.0).civics == base.civics


def test_policy_reads_the_overview_only_for_a_civic_in_progress():
    idle = Game()
    run(ResearchPolicy().run(idle))
    assert "get_game_overview" not in idle.calls
    busy = Game(
        research=replace(
            status(civics=[civic("Alpha")], current_civic="Alpha"), current_civic_turns=5
        )
    )
    run(ResearchPolicy().run(busy))
    assert "get_game_overview" in busy.calls


# Boost judgement (#322).

POP6 = "Grow your civilization to at least 6 population."


def tech_with_boost(name, turns, desc):
    return lq.TechOption(
        name, f"TECHNOLOGY_{name.upper().replace(' ', '_')}", 50, 0, turns, False, desc, ""
    )


def boost_inputs(state=None, signals=None):
    from civ6_ai.strategy.boost_judgement import BoostState, PlanSignals

    async def read():
        return state or BoostState(turn=10, population=4), signals or PlanSignals(
            growth_turns_per_pop=2.0
        )

    return read


def boost_techs():
    # Alpha is quicker and has a boost close, but it is cheap, so waiting saves little.
    return [tech_with_boost("Alpha", 4, POP6), tech_with_boost("Beta", 9, "Build a wonder.")]


def test_boost_judgement_off_leaves_the_ranking_unchanged():
    r = research(techs=[tech_with_boost("Alpha", 12, POP6), tech_with_boost("Beta", 9, "")])
    off = ResearchWeights(priorities=())
    assert off.boost_judgement is False
    # The inputs would delay Alpha, but an off setting applies no adjustment at all.
    policy = ResearchPolicy(off, boost_inputs())
    assert run(policy._boost_adjustments(r)) == {}
    assert names(choose_tech(r, off)) == ["Beta", "Alpha"]
    on = ResearchWeights(priorities=(), boost_judgement=True)
    assert names(choose_tech(r, on)) == names(choose_tech(r, off))  # No adjustments passed.


def test_boost_judgement_on_delays_an_item_whose_boost_lands_soon():
    r = research(techs=[tech_with_boost("Alpha", 12, POP6), tech_with_boost("Beta", 9, "")])
    weights = ResearchWeights(priorities=(), boost_judgement=True)
    assert names(choose_tech(r, ResearchWeights(priorities=())))[0] == "Beta"
    policy = ResearchPolicy(weights, boost_inputs())
    adjustments = run(policy._boost_adjustments(r))
    assert adjustments == {"TECHNOLOGY_ALPHA": -weights.boost_delay_penalty}
    assert names(choose_tech(r, weights, adjustments)) == ["Beta", "Alpha"]


def test_boost_judgement_logs_the_reason_and_skips_a_small_saving(caplog):
    weights = ResearchWeights(priorities=(), boost_judgement=True)
    policy = ResearchPolicy(weights, boost_inputs())
    # Alpha costs only 4 turns, so waiting saves 1.6 turns less 2: not worth a delay.
    assert run(policy._boost_adjustments(research(techs=boost_techs()))) == {}
    slow = research(techs=[tech_with_boost("Alpha", 12, POP6), tech_with_boost("Beta", 9, "")])
    with caplog.at_level("INFO"):
        run(policy._boost_adjustments(slow))
    assert "delaying Alpha: boost in about 4 turns from population growth" in caplog.text


def test_boost_judgement_gives_up_after_the_wait_limit():
    r = research(techs=[tech_with_boost("Alpha", 12, POP6), tech_with_boost("Beta", 9, "")])
    from civ6_ai.strategy.boost_judgement import BoostState, PlanSignals

    weights = ResearchWeights(priorities=(), boost_judgement=True, boost_delay_max_turns=5)
    policy = ResearchPolicy(weights, boost_inputs())
    assert run(policy._boost_adjustments(r))  # Delayed on turn 10.
    stuck = boost_inputs(BoostState(turn=16, population=4), PlanSignals(growth_turns_per_pop=2.0))
    policy.boost_inputs = stuck
    assert run(policy._boost_adjustments(r)) == {}  # Waited 6 turns: researches it.


def test_boost_judgement_prefers_an_already_boosted_item():
    boosted = lq.TechOption("Gamma", "TECHNOLOGY_GAMMA", 50, 0, 6, True, "Build a wonder.", "")
    r = research(techs=[boosted, tech_with_boost("Beta", 6, "")])
    weights = ResearchWeights(priorities=(), boost_judgement=True)
    adjustments = run(ResearchPolicy(weights, boost_inputs())._boost_adjustments(r))
    assert adjustments == {"TECHNOLOGY_GAMMA": weights.boost_prefer}


def test_boost_judgement_on_without_inputs_changes_nothing():
    r = research(techs=boost_techs())
    policy = ResearchPolicy(ResearchWeights(priorities=(), boost_judgement=True))
    assert run(policy._boost_adjustments(r)) == {}


def test_policy_starts_the_other_item_while_one_is_delayed():
    r = status(techs=[tech_with_boost("Alpha", 12, POP6), tech_with_boost("Beta", 9, "")])
    game = Game(research=r)
    weights = ResearchWeights(priorities=(), boost_judgement=True)
    run(ResearchPolicy(weights, boost_inputs()).run(game))
    assert "TECHNOLOGY_BETA" in game.started
