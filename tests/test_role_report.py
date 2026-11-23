import json

import role_report as rr


def unit(role, uid=1, **extra):
    return {"u": uid, "role": role, **extra}


def bot(row):
    return f"2026-11-23 10:00:00,000 civ6_ai.unit_roles INFO military_roles {json.dumps(row)}"


def write(tmp_path, rows, name="run"):
    (tmp_path / f"{name}_bot.log").write_text("\n".join(bot(r) for r in rows), encoding="utf-8")
    return str(tmp_path / name)


def full_rows():
    plan_short = {"at": [5, 5], "need": 10.0, "have": 4.0, "units": 1, "committed": False}
    plan_met = {"at": [5, 5], "need": 4.0, "have": 6.0, "units": 2, "committed": False}
    return [
        {"turn": 30, "cities": 2, "plan": plan_short, "short": [],
         "units": [unit("spare", 1, nw=3, nk="camp"), unit("garrison", 2), unit("spare", 3)]},
        {"turn": 31, "cities": 2, "plan": plan_met, "short": [{"id": 7}],
         "units": [unit("spare", 1, nw=2, nk="city"), unit("camp", 2)]},
        {"turn": 32, "cities": 4, "plan": None, "short": [],
         "units": [unit("spare", 1), unit("garrison", 2)]},
    ]  # fmt: skip


def test_every_count_on_a_run_that_has_each_source(tmp_path, capsys):
    rr.report(write(tmp_path, full_rows()), None, None)
    out = capsys.readouterr().out
    assert "unit-turns 7" in out
    assert "spare 4, garrison 2, camp 1" in out
    assert "spare share: 57% (4)" in out
    assert "camp plan is short: 2" in out  # Turn 30's two spare units.
    assert "any city is short: 1" in out  # Turn 31's one.
    assert "units per city: 0.88" in out  # 7 unit-turns over 8 city-turns.
    assert "2 turns x1, 3 turns x1; not measured for 1" in out  # Turn 30's unit 3 has no walk.


def test_the_turn_range_and_the_last_line_of_a_turn(tmp_path):
    rows = full_rows()
    rows.insert(0, {**rows[0], "units": [unit("garrison", 9)]})  # Same turn, logged earlier.
    path = rr.bot_log(write(tmp_path, rows))
    kept = rr.read_lines(path, 31, 31)
    assert [r["turn"] for r in kept] == [31]
    assert len(rr.read_lines(path, None, None)) == 3
    assert rr.summarise(rr.read_lines(path, None, None))["unit_turns"] == 7


def test_absent_sources_print_not_measured_never_zero(tmp_path, capsys):
    rows = [{"turn": 30, "cities": 1, "units": [unit("spare"), unit("garrison", 2)]}]
    rr.report(write(tmp_path, rows), None, None)
    out = capsys.readouterr().out
    assert "camp plan is short: not measured" in out
    assert "any city is short: not measured" in out
    assert "no spare unit-turn had an unmet need" in out


def test_a_need_with_no_walk_is_not_measured(tmp_path, capsys):
    rows = [{"turn": 30, "cities": 1, "short": [{"id": 1}], "plan": None,
             "units": [unit("spare")]}]  # fmt: skip
    rr.report(write(tmp_path, rows), None, None)
    assert "walk to the nearest unmet need: not measured (1 unit-turns)" in capsys.readouterr().out


def test_a_run_with_no_lines_and_the_command_line(tmp_path, capsys):
    assert rr.main([str(tmp_path / "missing.jsonl"), "--from", "30", "--to", "60"]) == 0
    assert "no military_roles lines (not measured)" in capsys.readouterr().out
    assert rr.main([write(tmp_path, full_rows()) + ".jsonl", "--to", "30"]) == 0
    assert "unit-turns 3" in capsys.readouterr().out


def test_garrison_in_a_city_not_short_is_split_by_map_reading(tmp_path, capsys):
    calm = {"short": False, "rd": "calm"}
    read = {"short": False, "rd": "map", "net": 0.0}
    rows = [{"turn": 30, "cities": 3, "units": [
        unit("garrison", 1, city=calm), unit("garrison", 2, city=read),
        unit("garrison", 3, city=read), unit("garrison", 4, city={"short": True, "rd": "map"}),
    ]}]  # fmt: skip
    rr.report(write(tmp_path, rows), None, None)
    assert "by map reading: calm 1, map 2" in capsys.readouterr().out
