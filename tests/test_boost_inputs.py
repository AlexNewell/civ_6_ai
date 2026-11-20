"""The boost judgement's real inputs and the hooks that use its nudges (#344)."""

import asyncio
from types import SimpleNamespace

import pytest
from civ_mcp import lua as lq

from civ6_ai import bot
from civ6_ai.monitors import EventBus
from civ6_ai.orchestrator import TurnLog
from civ6_ai.policies.builder import BuilderPolicy, BuilderSettings, BuilderWeights
from civ6_ai.policies.military import MilitaryDecision, MilitaryPolicy, _Turn
from civ6_ai.policies.research import ResearchPolicy, ResearchWeights, horizon_line
from civ6_ai.state import BuilderJob, read_research
from civ6_ai.state.models import ResearchOption, Threat
from civ6_ai.state.reader import build_city, build_unit
from civ6_ai.strategy import boost_inputs as bi
from civ6_ai.strategy.boost_judgement import (
    BoostState,
    Decision,
    Nudge,
    NudgeBoard,
    PlanSignals,
    judge_boosts,
    nudges,
)
from fixtures import civ_state as fx
from test_policy_builder import ActionGame, repair_task, task, worker


def run(coro):
    return asyncio.run(coro)


def city(**changes):
    return build_city(fx.city_info(**changes), [], set())


def unit(unit_type="UNIT_SLINGER", **changes):
    base = dict(unit_type=unit_type, combat_strength=10, ranged_strength=0, targets=[])
    return build_unit(fx.unit_info(**{**base, **changes}))


def job(improvement="IMPROVEMENT_QUARRY", x=10, y=11, resource="STONE", city="Rome", **changes):
    base = dict(
        priority="normal",
        x=x,
        y=y,
        improvement=improvement,
        resource=resource,
        resource_class="bonus" if resource else None,
        city=city,
        nearest_builder_id=None,
        distance=None,
    )
    return BuilderJob(**{**base, **changes})


def threat(x=5, y=5, owner_id=63, combat=8, ranged=0, hostile=True):
    return Threat(
        owner_id, "Barbarians", "UNIT_WARRIOR", x, y, 100, 100, combat, ranged, 2, hostile
    )


# ----- builder jobs and sites -----


def test_job_site_reads_like_the_nudge_example():
    assert bi.job_site(job()) == "the stone near Rome"
    assert bi.job_site(job(resource=None)) == "a tile near Rome"
    assert bi.job_site(job(resource="IRON_ORE", city="")) == "the iron ore nearby"


def test_job_matches_needs_the_site_and_the_improvement():
    site = "the stone near Rome"
    assert bi.job_matches(job(), "QUARRY", site)
    assert bi.job_matches(job(), "ANY", site)
    assert not bi.job_matches(job(), "MINE", site)  # Another improvement.
    assert not bi.job_matches(job(city="Paris"), "QUARRY", site)  # Another city.
    assert not bi.job_matches(job(resource=None), "QUARRY", site)  # Not on the resource.
    assert not bi.job_matches(job(is_repair=True), "ANY", site)  # A repair is no new tile.
    boats = job("IMPROVEMENT_FISHING_BOATS", resource="FISH")
    assert bi.job_matches(boats, "SEA_RESOURCE", "the fish near Rome")
    assert not bi.job_matches(job(), "SEA_RESOURCE", site)


def test_claimed_jobs_are_the_plan_and_the_rest_are_sites():
    claimed_job = job(x=1, y=1)
    open_job = job("IMPROVEMENT_FARM", x=2, y=2, resource=None)
    signals = bi.improvement_signals(
        [claimed_job, open_job], frozenset({(1, 1, "IMPROVEMENT_QUARRY")})
    )
    assert signals.jobs == {"QUARRY": 1}
    assert signals.resource_jobs == {"QUARRY": 1}
    assert signals.sites == {"FARM": ("a tile near Rome",)}
    assert signals.resource_sites == {}  # The open job is on no resource.


def test_no_claim_means_everything_is_a_site_and_nothing_is_planned():
    signals = bi.improvement_signals([job()], frozenset())
    assert signals.jobs == {}
    assert signals.sites == {"QUARRY": ("the stone near Rome",)}
    assert signals.resource_sites == {"QUARRY": ("the stone near Rome",)}


def test_one_job_per_tile_prefers_the_claimed_one_and_repairs_are_left_out():
    quarry = job("IMPROVEMENT_QUARRY", x=1, y=1)
    mine = job("IMPROVEMENT_MINE", x=1, y=1)
    repair = job("IMPROVEMENT_FARM", x=2, y=2, is_repair=True)
    signals = bi.improvement_signals(
        [quarry, mine, repair], frozenset({(1, 1, "IMPROVEMENT_MINE")})
    )
    assert signals.jobs == {"MINE": 1}
    assert signals.sites == {}
    unclaimed = bi.improvement_signals([quarry, mine, repair], frozenset())
    assert unclaimed.sites == {"MINE": ("the stone near Rome",)}  # First by name, one tile.


def test_a_sea_job_counts_once_under_the_sea_key():
    boats = job("IMPROVEMENT_FISHING_BOATS", resource="FISH")
    signals = bi.improvement_signals([boats], frozenset({(10, 11, "IMPROVEMENT_FISHING_BOATS")}))
    assert signals.jobs == {"SEA_RESOURCE": 1}


def test_empty_jobs_give_empty_signals():
    assert bi.improvement_signals([], frozenset()) == bi.ImprovementSignals()


# ----- state -----


def build_state(cities=(), units=(), **changes):
    args = dict(
        turn=12,
        cities=list(cities),
        units=list(units),
        built={},
        built_on_resource={},
        kills={},
        civilizations_met=0,
        city_states_met=0,
        has_pantheon=False,
    )
    return bi.build_boost_state(**{**args, **changes})


def test_state_counts_population_districts_buildings_and_units():
    big = city(population=7)
    small = city(
        city_id=2,
        population=2,
        districts=["DISTRICT_CITY_CENTER@1,1", "DISTRICT_CAMPUS@2,1", "DISTRICT_HOLY_SITE@3,1"],
        buildings=["PALACE", "BUILDING_TEMPLE"],
    )
    state = build_state([big, small], [unit(), unit("UNIT_SLINGER"), unit("UNIT_ARCHER")])
    assert (state.population, state.biggest_city_population, state.cities) == (9, 7, 2)
    assert state.districts == {"CAMPUS": 2, "HOLY_SITE": 1}  # City centres left out.
    assert state.buildings["TEMPLE"] == 1
    assert state.units == {"SLINGER": 2, "ARCHER": 1}


def test_land_combat_leaves_out_scouts_ships_and_civilians():
    units = [
        unit("UNIT_WARRIOR"),
        unit("UNIT_SCOUT"),
        unit("UNIT_GALLEY"),
        unit("UNIT_BUILDER", combat_strength=0),
        unit("UNIT_ARCHER", combat_strength=0, ranged_strength=25),
    ]
    assert build_state([city()], units).land_combat_units == 2


def test_no_cities_and_no_units_give_an_empty_state_not_an_error():
    state = build_state()
    assert (state.population, state.biggest_city_population, state.land_combat_units) == (0, 0, 0)


def test_state_carries_builder_kill_and_meeting_counters():
    state = build_state(
        [city()],
        built={"QUARRY": 1, "FISHING_BOATS": 2},
        built_on_resource={"QUARRY": 1},
        kills={"BARBARIAN": 2},
        civilizations_met=1,
        city_states_met=3,
        has_pantheon=True,
    )
    assert state.improvements == {"QUARRY": 1, "FISHING_BOATS": 2}
    assert state.sea_resources_improved == 2
    assert state.resource_improvements == {"QUARRY": 1}
    assert state.kills == {"BARBARIAN": 2}
    assert (state.civilizations_met, state.city_states_met, state.has_pantheon) == (1, 3, True)


def test_resource_improvements_are_unknown_without_a_builder():
    assert build_state([city()], built=None, built_on_resource=None).resource_improvements is None


def test_state_does_not_alias_the_builders_counters():
    built = {"QUARRY": 1}
    state = build_state([city()], built=built)
    built["QUARRY"] = 5
    assert state.improvements == {"QUARRY": 1}


# ----- growth -----


def test_growth_rate_adds_up_growing_cities():
    cities = [city(turns_to_grow=4), city(city_id=2, turns_to_grow=4)]
    overall, _ = bi.growth_rates(cities)
    assert overall == pytest.approx(2.0)


def test_a_stalled_city_is_not_counted_as_growing():
    stalled = [
        city(turns_to_grow=0),
        city(city_id=2, food_surplus=0.0),
        city(city_id=3, turns_to_grow=bi.MAX_GROWTH_TURNS + 1),
    ]
    assert bi.growth_rates(stalled) == (None, 0.0)


def test_the_city_rate_is_the_biggest_city_not_the_fastest():
    cities = [city(population=8, turns_to_grow=10), city(city_id=2, population=3, turns_to_grow=2)]
    overall, biggest = bi.growth_rates(cities)
    assert biggest == 10.0
    assert overall < 2.0  # The small city grows fast, which does not help a city-size boost.


def test_a_biggest_city_that_is_not_growing_has_a_known_stalled_rate():
    cities = [city(population=8, food_surplus=0.0), city(city_id=2, population=3, turns_to_grow=2)]
    overall, biggest = bi.growth_rates(cities)
    assert biggest == 0.0 and overall is not None


def test_a_stalled_biggest_city_does_not_borrow_the_other_cities_growth():
    cities = [city(population=4, food_surplus=0.0), city(city_id=2, population=2, turns_to_grow=2)]
    option = ResearchOption(
        "T", "Pottery", 60, 0, 12, False, "Grow a city to 5 population.", (), ""
    )
    state = build_state(cities)
    signals = PlanSignals(*bi.growth_rates(cities))
    assert (
        judge_boosts([option], state, signals)[0].decision is Decision.RESEARCH_NOW
    )  # Not approaching, so no wait.


def test_a_tied_leader_that_is_not_growing_does_not_hold_the_other_back():
    cities = [city(population=5, food_surplus=0.0), city(city_id=2, population=5, turns_to_grow=3)]
    assert bi.growth_rates(cities)[1] == 3.0


def test_no_cities_have_no_growth():
    assert bi.growth_rates([]) == (None, None)


def test_unique_districts_count_as_their_base_kind():
    state = build_state(
        [city(districts=["DISTRICT_SEOWON@1,1", "DISTRICT_ROYAL_NAVY_DOCKYARD@2,1"])]
    )
    assert state.districts.get("CAMPUS") == 1 and state.districts.get("HARBOR") == 1
    queued, *_ = bi.queued_by_kind([city(currently_building="DISTRICT_SEOWON")])
    assert queued == {"CAMPUS": 1}


def test_city_growth_rate_is_used_for_a_city_boost_and_the_total_for_the_rest():
    option = ResearchOption(
        "T", "Pottery", 60, 0, 12, False, "Grow a city to 5 population.", (), ""
    )
    state = BoostState(turn=20, biggest_city_population=3, population=3)
    fast_total = PlanSignals(growth_turns_per_pop=1.0, city_growth_turns_per_pop=9.0)
    assert judge_boosts([option], state, fast_total)[0].decision is Decision.RESEARCH_NOW
    no_city_rate = PlanSignals(growth_turns_per_pop=1.0)
    assert judge_boosts([option], state, no_city_rate)[0].decision is Decision.DELAY


# ----- queued items, offers, pantheon, research -----


def test_queued_items_by_kind_split_wonders_from_buildings():
    cities = [
        city(currently_building="DISTRICT_CAMPUS"),
        city(city_id=2, currently_building="UNIT_SLINGER"),
        city(city_id=3, currently_building="BUILDING_TEMPLE"),
        city(city_id=4, currently_building="BUILDING_PYRAMIDS"),
        city(city_id=5, currently_building="nothing"),
    ]
    districts, units, buildings, wonder = bi.queued_by_kind(cities)
    assert (dict(districts), dict(units), dict(buildings), wonder) == (
        {"CAMPUS": 1},
        {"SLINGER": 1, "LAND_COMBAT": 1},
        {"TEMPLE": 1},
        True,
    )


def test_nothing_queued_when_every_city_is_idle():
    districts, units, buildings, wonder = bi.queued_by_kind([city(currently_building="NONE")])
    assert not districts and not units and not buildings and not wonder


def attacker(unit_type="UNIT_SLINGER", targets=("UNIT_WARRIOR@5,5(100hp)",), **changes):
    return unit(unit_type, targets=[f"{t}" for t in targets], **changes)


def test_a_barbarian_in_range_is_one_offer_however_many_units_could_take_it():
    units = [attacker(unit_id=1), attacker("UNIT_ARCHER", unit_id=2)]
    offers = bi.kill_offers(units, [threat()])
    assert offers == {"SLINGER": 1, "ARCHER": 1, "BARBARIAN": 1}


def test_a_weaker_hurt_or_peaceful_case_is_not_an_offer():
    weak = attacker(combat_strength=5)
    hurt = attacker(health=40)
    assert bi.kill_offers([weak], [threat(combat=8)]) == {}
    assert bi.kill_offers([hurt], [threat()]) == {}
    assert bi.kill_offers([attacker()], [threat(hostile=False)]) == {}
    assert bi.kill_offers([attacker()], []) == {}  # The scan does not show it.


def test_a_ranged_attacker_is_judged_by_its_ranged_strength():
    archer = attacker("UNIT_ARCHER", combat_strength=15, ranged_strength=25)
    assert bi.kill_offers([archer], [threat(combat=20)]) == {"ARCHER": 1, "BARBARIAN": 1}


def test_an_offer_on_a_foreign_unit_is_not_a_barbarian_kill():
    offers = bi.kill_offers([attacker()], [threat(owner_id=3)])
    assert offers == {"SLINGER": 1}


def test_civilians_and_scouts_make_no_offers():
    builder = attacker("UNIT_BUILDER", combat_strength=0)
    assert bi.kill_offers([builder], [threat()]) == {}


def test_pantheon_turns():
    assert bi.pantheon_turns(False, 5.0, 2.0) == 10
    assert bi.pantheon_turns(False, 30.0, 2.0) == 1  # Enough already: this turn or next.
    assert bi.pantheon_turns(False, 5.0, 0.0) is None  # No faith income.
    assert bi.pantheon_turns(True, 5.0, 2.0) is None


def test_research_turns_keys_match_the_boost_target_names():
    research = run(read_research(fx.FakeGame()))
    turns = bi.research_turns(research)
    assert turns == {"BRONZE_WORKING": 5}  # The civic "None" with 0 turns is left out.


# ----- the provider -----


class Builderish:
    def __init__(self, turn=12, jobs=(), claimed=()):
        self.built = {"QUARRY": 1}
        self.built_on_resource = {"QUARRY": 1}
        self.open_jobs = list(jobs)
        self.claimed = frozenset(claimed)
        self.jobs_turn = turn


def provider(game=None, builder=None, military=None):
    game = game or fx.FakeGame(overview=fx.overview(turn=12))
    return game, bi.BoostInputs(lambda: game, builder, military)


def test_provider_builds_both_inputs_from_shared_reads():
    military = SimpleNamespace(kills={"BARBARIAN": 1})
    builder = Builderish(jobs=[job()], claimed=[])
    game, read = provider(builder=builder, military=military)
    state, signals = run(read())
    assert state.turn == 12 and state.population == 10  # Two cities of 5.
    assert state.civilizations_met == 1  # Rome met, Egypt not.
    assert state.city_states_met == 2
    assert state.kills == {"BARBARIAN": 1}
    assert state.improvements == {"QUARRY": 1}
    assert signals.improvement_sites == {"QUARRY": ("the stone near Rome",)}
    assert signals.research_turns == {"BRONZE_WORKING": 5}
    # Only reads other policies make: no builder-task or map-area read.
    assert not {"get_builder_tasks", "get_map_area", "get_diary_snapshot"} & set(game.calls)


def test_provider_ignores_jobs_that_are_too_old():
    stale = Builderish(turn=12 - bi.MAX_JOB_AGE - 1, jobs=[job()])
    _, signals = run(provider(builder=stale)[1]())
    assert signals.improvement_sites == {}
    fresh = Builderish(turn=12 - bi.MAX_JOB_AGE, jobs=[job()])
    assert run(provider(builder=fresh)[1]())[1].improvement_sites


def test_provider_works_without_a_builder_or_military_policy():
    state, signals = run(provider()[1]())
    assert state.resource_improvements is None and state.kills == {}
    assert signals.improvement_jobs == {}


def test_provider_does_not_count_the_player_as_a_met_civ():
    game = fx.FakeGame(
        overview=fx.overview(turn=12),
        civs=[fx.civ_info(player_id=fx.ME), fx.civ_info()],
    )
    assert run(provider(game)[1]())[0].civilizations_met == 1


# ----- research: the provider and the nudge board -----


def tech(name, turns, desc):
    return ResearchOption(f"TECH_{name.upper()}", name, 60, 0, turns, False, desc, (), "")


def research_view(techs):
    return SimpleNamespace(techs=tuple(techs), civics=())


def test_research_publishes_nudges_and_logs_the_horizon(caplog):
    board = NudgeBoard()
    signals = PlanSignals(improvement_sites={"QUARRY": ("the stone near Rome",)})

    async def inputs():
        return BoostState(turn=8), signals

    policy = ResearchPolicy(
        ResearchWeights(priorities=(), boost_judgement=True), inputs, nudge_board=board
    )
    options = [tech("Masonry", 12, "Build a Quarry."), tech("Other", 5, "")]
    with caplog.at_level("INFO"):
        run(policy._boost_adjustments(research_view(options)))
    (nudge,) = board.current(8)
    assert (nudge.target, nudge.site, nudge.policy) == ("QUARRY", "the stone near Rome", "builder")
    assert '"Masonry": ["cheap to cause"' in caplog.text
    assert "Other" not in caplog.text.split("boost_horizon")[1]


def test_off_means_no_read_no_nudge_and_no_log(caplog):
    called = []
    board = NudgeBoard()

    async def inputs():
        called.append(1)
        return BoostState(), PlanSignals()

    policy = ResearchPolicy(ResearchWeights(priorities=()), inputs, nudge_board=board)
    with caplog.at_level("INFO"):
        assert (
            run(policy._boost_adjustments(research_view([tech("A", 5, "Build a Quarry.")]))) == {}
        )
    assert not called and board.items == () and "boost_horizon" not in caplog.text


def test_a_failing_provider_costs_nothing_but_a_log_line(caplog):
    async def broken():
        raise ValueError("bad read")

    policy = ResearchPolicy(ResearchWeights(priorities=(), boost_judgement=True), broken)
    with caplog.at_level("INFO"):
        assert run(policy._boost_adjustments(research_view([tech("A", 5, "")]))) == {}
    assert "Reading the boost judgement inputs failed" in caplog.text


def test_a_lost_connection_is_not_swallowed():
    async def gone():
        raise ConnectionError("down")

    policy = ResearchPolicy(ResearchWeights(priorities=(), boost_judgement=True), gone)
    with pytest.raises(ConnectionError):
        run(policy._boost_adjustments(research_view([tech("A", 5, "")])))


def test_horizon_line_lists_only_the_near_horizon():
    state = BoostState(turn=9, improvements={"QUARRY": 0})
    signals = PlanSignals(improvement_sites={"QUARRY": ("the stone near Rome",)})
    options = [tech("Masonry", 12, "Build a Quarry."), tech("Far", 12, "Meet 3 city-states.")]
    line = horizon_line(judge_boosts(options, state, signals), 9)
    assert list(line["items"]) == ["Masonry"]
    assert line["turn"] == 9


def test_nudge_board_hands_out_only_fresh_nudges():
    board = NudgeBoard()
    nudge = Nudge("T", "A", "do it", "builder", 2.0)
    assert board.current(5) == ()
    board.publish(5, [nudge])
    assert board.current(5) == (nudge,) and board.current(6) == (nudge,)
    assert board.current(7) == ()  # Stale.
    assert board.current(4) == ()  # From the future: never.


def test_a_nudge_carries_its_target_and_site():
    option = tech("Masonry", 12, "Build a Quarry.")
    signals = PlanSignals(improvement_sites={"QUARRY": ("the stone near Rome",)})
    (n,) = nudges(judge_boosts([option], BoostState(turn=8), signals))
    assert (n.target, n.site) == ("QUARRY", "the stone near Rome")
    assert n.action == "build a quarry on the stone near Rome"


# ----- builder: counters and the nudge bonus -----


def nudge(site="the stone near Rome", target="QUARRY", policy="builder"):
    return Nudge("TECH_MASONRY", "Masonry", "build a quarry", policy, 3.0, target, site)


def quarry_task(x, y):
    return task(x=x, y=y, improvement="IMPROVEMENT_QUARRY", resource="STONE", cls="bonus")


def farm_task(x, y):
    return task(x=x, y=y, improvement="IMPROVEMENT_FARM", resource="WHEAT", cls="bonus")


def stone_job_game(**overrides):
    # A farm and a quarry, both on a resource of the same class, equally far from the builder.
    jobs = [farm_task(10, 11), quarry_task(11, 10)]
    return ActionGame(jobs=jobs, builders=[worker(x=10, y=10)], owned=[], **overrides)


def visible(game):
    game.owned = [
        lq.OwnedResource("STONE", "bonus", False, 11, 10),
        lq.OwnedResource("WHEAT", "bonus", False, 10, 11),
    ]
    return game


def first_improvement(policy, game):
    run(policy.run(game))
    return next(d.improvement for d in policy.decisions if d.action in ("move", "improve"))


def builder_with(use, nudges_=None):
    return BuilderPolicy(
        settings=BuilderSettings(use_boost_nudges=use),
        nudges=lambda turn: nudges_ if nudges_ is not None else [],
    )


def test_a_matching_nudge_wins_a_tie_between_two_jobs():
    quarry_nudge = [nudge(site="the stone near Alpha")]
    plain = first_improvement(builder_with(False, quarry_nudge), visible(stone_job_game()))
    nudged = first_improvement(builder_with(True, quarry_nudge), visible(stone_job_game()))
    assert nudged == "IMPROVEMENT_QUARRY"
    assert plain == "IMPROVEMENT_FARM"  # Same distance and class: the sort order picks the farm.


def test_the_setting_off_ignores_every_nudge():
    policy = builder_with(False, [nudge(site="the stone near Alpha")])
    run(policy.run(visible(stone_job_game())))
    assert policy._nudged == set()


def test_nudges_for_other_policies_or_no_site_are_ignored():
    for odd in (nudge("the stone near Alpha", policy="production"), nudge(site="")):
        policy = builder_with(True, [odd])
        run(policy.run(visible(stone_job_game())))
        assert policy._nudged == set()


def test_no_nudge_source_means_no_bonus_even_with_the_setting_on():
    policy = BuilderPolicy(settings=BuilderSettings(use_boost_nudges=True))
    run(policy.run(visible(stone_job_game())))
    assert policy._nudged == set()


def test_the_bonus_is_bounded_and_flat():
    weights = BuilderWeights()
    two = [nudge(site="the stone near Alpha"), nudge(site="the stone near Alpha", target="ANY")]
    policy = builder_with(True, two)
    run(policy.run(visible(stone_job_game())))
    quarry = BuilderJob(
        "normal", 11, 10, "IMPROVEMENT_QUARRY", "STONE", "bonus", "Alpha", None, None
    )
    assert policy._bonus(quarry) == weights.nudge_bonus  # Not doubled by a second nudge.
    # And never enough to lift a plain job over a strategic resource or a repair.
    assert weights.normal + weights.nudge_bonus < weights.urgent + weights.strategic


def test_a_nudge_cannot_pull_a_builder_off_a_strategic_job():
    iron = task("urgent", 11, 10, "IMPROVEMENT_MINE", "IRON", "strategic")
    jobs = [iron, quarry_task(10, 11)]
    game = ActionGame(jobs=jobs, builders=[worker(x=10, y=10)])
    game.owned = [
        lq.OwnedResource("IRON", "strategic", False, 11, 10),
        lq.OwnedResource("STONE", "bonus", False, 10, 11),
    ]
    policy = builder_with(True, [nudge(site="the stone near Alpha")])
    assert first_improvement(policy, game) == "IMPROVEMENT_MINE"


def test_builder_counts_an_improvement_once_per_tile_and_type():
    game = ActionGame(jobs=[quarry_task(10, 10)], builders=[worker(x=10, y=10)])
    game.owned = [lq.OwnedResource("STONE", "bonus", False, 10, 10)]
    policy = BuilderPolicy()
    run(policy.run(game))
    run(policy.run(game))  # The same job worked a second turn.
    assert policy.built == {"QUARRY": 1}
    assert policy.built_on_resource == {"QUARRY": 1}


def test_builder_counts_a_plain_improvement_off_resource_and_skips_repairs():
    game = ActionGame(jobs=[task(x=10, y=10)], builders=[worker(x=10, y=10)])
    policy = BuilderPolicy()
    run(policy.run(game))
    assert policy.built == {"FARM": 1} and policy.built_on_resource == {}
    repairs = ActionGame(
        jobs=[repair_task(10, 10)],
        builders=[worker(x=10, y=10)],
    )
    again = BuilderPolicy()
    run(again.run(repairs))
    assert again.built == {}


def test_a_refused_improvement_is_not_counted():
    game = ActionGame(jobs=[task(x=10, y=10)], builders=[worker(x=10, y=10)])
    game.replies["improve"] = "ERR:CANNOT_IMPROVE|nope"
    policy = BuilderPolicy()
    run(policy.run(game))
    assert policy.built == {}


def test_builder_records_the_jobs_it_saw_and_what_it_holds():
    game = visible(ActionGame(jobs=[quarry_task(11, 10)], builders=[worker(x=10, y=10)]))
    policy = BuilderPolicy()
    run(policy.run(game))
    assert policy.jobs_turn == 5
    assert [j.improvement for j in policy.open_jobs] == ["IMPROVEMENT_QUARRY"]
    assert policy.claimed == frozenset({(11, 10, "IMPROVEMENT_QUARRY")})


def test_a_job_just_worked_is_built_not_also_on_offer_or_held():
    game = ActionGame(jobs=[quarry_task(10, 10)], builders=[worker(x=10, y=10)])
    game.owned = [lq.OwnedResource("STONE", "bonus", False, 10, 10)]
    policy = BuilderPolicy()
    run(policy.run(game))
    assert policy.built == {"QUARRY": 1}
    assert policy.open_jobs == [] and policy.claimed == frozenset()


def test_builder_with_no_free_builder_keeps_its_last_view():
    game = visible(ActionGame(jobs=[quarry_task(11, 10)], builders=[]))
    policy = BuilderPolicy()
    run(policy.run(game))
    assert policy.jobs_turn is None and policy.open_jobs == []


# ----- military: kill counters -----


def military_turn(units, killed, turn=7):
    return _Turn(turn, tuple(units), (), (), list(units), frozenset(), killed=set(killed))


def test_military_counts_a_kill_by_its_killers_type_and_as_a_barbarian():
    policy = MilitaryPolicy()
    slinger = unit(unit_id=1)
    policy.decisions.append(MilitaryDecision(7, 1, 4, 5, "attack", "attack", target=(5, 5)))
    policy._count_kills(military_turn([slinger], {(5, 5)}), {(5, 5)})
    assert policy.kills == {"BARBARIAN": 1, "SLINGER": 1}


def test_military_counts_a_tile_once_even_if_run_twice_in_a_turn():
    policy = MilitaryPolicy()
    policy.decisions.append(MilitaryDecision(7, 1, 4, 5, "attack", "attack", target=(5, 5)))
    t = military_turn([unit(unit_id=1)], {(5, 5)})
    policy._count_kills(t, {(5, 5)})
    policy._count_kills(t, {(5, 5)})
    assert policy.kills["BARBARIAN"] == 1


def test_a_refused_attack_or_a_city_strike_gives_no_killer_type():
    policy = MilitaryPolicy()
    policy.decisions += [
        MilitaryDecision(7, 1, 4, 5, "attack", "attack refused", target=(5, 5)),
        MilitaryDecision(7, 2, 4, 5, "city_strike", "strike", target=(6, 6)),
    ]
    policy._count_kills(military_turn([unit(unit_id=1)], {(5, 5), (6, 6)}), {(5, 5)})
    assert policy.kills == {"BARBARIAN": 1}  # No unit type was credited.


def test_a_non_barbarian_kill_is_not_counted_as_one():
    policy = MilitaryPolicy()
    policy.decisions.append(MilitaryDecision(7, 1, 4, 5, "attack", "attack", target=(5, 5)))
    policy._count_kills(military_turn([unit(unit_id=1)], {(5, 5)}), set())
    assert policy.kills == {"SLINGER": 1}


def test_cleared_camps_count_as_outposts():
    policy = MilitaryPolicy()
    policy._camps_ours = 1
    policy._count_kills(military_turn([], set()), set())
    assert policy.kills == {"BARBARIAN_OUTPOST": 1}


# ----- the bot -----


def test_bot_wires_the_provider_and_board_but_leaves_everything_off(tmp_path):
    orchestrator = bot.build_orchestrator(
        fx.FakeGame(), EventBus(), TurnLog(tmp_path / "game.jsonl")
    )
    handlers = orchestrator._handlers
    (research,) = [h for h in handlers if isinstance(h, ResearchPolicy)]
    (builder,) = [h for h in handlers if isinstance(h, BuilderPolicy)]
    assert isinstance(research.boost_inputs, bi.BoostInputs)
    assert research.boost_inputs.builder is builder
    assert research.weights.boost_judgement is False
    assert builder.settings.use_boost_nudges is False
    research.nudge_board.publish(3, [nudge()])
    assert builder.nudges(3) == (nudge(),)  # The same board.


def test_bot_provider_reads_through_the_shared_game(tmp_path):
    game = fx.FakeGame(overview=fx.overview(turn=4))
    orchestrator = bot.build_orchestrator(game, EventBus(), TurnLog(tmp_path / "game.jsonl"))
    (research,) = [h for h in orchestrator._handlers if isinstance(h, ResearchPolicy)]
    state, _ = run(research.boost_inputs())
    assert state.turn == 4
    calls = len(game.calls)
    run(research.boost_inputs())
    assert all(c.startswith("get_") for c in game.calls)  # Reads only, never an action.
    assert len(game.calls) >= calls


# ----- review fixes: resource mines, one job per nudge -----


def test_an_iron_mine_job_counts_as_an_iron_mine_not_a_plain_mine():
    iron = job("IMPROVEMENT_MINE", resource="IRON")
    open_ = bi.improvement_signals([iron], frozenset())
    assert open_.sites == {"IRON_MINE": ("the iron near Rome",)}
    assert bi.job_matches(iron, "IRON_MINE", "the iron near Rome")
    assert bi.job_matches(iron, "MINE", "the iron near Rome")
    assert not bi.job_matches(iron, "NITER_MINE", "the iron near Rome")
    claimed = bi.improvement_signals([iron], frozenset({(10, 11, "IMPROVEMENT_MINE")}))
    assert claimed.jobs == {"IRON_MINE": 1}  # Once, so the total does not double count.


def test_an_iron_mine_boost_sees_the_iron_job_and_a_plain_mine_boost_does_too():
    state = BoostState(turn=20)
    signals = PlanSignals(improvement_jobs={"IRON_MINE": 1})
    for text in ("Build an Iron Mine.", "Build a Mine."):
        option = ResearchOption("T", "Bronze Working", 60, 0, 12, False, text, (), "")
        assert judge_boosts([option], state, signals)[0].decision is Decision.DELAY


def test_one_nudge_picks_one_job_not_every_job_that_reads_the_same():
    jobs = [job(x=10, y=11), job(x=12, y=11), job(x=13, y=11)]
    policy = builder_with(True, [nudge(site="the stone near Rome")])
    assert len(policy._nudged_jobs(jobs, 3)) == 1


def test_a_district_being_built_is_not_counted_as_done():
    building = city(districts=["DISTRICT_CAMPUS@2,1"], currently_building="DISTRICT_CAMPUS")
    done = city(city_id=2, districts=["DISTRICT_CAMPUS@4,1"], currently_building="UNIT_SLINGER")
    state = build_state([building, done])
    assert state.districts.get("CAMPUS") == 1
    seowon = city(districts=["DISTRICT_SEOWON@2,1"], currently_building="DISTRICT_SEOWON")
    assert "CAMPUS" not in build_state([seowon]).districts


def test_a_resource_mine_job_is_seen_by_the_mine_a_resource_boost():
    option = ResearchOption("T", "Wheel", 60, 0, 12, False, "Mine a resource.", (), "")
    state = BoostState(turn=20, resource_improvements={})
    signals = PlanSignals(resource_improvement_jobs={"IRON_MINE": 1})
    assert judge_boosts([option], state, signals)[0].decision is Decision.DELAY


def test_unique_units_count_as_the_unit_they_replace():
    state = build_state(units=[unit("UNIT_NUBIAN_PITATI"), unit("UNIT_ARCHER")])
    assert state.units == {"ARCHER": 2}
    _, queued, _, _ = bi.queued_by_kind([city(currently_building="UNIT_NUBIAN_PITATI")])
    assert queued == {"ARCHER": 1, "LAND_COMBAT": 1}


def test_queued_land_combat_units_have_an_aggregate_and_civilians_do_not_count():
    cities = [
        city(currently_building="UNIT_SLINGER"),
        city(city_id=2, currently_building="UNIT_BUILDER"),
        city(city_id=3, currently_building="UNIT_GALLEY"),
    ]
    _, queued, _, _ = bi.queued_by_kind(cities)
    assert queued["LAND_COMBAT"] == 1
    option = ResearchOption(
        "T", "Bronze", 60, 0, 12, False, "Have 8 land combat units in your military.", (), ""
    )
    state = BoostState(turn=20, land_combat_units=7)
    assert (
        judge_boosts([option], state, PlanSignals(units_queued=dict(queued)))[0].decision
        is Decision.DELAY
    )
