"""Issue 398: the camp mission, its phases and the failure modes of the earlier attempts."""

import dataclasses
import json
import logging
import types

from civ6_ai.camp_mission import (
    CampMission,
    Discoverer,
    MissionBook,
    MissionSettings,
    Phase,
    abort_verdict,
    assign_roles,
    choose_discoverer,
    choose_muster,
    commit_verdict,
    facts_improved,
    margin,
    muster_candidates,
    muster_score,
    progressed,
    setback_events,
    should_reopen,
)
from civ6_ai.policies import military as military_module
from civ6_ai.policies.military import MilitaryPolicy
from civ6_ai.state.models import Threat
from civ6_ai.threat_map import Ally, ThreatMap
from fixtures.civ_state import city_info
from test_policy_military import (
    BAY_WALL,
    STRONG,
    MilitaryGame,
    _land,
    _store,
    _turn_for,
    barbarian,
    camp_game,
    run,
    scout,
    warrior,
)

S = MissionSettings()


# ----- commit and abort are weighed, not filtered -----


def test_a_big_margin_commits_and_a_long_walk_or_risk_can_hold_it_back():
    assert commit_verdict(9, 6, 1, 0.0, S).go
    assert not commit_verdict(6, 6, 4, 0.0, S).go  # Just enough, far from the camp.
    assert not commit_verdict(6, 6, 1, 0.8, S).go  # Just enough, hostiles on the force.
    assert commit_verdict(12, 6, 1, 0.8, S).go  # A big margin outweighs the same risk.
    assert margin(6, 0) == 1.0  # An empty camp needs nothing.


def test_the_weights_are_tunable_not_a_rule():
    strict = dataclasses.replace(S, risk_weight=30.0)
    assert commit_verdict(9, 6, 1, 0.5, S).go
    assert not commit_verdict(9, 6, 1, 0.5, strict).go


def test_a_committed_force_is_called_off_by_margin_walk_and_losses_together():
    assert not abort_verdict(6, 6, 0, 0.0, 0, S).go
    assert not abort_verdict(6, 6, 0, 0.0, 1, S).go  # One loss alone does not end it.
    assert abort_verdict(3, 6, 0, 0.0, 2, S).go  # Half the force and two lost does.


def test_a_called_off_mission_reopens_when_the_facts_improve_not_when_time_passes():
    assert abort_verdict(3, 6, 0, 0.0, 2, S).go
    assert not should_reopen(
        3, 6, 2, 0.0, 2, S
    ).go  # The same facts: stays off, any number of turns.
    assert should_reopen(9, 6, 2, 0.0, 0, S).go  # A stronger force reopens it.


# ----- discovery -----


def look(uid, scout_=False, walk=3.0, threat=0.0, strength=20.0):
    return Discoverer(uid, scout_, walk, threat, strength, (1, 1))


def test_a_scout_is_preferred_to_a_soldier_for_the_look():
    best, why = choose_discoverer([look(1), look(2, True)], S)
    assert best.unit_id == 2 and "scout" in why


def test_a_look_out_risk_is_weighed_so_a_soldier_may_beat_a_threatened_scout():
    best, _ = choose_discoverer([look(1, walk=2.0), look(2, True, walk=2.0, threat=12.0)], S)
    assert best.unit_id == 1


def test_nobody_goes_when_every_look_out_is_too_risky_and_the_reason_says_so():
    best, why = choose_discoverer([look(1, threat=30.0), look(2, True, threat=30.0)], S)
    assert best is None and "too risky" in why
    assert choose_discoverer([], S)[0] is None


def test_the_look_out_already_walking_goes_on_at_a_tie():
    best, _ = choose_discoverer([look(1), look(2)], S, current=2)
    assert best.unit_id == 2


# ----- one role per unit -----


def test_a_unit_given_a_role_this_turn_is_not_the_missions():
    units, released = assign_roles({1, 2}, {1: True, 2: True}, {2: "heal"}, (), {1, 2})
    assert units == {1: "march"} and "heal" in released[2]


def test_a_unit_of_a_city_the_threat_map_calls_short_goes_back_to_it():
    # Issue 397's tug of war: the camp force counted the unit free while cover pulled it back.
    units, released = assign_roles({1}, {1: True}, {}, {1}, {1})
    assert units == {} and "short" in released[1]


def test_a_unit_out_of_reach_or_hurt_is_let_go_with_the_reason():
    units, released = assign_roles({1, 2}, {1: True, 2: False}, {}, (), set())
    assert units == {} and set(released) == {1, 2}


# ----- the muster tile is on the walking route -----


def test_muster_candidates_are_on_the_route_inside_the_walk_of_the_camp():
    cost = {(5, 5): 6.0, (6, 5): 4.0, (7, 5): 3.0, (8, 5): 1.0}
    route = [(4, 5), (5, 5), (6, 5), (7, 5), (8, 5)]
    got = muster_candidates(route, cost, 4.0, 0.0, lambda p: p != (6, 5))
    assert got == [(7, 5), (8, 5)]  # Not (5,5) too far, not (6,5) refused, not (4,5) off the field.


def test_muster_score_weighs_walk_nearness_and_exposure_and_keeps_the_old_tile():
    base = muster_score([2.0], 4.0, 0, S)
    assert muster_score([4.0], 4.0, 0, S) > base
    assert muster_score([2.0], 2.0, 0, S) < base
    assert muster_score([2.0], 4.0, 1, S) > base
    scores = {(1, 1): 5.0, (2, 2): 4.5}
    assert choose_muster(scores, (1, 1), 0.75) == (1, 1)
    assert choose_muster(scores, (1, 1), 0.25) == (2, 2)
    assert choose_muster({}, None, 0.75) is None


def test_a_camp_across_water_gets_a_muster_tile_on_the_land_route_not_the_shore():
    policy = MilitaryPolicy()
    policy.settings = dataclasses.replace(policy.settings, map_width=74)
    policy.terrain = _store(_land(BAY_WALL))
    units = [warrior(uid=1, x=10, y=10, **STRONG), warrior(uid=2, x=10, y=11, **STRONG)]
    policy, t = _turn_for(policy, MilitaryGame(units))
    mission = policy._missions.open((14, 10), 20)
    policy._pick_muster(t, mission, t.mine, (14, 10))
    assert mission.muster is not None
    assert mission.muster not in BAY_WALL
    assert policy._walk_cost(mission.muster, (14, 10)) <= policy.settings.camp_muster_walk + 2
    assert mission.muster != (11, 10)  # The shore facing the camp, across the bay.


# ----- the object and its phases -----


def test_a_mission_goes_discover_gather_approach_assault_done():
    book = MissionBook()
    m = book.discover((20, 20), 5)
    assert m.phase is Phase.DISCOVER and not m.confirmed
    assert book.discover((21, 20), 6) is m and m.camp == (21, 20)  # The guess moved, not a second.
    book.confirm((21, 20), (22, 21), 7)
    assert m.phase is Phase.GATHER and m.confirmed and m.camp == (22, 21)
    m.move_to(Phase.APPROACH, 9, "committed")
    assert book.committed((22, 21))
    m.move_to(Phase.ASSAULT, 10, "beside it")
    assert book.committed((22, 21)) and m.since == 10
    book.close((22, 21), 11, "cleared")
    assert m.phase is Phase.DONE and m.outcome == "cleared" and not m.units
    book.sweep()
    assert not book.missions


def test_a_suspicion_that_ends_closes_the_discover_mission_with_no_camp():
    policy = MilitaryPolicy()
    policy._missions.discover((20, 20), 5)
    policy, t = _turn_for(policy, MilitaryGame([warrior()]))
    policy._sync_missions(t, [], None)
    (m,) = policy._missions.missions.values()
    assert m.phase is Phase.DONE and m.outcome == "no_camp"


def test_a_camp_seen_near_the_spot_turns_the_discover_mission_into_a_gather():
    policy = MilitaryPolicy()
    policy._camps[(22, 21)] = 5
    policy._missions.discover((20, 20), 5)
    policy, t = _turn_for(policy, MilitaryGame([warrior()]))
    policy._sync_missions(t, [(22, 21)], (20, 20))
    m = policy._missions.get((22, 21))
    assert m is not None and m.phase is Phase.GATHER and m.confirmed


def test_the_committed_view_follows_the_mission_phase():
    policy = MilitaryPolicy()
    policy._committed[(5, 5)] = 20
    assert (5, 5) in policy._committed and policy._missions.get((5, 5)).phase is Phase.APPROACH
    policy._committed.pop((5, 5), None)
    assert not policy._committed and policy._missions.get((5, 5)).phase is Phase.GATHER


# ----- cover versus the camp force (the tug of war) -----


def _held(policy, t, uid, role="camp"):
    t.roles[uid] = role


def test_a_unit_the_mission_holds_is_not_taken_by_cover_unless_the_city_is_short():
    policy, t = _turn_for(MilitaryPolicy(), MilitaryGame([warrior(uid=1, x=12, y=10)]))
    c = types.SimpleNamespace(city_id=65536, x=10, y=10)
    _held(policy, t, 1)
    assert policy._held_off(t, t.mine[0], c)
    policy._city_short = lambda _c: True  # The threat state calls it short: the mission lets go.
    assert not policy._held_off(t, t.mine[0], c)


def test_a_unit_given_a_city_role_leaves_its_mission_role():
    policy, t = _turn_for(MilitaryPolicy(), MilitaryGame([warrior(uid=1, x=12, y=10)]))
    t.roles[1] = "camp"
    policy._log(t, t.mine[0], "heal", "hurt")
    assert t.roles[1] == "heal"
    t.roles[1] = "camp"
    policy._log(t, t.mine[0], "cover", "city short")
    assert t.roles[1] == "cover"  # Only reachable for a short city: the unit leaves the force.


def test_the_claim_comes_before_other_policies_and_marks_the_marching_units():
    units = [warrior(uid=1, x=12, y=10, **STRONG), warrior(uid=2, x=12, y=11, **STRONG)]
    policy, t = _turn_for(MilitaryPolicy(), MilitaryGame(units))
    m = policy._missions.open((14, 10), 20)
    m.units = {1: "march", 2: "march"}
    policy._claim_missions(t)
    assert t.roles == {1: "camp", 2: "camp"}


# ----- a whole turn -----


def mission_lines(caplog):
    return [
        json.loads(r.getMessage().split(" ", 1)[1])
        for r in caplog.records
        if r.getMessage().startswith("camp_mission ")
    ]


def test_a_known_camp_writes_one_camp_mission_line_per_turn_with_phase_units_and_reason(caplog):
    game = camp_game(
        [
            warrior(uid=1, x=12, y=10, **STRONG),
            warrior(uid=2, x=12, y=11, **STRONG),
            warrior(uid=3),
        ],
        [barbarian(x=15, y=10, cs=5)],
    )
    policy = MilitaryPolicy()
    with caplog.at_level(logging.INFO):
        run(policy, game)
    lines = mission_lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    assert line["camp"] == [14, 10] and line["phase"] in ("approach", "assault", "gather")
    assert set(line["units"]) == {"1", "2"} and line["reason"]
    assert policy._committed  # Two strong units on a weak camp commit.


def test_a_force_too_weak_for_the_camp_waits_in_gather_with_the_reason(caplog):
    game = camp_game(
        [warrior(uid=1, x=12, y=10), warrior(uid=2, x=12, y=11), warrior(uid=3)],
        [barbarian(x=15, y=10, cs=40)],
    )
    with caplog.at_level(logging.INFO):
        run(MilitaryPolicy(), game)
    (line,) = mission_lines(caplog)
    assert line["phase"] == "gather" and "short" in line["reason"]


def test_a_committed_force_that_loses_its_margin_is_called_off_and_waits_for_more(caplog):
    game = camp_game(
        [
            warrior(uid=1, x=12, y=10, **STRONG),
            warrior(uid=2, x=12, y=11, **STRONG),
            warrior(uid=3),
        ],
        [barbarian(x=15, y=10, cs=5)],
    )
    policy = MilitaryPolicy()
    policy._committed[(14, 10)] = 20
    policy._losses[(14, 10)] = [19, 19, 19, 19]  # A bloody campaign: the loss weight does it.
    with caplog.at_level(logging.INFO):
        run(policy, game)
    m = policy._missions.get((14, 10))
    assert m.phase is Phase.ABORT and m.aborted_at is not None
    assert not policy._committed
    assert "military_camp_abort" in caplog.text


# ----- discovery in a turn -----


def map_with_city():
    tm = ThreatMap()
    tm.allies = [Ally("city", 10, 10, 10.0)]
    return tm


def suspecting_map():
    tm = map_with_city()
    for turn, x, y in [(20, 16, 10), (23, 17, 11), (27, 16, 9)]:
        tm.see_threats(
            turn, [Threat(63, "Barbarians", "UNIT_COURSER", x, y, 100, 100, 20, 0, 3, True)]
        )
        tm.sources = []
    return tm


def discovery(units, caplog):
    game = MilitaryGame(units, [], cities=[city_info()])
    policy = MilitaryPolicy()
    policy.threat_map = suspecting_map()
    with caplog.at_level(logging.INFO):
        run(policy, game)
    return policy, game


def test_a_scout_settling_left_with_moves_goes_to_look_and_holds_the_discover_role(caplog):
    units = [warrior(uid=1, x=10, y=10), scout(uid=9, x=9, y=9)]
    policy, game = discovery(units, caplog)
    (decision,) = [d for d in policy.decisions if d.action == "probe"]
    assert decision.unit_id == 9
    (line,) = mission_lines(caplog)
    assert line["phase"] == "discover" and line["units"] == {"9": "discover"}
    assert not line["confirmed"]


def test_with_no_unit_to_send_the_discover_mission_says_why_and_still_asks_for_a_force(caplog):
    policy, game = discovery([warrior(uid=1, x=10, y=10)], caplog)
    (line,) = mission_lines(caplog)
    assert line["phase"] == "discover" and "no unit" in line["reason"]
    assert not [d for d in policy.decisions if d.action == "probe"]


def test_a_discovered_camp_is_planned_for_before_it_is_seen():
    from civ6_ai.policies.defence import DefenceState

    state = DefenceState()
    policy = MilitaryPolicy(defence=state)
    policy.settings = dataclasses.replace(policy.settings, camp_goal_radius=12)
    policy.threat_map = suspecting_map()
    game = MilitaryGame([warrior(uid=1, x=10, y=10)], [], cities=[city_info()])
    run(policy, game)
    assert state.camp_plan is not None and state.camp_plan.units_wanted > 0
    assert military_module  # The plan's target is the inferred spot: production builds the force.
    (m,) = policy._missions.live()
    assert m.shortfall == state.camp_plan.units_wanted and m.phase is Phase.DISCOVER


def test_the_mission_row_is_json_and_names_every_field_the_log_promises():
    m = CampMission((3, 4), since=2)
    row = m.as_row(5)
    assert {"phase", "units", "walk_turns", "reason", "camp", "shortfall"} <= set(row)
    assert row["phase_turns"] == 3
    json.dumps(row)


def test_a_called_off_mission_that_does_not_reopen_holds_no_unit():
    game = camp_game(
        [
            warrior(uid=1, x=12, y=10, **STRONG),
            warrior(uid=2, x=12, y=11, **STRONG),
            warrior(uid=3),
        ],
        [barbarian(x=15, y=10, cs=5)],
    )
    policy = MilitaryPolicy()
    mission = policy._missions.open((14, 10), 19)
    policy._end_mission(types.SimpleNamespace(turn=19), mission, "lost the margin")
    mission.aborted_at = (0.0, 10.0)
    policy._losses[(14, 10)] = [19, 19, 19, 19]
    run(policy, game)
    assert mission.phase is Phase.ABORT and mission.units == {}


def test_confirming_the_camp_frees_the_look_out_to_join_the_force():
    book = MissionBook()
    m = book.discover((20, 20), 5)
    m.units = {7: "discover"}
    book.confirm((20, 20), (21, 21), 6)
    assert m.phase is Phase.GATHER and m.units == {}


def test_a_called_off_mission_does_not_reopen_while_the_hostile_risk_that_ended_it_stands():
    units = [warrior(uid=1, x=12, y=10, **STRONG), warrior(uid=2, x=12, y=11, **STRONG)]
    policy = MilitaryPolicy()
    mission_weights = dataclasses.replace(policy.settings.mission, risk_weight=30.0)
    policy.settings = dataclasses.replace(policy.settings, mission=mission_weights)
    policy, t = _turn_for(policy, MilitaryGame(units))
    mission = policy._missions.open((14, 10), 19)
    # Another nation's soldier beside both units: the whole force is exposed.
    t.threats = (
        Threat(5, "Rival", "UNIT_SWORDSMAN", 13, 10, 100, 100, 30, 0, 3, True),
        Threat(5, "Rival", "UNIT_SWORDSMAN", 13, 11, 100, 100, 30, 0, 3, True),
    )
    assert policy._force_risk(t, t.mine, (14, 10)) == 1.0
    defenders = [Threat(-1, "Barbarians", "UNIT_WARRIOR", 14, 10, 100, 100, 5, 0, 3, True)]
    assert not policy._reopens(t, mission, t.mine, defenders)
    t.threats = ()
    assert policy._reopens(t, mission, t.mine, defenders)


def test_a_unit_the_mission_holds_still_flees_when_exposed_but_does_not_fortify():
    game = MilitaryGame([warrior(x=14, y=10)], [barbarian(x=15, y=10)])
    policy = MilitaryPolicy()
    policy._claim_missions = lambda t: t.roles.update({1: "camp"})  # Held, with no camp order.
    run(policy, game)
    assert game.orders("move") == [("move", 1, 10, 10)]  # The safety flee runs.
    assert [d.action for d in policy.decisions if d.unit_id == 1][-1] == "flee"


def test_switching_targets_releases_the_previous_missions_units():
    units = [warrior(uid=1, x=12, y=10, **STRONG), warrior(uid=2, x=12, y=11, **STRONG)]
    policy, t = _turn_for(MilitaryPolicy(), MilitaryGame(units))
    old = policy._missions.open((14, 10), 19)
    new = policy._missions.open((30, 10), 19)
    old.units = {1: "march", 2: "march"}
    policy._plan_target = (30, 10)  # An urgent camp replaced the target.
    policy._claim_missions(t)
    assert old.units == {} and "not the target" in old.reason
    assert t.roles == {}  # Nobody is held for the abandoned mission.
    assert new.units == {}


# ----- live STD_T1_03 (914b11a): the diagnosis, as tests -----


def slinger(uid, x, y):
    return warrior(
        uid=uid, x=x, y=y, unit_type="UNIT_SLINGER", combat_strength=15, ranged_strength=25
    )


def test_a_force_that_beats_the_need_but_has_no_melee_unit_still_closes_up_at_the_muster(caplog):
    # T60-71: need 5-8, available 9-12, "force short of the need" with shortfall 1 (melee), and
    # nobody ordered for 12 turns. Strength and the melee unit are not a gate on the walk.
    units = [slinger(1, 6, 10), slinger(2, 6, 11), slinger(3, 6, 9), warrior(uid=4)]
    game = camp_game(units, [barbarian(x=15, y=10, cs=5)])
    policy = MilitaryPolicy()
    with caplog.at_level(logging.INFO):
        run(policy, game)
    moved = {m[1] for m in game.orders("move")}
    assert moved & {1, 2, 3}  # They walk toward the muster.
    assert not policy._committed  # No melee unit to take the tile: the commit waits for one.
    assert "no melee unit yet" in caplog.text


def test_a_force_too_weak_for_the_camp_still_closes_up_but_does_not_commit():
    # T49-71: units stood at "march" with no order for 20 turns while the force was short.
    units = [warrior(uid=1, x=6, y=10), warrior(uid=2, x=6, y=11), warrior(uid=3)]
    game = camp_game(units, [barbarian(x=15, y=10, cs=40), barbarian(x=15, y=11, cs=40)])
    policy = MilitaryPolicy()
    run(policy, game)
    assert {m[1] for m in game.orders("move")} & {1, 2}
    assert not policy._committed


def test_one_unit_in_reach_is_no_force_and_is_not_walked_out_alone():
    game = camp_game([warrior(uid=1, x=6, y=10, **STRONG), warrior(uid=3)], [])
    policy = MilitaryPolicy()
    run(policy, game)
    assert not [m for m in game.orders("move") if m[1] == 1]


def threats(n, cs=20):
    return [
        Threat(-1, "Barbarians", "UNIT_WARRIOR", 65, 10 + i, 100, 100, cs, 0, 3, True)
        for i in range(n)
    ]


def test_the_defenders_remembered_do_not_age_out_and_a_camp_seen_empty_clears_them():
    # The need went 22 -> 7 when two defenders seen at T49 aged out of a ten turn window at T60.
    policy, t = _turn_for(MilitaryPolicy(), MilitaryGame([warrior()]))
    camp = (65, 10)
    t.turn = 49
    policy._camp_seen[camp] = 49
    assert len(policy._remember(t, camp, threats(2))[0]) == 2
    for turn in (50, 55, 60, 70):  # Out of sight: the memory stands.
        t.turn = turn
        assert len(policy._remember(t, camp, [])[0]) == 2
    t.turn = 71
    policy._camp_seen[camp] = 71  # Seen again, and empty: that is the evidence.
    assert policy._remember(t, camp, [])[0] == []
    t.turn = 72
    assert policy._remember(t, camp, [])[0] == []  # And it stays gone.


def test_a_seen_group_replaces_the_memory_so_a_killed_defender_lowers_the_need():
    policy, t = _turn_for(MilitaryPolicy(), MilitaryGame([warrior()]))
    camp = (65, 10)
    policy._camp_seen[camp] = t.turn
    assert len(policy._remember(t, camp, threats(2))[0]) == 2
    assert len(policy._remember(t, camp, threats(1))[0]) == 1


def test_a_hurt_unit_of_the_mission_stays_held_while_it_heals():
    # T50-69: units were released as "not healthy or not free" every few turns.
    policy, t = _turn_for(MilitaryPolicy(), MilitaryGame([warrior(uid=1, x=12, y=10, health=60)]))
    mission = policy._missions.open((14, 10), 19)
    mission.units = {1: "march"}
    policy._claim_missions(t)
    assert t.roles == {1: "camp"} and mission.units == {1: "march"}
    policy._hold_force(t, mission, [], None)  # Not healthy enough for the force this turn.
    assert mission.units == {1: "march"}


def test_a_badly_hurt_unit_is_released_from_the_mission():
    policy, t = _turn_for(MilitaryPolicy(), MilitaryGame([warrior(uid=1, x=12, y=10, health=30)]))
    mission = policy._missions.open((14, 10), 19)
    mission.units = {1: "march"}
    policy._claim_missions(t)
    assert mission.units == {} and "not healthy" in mission.reason


def test_a_mission_that_is_not_the_target_says_so_in_its_line(caplog):
    policy, t = _turn_for(MilitaryPolicy(), MilitaryGame([warrior()]))
    policy._missions.open((55, 7), 19)
    policy._plan_target = (65, 10)
    with caplog.at_level(logging.INFO):
        policy._log_missions(t)
    (line,) = mission_lines(caplog)
    assert "not the target" in line["reason"]


# ----- live runs on 710b91c: progress, not elapsed turns -----


def test_waiting_is_not_a_setback_and_progress_is_recognised():
    # STD_T1_03 [55,7], T72: need 9, available 9, walk turns 7 to 2 over 20 turns: the cutoff
    # ended it. Waiting adds nothing: setbacks come from evidence only.
    assert not abort_verdict(9, 9, 2, 0.0, 0, S, setbacks=0, supplying=True).go
    assert progressed(3.0, 4.0, 1, 1, 3, 3, False, False)  # The mean walk fell by one tile.
    assert not progressed(4.0, 4.0, 1, 1, 3, 3, False, False)  # Nothing changed.
    assert progressed(4.0, 4.0, 2, 1, 3, 3, False, False)  # Another unit rallied.
    assert progressed(4.0, 4.0, 1, 1, 4, 3, False, False)  # Another unit joined.
    assert progressed(4.0, 4.0, 1, 1, 3, 3, True, False)  # A hit landed on the camp.
    assert progressed(4.0, None, 0, 0, 3, 0, False, False)  # The first check.


def test_setbacks_are_the_need_rising_a_unit_lost_or_a_way_closing():
    assert setback_events(8.0, 8.0, 0, 0, True, True) == 0  # Nothing changed.
    assert setback_events(8.0, 15.0, 0, 0, True, True) == 1  # STD_T1_03 T42: need 9 to 15+.
    assert setback_events(8.0, 8.0, 0, 2, True, True) == 2  # Two units lost.
    assert setback_events(8.0, 8.0, 0, 0, True, False) == 1  # A way closed.
    assert setback_events(8.0, 8.0, 0, 0, False, False) == 0  # Already closed: not new.
    assert setback_events(None, 8.0, 0, 0, True, True) == 0  # The first check.


def test_a_force_with_setbacks_is_called_off_by_the_numbers_with_its_reason():
    # [55,7]: margin 0, walk 2. [62,17]: margin +0.5, walk 5.
    assert not abort_verdict(9, 9, 2, 0.0, 0, S, setbacks=2, supplying=True).go
    assert abort_verdict(9, 9, 2, 0.0, 0, S, setbacks=3, supplying=True).go
    assert not abort_verdict(12, 8, 5, 0.0, 0, S, setbacks=2, supplying=True).go
    verdict = abort_verdict(12, 8, 5, 0.0, 0, S, setbacks=5, supplying=True)
    assert verdict.go and "5 setbacks" in verdict.reason


def test_a_short_gathering_force_is_not_called_off_for_being_short_only():
    # Production is building the shortfall: margin -1 would otherwise end every gather at once.
    assert abort_verdict(3, 9, 2, 0.0, 0, S, setbacks=0, supplying=False).go
    assert not abort_verdict(3, 9, 2, 0.0, 0, S, setbacks=0, supplying=True).go


def test_a_force_called_off_for_a_setback_reopens_only_when_the_facts_change():
    assert not facts_improved(9, 9, 0.0, (9.0, 9.0, 0.0))
    assert facts_improved(12, 9, 0.0, (9.0, 9.0, 0.0))  # More force.
    assert facts_improved(9, 5, 0.0, (9.0, 9.0, 0.0))  # A smaller need.
    assert facts_improved(9, 9, 0.0, (9.0, 9.0, 1.0))  # Less risk about.


def test_the_policy_clears_setbacks_on_progress_and_adds_them_on_evidence():
    units = [warrior(uid=1, x=2, y=10, **STRONG), warrior(uid=2, x=2, y=11, **STRONG)]
    policy, t = _turn_for(MilitaryPolicy(), MilitaryGame(units))
    mission = policy._missions.open((14, 10), 5)
    where = (14, 10)
    policy._note_progress(t, mission, t.mine, [], where, False, 6.0)
    for _ in range(50):  # Waiting turn after turn adds nothing.
        policy._note_progress(t, mission, t.mine, [], where, False, 6.0)
    assert mission.setbacks == 0
    policy._note_progress(t, mission, t.mine, [], where, False, 12.0)  # The need doubled.
    assert mission.setbacks == 1
    t.placed = {1: (8, 10), 2: (8, 11)}  # They walked six tiles.
    policy._note_progress(t, mission, t.mine, [], where, False, 12.0)
    assert mission.setbacks == 0


def test_a_unit_that_cannot_step_says_so_instead_of_step_unsafe():
    hold = types.SimpleNamespace(risk=0.0, exposure=0.0)
    why = MilitaryPolicy._why_held
    assert "no free step" in why([], [], hold)
    scored = [((1, 1), types.SimpleNamespace(net=-0.5))]
    assert "no step scores better" in why([(1, 1)], scored, hold)
    assert why([(1, 1)], scored, types.SimpleNamespace(risk=2.0, exposure=0.5)) == (
        "step unsafe, holding"
    )


# ----- the corridor deadlock (live STD_T1_03 T56-66, STD_T1_04 T36-43) -----

CORRIDOR = {  # The ground around (55,7), rows 5 to 12, columns 54 to 63, as the run's dump gave it.
    5: "..wwwwww...", 6: "...wwwwwww", 7: "whhhsgwwww", 8: "whhmgfgggg", 9: "hhmgrgrfgrg"[:10],
    10: "hhhggggggg", 11: "hhhggggggg", 12: "mhfggggfgg",
}  # fmt: skip


def corridor_store():
    from civ6_ai.state.reader import build_tile
    from civ6_ai.terrain import TerrainStore
    from fixtures.civ_state import tile as make_tile

    store = TerrainStore(map_width=74)
    tiles = []
    for y, row in CORRIDOR.items():
        for dx, ch in enumerate(row):
            x = 54 + dx
            if ch == ".":
                continue
            terrain = {"w": "TERRAIN_COAST", "m": "TERRAIN_GRASS_MOUNTAIN"}.get(ch, "TERRAIN_GRASS")
            feature = {"f": "FEATURE_FOREST", "s": "FEATURE_MARSH"}.get(ch)
            tiles.append(
                build_tile(
                    make_tile(
                        x=x, y=y, terrain=terrain, feature=feature, is_hills=ch == "h",
                        is_river=ch == "r", improvement=None, units=[],
                    )
                )
            )  # fmt: skip
    store.learn(tiles)
    return store


def corridor_game():
    units = [
        warrior(uid=1, x=59, y=7, **STRONG),  # The front unit, rallied.
        warrior(uid=2, x=60, y=8, **STRONG),  # Directly behind it.
        warrior(uid=3, x=61, y=8, **STRONG),  # Behind that.
        warrior(uid=4, x=59, y=10),  # The capital's garrison.
    ]
    from fixtures.civ_state import tile

    camp = tile(x=55, y=7, improvement="IMPROVEMENT_BARBARIAN_CAMP", owner_id=-1, units=[])
    cities = [city_info(city_id=65536, name="C", x=59, y=10, garrison_unit="")]
    return camp_game(units, [], camp=camp, cities=cities)


def corridor_policy():
    policy = MilitaryPolicy()
    policy.settings = dataclasses.replace(policy.settings, map_width=74)
    policy.terrain = corridor_store()
    return policy


def test_three_units_single_file_in_a_corridor_count_as_rallied_and_commit():
    policy = corridor_policy()
    run(policy, corridor_game())
    assert (55, 7) in policy._committed  # The column commits; it no longer waits for the back.
    assert policy._column_ids >= {1, 2, 3}


def test_the_unit_behind_the_front_one_counts_as_rallied_only_in_an_unbroken_line():
    policy = corridor_policy()
    game = corridor_game()
    units = [u for u in game.data["units"] if u.unit_id != 2]  # A gap in the line,
    for u in units:
        if u.unit_id == 3:
            u.x = 62  # and the back unit further off: three route tiles from the front one.
    game.data["units"] = units
    run(policy, game)
    assert 3 not in policy._column_ids


def test_a_muster_that_cannot_hold_the_force_scores_worse_than_one_that_can():
    from civ6_ai.camp_mission import muster_capacity

    cost = {(1, 1): 2.0, (2, 1): 3.0, (1, 2): 3.0, (0, 1): 3.0, (5, 5): 3.0}
    around = lambda p: [(p[0] + 1, p[1]), (p[0] - 1, p[1]), (p[0], p[1] + 1)]  # noqa: E731
    room = muster_capacity((1, 1), cost, 4.0, lambda p: True, around)
    assert room == 4
    corridor = muster_capacity((1, 1), {(1, 1): 2.0, (2, 1): 3.0}, 4.0, lambda p: True, around)
    assert corridor == 2
    wide = muster_score([2.0] * 3, 2.0, 0, S, no_room=max(0, 3 - room))
    narrow = muster_score([2.0] * 3, 2.0, 0, S, no_room=max(0, 3 - corridor))
    assert narrow > wide


def test_the_only_melee_unit_weighs_leaving_the_force_to_fight():
    # T59: the melee unit left the force to fight and the capture lost its unit.
    assert S.melee_stay_weight > 0
    from civ6_ai.camp_mission import column_rallied

    got = column_rallied({1}, {1: (5, 5), 2: (6, 5), 3: (7, 5)},
                         {2: [(6, 5), (5, 5)], 3: [(7, 5), (6, 5)]}, 2)  # fmt: skip
    assert got == {1, 2, 3}
