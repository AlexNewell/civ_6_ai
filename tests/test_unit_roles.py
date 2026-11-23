import json
import logging
import types

from civ6_ai import unit_roles
from civ6_ai.policies.military import MilitaryPolicy, MilitarySettings
from fixtures.civ_state import city_info, overview
from test_policy_military import (
    MilitaryGame,
    archer,
    barbarian,
    run,
    run20_turn100,
    scout,
    settler,
    warrior,
)


def lines(caplog):
    return [
        json.loads(r.getMessage().split("military_roles ", 1)[1])
        for r in caplog.records
        if r.getMessage().startswith("military_roles ")
    ]


def by_id(line):
    return {u["u"]: u for u in line["units"]}


def two_cities():
    return [city_info(), city_info(city_id=2, name="Beta", x=30, y=20)]


def test_every_combat_unit_has_one_entry_and_each_silent_cause_is_named(caplog):
    caplog.set_level(logging.INFO)
    units = [
        warrior(uid=1, x=10, y=10),
        warrior(uid=3, x=30, y=20),  # Beta's only unit: nothing orders it.
        archer(uid=4, x=30, y=21),
        scout(uid=9, x=40, y=40),
        settler(uid=8, x=15, y=10),
        warrior(uid=5, x=15, y=10),  # The settler's escort.
        settler(uid=7, x=16, y=10),  # Not a combat unit: no entry.
    ]
    game = MilitaryGame(units, [barbarian(x=12, y=12)], cities=two_cities())
    run(MilitaryPolicy(), game)
    (line,) = lines(caplog)
    units = by_id(line)
    assert sorted(units) == [1, 3, 4, 5, 9]  # One entry each, the civilian left out.
    assert units[5]["role"] == "escort" and units[5]["own"] == "settling"
    assert units[5]["why"].startswith("settling_reserved")
    assert units[9]["role"] == "recon" and units[9]["why"].startswith("settling_reserved")
    assert units[3]["role"] == "garrison"
    assert units[3]["act"] == "none" and units[3]["why"].startswith("held_city_garrison")
    assert units[1]["act"] == "fortify" and units[1]["role"] == "garrison"
    assert units[1]["city"]["short"] is False and units[1]["city"]["def"] >= 1
    assert all(u["role"] in unit_roles.ROLES for u in units.values())


def test_a_unit_that_is_not_land_military_is_named_other(caplog):
    caplog.set_level(logging.INFO)
    ship = warrior(uid=6, x=20, y=20, unit_type="UNIT_GALLEY", name="Galley")
    game = MilitaryGame([warrior(uid=1), ship], [], cities=[city_info()])
    run(MilitaryPolicy(), game)
    entry = by_id(lines(caplog)[-1])[6]
    assert entry["role"] == "other" and entry["own"] == "none"
    assert entry["why"].startswith("not_land_military")


def test_a_hurt_unit_resting_is_named_and_a_spare_unit_is_spare(caplog):
    caplog.set_level(logging.INFO)
    policy = MilitaryPolicy(settings=MilitarySettings(spare_explorers=0))
    hurt = warrior(uid=1, health=30)
    game = MilitaryGame([hurt, warrior(uid=2, x=14, y=10)], [], cities=[city_info()])
    run(policy, game)
    game.data["overview"] = overview(turn=21, num_cities=1)
    run(policy, game)  # The next turn: resting, so `_heal` skips it with no log.
    entry = by_id(lines(caplog)[-1])[1]
    assert entry["role"] == "heal_retreat" and entry["act"] == "none"
    assert entry["why"].startswith("resting_until_healed")
    assert {u["role"] for u in lines(caplog)[-1]["units"]} >= {"spare", "heal_retreat"}


def test_the_camp_plan_counts_one_unit_and_says_why_not_for_the_rest(caplog):
    caplog.set_level(logging.INFO)
    game, policy, state = run20_turn100()
    run(policy, game)
    (line,) = lines(caplog)
    assert line["plan"]["units"] == 1 and line["plan"]["have"] < line["plan"]["need"]
    units = by_id(line)
    assert [u["cnt"] for u in line["units"]].count(True) == 1
    lone = units[3]  # Thessalonica's lone Archer.
    assert lone["cnt"] is False and lone["cnt_why"] == "sole defender of its city"
    assert lone["role"] == "garrison"
    assert {u["role"] for u in line["units"]} == {"garrison", "spare"}


def test_a_spare_unit_walks_to_the_nearest_unmet_need_when_terrain_is_known(caplog):
    caplog.set_level(logging.INFO)
    game, policy, state = run20_turn100()
    run(policy, game)
    plain = by_id(lines(caplog)[-1])
    assert not any("nw" in u for u in plain.values())  # No terrain: nothing to measure.

    stub = types.SimpleNamespace(
        walk_turns=lambda here, goal, moves: 3 if goal == (55, 7) else None,
        cost_field=lambda goal: None,
    )
    game, policy, state = run20_turn100()
    policy._mv = lambda: stub
    run(policy, game)
    spare = [u for u in lines(caplog)[-1]["units"] if u["role"] == "spare"]
    assert spare and all(u["nw"] == 3 and u["nk"] == "camp" for u in spare)


def test_a_unit_missing_since_the_last_line_is_gone(caplog):
    caplog.set_level(logging.INFO)
    policy = MilitaryPolicy()
    run(policy, MilitaryGame([warrior(uid=1), archer(uid=2)], [], cities=[city_info()]))
    run(policy, MilitaryGame([warrior(uid=1)], [], cities=[city_info()]))
    first, second = lines(caplog)
    assert "gone" not in first
    assert [g["u"] for g in second["gone"]] == [2] and second["gone"][0]["t"] == "ARCHER"


def test_a_unit_the_expected_damage_killed_is_gone_not_listed(caplog):
    caplog.set_level(logging.INFO)
    game, policy, _ = run20_turn100()
    original = policy._publish_defence

    def publish(t):
        t.hp[1] = 0.0
        original(t)

    policy._publish_defence = publish
    run(policy, game)
    line = lines(caplog)[-1]
    assert 1 not in by_id(line) and line["gone"][0]["u"] == 1


def test_logging_never_changes_play_or_calls_the_game(monkeypatch, caplog):
    def play():
        game, policy, _ = run20_turn100()
        run(policy, game)
        return game.actions, game.calls, [d.reason for d in policy.decisions]

    with_log = play()
    monkeypatch.setattr(unit_roles, "build_line", lambda *a: 1 / 0)
    with caplog.at_level(logging.WARNING):
        broken = play()
    assert broken == with_log
    assert any("military_roles failed" in r.getMessage() for r in caplog.records)


def test_the_line_stays_compact(caplog):
    caplog.set_level(logging.INFO)
    units = [warrior(uid=i, x=10 + i % 3, y=10) for i in range(1, 8)]
    game = MilitaryGame(units, [barbarian(x=12, y=12)], cities=[city_info()])
    run(MilitaryPolicy(), game)
    text = next(
        r.getMessage() for r in caplog.records if r.getMessage().startswith("military_roles ")
    )
    assert len(text) / 7 < 260  # Bytes a unit; seven units here.


def test_the_line_is_written_when_no_land_military_unit_remains(caplog):
    caplog.set_level(logging.INFO)
    policy = MilitaryPolicy()
    cities = [city_info()]
    galley = warrior(uid=6, x=20, y=20, unit_type="UNIT_GALLEY", name="Galley")
    run(policy, MilitaryGame([warrior(uid=1), galley], [], cities=cities))
    game = MilitaryGame([galley], [], cities=cities, turn=21)
    run(policy, game)  # The last land unit is gone.
    line = lines(caplog)[-1]
    assert line["turn"] == 21 and [g["u"] for g in line["gone"]] == [1]
    assert by_id(line)[6]["role"] == "other" and not game.orders()
    run(policy, MilitaryGame([], [], cities=cities, turn=22))
    assert [g["u"] for g in lines(caplog)[-1]["gone"]] == [6]  # The loss of the last one.
    run(policy, MilitaryGame([], [], cities=cities, turn=23))
    assert lines(caplog)[-1]["turn"] == 22  # Nothing left to say: no line.


def test_the_camp_stage_records_who_it_counted_and_why_not(caplog):
    caplog.set_level(logging.INFO)
    game, policy, state = run20_turn100()
    policy._refused[(1, "move")] = 103  # Unit 1's move order is on a refusal cooldown.
    run(policy, game)
    units = by_id(lines(caplog)[-1])
    assert units[1]["cnt"] is False and units[1]["cnt_why"] == "move order refused lately"
    assert units[2]["cnt"] is True  # The one the stage counted, which `plan.units` says.
    assert units[3]["cnt_why"] == "sole defender of its city"
    assert not any("recomputed" in u.get("cnt_why", "") for u in units.values())


def test_without_the_camp_stage_the_membership_is_recomputed_and_labelled(caplog):
    caplog.set_level(logging.INFO)
    game, policy, _ = run20_turn100()

    async def skip(game, t):
        return None

    policy._clear_camp = skip
    run(policy, game)
    whys = [u["cnt_why"] for u in lines(caplog)[-1]["units"] if not u["cnt"]]
    assert whys and all(w.startswith("recomputed: ") for w in whys)


def test_a_unit_beyond_the_camps_reach_is_not_counted(caplog):
    caplog.set_level(logging.INFO)
    game, policy, state = run20_turn100()
    policy.settings = MilitarySettings(camp_reach=2)  # The camp is five tiles off.
    run(policy, game)
    line = lines(caplog)[-1]
    assert line["plan"]["units"] == 0
    assert not any(u["cnt"] for u in line["units"])
    assert "beyond the camp's reach" in {u.get("cnt_why") for u in line["units"]}


def test_the_no_land_unit_line_keeps_the_city_count(caplog):
    caplog.set_level(logging.INFO)
    galley = warrior(uid=6, x=20, y=20, unit_type="UNIT_GALLEY", name="Galley")
    run(MilitaryPolicy(), MilitaryGame([galley], [], cities=two_cities()))
    assert lines(caplog)[-1]["cities"] == 2


def test_a_calm_city_and_a_threatened_one_with_no_reading_are_told_apart(caplog):
    from civ6_ai.policies.defence import CityThreat, DefenceState

    caplog.set_level(logging.INFO)
    state = DefenceState()
    policy = MilitaryPolicy(defence=state)
    game = MilitaryGame([warrior(uid=1), warrior(uid=3, x=30, y=20)], [], cities=two_cities())
    state.cities = {65536: CityThreat(65536, "Rome", 10, 10, ("hostile near",), 1)}
    run(policy, game)
    units = by_id(lines(caplog)[-1])
    assert units[1]["city"]["rd"] == "none"  # Threatened, no map reading.
    assert units[3]["city"]["rd"] == "calm"


def test_scouts_stay_settlings_when_no_land_unit_remains(caplog):
    caplog.set_level(logging.INFO)
    run(MilitaryPolicy(), MilitaryGame([scout(uid=9, x=40, y=40)], [], cities=two_cities()))
    entry = by_id(lines(caplog)[-1])[9]
    assert entry["role"] == "recon" and entry["own"] == "settling"
    assert entry["why"].startswith("settling_reserved")


def test_every_action_the_military_policy_logs_has_a_role():
    # Every action string `_log` and `MilitaryDecision` carry in policies/military.py.
    mapped = {
        "garrison": "garrison", "swap": "garrison", "cover": "cover", "heal": "heal_retreat",
        "retreat": "heal_retreat", "flee": "heal_retreat", "camp": "camp", "attack": "attack",
        "city_strike": "attack", "hunt": "attack", "recapture": "recapture", "recon": "recon",
        "explore": "recon", "probe": "recon", "stack": "stack",
    }  # fmt: skip
    for action, role in mapped.items():
        assert unit_roles._decision_role(action, "ordered") == role
    # Read by the reason: fortify and skip. No role of their own: a promotion, and any refusal.
    assert unit_roles._decision_role("fortify", "spare unit, nothing to attack") == "spare"
    assert unit_roles._decision_role("fortify", "covering a threatened city") == "cover"
    assert unit_roles._decision_role("skip", "spare, already fortified") == "spare"
    assert unit_roles._decision_role("skip", "no attack: odds") is None
    assert unit_roles._decision_role("promote", "promotion spent") is None
    assert unit_roles._decision_role("probe", "move refused") is None


def test_a_successful_fallback_move_keeps_its_role_and_a_refusal_does_not():
    ok = "city tile refused, so went to a tile beside it"
    assert unit_roles._decision_role("garrison", ok) == "garrison"
    assert unit_roles._decision_role("garrison", "move refused") is None
    assert unit_roles._decision_role("garrison", "fallback move refused") is None
    assert unit_roles._decision_role("attack", "attack failed: no damage") is None
    # A fortify in place of a refused move takes the role of the order that was refused.
    assert (
        unit_roles._decision_role("fortify", "move refused, so fortified where it stands") is None
    )
    assert unit_roles._decision_role("fortify", "city has hostile units near it") == "garrison"
    assert unit_roles._decision_role("skip", "spare, fortify refused") == "spare"


def test_a_garrison_whose_move_was_refused_but_fallback_worked_stays_garrison(caplog):
    caplog.set_level(logging.INFO)
    cities = [city_info(garrison_unit="")]
    game = MilitaryGame([warrior(uid=1, x=12, y=10), warrior(uid=2, x=12, y=11)],
                        [barbarian(x=11, y=11)], cities=cities)  # fmt: skip
    game.refuse_moves_to = {(10, 10)}  # The city tile is refused; a tile beside it works.
    policy = MilitaryPolicy()
    run(policy, game)
    done = [d for d in policy.decisions if d.reason.startswith("city tile refused")]
    assert done
    entry = by_id(lines(caplog)[-1])[done[0].unit_id]
    assert entry["role"] == "garrison" and entry["act"] == "garrison"
