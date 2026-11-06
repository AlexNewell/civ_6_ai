"""The two-set comparison, on small synthetic scores. No game or dataset is needed."""

import json

from civ6_ai.metrics.report import compare, format_report
from compare_runs import main


def run(score=None, rank=None, stop=None, speed="STANDARD", milestones=None):
    metrics = {"score": score, "rank": rank, "stop_reason": stop}
    return {
        "metrics": {k: v for k, v in metrics.items() if v is not None},
        "absent": {},
        "milestones": milestones or {},
        "run": {"game_speed": speed},
    }


def test_effect_and_counts():
    a = [run(score=s) for s in (10, 12, 11, 13, 9, 10, 12, 11)]
    b = [run(score=s) for s in (20, 22, 21, 23, 19, 20, 22, 21)]
    m = compare("a", a, "b", b)["metrics"]["score"]
    assert m["cliffs_delta"]["estimate"] == 1.0
    assert m["shift"]["estimate"] == 10
    low, high = m["shift"]["interval"]
    assert low > 0 and high >= low
    assert m["p_value"] < 0.01
    assert m["n"] == {"a": 8, "b": 8}


def test_small_sets_warn_and_cannot_reach_significance():
    m = compare("a", [run(score=1), run(score=2)], "b", [run(score=8), run(score=9)])["metrics"][
        "score"
    ]
    assert m["shift"]["interval"] is None
    text = " ".join(m["warnings"])
    assert "no interval" in text
    assert "cannot reach p < 0.05" in text
    assert m["p_value"] == 2 / 6 * 1  # exact: 6 splits, the observed one and its mirror


def test_absent_is_not_zero():
    a = [run(score=10, rank=1), run(score=10), run(score=10, rank=2)]
    b = [run(score=10, rank=4), run(score=10, rank=5), run(score=10)]
    m = compare("a", a, "b", b)["metrics"]["rank"]
    assert m["n"] == {"a": 2, "b": 2}
    assert m["absent"] == {"a": 1, "b": 1}
    assert m["median"] == {"a": 1.5, "b": 4.5}
    assert any("lack this metric" in w for w in m["warnings"])


def test_metric_missing_from_one_set_is_not_compared():
    m = compare("a", [run(score=1)], "b", [run(score=2, rank=3)])["metrics"]["rank"]
    assert m["shift"] is None and m["p_value"] is None
    assert any("no run in A" in w for w in m["warnings"])


def test_non_numeric_skipped_and_wins_count_as_one_zero():
    a = [run(stop="done"), {"metrics": {"won": False}, "milestones": {}}]
    b = [{"metrics": {"won": True}, "milestones": {}}]
    result = compare("a", a, "b", b)
    assert result["skipped_non_numeric"] == ["stop_reason"]
    assert result["metrics"]["won"]["median"] == {"a": 0.0, "b": 1.0}


def test_milestones_are_compared_where_present():
    a = [run(milestones={"50": {"score": 5}}), run()]
    b = [run(milestones={"50": {"score": 9}})]
    m = compare("a", a, "b", b)["metrics"]["turn_50_score"]
    assert m["n"] == {"a": 1, "b": 1}
    assert m["shift"]["estimate"] == 4


def test_mixed_speed_warns():
    result = compare("a", [run(score=1)], "b", [run(score=1, speed="ONLINE")])
    assert any("differ in game_speed" in w for w in result["warnings"])


def test_deterministic_and_seed_sensitive():
    a = [run(score=s) for s in (1, 4, 2, 8, 5)]
    b = [run(score=s) for s in (6, 9, 3, 12, 7)]
    first = compare("a", a, "b", b, seed=3)
    assert first == compare("a", a, "b", b, seed=3)
    other = compare("a", a, "b", b, seed=4)["metrics"]["score"]["shift"]["interval"]
    assert first["metrics"]["score"]["shift"]["interval"] is not None
    assert other is not None


def test_large_sets_use_monte_carlo():
    a = [run(score=s) for s in range(12)]
    b = [run(score=s + 3) for s in range(12)]
    m = compare("a", a, "b", b, resamples=50)["metrics"]["score"]
    assert m["p_method"] == "Monte Carlo permutation"
    assert 0 < m["p_value"] <= 1


def test_format_report_mentions_warnings():
    text = format_report(compare("old", [run(score=1)], "new", [run(score=2)]))
    assert "old" in text and "new" in text and "no interval" in text


def test_command(tmp_path, capsys):
    files = []
    for i, s in enumerate((1, 2, 3, 4)):
        path = tmp_path / f"{i}.json"
        path.write_text(json.dumps(run(score=s)), encoding="utf-8")
        files.append(str(path))
    argv = ["--label-a", "x", "--a", *files[:2], "--label-b", "y", "--b", *files[2:], "--json"]
    assert main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["labels"] == {"a": "x", "b": "y"}


def test_command_rejects_bad_input(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("[1]", encoding="utf-8")
    assert main(["--label-a", "x", "--a", str(bad), "--label-b", "y", "--b", str(bad)]) == 1
    assert "FAILED" in capsys.readouterr().err
