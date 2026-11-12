import json
from pathlib import Path

from civ6_ai.effectiveness import (
    FAIL,
    NO_DATA,
    PASS,
    RunLogs,
    format_report,
    load_run,
    run_checks,
)

STAMP = "2026-11-11 17:00:00,000"


def bot(logger, msg, level="INFO"):
    return (f"policies.{logger}" if logger != "blockers" else logger, level, msg)


def turn(n, calls=None, detail="", errors=None, stop=None):
    rec = {"turn": n, "detail": detail, "handler_errors": errors or {}, "stop_reason": stop}
    if calls is not None:
        rec["handler_calls"] = {"h": {k: [v, 0.1] for k, v in calls.items()}}
    return rec


def by_name(run):
    return {(c.policy, c.name): c for c in run_checks(run)}


def gp_line(turn_no, names):
    people = "; ".join(f"{n} (UNIT_GREAT_ENGINEER) at 1,1: working on it" for n in names)
    return bot("secondary", f"great_people turn {turn_no}: {len(names)} on the map: {people}")


def test_great_people_created_but_never_used_fails():
    names = [f"Person {i}" for i in range(8)]
    run = RunLogs(
        records=[turn(1, {"activate_great_person": 0})],
        bot=[gp_line(1, names)],
        has_bot_log=True,
    )
    check = by_name(run)[("great people", "seen vs activated")]
    assert check.status == FAIL
    assert (check.created, check.used) == (8, 0)


def test_great_people_used_passes_and_refusals_do_not_count_as_use():
    names = [f"Person {i}" for i in range(6)]
    refusals = [bot("secondary", "X can't activate at Y: Error") for _ in range(2)]
    run = RunLogs(
        records=[turn(1, {"activate_great_person": 8})],
        bot=[gp_line(1, names), *refusals],
        has_bot_log=True,
    )
    check = by_name(run)[("great people", "seen vs activated")]
    assert check.used == 6
    assert check.status == PASS


def test_a_pile_up_is_flagged():
    run = RunLogs(records=[turn(1)], bot=[gp_line(1, [f"P{i}" for i in range(21)])])
    assert by_name(run)[("great people", "most waiting at once")].status == FAIL


def test_great_people_without_call_counts_is_no_data_not_zero():
    run = RunLogs(records=[turn(1)], bot=[gp_line(1, ["A"])], has_bot_log=True)
    assert by_name(run)[("great people", "seen vs activated")].status == NO_DATA


def test_an_unhandled_blocker_is_named():
    msg = "No handler for blocker ENDTURN_BLOCKING_CLAIM_GREAT_PERSON: Claim Great Person"
    run = RunLogs(records=[turn(1)], bot=[bot("blockers", msg)], has_bot_log=True)
    check = by_name(run)[("blockers", "turns held by an unhandled blocker")]
    assert check.status == FAIL
    assert "CLAIM_GREAT_PERSON" in check.note


def test_faith_held_for_many_turns_fails():
    idle = "Faith 500 above reserve 60 but nothing bought: great people: cheapest 550 faith"
    run = RunLogs(records=[turn(1, {})], bot=[bot("faith", idle)] * 10, has_bot_log=True)
    checks = by_name(run)
    assert checks[("faith", "turns idle vs turns spent")].status == FAIL
    assert checks[("faith", "most faith idle above reserve")].created == 440


def test_a_success_logged_as_a_refusal_is_flagged():
    msg = "Upgrade of UNIT_WARRIOR refused: UPGRADED|UNIT_WARRIOR -> UNIT_SWORDSMAN"
    run = RunLogs(records=[turn(1)], bot=[bot("gold", msg)], has_bot_log=True)
    assert by_name(run)[("gold", "upgrade logged as refused when it succeeded")].status == FAIL


def test_repeated_refusals_of_one_build_are_flagged():
    msg = "Coba can't build DISTRICT_HARBOR: ERR:NO_PLACEMENT|DISTRICT_HARBOR: none"
    run = RunLogs(records=[turn(1)], bot=[bot("production", msg)] * 30, has_bot_log=True)
    check = by_name(run)[("production", "most repeats of one refused build")]
    assert check.status == FAIL
    assert "Coba DISTRICT_HARBOR" in check.note


def test_builder_failures_and_idle_turns():
    def decision(action):
        payload = {"action": action, "improvement": "IMPROVEMENT_MINE", "target": [1, 1]}
        return bot("builder", "builder_decision " + json.dumps(payload))

    lines = [decision("failed")] * 6 + [decision("improve")] * 2 + [decision("idle")] * 10
    checks = by_name(RunLogs(records=[turn(1)], bot=lines, has_bot_log=True))
    assert checks[("builders", "improve attempts that failed")].status == FAIL
    assert checks[("builders", "builder-turns idle")].status == FAIL


def test_refused_military_moves_are_a_share_of_acting_decisions():
    def decision(action, reason):
        payload = {"action": action, "reason": reason}
        return bot("military", "military_decision " + json.dumps(payload))

    lines = [decision("skip", "spare")] * 50
    lines += [decision("garrison", "move refused")] * 6 + [decision("attack", "odds")] * 6
    run = RunLogs(records=[turn(1)], bot=lines, has_bot_log=True)
    check = by_name(run)[("military", "moves refused by the game")]
    assert (check.created, check.used) == (12, 6)
    assert check.status == FAIL


def test_units_built_versus_attacks_uses_the_completion_notices():
    notice = "  >> Coba finished building UNIT_SWORDSMAN. Now: nothing."
    records = [turn(i, {"attack_unit": 0}, detail=notice) for i in range(6)]
    check = by_name(RunLogs(records=records))[("military", "combat units built vs attacks made")]
    assert (check.created, check.used) == (6, 0)
    assert check.status == FAIL


def test_builders_and_settlers_are_not_combat_units():
    notice = "  >> Coba finished building UNIT_BUILDER. Now: nothing."
    records = [turn(i, {"attack_unit": 0}, detail=notice) for i in range(6)]
    check = by_name(RunLogs(records=records))[("military", "combat units built vs attacks made")]
    assert check.created == 0


def test_settlers_that_never_found_a_city_fail():
    notice = "  >> Coba finished building UNIT_SETTLER. Now: nothing."
    records = [turn(i, {"found_city": 0}, detail=notice) for i in range(6)]
    check = by_name(RunLogs(records=records))[("settling", "settlers built vs cities founded")]
    assert check.status == FAIL


def test_policy_slots_set_again_every_turn_are_flagged():
    lines = [bot("government", "Slot 1 takes POLICY_GOD_KING")] * 30
    run = RunLogs(records=[turn(1)], bot=lines, has_bot_log=True)
    check = by_name(run)[("government", "times each slot/policy pair was set again")]
    assert check.created == 30
    assert check.status == FAIL


def test_handler_errors_are_a_share_of_turns():
    records = [turn(i, errors={"secondary": "boom"} if i < 5 else None) for i in range(10)]
    check = by_name(RunLogs(records=records))[("handlers", "turn-handler failures")]
    assert (check.created, check.used) == (10, 5)
    assert check.status == FAIL


def test_missing_logs_give_no_data_never_a_pass():
    checks = by_name(RunLogs(records=[turn(1)]))
    assert checks[("faith", "held vs spent")].status == NO_DATA
    assert checks[("production", "builds refused")].status == NO_DATA
    assert checks[("builders", "improvements failed")].status == NO_DATA


def test_load_run_reads_the_sibling_logs(tmp_path: Path):
    log = tmp_path / "STD_T1_01_20261111T000000Z.jsonl"
    log.write_text(json.dumps(turn(1, {"found_city": 1})) + "\n", encoding="utf-8")
    line = f"{STAMP} civ6_ai.policies.faith INFO Faith bought A (Great Engineer) for 300 faith\n"
    log.with_name(log.stem + "_bot.log").write_text(line + "garbage\n", encoding="utf-8")
    log.with_name(log.stem + "_events.jsonl").write_text(
        json.dumps({"kind": "city_lost", "turn": 5}) + "\nnot json\n", encoding="utf-8"
    )
    run = load_run([log])
    assert run.has_bot_log and run.has_events and not run.has_settle
    assert run.messages("policies.faith") == ["Faith bought A (Great Engineer) for 300 faith"]
    assert run.calls("found_city") == 1
    assert by_name(run)[("settling", "cities lost")].created == 1


def test_the_report_lists_failures_and_figures():
    run = RunLogs(records=[turn(1)], bot=[gp_line(1, [f"P{i}" for i in range(9)])])
    text = format_report("Run X", run, run_checks(run))
    assert "Run X" in text
    assert "[FAIL   ] great people" in text
    assert "checks failed" in text
