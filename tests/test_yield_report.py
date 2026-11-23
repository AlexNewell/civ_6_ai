import json
from pathlib import Path

import pytest

import yield_report as yr


def city(city_id, name, **over):
    row = {"city_id": city_id, "name": name, "pop": 3, "food": 2.0, "production": 2.0,
           "gold": 1.0, "science": 1.0, "culture": 1.0, "faith": 1.0, "housing": 4.0,
           "amenities": 1}  # fmt: skip
    return {**row, **over}


def turn_row(turn, cities, detail=""):
    totals = {k: sum(c[k] for c in cities) for k in yr.YIELDS}
    return {"turn": turn, "cities": len(cities), "yields": totals, "city_yields": cities,
            "detail": detail}  # fmt: skip


def write_run(tmp_path: Path, name, rows, events=(), bot=()):
    log = tmp_path / f"{name}.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    if events:
        (tmp_path / f"{name}_events.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events), encoding="utf-8"
        )
    if bot:
        (tmp_path / f"{name}_bot.log").write_text("\n".join(bot), encoding="utf-8")
    return log


def bot_line(logger, tag, payload):
    return f"2026-11-22 10:00:00,000 civ6_ai.policies.{logger} INFO {tag} {json.dumps(payload)}"


def offense(turn, near, captured=0, recaptures=0, units=3):
    payload = {"turn": turn, "barb_near": near, "civilians_captured": captured,
               "recaptures": recaptures, "combat_units": units}  # fmt: skip
    return bot_line("military", "military_offense", payload)


# Weighted yield of one default city: science 1.25 + culture 1 + faith 0.5 + production 2 = 4.75.
ONE = 4.75


def test_weights_come_from_production_weights():
    assert yr.weights() == {"science": 1.25, "culture": 1.0, "faith": 0.5, "production": 1.0}


def test_yield_turns_per_city_and_empire(tmp_path):
    rows = [
        turn_row(1, [city(1, "A")]),
        turn_row(2, [city(1, "A"), city(2, "B")]),
        turn_row(3, [city(1, "A"), city(2, "B")]),
        turn_row(4, [city(1, "A")]),  # City B is lost.
    ]
    log = write_run(
        tmp_path,
        "r",
        rows,
        bot=[
            offense(1, 0, units=4),
            offense(2, 2, captured=1, units=4),
            offense(3, 1, captured=2, recaptures=1, units=2),  # Two combat units gone.
            offense(4, 0, units=3),  # A rise is not a loss.
            offense(5, 0, units=1),  # Past nothing: the window is T1-T60, so this counts.
            bot_line("military", "military_camp_gone", {"turn": 2, "camp": [1, 1], "by": "us"}),
            bot_line("military", "military_camp_gone", {"turn": 3, "camp": [2, 2], "by": "other"}),
            bot_line("military", "military_camp_gone", {"turn": 70, "camp": [3, 3], "by": "us"}),
            bot_line("builder", "builder_decision", {"turn": 1}),
            bot_line("builder", "builder_lost", {"turn": 3, "unit_id": 9}),
            bot_line("builder", "builder_lost", {"turn": 4, "unit_id": 10}),
        ],
    )
    report = yr.analyse("r", log)
    assert report.empire == pytest.approx(6 * ONE)
    assert report.cities[1]["yield_turns"] == pytest.approx(4 * ONE)
    assert report.cities[2]["founded"] == 2
    assert report.city_turns == 6
    assert report.per_city == pytest.approx(6 * ONE / (6 / 4))
    assert report.founding_turns == [1, 2]
    assert report.barb_near_turns == 2
    assert report.cities_lost == 1
    assert report.units_lost == 4  # 4 -> 2, then 3 -> 1.
    assert report.builders_lost == 2
    assert report.camps_cleared == 1
    # The counters reset after each line, so the total is their sum.
    assert (report.civilians_captured, report.recaptures) == (3, 1)
    text = yr.format_single(report)
    text = "\n".join(text)
    assert "ProductionWeights" in text and "A: T1" in text and "lower bound" in text


def test_sources_that_are_absent_are_not_measured(tmp_path):
    rows = [turn_row(1, [city(1, "A")])]
    log = write_run(tmp_path, "r", rows)  # No bot log at all.
    report = yr.analyse("r", log)
    assert report.cities_lost == 0  # Measured from the city count itself.
    for name in ("units_lost", "builders_lost", "camps_cleared", "barb_near_turns"):
        assert getattr(report, name) is None
    assert "not measured" in "\n".join(yr.format_single(report))
    # A bot log that has the policies' lines but no loss lines is a real zero.
    log = write_run(
        tmp_path, "z", rows, bot=[offense(1, 0), bot_line("builder", "builder_decision", {})]
    )
    report = yr.analyse("z", log)
    assert (report.builders_lost, report.camps_cleared, report.units_lost) == (0, 0, 0)


def test_checkpoints_stop_at_the_last_turn_reached(tmp_path):
    rows = [turn_row(t, [city(1, "A")]) for t in range(1, 25)]
    rows += [turn_row(t, [city(1, "A")]) for t in range(25, 41)]
    report = yr.analyse("r", write_run(tmp_path, "r", rows))
    assert report.checkpoints[20]["turn"] == 20
    assert report.checkpoints[40]["turn"] == 40
    assert 60 not in report.checkpoints


def test_old_logs_without_yields_or_side_files_report_none(tmp_path):
    rows = [{"turn": t, "cities": 1 + t // 3} for t in range(1, 7)]
    report = yr.analyse("old", write_run(tmp_path, "old", rows))
    assert report.empire is None and report.per_city is None
    assert report.barb_near_turns is None and report.cities_lost == 0
    assert report.founding_turns == [1, 3, 6]
    assert "not measured" in "\n".join(yr.format_single(report))


def test_turns_past_the_window_are_ignored(tmp_path):
    rows = [turn_row(t, [city(1, "A")]) for t in (1, 60, 61)]
    report = yr.analyse("r", write_run(tmp_path, "r", rows))
    assert report.turns_seen == 2


def test_pair_flags_a_sign_disagreement(tmp_path):
    # The change founded one city fewer: more per city, less in total.
    base = [turn_row(t, [city(1, "A", science=1), city(2, "B", science=1)]) for t in range(1, 11)]
    change = [turn_row(t, [city(1, "A", science=3)]) for t in range(1, 11)]
    a = yr.analyse("base", write_run(tmp_path, "a", base))
    b = yr.analyse("chg", write_run(tmp_path, "b", change))
    assert yr.pct_change(a.per_city, b.per_city) > 0
    assert yr.pct_change(a.empire, b.empire) < 0
    text = "\n".join(yr.format_pairs([a, b]))
    assert "FLAG" in text and "disagree in sign" in text


def test_three_pairs_print_the_mean(tmp_path):
    reports = []
    for i, boost in enumerate((1, 2, 3)):
        for label, sci in (("base", 1), ("chg", 1 + boost)):
            rows = [turn_row(t, [city(1, "A", science=sci)]) for t in range(1, 6)]
            reports.append(yr.analyse(label, write_run(tmp_path, f"{label}{i}", rows)))
    text = "\n".join(yr.format_pairs(reports))
    assert text.count("== pair") == 3
    assert "mean change over 3 pairs" in text
    assert "FLAG" not in text


def test_pair_with_missing_fields_does_not_crash(tmp_path):
    old = yr.analyse("old", write_run(tmp_path, "o", [{"turn": 1, "cities": 1}]))
    new = yr.analyse("new", write_run(tmp_path, "n", [turn_row(1, [city(1, "A")])]))
    text = "\n".join(yr.format_pairs([old, new]))
    assert "n/a" in text


def test_main_prints_and_rejects_an_odd_count(tmp_path, capsys):
    log = write_run(tmp_path, "r", [turn_row(1, [city(1, "A")])])
    assert yr.main([str(log.with_suffix(""))]) == 0
    assert "empire yield-turns" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        yr.main([f"a={log}", f"b={log}", f"c={log}"])


def test_specs_take_labels():
    assert yr.parse_specs(["x=runs/a"]) == [("x", Path("runs/a.jsonl"))]
    assert yr.parse_specs(["runs/b_turns"])[0][0] == "b"
