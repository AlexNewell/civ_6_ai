"""Playbooks: the parser, the runner and the combining rule (issue 244)."""

import asyncio
import contextlib
import copy
import json
from pathlib import Path

import pytest

from civ6_ai.strategy.playbook import (
    ActiveLevel,
    PlaybookError,
    combine,
    evaluate,
    load_playbooks,
    parse_condition,
    parse_playbook,
    parse_playbook_json,
)
from civ6_ai.strategy.playbook_runner import (
    PlaybookRunner,
    Readings,
    read_occurrences,
    summarise,
)
from civ6_ai.strategy.policy_document import (
    DEFAULT_DOCUMENT,
    Posture,
    parse_policy_document,
    resolve,
)

EXAMPLE = Path(__file__).parent.parent / "playbooks" / "EXAMPLE_barbarian_pressure.json"


def cmp(signal, op, value):
    return {"signal": signal, "op": op, "value": value}


def make(**over):
    data = {
        "version": 1,
        "name": "pb",
        "playbook_version": "1",
        "trigger": {"when": cmp("camps_near_city", ">=", 1)},
        "levels": [
            {"name": "low", "urgency": 1, "raise_when": cmp("city_danger_max", ">=", 50)},
            {"name": "high", "urgency": 2, "lower_when": cmp("city_danger_max", "<", 20)},
        ],
        "end": {"when": cmp("camps_near_city", "==", 0), "hold": 2},
        "raise_hold": 2,
        "lower_hold": 3,
        "min_dwell": 0,
    }
    data.update(over)
    return data


def problems_of(data):
    with pytest.raises(PlaybookError) as error:
        parse_playbook(data)
    return error.value.problems


def has(problems, text):
    return any(text in p for p in problems)


def parse_cond(raw):
    problems = []
    cond = parse_condition(raw, "c", problems)
    assert not problems
    return cond


# Parser


def test_valid_playbook_parses():
    playbook = parse_playbook(make())
    assert playbook.name == "pb"
    assert [level.name for level in playbook.levels] == ["low", "high"]
    assert playbook.end.hold == 2
    assert playbook.raise_hold == 2


def test_example_file_parses_and_is_marked_example():
    playbook = parse_playbook_json(EXAMPLE.read_text(encoding="utf-8"))
    assert playbook.example
    assert playbook.levels[2].settings.posture is Posture.DEFEND


def test_not_an_object_and_bad_json():
    assert problems_of([]) == ["playbook: must be an object"]
    with pytest.raises(PlaybookError):
        parse_playbook_json("{nope")


def test_every_problem_is_listed_together():
    data = make(name="bad name", bogus=1, raise_hold=0)
    data["levels"][0]["urgency"] = "x"
    data["trigger"] = {"when": cmp("nonsense", "~", "a")}
    problems = problems_of(data)
    for text in ("unknown fields", "name:", "raise_hold", "urgency", "signal", "op", "value"):
        assert has(problems, text), text


@pytest.mark.parametrize("key", ["name", "playbook_version", "trigger", "levels", "end"])
def test_missing_required_field(key):
    data = make()
    del data[key]
    assert has(problems_of(data), f"missing {key}")


def test_wrong_schema_version():
    assert has(problems_of(make(version=2)), "version")


def test_levels_must_rise_and_be_reachable():
    data = make()
    data["levels"][1]["urgency"] = 1
    assert has(problems_of(data), "urgency must rise")
    data = make()
    del data["levels"][0]["raise_when"]
    assert has(problems_of(data), "trap")
    data = make()
    data["levels"][1]["raise_when"] = cmp("turn", ">", 1)
    assert has(problems_of(data), "top level cannot be raised")
    data = make()
    data["levels"][0]["lower_when"] = cmp("turn", ">", 1)
    assert has(problems_of(data), "lowest level cannot be lowered")
    data = make()
    data["levels"][1]["name"] = "low"
    assert has(problems_of(data), "used twice")


def test_level_count_and_shape():
    assert has(problems_of(make(levels=[])), "non-empty")
    assert has(problems_of(make(levels=[{"name": "a", "urgency": 1}] * 7)), "more than")
    assert has(problems_of(make(levels=["x"])), "must be an object")


def test_level_settings_use_the_policy_document_rules():
    data = make()
    data["levels"][1]["settings"] = {"posture": "nope", "weights": {"production": {"zz": 1}}}
    problems = problems_of(data)
    assert has(problems, "levels[1].settings: posture")
    assert has(problems, "unknown weight")


def army(**over):
    return {"setting": "production.army_per_city", "step": 0.1, "min": 1, "max": 2, **over}


@pytest.mark.parametrize(
    "nudge, text",
    [
        (army(setting="production.nope"), "allow"),
        (army(setting="army"), "allow"),
        (army(step=9), "step"),
        (army(step=-1), "step"),
        (army(min=3), "above max"),
        (army(min=-1), "between"),
        ({"setting": "production.army_per_city", "step": 0.1, "min": 1}, "missing"),
        (army(step="a"), "finite"),
        (army(setting="production.target_cities", min=0, max=99), "bounds"),
        (army(setting="military.heal_below", min=0, max=2), "share of health"),
        (army(extra=1), "unknown fields"),
    ],
)
def test_invalid_nudges(nudge, text):
    data = make()
    data["levels"][0]["nudges"] = [nudge]
    assert has(problems_of(data), text)


def test_nudge_twice_in_a_level_is_refused():
    data = make()
    data["levels"][0]["nudges"] = [army(), army()]
    assert has(problems_of(data), "nudged twice")


@pytest.mark.parametrize(
    "raw, text",
    [
        ({}, "exactly one"),
        ({"all": []}, "non-empty"),
        ({"all": [cmp("turn", ">", 1)], "any": [cmp("turn", ">", 1)]}, "exactly one"),
        ({"signal": "turn", "op": ">", "value": 1, "x": 1}, "unknown fields"),
        ({"signal": "turn", "op": ">", "value": True}, "finite"),
        ({"signal": "turn", "op": ">", "value": float("inf")}, "finite"),
        ({"eval": "1+1"}, "exactly one"),
        ({"all": [cmp("turn", ">", 1)] * 9}, "more than"),
        ("turn > 1", "must be an object"),
    ],
)
def test_invalid_conditions(raw, text):
    problems = []
    parse_condition(raw, "c", problems)
    assert has(problems, text)


def test_condition_depth_is_limited():
    raw = cmp("turn", ">", 1)
    for _ in range(6):
        raw = {"not": raw}
    problems = []
    parse_condition(raw, "c", problems)
    assert has(problems, "deep")


def test_gate_and_hold_problems():
    assert has(problems_of(make(end={"hold": 2})), "`when`")
    assert has(problems_of(make(end={"when": cmp("turn", ">", 1), "hold": 99})), "end.hold")
    assert has(problems_of(make(outcome_signals=["nope"])), "outcome_signals")
    assert has(problems_of(make(example="yes")), "example")


def test_evaluate():
    values = {"turn": 10, "cities": 3}
    assert evaluate(parse_cond(cmp("turn", ">=", 10)), values)
    assert not evaluate(parse_cond(cmp("turn", ">", 10)), values)
    assert evaluate(parse_cond({"all": [cmp("turn", ">", 1), cmp("cities", "==", 3)]}), values)
    assert evaluate(parse_cond({"any": [cmp("turn", ">", 99), cmp("cities", "<", 5)]}), values)
    assert evaluate(parse_cond({"not": cmp("turn", ">", 99)}), values)
    assert not evaluate(parse_cond(cmp("army_ratio", ">", 0)), values)  # Missing reads false.


def test_loader_skips_examples_and_lists_refusals(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(make()), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(make(example=True, name="ex")), encoding="utf-8")
    (tmp_path / "c.json").write_text("{", encoding="utf-8")
    (tmp_path / "d.json").write_text(json.dumps(make()), encoding="utf-8")  # Same name as a.
    found, refused = load_playbooks(tmp_path)
    assert [p.name for p in found] == ["pb"]
    assert set(refused) == {"c.json", "d.json"}
    found, _ = load_playbooks(tmp_path, include_examples=True)
    assert [p.name for p in found] == ["pb", "ex"]


def test_shipped_directory_loads_nothing():
    found, refused = load_playbooks(EXAMPLE.parent)
    assert found == [] and refused == {}


def test_parsing_does_not_change_its_input():
    data = make()
    before = copy.deepcopy(data)
    parse_playbook(data)
    assert data == before


# Runner


class FakePolicy:
    def __init__(self):
        self.weights = None


def runner_for(data=None, enabled=True, records=None, base=DEFAULT_DOCUMENT):
    policy = FakePolicy()
    runner = PlaybookRunner(
        [parse_playbook(data or make())],
        signals=None,
        enabled=enabled,
        records=records,
        base_document=lambda: base,
        production=policy,
    )
    return runner, policy


def feed(runner, turn, **values):
    runner.step(Readings(turn, values))


def level_of(runner):
    occurrence = runner.open["pb"]
    return occurrence.playbook.levels[occurrence.level].name


def test_off_by_default_touches_nothing():
    policy = FakePolicy()

    async def provider(game):
        raise AssertionError("an off runner must not even read")

    runner = PlaybookRunner([parse_playbook(make())], provider, production=policy)
    assert not runner.enabled
    asyncio.run(runner.run(object()))
    assert policy.weights is None and runner.open == {}


def test_runner_reads_through_the_provider_when_enabled():
    policy = FakePolicy()

    async def provider(game):
        return Readings(5, {"camps_near_city": 1})

    runner = PlaybookRunner([parse_playbook(make())], provider, enabled=True, production=policy)
    asyncio.run(runner.run(object()))
    assert "pb" in runner.open
    assert policy.weights is not None


def test_no_trigger_means_no_weights_ever_applied():
    runner, policy = runner_for()
    for turn in range(1, 6):
        feed(runner, turn, camps_near_city=0, city_danger_max=0)
    assert runner.open == {} and policy.weights is None


def test_trigger_starts_an_occurrence_at_the_lowest_level():
    runner, _ = runner_for()
    feed(runner, 10, camps_near_city=1)
    assert level_of(runner) == "low"
    assert runner.open["pb"].start_turn == 10


def test_trigger_hold_needs_consecutive_turns():
    runner, _ = runner_for(make(trigger={"when": cmp("camps_near_city", ">=", 1), "hold": 2}))
    feed(runner, 1, camps_near_city=1)
    feed(runner, 2, camps_near_city=0)
    feed(runner, 3, camps_near_city=1)
    assert runner.open == {}
    feed(runner, 4, camps_near_city=1)
    assert runner.open["pb"].start_turn == 4


def test_raise_needs_the_hold_then_moves_one_level():
    runner, _ = runner_for()
    feed(runner, 1, camps_near_city=1, city_danger_max=60)
    feed(runner, 2, camps_near_city=1, city_danger_max=60)
    assert level_of(runner) == "low"  # Held once after opening; needs two turns.
    feed(runner, 3, camps_near_city=1, city_danger_max=60)
    assert level_of(runner) == "high"
    assert runner.open["pb"].levels == [("low", 1), ("high", 3)]


def test_a_flicker_does_not_raise():
    runner, _ = runner_for()
    feed(runner, 1, camps_near_city=1)
    for turn in range(2, 12):
        danger = 60 if turn % 2 == 0 else 0  # Never two in a row.
        feed(runner, turn, camps_near_city=1, city_danger_max=danger)
    assert level_of(runner) == "low"


def test_lower_is_slower_than_raise_and_hysteresis_holds_between():
    runner, _ = runner_for()
    for turn in (1, 2, 3):
        feed(runner, turn, camps_near_city=1, city_danger_max=60)
    assert level_of(runner) == "high"
    # Danger between the raise (50) and lower (20) lines changes nothing, however long.
    for turn in range(4, 14):
        feed(runner, turn, camps_near_city=1, city_danger_max=35)
    assert level_of(runner) == "high"
    for turn in (14, 15):
        feed(runner, turn, camps_near_city=1, city_danger_max=5)
    assert level_of(runner) == "high"  # lower_hold is 3.
    feed(runner, 16, camps_near_city=1, city_danger_max=5)
    assert level_of(runner) == "low"


def test_min_dwell_blocks_a_quick_second_move():
    runner, _ = runner_for(make(min_dwell=5, raise_hold=1))
    feed(runner, 1, camps_near_city=1, city_danger_max=60)
    for turn in (2, 3, 4, 5):
        feed(runner, turn, camps_near_city=1, city_danger_max=60)
        assert level_of(runner) == "low"
    feed(runner, 6, camps_near_city=1, city_danger_max=60)
    assert level_of(runner) == "high"


def test_end_closes_after_its_hold_and_a_new_one_can_start():
    runner, policy = runner_for()
    feed(runner, 1, camps_near_city=1, city_danger_max=60)
    feed(runner, 2, camps_near_city=0)
    assert "pb" in runner.open  # Held once of two.
    feed(runner, 3, camps_near_city=1)  # The streak breaks.
    feed(runner, 4, camps_near_city=0)
    feed(runner, 5, camps_near_city=0)
    assert runner.open == {}
    assert runner.closed[0].end_turn == 5
    assert policy.weights == resolve(DEFAULT_DOCUMENT).production  # The base is restored.
    feed(runner, 6, camps_near_city=1)
    assert runner.open["pb"].start_turn == 6


def test_applies_the_level_settings_and_nudges():
    data = make()
    data["levels"][0]["settings"] = {"posture": "defend"}
    data["levels"][0]["nudges"] = [army(step=0.5)]
    runner, policy = runner_for(data)
    feed(runner, 1, camps_near_city=1)
    defend = resolve(parse_policy_document({"posture": "defend"})).production
    assert policy.weights.military == defend.military
    assert policy.weights.army_per_city == 2.0  # 2.25 before the bound.


def test_a_changed_base_document_is_reapplied_without_a_level_change():
    holder = [DEFAULT_DOCUMENT]
    policy = FakePolicy()
    runner = PlaybookRunner(
        [parse_playbook(make())],
        None,
        enabled=True,
        base_document=lambda: holder[0],
        production=policy,
    )
    feed(runner, 1, camps_near_city=1)
    first = policy.weights
    holder[0] = parse_policy_document({"posture": "expand"})
    feed(runner, 2, camps_near_city=1)
    assert policy.weights != first


def test_backwards_turn_forgets_open_occurrences():
    runner, policy = runner_for()
    feed(runner, 50, camps_near_city=1)
    feed(runner, 10, camps_near_city=0)
    assert runner.open == {}
    assert policy.weights == resolve(DEFAULT_DOCUMENT).production


# Records


def test_records_roundtrip_with_unresolved_and_resolved(tmp_path):
    path = tmp_path / "run_playbooks.jsonl"
    runner, _ = runner_for(make(outcome_signals=["city_danger_max"]), records=path)
    feed(runner, 3, camps_near_city=1, city_danger_max=10)
    for turn in (4, 5, 6):
        feed(runner, turn, camps_near_city=1, city_danger_max=60)
    (still_open,) = read_occurrences(path)
    assert not still_open.resolved and still_open.status == "unresolved at game end"
    assert still_open.peak_level == "high"
    feed(runner, 7, camps_near_city=0, city_danger_max=2)
    feed(runner, 8, camps_near_city=0, city_danger_max=2)
    (record,) = read_occurrences(path)
    assert record.resolved and record.end_turn == 8 and record.turns_open == 5
    assert record.version == "1"
    assert record.levels == (("low", 3), ("high", 5))
    assert record.outcome["signals"]["city_danger_max"] == {"start": 10, "max": 60, "end": 2}
    table = summarise([record])
    assert table["pb v1"]["resolved"] == 1
    assert table["pb v1"]["mean_turns_to_resolve"] == 5


def test_reader_tolerates_missing_file_and_bad_lines(tmp_path):
    assert read_occurrences(tmp_path / "none.jsonl") == []
    path = tmp_path / "x.jsonl"
    path.write_text("not json\n{}\n", encoding="utf-8")
    assert read_occurrences(path) == []


# Combining


def level(name, urgency, **fields):
    data = make()
    data["levels"] = [
        {"name": name, "urgency": urgency, **fields, "raise_when": cmp("turn", ">", 1)},
        {"name": "top", "urgency": urgency + 1},
    ]
    return parse_playbook(data).levels[0]


def culture(value):
    return {"weights": {"production": {"culture": value}}}


def test_combine_with_nothing_active_is_the_plain_resolve():
    doc = parse_policy_document({"posture": "defend"})
    assert combine(doc, []) == resolve(doc)
    assert combine(DEFAULT_DOCUMENT) == resolve(DEFAULT_DOCUMENT)


def test_posture_first_then_playbook_overrides_it():
    base = parse_policy_document({"posture": "expand"})
    lvl = level("a", 1, settings={"posture": "defend"})
    expected = resolve(parse_policy_document({"posture": "defend"}))
    assert combine(base, [ActiveLevel("a", lvl, 5)]) == expected


def test_higher_urgency_wins_a_clash_whatever_the_order():
    low = ActiveLevel("l", level("l", 1, settings=culture(2.0)), 1)
    high = ActiveLevel("h", level("h", 3, settings=culture(5.0)), 9)
    for order in ([low, high], [high, low]):
        assert combine(DEFAULT_DOCUMENT, order).production.culture == 5.0


def test_tie_goes_to_the_earlier_started():
    one, two = level("a", 2, settings=culture(2.0)), level("b", 2, settings=culture(7.0))
    got = combine(DEFAULT_DOCUMENT, [ActiveLevel("b", two, 30), ActiveLevel("a", one, 10)])
    assert got.production.culture == 2.0
    got = combine(DEFAULT_DOCUMENT, [ActiveLevel("b", two, 10), ActiveLevel("a", one, 30)])
    assert got.production.culture == 7.0


def test_non_clashing_fields_from_both_apply():
    one = level("a", 1, settings=culture(2.0))
    two = level("b", 1, settings={"weights": {"production": {"faith": 3.0}}})
    got = combine(DEFAULT_DOCUMENT, [ActiveLevel("a", one, 1), ActiveLevel("b", two, 2)])
    assert (got.production.culture, got.production.faith) == (2.0, 3.0)


def test_explicit_document_weights_win_over_all():
    base = parse_policy_document(
        {
            "weights": {"production": {"culture": 1.5, "army_per_city": 2.0}},
            "expansion": {"target_cities": 6},
        }
    )
    tc = {"setting": "production.target_cities", "step": 1.0, "min": 1, "max": 20}
    lvl = level(
        "a",
        9,
        settings={**culture(9.0), "expansion": {"target_cities": 3}},
        nudges=[army(step=1.0, max=9), tc],
    )
    got = combine(base, [ActiveLevel("a", lvl, 1)])
    assert got.production.culture == 1.5
    assert got.production.army_per_city == 2.0
    assert got.production.target_cities == 6
    assert got.gold.target_cities == 6


def test_nudge_scales_clamps_and_the_higher_level_nudge_wins():
    default = resolve(DEFAULT_DOCUMENT).production.army_per_city
    small = level("a", 1, nudges=[army(step=0.2, min=0.5, max=9)])
    big = level("b", 2, nudges=[army(step=1.0, min=0.5, max=2.5)])
    got = combine(DEFAULT_DOCUMENT, [ActiveLevel("a", small, 1)])
    assert got.production.army_per_city == pytest.approx(default * 1.2)
    both = combine(DEFAULT_DOCUMENT, [ActiveLevel("a", small, 1), ActiveLevel("b", big, 1)])
    assert both.production.army_per_city == 2.5
    cut = level("c", 1, nudges=[army(setting="production.target_cities", step=-0.5, min=3, max=12)])
    got = combine(DEFAULT_DOCUMENT, [ActiveLevel("c", cut, 1)])
    assert got.production.target_cities == 6 and got.gold.target_cities == 6
    assert isinstance(got.production.target_cities, int)


def test_the_example_playbook_top_level_combines():
    pb = parse_playbook_json(EXAMPLE.read_text(encoding="utf-8"))
    got = combine(DEFAULT_DOCUMENT, [ActiveLevel(pb.name, pb.levels[2], 1)])
    assert got.production.army_per_city <= 3.5
    assert got.production.expansion < resolve(DEFAULT_DOCUMENT).production.expansion


def test_signal_changes_persist_without_a_transition(tmp_path):
    path = tmp_path / "p.jsonl"
    runner, _ = runner_for(make(outcome_signals=["city_danger_max"]), records=path)
    feed(runner, 1, camps_near_city=1, city_danger_max=10)
    feed(runner, 2, camps_near_city=1, city_danger_max=40)  # Below the raise line: no move.
    (record,) = read_occurrences(path)
    assert record.levels == (("low", 1),)
    assert record.outcome["signals"]["city_danger_max"] == {"start": 10, "max": 40, "end": 40}


def test_pinned_base_target_beats_a_level_target_weight():
    base = parse_policy_document({"expansion": {"target_cities": 6}})
    lvl = level("a", 5, settings={"weights": {"production": {"target_cities": 3}}})
    got = combine(base, [ActiveLevel("a", lvl, 1)])
    assert got.production.target_cities == 6 and got.gold.target_cities == 6
    free = combine(DEFAULT_DOCUMENT, [ActiveLevel("a", lvl, 1)])
    assert free.production.target_cities == 3  # Unpinned, the level's weight still applies.


@pytest.mark.parametrize("field", ["signal", "op"])
def test_non_string_signal_or_op_is_a_refusal_not_a_crash(field, tmp_path):
    bad = {"signal": "turn", "op": ">", "value": 1, field: []}
    assert has(problems_of(make(trigger={"when": bad})), f"{field}:")
    (tmp_path / "bad.json").write_text(json.dumps(make(trigger={"when": bad})), encoding="utf-8")
    (tmp_path / "good.json").write_text(json.dumps(make(name="ok")), encoding="utf-8")
    found, refused = load_playbooks(tmp_path)
    assert [p.name for p in found] == ["ok"] and "bad.json" in refused


WEIRD = [[], {}, [[]], {"a": 1}, None, True, 1.5, float("inf"), float("nan"), -3, "x", ""]


def _paths():
    """Every field of a valid playbook, as a function that plants a value at it."""

    def top(key):
        return lambda data, v: data.__setitem__(key, v)

    def level_field(key):
        return lambda data, v: data["levels"][0].__setitem__(key, v)

    def nudge_field(key):
        def plant(data, v):
            data["levels"][0]["nudges"] = [army()]
            data["levels"][0]["nudges"][0][key] = v

        return plant

    def cond(key):
        return lambda data, v: data["trigger"]["when"].__setitem__(key, v)

    out = {
        f"top.{k}": top(k)
        for k in (
            "version",
            "name",
            "playbook_version",
            "example",
            "description",
            "trigger",
            "levels",
            "raise_hold",
            "lower_hold",
            "min_dwell",
            "end",
            "outcome_signals",
        )
    }
    out |= {
        f"level.{k}": level_field(k)
        for k in ("name", "urgency", "settings", "nudges", "raise_when", "lower_when")
    }
    out |= {f"nudge.{k}": nudge_field(k) for k in ("setting", "step", "min", "max")}
    out |= {f"cond.{k}": cond(k) for k in ("signal", "op", "value")}
    out["outcome.item"] = lambda data, v: data.__setitem__("outcome_signals", [v])
    out["trigger.hold"] = lambda data, v: data["trigger"].__setitem__("hold", v)
    out["end.hold"] = lambda data, v: data["end"].__setitem__("hold", v)
    out["all.child"] = lambda data, v: data["trigger"].__setitem__("when", {"all": [v]})
    out["settings.weights"] = lambda data, v: data["levels"][0].__setitem__(
        "settings", {"weights": v}
    )
    out["settings.domain"] = lambda data, v: data["levels"][0].__setitem__(
        "settings", {"weights": {"production": v}}
    )
    out["settings.posture"] = lambda data, v: data["levels"][0].__setitem__(
        "settings", {"posture": v}
    )
    return out


@pytest.mark.parametrize("path", sorted(_paths()))
@pytest.mark.parametrize("value", WEIRD, ids=repr)
def test_malformed_values_never_raise_anything_but_playbook_error(path, value):
    data = make()
    _paths()[path](data, copy.deepcopy(value))
    with contextlib.suppress(PlaybookError):  # A refusal is fine, and so is a value that is valid.
        parse_playbook(data)


def test_outcome_signal_of_a_non_string_is_listed(tmp_path):
    assert has(problems_of(make(outcome_signals=[[]])), "outcome_signals")
    (tmp_path / "bad.json").write_text(json.dumps(make(outcome_signals=[{}])), encoding="utf-8")
    (tmp_path / "good.json").write_text(json.dumps(make(name="ok")), encoding="utf-8")
    found, refused = load_playbooks(tmp_path)
    assert [p.name for p in found] == ["ok"] and "bad.json" in refused


@pytest.mark.parametrize("via", ["settings", "nudges"])
@pytest.mark.parametrize("domain", ["production", "gold"])
def test_pinned_target_wins_through_every_level_path(via, domain):
    base = parse_policy_document({"expansion": {"target_cities": 6}})
    if via == "settings":
        lvl = level("a", 5, settings={"weights": {domain: {"target_cities": 3}}})
    else:
        lvl = level(
            "a", 5, nudges=[army(setting=f"{domain}.target_cities", step=-0.5, min=1, max=9)]
        )
    got = combine(base, [ActiveLevel("a", lvl, 1)])
    assert got.production.target_cities == 6 and got.gold.target_cities == 6
