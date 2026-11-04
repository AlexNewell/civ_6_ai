import copy
from pathlib import Path

import pytest

import seed_github as sg


@pytest.fixture
def seed():
    return sg.load_seed()


def test_seed_is_valid(seed):
    assert sg.validate(seed) == []


def test_roadmap_matches_seed(seed):
    text = Path(sg.ROADMAP_PATH).read_text(encoding="utf-8")
    assert sg.check_roadmap(seed, text) == []


def test_every_issue_has_acceptance_criteria(seed):
    assert all(issue["acceptance"] for issue in seed["issues"])


def test_detects_unknown_dependency(seed):
    broken = copy.deepcopy(seed)
    broken["issues"][0]["depends"] = ["nope"]
    assert any("unknown dependency" in e for e in sg.validate(broken))


def test_detects_cycle(seed):
    broken = copy.deepcopy(seed)
    broken["issues"][0]["depends"] = ["T.2"]
    assert any("cycle" in e for e in sg.validate(broken))


def test_body_links_known_numbers_and_marks_others_pending():
    issue = {
        "id": "1.5",
        "goal": "g",
        "scope": "s",
        "acceptance": ["a"],
        "depends": ["1.2", "1.3"],
        "budget": "none",
    }
    body = sg.render_body(issue, {"1.2": 7})
    assert "<!-- seed-id: 1.5 -->" in body
    assert "#7" in body
    assert f"1.3 {sg.PENDING}" in body
    assert "- [ ] a" in body


def test_link_roadmap_is_repeatable(tmp_path):
    path = tmp_path / "roadmap.md"
    path.write_text("- `1.5` City production policy\n- `0.1` Setup\n", encoding="utf-8")
    sg.link_roadmap({"1.5": 9}, "owner/repo", path)
    once = path.read_text(encoding="utf-8")
    assert "- [`1.5`](https://github.com/owner/repo/issues/9) City production policy" in once
    assert "- `0.1` Setup" in once
    sg.link_roadmap({"1.5": 9}, "owner/repo", path)
    assert path.read_text(encoding="utf-8") == once
    assert sg.roadmap_items(once)["1.5"] == "City production policy"
