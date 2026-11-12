import json

from civ6_ai.triage import NOT_NEEDED, WORTH_READING, assess, format_report, triage


def rec(turn, stop=None, detail="", errors=None):
    return {"turn": turn, "stop_reason": stop, "detail": detail, "handler_errors": errors or {}}


def test_an_empty_log_has_nothing_to_look_at():
    assert assess([], []).screenshot == NOT_NEEDED


def test_a_clean_run_needs_no_screenshot():
    assert assess([rec(1), rec(2)], []).screenshot == NOT_NEEDED


def test_game_over_is_explained_by_the_report():
    assert assess([rec(9, "game_over", "GAME OVER")], []).screenshot == NOT_NEEDED


def test_a_blocked_turn_with_a_named_blocker_is_explained():
    records = [rec(5, "blocked", "Cannot end turn: ENDTURN_BLOCKING_PRODUCTION")]
    verdict = assess(records, [])
    assert verdict.screenshot == NOT_NEEDED
    assert "ENDTURN_BLOCKING_PRODUCTION" in verdict.reasons[0]


def test_a_blocker_named_only_in_the_bot_log_counts():
    verdict = assess([rec(5, "hung", "HANG:5:save")], ["INFO open DIPLOMACY_SESSION"])
    assert verdict.screenshot == NOT_NEEDED


def test_a_blocked_turn_with_no_known_blocker_is_worth_a_screenshot():
    verdict = assess([rec(5, "blocked", "Turn still reads 5")], ["INFO nothing relevant"])
    assert verdict.screenshot == WORTH_READING


def test_a_blocker_from_an_old_turn_does_not_explain_a_new_stop():
    records = [
        rec(1, detail="ENDTURN_BLOCKING_RESEARCH"),
        rec(2),
        rec(3),
        rec(4),
        rec(5, "blocked"),
    ]
    assert assess(records, []).screenshot == WORTH_READING


def test_handler_errors_point_at_the_bot_log():
    verdict = assess([rec(5, errors={"faith": "KeyError"})], [])
    assert verdict.screenshot == NOT_NEEDED
    assert "faith" in verdict.reasons[0]


def write_run(tmp_path, records, bot=None):
    log = tmp_path / "STD_T1_01_20260101T000000Z.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    if bot:
        log.with_name(f"{log.stem}_bot.log").write_text(bot, encoding="utf-8")
    return log


def test_triage_reads_the_logs_and_finds_the_stop_turn_screenshot(tmp_path):
    log = write_run(tmp_path, [rec(4), rec(5, "blocked", "Turn still reads 5")], "a\nb\n")
    shots = tmp_path / "screenshots"
    shots.mkdir()
    mine = shots / "STD_T1_01_20260101T000000Z_T005_blocked.jpg"
    mine.write_bytes(b"x")
    (shots / "STD_T1_01_20260101T000000Z_T004_x.jpg").write_bytes(b"x")
    result = triage(log)
    assert (result.turn, result.stop_reason) == (5, "blocked")
    assert result.bot_log_tail == ["a", "b"]
    assert result.screenshots == [mine]
    report = format_report(result)
    assert "Screenshot: worth reading" in report
    assert "T005_blocked.jpg" in report


def test_a_worth_reading_stop_without_a_shot_says_how_to_take_one(tmp_path):
    report = format_report(triage(write_run(tmp_path, [rec(5, "hung", "HANG:5:save")])))
    assert "No screenshot was taken" in report
