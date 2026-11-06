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
    rank_options,
)
from civ6_ai.state import reader
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
