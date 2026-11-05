#!/usr/bin/env python3
"""Fail if the repo holds anything that should not become public.

Checks tracked and new files, and with --history every commit's content and metadata.
Personal strings belong in the untracked file .local/public-ready-patterns.txt, one
case-insensitive regular expression per line, so this check never publishes them.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_PATTERNS = ROOT / ".local" / "public-ready-patterns.txt"

# Real user paths only. Placeholders such as <you> don't match, and PLACEHOLDER_USERS are allowed.
PATH_PATTERN = re.compile(
    r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/[a-z]/Users/|/Users/|/home/)[A-Za-z0-9_.-]+",
)
EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![A-Za-z0-9])"
)
ALLOWED_EMAIL = re.compile(
    r"^(?:noreply@anthropic\.com|noreply@github\.com"
    r"|[A-Za-z0-9+._-]+@users\.noreply\.github\.com)$",
    re.IGNORECASE,
)
FORBIDDEN_SUFFIXES = {".civ6save", ".civ6replay", ".jsonl", ".parquet", ".eval", ".pem", ".key"}
FORBIDDEN_NAMES = {".env"}
PLACEHOLDER_USERS = {"me", "name", "user", "username", "you", "your", "yourname"}


def run_git(*args: str, cwd: Path = ROOT) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)
    return result.stdout.decode("utf-8", errors="replace")


def load_personal_patterns(path: Path = LOCAL_PATTERNS) -> list[re.Pattern[str]]:
    if not path.exists():
        return []
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [re.compile(line, re.IGNORECASE) for line in lines if line and not line.startswith("#")]


def scan_text(text: str, personal: list[re.Pattern[str]]) -> list[str]:
    """Return one reason per problem. Personal matches are never echoed."""
    problems: list[str] = []
    for match in PATH_PATTERN.finditer(text):
        user = re.split(r"[\\/]", match.group(0))[-1].lower()
        if user not in PLACEHOLDER_USERS:
            problems.append(f"local path: {match.group(0)}")
    for match in EMAIL_PATTERN.finditer(text):
        if not ALLOWED_EMAIL.match(match.group(0)):
            problems.append(f"email address: {match.group(0)}")
    problems.extend("personal pattern matched" for p in personal if p.search(text))
    return problems


def scan_name(name: str) -> list[str]:
    path = Path(name)
    if path.name.lower() in FORBIDDEN_NAMES:
        return [f"forbidden file: {name}"]
    if path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return [f"forbidden file type: {name}"]
    return []


def scan_worktree(root: Path, personal: list[re.Pattern[str]]) -> list[str]:
    listing = run_git("ls-files", "--cached", "--others", "--exclude-standard", "-z", cwd=root)
    problems: list[str] = []
    for name in filter(None, listing.split("\0")):
        problems.extend(scan_name(name))
        path = root / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        text = data.decode("utf-8", errors="replace")
        problems.extend(f"{name}: {p}" for p in scan_text(text, personal))
    return problems


def has_commits(root: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
        cwd=root,
        capture_output=True,
    )
    return result.returncode == 0


def scan_history(root: Path, personal: list[re.Pattern[str]]) -> list[str]:
    if not has_commits(root):
        return []
    problems: list[str] = []
    metadata = run_git("log", "--all", "--format=%an <%ae>%n%cn <%ce>%n%B", cwd=root)
    problems.extend(f"commit metadata: {p}" for p in scan_text(metadata, personal))
    names = run_git("log", "--all", "--name-only", "--format=", cwd=root)
    for name in sorted(set(filter(None, names.splitlines()))):
        problems.extend(f"history: {p}" for p in scan_name(name))
    diff = run_git("log", "--all", "-p", "--format=", "--unified=0", "--text", cwd=root)
    added = "\n".join(
        line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("++")
    )
    problems.extend(f"history: {p}" for p in scan_text(added, personal))
    return sorted(set(problems))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true", help="also scan all commits")
    args = parser.parse_args(argv)

    personal = load_personal_patterns()
    problems = scan_worktree(ROOT, personal)
    if args.history:
        problems += scan_history(ROOT, personal)
    for problem in problems:
        print(problem)
    print(f"public-ready check: {len(problems)} problem(s), {len(personal)} personal pattern(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
