import json
import os
from pathlib import Path

from civ6_ai.effectiveness import (
    FAIL,
    NO_DATA,
    PASS,
    RunLogs,
    city_losses,
    end_of_run_lines,
    format_report,
    gate_failures,
    load_run,
    run_checks,
    warnings,
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
    assert checks[("builders", "builder actions the game refused")].status == FAIL
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


def military_line(action, reason):
    return bot("military", "military_decision " + json.dumps({"action": action, "reason": reason}))


def test_units_built_versus_attacks_uses_the_completion_notices():
    notice = "  >> Coba finished building UNIT_SWORDSMAN. Now: nothing."
    records = [turn(i, {"attack_unit": 6}, detail=notice) for i in range(6)]
    lines = [military_line("attack", "attack refused")] * 6
    run = RunLogs(records=records, bot=lines, has_bot_log=True)
    check = by_name(run)[("military", "combat units built vs attacks made")]
    assert (check.created, check.used) == (6, 0)
    assert check.status == FAIL


def test_attacks_that_landed_count_as_use():
    notice = "  >> Coba finished building UNIT_SWORDSMAN. Now: nothing."
    records = [turn(i, {}, detail=notice) for i in range(6)]
    lines = [military_line("attack", "good odds")] * 4 + [military_line("city_strike", "in range")]
    run = RunLogs(records=records, bot=lines, has_bot_log=True)
    check = by_name(run)[("military", "combat units built vs attacks made")]
    assert (check.created, check.used) == (6, 5)
    assert check.status == PASS


def test_builders_and_settlers_are_not_combat_units():
    notice = "  >> Coba finished building UNIT_BUILDER. Now: nothing."
    records = [turn(i, {}, detail=notice) for i in range(6)]
    lines = [military_line("skip", "spare")]
    run = RunLogs(records=records, bot=lines, has_bot_log=True)
    assert by_name(run)[("military", "combat units built vs attacks made")].created == 0


def test_sessions_without_a_bot_log_are_left_out_when_others_have_one(tmp_path: Path):
    first = tmp_path / "STD_T1_01_20261111T000000Z.jsonl"
    second = tmp_path / "STD_T1_01_20261111T010000Z.jsonl"
    for log in (first, second):
        log.write_text(json.dumps(turn(1, {"x": 1})) + os.linesep, encoding="utf-8")
    first.with_name(first.stem + "_bot.log").write_text("", encoding="utf-8")
    run = load_run([first, second])
    assert run.has_bot_log
    assert run.dropped == [second.name]
    assert len(run.records) == 1
    assert "Left out" in format_report("X", run, [])


def test_settlers_that_never_found_a_city_fail():
    notice = "  >> Coba finished building UNIT_SETTLER. Now: nothing."
    records = [{**turn(i, {"found_city": 1}, detail=notice), "_settle": True} for i in range(6)]
    settle = [{"action": "found_failed"}] * 6
    run = RunLogs(records=records, settle=settle, has_settle=True)
    check = by_name(run)[("settling", "settlers built vs cities founded")]
    assert (check.created, check.used) == (6, 0)
    assert check.status == FAIL


def test_founding_without_the_settle_log_is_no_data():
    notice = "  >> Coba finished building UNIT_SETTLER. Now: nothing."
    records = [turn(i, {"found_city": 1}, detail=notice) for i in range(6)]
    check = by_name(RunLogs(records=records))[("settling", "settlers vs cities")]
    assert check.status == NO_DATA


def test_great_people_event_checks_need_the_bot_log():
    checks = by_name(RunLogs(records=[turn(1, {"activate_great_person": 0})]))
    assert ("great people", "activation refused") not in checks
    assert ("great people", "'no district' waits logged") not in checks


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


def test_several_handlers_failing_on_one_turn_count_that_turn_once():
    errors = {"secondary": "boom", "military": "boom", "wars": "boom"}
    records = [turn(i, errors=errors if i == 0 else None) for i in range(100)]
    check = by_name(RunLogs(records=records))[("handlers", "turn-handler failures")]
    assert (check.created, check.used) == (100, 1)
    assert check.status == PASS


def test_empty_call_counts_are_not_measured_zeros():
    notice = "  >> Coba finished building UNIT_SWORDSMAN. Now: nothing."
    records = [turn(i, {}, detail=notice) for i in range(6)]
    run = RunLogs(records=records)
    assert not run.has_calls
    assert by_name(run)[("military", "units built vs attacks")].status == NO_DATA


def test_gold_idle_turns_come_from_the_gold_handler_calls():
    def gold_turn(n, calls):
        return {**turn(n), "handler_calls": {"gold": {k: [1, 0.1] for k in calls}}}

    records = [gold_turn(i, ["list_city_production"]) for i in range(8)]
    records += [gold_turn(8, ["list_city_production", "purchase_item"])]
    records += [gold_turn(9, ["get_game_overview"])]
    check = by_name(RunLogs(records=records))[("gold", "turns above reserve with nothing bought")]
    assert (check.created, check.used) == (9, 8)
    assert check.status == FAIL


def test_gold_upgrade_only_turns_count_as_eligible_and_spent():
    def gold_turn(n, calls):
        return {**turn(n), "handler_calls": {"gold": {k: [1, 0.1] for k in calls}}}

    records = [gold_turn(i, ["upgrade_unit"]) for i in range(8)]
    records += [gold_turn(8 + i, ["list_city_production"]) for i in range(5)]
    check = by_name(RunLogs(records=records))[("gold", "turns above reserve with nothing bought")]
    assert (check.created, check.used) == (13, 5)
    assert check.status == PASS


def test_faith_buying_twice_in_one_turn_counts_one_spending_turn():
    idle = "Faith 500 above reserve 60 but nothing bought: great people: cheapest 550 faith"
    bought = "Faith bought A (Great Engineer) for 300 faith"
    records = [turn(i, {"x": 1}) for i in range(8)]
    records.append({**turn(8), "handler_calls": {"faith": {"patronize_great_person": [2, 0.2]}}})
    lines = [bot("faith", idle)] * 8 + [bot("faith", bought)] * 2
    run = RunLogs(records=records, bot=lines, has_bot_log=True)
    check = by_name(run)[("faith", "turns idle vs turns spent")]
    assert (check.created, check.used) == (9, 1)
    assert check.status == FAIL


def test_refused_gold_purchases_do_not_count_as_spending():
    def gold_turn(n):
        calls = {"list_city_production": [1, 0.1], "purchase_item": [1, 0.1]}
        return {**turn(n), "handler_calls": {"gold": calls}}

    refusal = "Purchase of BUILDING_X in Coba refused: Error: STACKING_CONFLICT|x"
    run = RunLogs(
        records=[gold_turn(i) for i in range(8)],
        bot=[bot("gold", refusal)] * 8,
        has_bot_log=True,
    )
    check = by_name(run)[("gold", "turns above reserve with nothing bought")]
    assert (check.created, check.used) == (8, 8)
    assert check.status == FAIL


def test_refused_faith_purchases_do_not_count_as_spending():
    idle = "Faith 500 above reserve 60 but nothing bought: great people: cheapest 550 faith"
    refused = {"faith": {"patronize_great_person": [1, 0.1]}}
    records = [{**turn(i), "handler_calls": refused} for i in range(8)]
    run = RunLogs(records=records, bot=[bot("faith", idle)] * 8, has_bot_log=True)
    check = by_name(run)[("faith", "turns idle vs turns spent")]
    assert (check.created, check.used) == (8, 0)
    assert check.status == FAIL


def test_removing_a_feature_is_builder_work():
    def decision(action):
        payload = {"action": action, "improvement": None, "target": [1, 1]}
        return bot("builder", "builder_decision " + json.dumps(payload))

    lines = [decision("idle")] * 5 + [decision("remove_feature")] * 5
    check = by_name(RunLogs(records=[turn(1)], bot=lines, has_bot_log=True))[
        ("builders", "builder-turns idle")
    ]
    assert (check.created, check.used) == (10, 5)
    assert check.status == PASS


def test_older_logs_that_call_a_successful_upgrade_a_refusal_still_count_it():
    def gold_turn(n):
        return {**turn(n), "handler_calls": {"gold": {"upgrade_unit": [1, 0.1]}}}

    line = "Upgrade of UNIT_WARRIOR refused: UPGRADED|UNIT_WARRIOR -> UNIT_SWORDSMAN"
    run = RunLogs(
        records=[gold_turn(i) for i in range(6)], bot=[bot("gold", line)] * 6, has_bot_log=True
    )
    check = by_name(run)[("gold", "turns above reserve with nothing bought")]
    assert (check.created, check.used) == (6, 0)


def test_events_and_settle_files_passed_by_a_glob_are_not_turn_logs(tmp_path: Path):
    log = tmp_path / "STD_T1_01_20261111T000000Z.jsonl"
    log.write_text(json.dumps(turn(1, {"x": 1})) + os.linesep, encoding="utf-8")
    events = tmp_path / "STD_T1_01_20261111T000000Z_events.jsonl"
    events.write_text(json.dumps({"kind": "city_lost", "turn": 1}) + os.linesep, encoding="utf-8")
    run = load_run([log, events])
    assert len(run.records) == 1


def test_refused_prophet_activations_are_not_counted_as_used():
    names = [f"Person {i}" for i in range(5)]
    refused = "Prophet still idle after 3 turns: on the Holy Site of Coba but refused: Error"
    run = RunLogs(
        records=[turn(1, {"activate_great_person": 3})],
        bot=[gp_line(1, names), *[bot("faith", refused, "WARNING")] * 3],
        has_bot_log=True,
    )
    check = by_name(run)[("great people", "seen vs activated")]
    assert (check.created, check.used) == (5, 0)
    assert check.status == FAIL


def test_a_log_with_no_faith_lines_is_no_data_for_faith():
    run = RunLogs(
        records=[turn(1)], bot=[bot("gold", "Gold 5, reserve 3, 2 to spend")], has_bot_log=True
    )
    assert by_name(run)[("faith", "held vs spent")].status == NO_DATA


def test_deal_acceptances_the_game_refused_do_not_count():
    accepted = "Deal from player 3 accepted: it returns 5 for 4"
    refused = "The game refused our answer to the deal: Error"
    lines = []
    for _ in range(6):
        lines += [bot("diplomacy", accepted), bot("diplomacy", refused, "WARNING")]
    check = by_name(RunLogs(records=[turn(1)], bot=lines, has_bot_log=True))[
        ("diplomacy", "deals offered vs accepted")
    ]
    assert (check.created, check.used) == (6, 0)
    assert check.status == FAIL


def test_faith_purchase_calls_are_not_gold_purchases():
    record = {
        **turn(1),
        "handler_calls": {
            "faith": {"purchase_item": [4, 0.4]},
            "gold": {"purchase_item": [1, 0.1]},
        },
    }
    check = by_name(RunLogs(records=[record], has_bot_log=True))[("gold", "purchase calls")]
    assert check.used == 1


def test_a_refused_rejection_does_not_cancel_an_accepted_deal():
    accepted = "Deal from player 3 accepted: it returns 5 for 4"
    declined = "Deal from player 3 refused: it was refused before"
    failed = "The game refused our answer to the deal: Error"
    lines = [bot("diplomacy", accepted)] * 2
    for _ in range(4):
        lines += [bot("diplomacy", declined), bot("diplomacy", failed, "WARNING")]
    check = by_name(RunLogs(records=[turn(1)], bot=lines, has_bot_log=True))[
        ("diplomacy", "deals offered vs accepted")
    ]
    assert (check.created, check.used) == (6, 2)


def test_cities_lost_separates_flips_that_came_back_and_loyalty_losses():
    events = [
        {"kind": "city_loyalty_warning", "turn": 120, "city_id": 7},
        {"kind": "city_lost", "turn": 125, "city_id": 7},  # a loyalty loss that came back
        {"kind": "city_gained", "turn": 130, "city_id": 7},
        {"kind": "city_lost", "turn": 135, "city_id": 1},  # captured, never returned
        {"kind": "city_lost", "turn": 200, "city_id": 9},  # warned too long ago to count
        {"kind": "city_loyalty_warning", "turn": 100, "city_id": 9},
        {"kind": "city_gained", "turn": 50, "city_id": 1},  # before the loss, so no return
    ]
    summary = city_losses(events)
    assert (summary.lost, summary.returned, summary.permanent, summary.loyalty) == (3, 1, 2, 1)
    run = RunLogs(records=[turn(1)], events=events, has_events=True)
    check = by_name(run)[("settling", "cities lost")]
    assert check.created == 2 and check.status == FAIL
    assert "1 came back" in check.note and "1 after a loyalty warning" in check.note


def test_cities_lost_passes_when_every_loss_came_back():
    events = [
        {"kind": "city_lost", "turn": 5, "city_id": 3},
        {"kind": "city_gained", "turn": 9, "city_id": 3},
    ]
    run = RunLogs(records=[turn(1)], events=events, has_events=True)
    assert by_name(run)[("settling", "cities lost")].status == PASS


def test_a_stop_that_is_not_game_over_or_the_turn_limit_fails_the_gate():
    cases = (("blocked", True), ("hung", True), ("game_over", False), ("turn_limit", False))
    for stop, fails in cases:
        run = RunLogs(records=[turn(1), turn(2, stop=stop)])
        names = {(c.policy, c.name) for c in gate_failures(run_checks(run))}
        assert (("run", "stops that were not the game ending") in names) is fails


def test_gate_and_warning_checks_are_separate():
    errors = [turn(n, errors={"h": "boom"}) for n in range(1, 11)]
    run = RunLogs(
        records=errors,
        bot=[gp_line(1, [f"P{i}" for i in range(9)])],
        has_bot_log=True,
    )
    checks = run_checks(run)
    gate = {(c.policy, c.name) for c in gate_failures(checks)}
    assert ("handlers", "turn-handler failures") in gate
    assert ("great people", "most waiting at once") in gate
    assert not gate & {(c.policy, c.name) for c in warnings(checks)}


def test_end_of_run_lines_put_gate_failures_first(tmp_path: Path):
    log = tmp_path / "STD_T1_01_20261111T000000Z.jsonl"
    rows = [turn(n, errors={"h": "boom"}) for n in range(1, 10)] + [turn(10, stop="blocked")]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    lines = end_of_run_lines([log])
    assert lines[0].startswith("Policy effectiveness: G1 gate FAILED")
    assert any(ln.strip().startswith("GATE handlers") for ln in lines)
    assert any(ln.strip().startswith("GATE run") for ln in lines)


def test_end_of_run_lines_for_a_clean_log(tmp_path: Path):
    log = tmp_path / "STD_T1_01_20261111T000000Z.jsonl"
    log.write_text(json.dumps(turn(1)) + "\n", encoding="utf-8")
    assert end_of_run_lines([log])[0].startswith("Policy effectiveness: G1 gate clean")


def logged(logger, msg):
    return (f"policies.{logger}", "INFO", msg)


def test_logged_activations_are_the_used_count_and_wasted_people_are_flagged():
    bot = [gp_line(1, [f"P{i}" for i in range(6)])]
    bot += [
        logged(
            "secondary",
            f"great_people activated turn {n}: P{n} (UNIT_GREAT_ENGINEER) at A after 2",
        )
        for n in range(5)
    ]
    bot += [
        logged(
            "secondary",
            f"great_people wasted turn {n}: P{n} (UNIT_GREAT_ENGINEER) left the map unused",
        )
        for n in range(4)
    ]
    run = RunLogs(records=[turn(1, {"activate_great_person": 0})], bot=bot, has_bot_log=True)
    checks = by_name(run)
    assert checks[("great people", "seen vs activated")].used == 5
    wasted = checks[("great people", "wasted (left the map unused or idle 20 turns)")]
    assert (wasted.created, wasted.status) == (4, FAIL)


def test_gold_totals_logged_per_turn_replace_the_call_based_bound():
    bot = [
        logged("gold", f"gold_spent turn total: {s} of 100 to spend") for s in (0, 0, 40, 60, 100)
    ]
    run = RunLogs(records=[turn(1)], bot=bot, has_bot_log=True)
    checks = by_name(run)
    idle = checks[("gold", "turns above reserve with nothing bought")]
    assert (idle.created, idle.used, idle.status) == (5, 2, PASS)
    assert checks[("gold", "gold spent")].used == 200


def test_envoy_tokens_held_on_turns_with_no_envoy_sent_are_flagged():
    held = lambda n: int(10 <= n <= 20)  # noqa: E731
    bot = [logged("wars", f"envoy_tokens turn {n}: {held(n)} available") for n in range(5, 30)]
    records = [
        turn(n, {"send_envoy": 1} if n in (10, 11, 12, 13, 14) else {}) for n in range(5, 30)
    ]
    run = RunLogs(records=records, bot=bot, has_bot_log=True)
    check = by_name(run)[("diplomacy", "turns an envoy token stayed unspent")]
    assert (check.created, check.status) == (6, FAIL)


def test_a_token_spent_the_turn_it_is_read_is_not_unspent():
    bot = [logged("wars", f"envoy_tokens turn {n}: 1 available") for n in range(5, 25)]
    records = [turn(n, {"send_envoy": 1}) for n in range(5, 25)]
    run = RunLogs(records=records, bot=bot, has_bot_log=True)
    assert by_name(run)[("diplomacy", "turns an envoy token stayed unspent")].created == 0


def test_a_wasted_person_idle_then_gone_counts_once_and_prophets_count_as_used():
    names = [f"P{i}" for i in range(6)]
    bot = [gp_line(1, names)]
    for n in names[:2]:
        bot.append(
            logged(
                "secondary",
                f"great_people idle turn 30: {n} (UNIT_GREAT_ENGINEER) on the map for 20 turns",
            )
        )
        bot.append(
            logged(
                "secondary",
                f"great_people wasted turn 40: {n} (UNIT_GREAT_ENGINEER) left the map unused",
            )
        )
    bot.append(
        logged(
            "secondary",
            "great_people activated turn 5: P2 (UNIT_GREAT_ENGINEER) at A after 2 turns",
        )
    )
    for _ in range(3):
        bot.append(logged("faith", "Great Prophet founded a religion in Ur: RELIGION_FOUNDED|x"))
    run = RunLogs(records=[turn(1, {"activate_great_person": 0})], bot=bot, has_bot_log=True)
    checks = by_name(run)
    assert checks[("great people", "wasted (left the map unused or idle 20 turns)")].created == 2
    assert checks[("great people", "seen vs activated")].used == 4


def test_envoy_tokens_without_lines_are_no_data():
    run = RunLogs(records=[turn(1)], bot=[logged("wars", "x")], has_bot_log=True)
    assert by_name(run)[("diplomacy", "turns an envoy token stayed unspent")].status == NO_DATA


def test_older_logs_without_the_new_great_person_lines_have_no_wasted_figure():
    run = RunLogs(records=[turn(1)], bot=[gp_line(1, ["P1", "P2"])], has_bot_log=True)
    check = by_name(run)[("great people", "wasted (left the map unused or idle 20 turns)")]
    assert check.status == NO_DATA


def test_envoy_tokens_need_call_counts_to_judge_unspent_turns():
    bot = [logged("wars", f"envoy_tokens turn {n}: 1 available") for n in range(5, 12)]
    run = RunLogs(records=[{"turn": 5, "detail": ""}], bot=bot, has_bot_log=True)
    assert by_name(run)[("diplomacy", "turns an envoy token stayed unspent")].status == NO_DATA


def test_the_activation_fallback_is_chosen_per_session(tmp_path: Path):
    old = tmp_path / "STD_T1_01_20261111T000000Z.jsonl"
    new = tmp_path / "STD_T1_01_20261111T010000Z.jsonl"
    old.write_text(json.dumps(turn(1, {"activate_great_person": 3})) + "\n", encoding="utf-8")
    new.write_text(json.dumps(turn(2, {"activate_great_person": 1})) + "\n", encoding="utf-8")
    names = [f"P{i}" for i in range(6)]
    gp = f"{STAMP} civ6_ai.policies.secondary INFO great_people turn 1: 6 on the map: " + "; ".join(
        f"{n} (UNIT_GREAT_ENGINEER) at 1,1: x" for n in names
    )
    act = (
        f"{STAMP} civ6_ai.policies.secondary INFO great_people activated turn 2: "
        "P3 (UNIT_GREAT_ENGINEER) at A after 2 turns"
    )
    old.with_name(old.stem + "_bot.log").write_text(gp + "\n", encoding="utf-8")
    new.with_name(new.stem + "_bot.log").write_text(act + "\n", encoding="utf-8")
    check = by_name(load_run([old, new]))[("great people", "seen vs activated")]
    assert check.used == 4


def test_the_gold_measure_is_chosen_per_session(tmp_path: Path):
    old = tmp_path / "STD_T1_01_20261111T000000Z.jsonl"
    new = tmp_path / "STD_T1_01_20261111T010000Z.jsonl"
    rows = [turn(n) for n in range(1, 7)]
    for row in rows:
        row["handler_calls"] = {"gold": {"list_city_production": [1, 0.1]}}
    old.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    new.write_text(json.dumps(turn(7, {"purchase_item": 1})) + "\n", encoding="utf-8")
    old.with_name(old.stem + "_bot.log").write_text("", encoding="utf-8")
    line = f"{STAMP} civ6_ai.policies.gold INFO gold_spent turn total: 50 of 100 to spend\n"
    new.with_name(new.stem + "_bot.log").write_text(line, encoding="utf-8")
    check = by_name(load_run([old, new]))[("gold", "turns above reserve with nothing bought")]
    assert (check.created, check.used) == (7, 6)


def _refused_event(city="Coba", item="DISTRICT_HARBOR", reply="ERR:NO_PLACEMENT|x: none"):
    return {
        "kind": "production_refused",
        "turn": 3,
        "city_name": city,
        "item": item,
        "reply": reply,
    }


def test_production_facts_read_the_structured_events_without_a_bot_log():
    events = [_refused_event()] * 30
    run = RunLogs(records=[turn(1)], events=events, has_events=True)
    check = by_name(run)[("production", "most repeats of one refused build")]
    assert check.status == FAIL
    assert "Coba DISTRICT_HARBOR" in check.note
    shares = by_name(run)[("production", "refused vs accepted choices")]
    assert "ERR x30" in shares.note and "from events" in shares.note


def test_production_facts_fall_back_to_text_for_a_session_with_old_events():
    text = "Coba can't build DISTRICT_HARBOR: ERR:NO_PLACEMENT|DISTRICT_HARBOR: none"
    old = RunLogs(
        records=[turn(1)],
        bot=[bot("production", text)] * 30,
        has_bot_log=True,
        events=[{"kind": "war_declared", "turn": 1}],
        has_events=True,
    )
    new = RunLogs(records=[turn(2)], events=[_refused_event("Rome")] * 2, has_events=True)
    run = RunLogs(
        records=old.records + new.records,
        bot=old.bot,
        events=old.events + new.events,
        has_bot_log=True,
        has_events=True,
        sessions=[old, new],
    )
    check = by_name(run)[("production", "most repeats of one refused build")]
    assert check.created == 30  # The old session's text counts, and the new one's events.


def test_production_facts_say_no_data_when_nothing_was_logged():
    run = RunLogs(records=[turn(1)], events=[{"kind": "war_declared", "turn": 1}], has_events=True)
    assert by_name(run)[("production", "builds refused")].status == NO_DATA
