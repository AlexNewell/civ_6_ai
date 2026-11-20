from civ6_ai.state.models import ResearchOption
from civ6_ai.strategy.boost_judgement import (
    DEFAULT_SETTINGS,
    BoostState,
    Decision,
    DelayRecord,
    PlanSignals,
    ReachLabel,
    judge_boosts,
    next_history,
    nudges,
)

POP6 = "Grow your civilization to at least 6 population."


def item(name, desc, turns=10, boosted=False, progress=0):
    return ResearchOption(
        f"TECH_{name.upper().replace(' ', '_')}", name, 60, progress, turns, boosted, desc, (), ""
    )


def one(option, state=None, signals=None, history=None, settings=DEFAULT_SETTINGS):
    state = state or BoostState(turn=20)
    return judge_boosts([option], state, signals or PlanSignals(), settings, history)[0]


# The owner's examples from #322.


def test_early_empire_is_delayed_while_population_grows_to_six():
    option = item("Early Empire", POP6, turns=10)
    j = one(option, BoostState(turn=20, population=4), PlanSignals(growth_turns_per_pop=2.0))
    assert j.decision is Decision.DELAY
    assert j.reach.label is ReachLabel.ON_TRACK
    assert j.reach.turns == 4
    assert j.reason == "delaying Early Empire: boost in about 4 turns from population growth"
    assert j.value == 4.0 - 0.5 * 4


def test_state_workforce_is_delayed_while_a_district_is_queued():
    option = item("State Workforce", "Build any specialty district.", turns=15)
    j = one(option, signals=PlanSignals(districts_queued={"CAMPUS": 1}))
    assert j.decision is Decision.DELAY
    assert j.reach.source == "a queued district"


def test_state_workforce_is_researched_when_no_district_is_queued():
    option = item("State Workforce", "Build any specialty district.", turns=15)
    assert one(option).decision is Decision.RESEARCH_NOW


def test_drama_and_poetry_is_researched_when_the_boost_is_far():
    j = one(item("Drama and Poetry", "Build a wonder."))
    assert j.decision is Decision.RESEARCH_NOW
    assert j.reach.label is ReachLabel.UNLIKELY


def test_population_too_far_off_is_not_waited_for():
    j = one(
        item("Early Empire", POP6),
        BoostState(turn=20, population=1),
        PlanSignals(growth_turns_per_pop=6.0),
    )
    assert j.decision is Decision.RESEARCH_NOW


def test_archery_nudges_for_an_offered_kill_then_researches_when_none_comes():
    option = item("Archery", "Kill a unit with a Slinger.", turns=10)
    j = one(option, signals=PlanSignals(kill_offers={"SLINGER": 1}))
    assert j.decision is Decision.NUDGE
    assert j.reach.label is ReachLabel.CHEAP
    assert j.reach.nudge == "kill a unit with a slinger"
    later = one(option, signals=PlanSignals())
    assert later.decision is Decision.RESEARCH_NOW
    assert later.reach.label is ReachLabel.UNLIKELY


def test_masonry_nudges_a_quarry_on_the_stone():
    option = item("Masonry", "Build a Quarry.", turns=8)
    signals = PlanSignals(improvement_sites={"QUARRY": ("the stone near Rome",)})
    j = one(option, signals=signals)
    assert j.decision is Decision.NUDGE
    assert j.reach.nudge == "build a quarry on the stone near Rome"
    assert j.reach.policy == "builder"
    (n,) = nudges([j])
    assert (n.name, n.action, n.policy) == ("Masonry", j.reach.nudge, "builder")


def test_a_queued_quarry_job_is_on_track_not_a_nudge():
    option = item("Masonry", "Build a Quarry.", turns=8)
    j = one(option, signals=PlanSignals(improvement_jobs={"QUARRY": 1}))
    assert j.decision is Decision.DELAY
    assert j.reach.turns == 3


def test_no_nudge_when_the_detour_costs_more_than_the_saving():
    option = item("Masonry", "Build a Quarry.", turns=2)  # Saves 0.8 turns, detour costs 1.
    signals = PlanSignals(improvement_sites={"QUARRY": ("the stone near Rome",)})
    j = one(option, signals=signals)
    assert j.decision is Decision.RESEARCH_NOW
    assert "detour" in j.reason
    assert nudges([j]) == ()


def test_craftsmanship_waits_for_builder_jobs_covering_three_tiles():
    option = item("Craftsmanship", "Improve 3 tiles.", turns=12)
    state = BoostState(turn=20, improvements={"FARM": 1})
    jobs = PlanSignals(improvement_jobs={"MINE": 1, "PASTURE": 1})
    assert one(option, state, jobs).decision is Decision.DELAY
    assert one(option, state).decision is Decision.RESEARCH_NOW


def test_sea_resources_without_a_buildable_boat_are_unlikely():
    option = item("Celestial Navigation", "Improve 2 sea resources.")
    assert one(option).reach.label is ReachLabel.UNLIKELY


# Rules of the model.


def test_an_earned_boost_is_just_researched():
    option = item("Archery", "Kill a unit with a Slinger.", boosted=True)
    j = one(option, signals=PlanSignals(kill_offers={"SLINGER": 1}))
    assert (j.decision, j.reach.label) == (Decision.RESEARCH_NOW, ReachLabel.EARNED)


def test_an_unknown_description_is_researched_without_a_guess():
    assert one(item("Mystery", "Do something new.")).decision is Decision.RESEARCH_NOW


def test_an_item_well_under_way_is_not_delayed():
    j = one(
        item("Early Empire", POP6, progress=50),
        BoostState(turn=20, population=4),
        PlanSignals(growth_turns_per_pop=2.0),
    )
    assert j.decision is Decision.RESEARCH_NOW


def test_waiting_that_saves_too_little_is_not_worth_it():
    j = one(
        item("Early Empire", POP6, turns=3),
        BoostState(turn=20, population=4),
        PlanSignals(growth_turns_per_pop=3.0),
    )
    assert j.decision is Decision.RESEARCH_NOW


def test_wait_is_bounded_by_max_delay():
    option = item("Early Empire", POP6, turns=30)
    state = BoostState(turn=20, population=4)
    signals = PlanSignals(growth_turns_per_pop=5.0)  # 10 turns: beyond max_delay of 8.
    assert one(option, state, signals).decision is Decision.RESEARCH_NOW


def test_give_up_when_the_estimate_slips():
    option = item("Early Empire", POP6, turns=20)
    record = DelayRecord(since_turn=17, first_eta=4)  # Waited 3 turns; 1 should remain.
    j = one(
        option,
        BoostState(turn=20, population=4),
        PlanSignals(growth_turns_per_pop=4.0),  # Now 8 turns away.
        {option.item_type: record},
    )
    assert j.decision is Decision.RESEARCH_NOW
    assert j.gave_up
    assert "slipped" in j.reason


def test_keep_waiting_when_the_estimate_holds():
    option = item("Early Empire", POP6, turns=20)
    record = DelayRecord(since_turn=17, first_eta=4)
    j = one(
        option,
        BoostState(turn=20, population=5),
        PlanSignals(growth_turns_per_pop=1.0),
        {option.item_type: record},
    )
    assert j.decision is Decision.DELAY


def test_give_up_after_max_delay_turns_even_if_on_schedule():
    option = item("Early Empire", POP6, turns=20)
    record = DelayRecord(since_turn=10, first_eta=12)
    j = one(
        option,
        BoostState(turn=20, population=5),
        PlanSignals(growth_turns_per_pop=1.0),
        {option.item_type: record},
    )
    assert j.decision is Decision.RESEARCH_NOW and j.gave_up


def test_history_records_delays_and_remembers_giving_up():
    option = item("Early Empire", POP6, turns=20)
    state = BoostState(turn=10, population=4)
    signals = PlanSignals(growth_turns_per_pop=2.0)
    first = judge_boosts([option], state, signals)
    history = next_history({}, first, 10)
    assert history[option.item_type] == DelayRecord(10, 4)
    # Still delayed next turn: the first record stands.
    second = judge_boosts([option], BoostState(turn=11, population=4), signals, history=history)
    assert next_history(history, second, 11)[option.item_type] == DelayRecord(10, 4)
    # Once given up, never delayed again for the same item.
    gave = DelayRecord(10, 4, gave_up=True)
    again = judge_boosts([option], state, signals, history={option.item_type: gave})
    assert again[0].decision is Decision.RESEARCH_NOW
    assert next_history({option.item_type: gave}, again, 12)[option.item_type].gave_up


def test_history_drops_items_no_longer_offered():
    assert next_history({"TECH_GONE": DelayRecord(1, 3)}, [], 5) == {}


def test_kill_barbarians_counts_kills_so_far():
    option = item("Bronze Working", "Kill 3 Barbarians.")
    state = BoostState(turn=20, kills={"BARBARIAN": 2})
    assert one(option, state, PlanSignals(kill_offers={"BARBARIAN": 1})).decision is Decision.NUDGE
    assert one(option, state).decision is Decision.RESEARCH_NOW


def test_units_queued_make_a_unit_boost_on_track():
    option = item("Shipbuilding", "Own 2 Galleys.", turns=12)
    state = BoostState(turn=20, units={"GALLEY": 1})
    assert one(option, state, PlanSignals(units_queued={"GALLEY": 1})).decision is Decision.DELAY
    assert one(option, state).decision is Decision.NUDGE  # One more is a small detour.
    assert one(option, BoostState(turn=20)).decision is Decision.RESEARCH_NOW  # Two more is not.


def test_pantheon_and_meeting_boosts():
    pantheon = item("Mysticism", "Found a pantheon.", turns=12)
    assert one(pantheon, signals=PlanSignals(pantheon_turns=3)).decision is Decision.DELAY
    assert one(pantheon).decision is Decision.RESEARCH_NOW
    meet = item("Writing", "Meet another civilization.", turns=12)
    assert one(meet, signals=PlanSignals(exploring=True)).decision is Decision.DELAY
    assert one(meet).decision is Decision.RESEARCH_NOW
