import re
import subprocess

import pytest

import check_public_ready as cpr

PERSON_EMAIL = "person" + "@" + "example.org"
WINDOWS_PATH = "C:" + "\\Users\\" + "someone\\saves"
POSIX_PATH = "/home/" + "someone/saves"


@pytest.mark.parametrize("text", [PERSON_EMAIL, WINDOWS_PATH, POSIX_PATH])
def test_flags_personal_text(text):
    assert cpr.scan_text(text, [])


@pytest.mark.parametrize(
    "text",
    [
        "noreply" + "@" + "anthropic.com",
        "1+someone" + "@" + "users.noreply.github.com",
        "C:" + "\\Users\\" + "<you>\\saves",
        "C:" + "\\Users\\" + "you\\saves",
    ],
)
def test_allows_safe_text(text):
    assert cpr.scan_text(text, []) == []


def test_personal_pattern_is_not_echoed():
    problems = cpr.scan_text("Hello Secretname", [re.compile("secretname", re.IGNORECASE)])
    assert problems == ["personal pattern matched"]


@pytest.mark.parametrize("name", [".env", "game.Civ6Save", "x/log.jsonl", "t.parquet"])
def test_flags_forbidden_files(name):
    assert cpr.scan_name(name)


def test_example_env_is_allowed():
    assert cpr.scan_name(".env.example") == []


def git(root, *args):
    identity = ["-c", "user.name=t", "-c", "user.email=t@users.noreply.github.com"]
    subprocess.run(["git", *identity, *args], cwd=root, check=True, capture_output=True)


def test_history_finds_removed_text(tmp_path):
    git(tmp_path, "init", "-q")
    planted = tmp_path / "notes.txt"
    planted.write_text(PERSON_EMAIL, encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "add")
    planted.write_text("clean", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "remove")

    assert cpr.scan_worktree(tmp_path, []) == []
    assert any(PERSON_EMAIL in p for p in cpr.scan_history(tmp_path, []))
