#!/usr/bin/env python3
"""Create the roadmap's labels, milestones and issues on GitHub.

Run with --dry-run to validate without touching GitHub. A real run is safe to repeat:
issues carry a hidden seed id and existing ones are skipped. Needs an authenticated `gh`
and a GitHub remote.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
SEED_PATH = ROOT / ".github" / "roadmap-issues.yaml"
ROADMAP_PATH = ROOT / "roadmap.md"
PENDING = "(link pending)"
REQUIRED = ("id", "title", "type", "goal", "scope", "acceptance", "budget")
ROADMAP_ITEM = re.compile(r"^- (?:\[`([^`]+)`\]\([^)]*\)|`([^`]+)`) (.+)$", re.MULTILINE)


def load_seed(path: Path = SEED_PATH) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def prefix(issue_id: str) -> str:
    return issue_id.split(".")[0]


def area_label(issue_id: str) -> str:
    head = prefix(issue_id)
    return f"phase:{head}" if head.isdigit() else f"workstream:{head.lower()}"


def labels_for(issue: dict[str, Any]) -> list[str]:
    labels = [area_label(issue["id"]), f"type:{issue['type']}"]
    if issue.get("parallel"):
        labels.append("parallel-ok")
    if issue.get("needs_user"):
        labels.append("needs:user")
    return labels


def find_cycle(issues: list[dict[str, Any]]) -> list[str]:
    graph = {i["id"]: i.get("depends", []) for i in issues}
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str]:
        state[node] = 1
        stack.append(node)
        for dep in graph.get(node, []):
            if state.get(dep) == 1:
                return [*stack[stack.index(dep) :], dep]
            if dep not in state and (cycle := visit(dep)):
                return cycle
        stack.pop()
        state[node] = 2
        return []

    for node in graph:
        if node not in state and (cycle := visit(node)):
            return cycle
    return []


def validate(seed: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    issues = seed["issues"]
    ids = [i.get("id") for i in issues]
    known_labels = {label["name"] for label in seed["labels"]}
    milestone_ids = {m["id"] for m in seed["milestones"]}

    errors.extend(f"duplicate id: {i}" for i in sorted({i for i in ids if ids.count(i) > 1}))
    for issue in issues:
        issue_id = issue.get("id", "?")
        errors.extend(f"{issue_id}: missing {f}" for f in REQUIRED if not issue.get(f))
        if prefix(issue_id) not in milestone_ids:
            errors.append(f"{issue_id}: no milestone")
        errors.extend(
            f"{issue_id}: unknown label {label}"
            for label in labels_for(issue)
            if label not in known_labels
        )
        errors.extend(
            f"{issue_id}: unknown dependency {dep}"
            for dep in issue.get("depends", [])
            if dep not in ids
        )
    if cycle := find_cycle(issues):
        errors.append("dependency cycle: " + " -> ".join(cycle))
    return errors


def roadmap_items(text: str) -> dict[str, str]:
    return {(m[1] or m[2]): m[3].strip() for m in ROADMAP_ITEM.finditer(text)}


def check_roadmap(seed: dict[str, Any], text: str) -> list[str]:
    """Each seeded issue must appear in the roadmap with a matching title."""
    items = {k: v for k, v in roadmap_items(text).items() if not k.startswith("0.")}
    expected = {i["id"]: i["title"] for i in seed["issues"]}
    errors = [f"roadmap is missing {i}" for i in expected if i not in items]
    errors += [f"roadmap has unknown item {i}" for i in items if i not in expected]
    errors += [
        f"title mismatch for {i}"
        for i, title in expected.items()
        if i in items and items[i] != title
    ]
    return errors


def render_body(issue: dict[str, Any], numbers: dict[str, int]) -> str:
    deps = issue.get("depends", [])
    depends = (
        ", ".join(f"#{numbers[d]}" if d in numbers else f"{d} {PENDING}" for d in deps) or "None"
    )
    acceptance = "\n".join(f"- [ ] {item}" for item in issue["acceptance"])
    return (
        f"<!-- seed-id: {issue['id']} -->\n"
        f"**Roadmap item:** {issue['id']}\n\n"
        f"## Goal\n{issue['goal']}\n\n"
        f"## Scope\n{issue['scope']}\n\n"
        f"## Acceptance criteria\n{acceptance}\n\n"
        f"## Depends on\n{depends}\n\n"
        f"## Budget\n{issue['budget']}\n"
    )


def gh(*args: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["gh", *args],
        input=stdin,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def seed_labels(seed: dict[str, Any]) -> None:
    for label in seed["labels"]:
        gh(
            "label",
            "create",
            label["name"],
            "--color",
            label["color"],
            "--description",
            label["description"],
            "--force",
        )


def seed_milestones(seed: dict[str, Any], repo: str) -> dict[str, str]:
    raw = gh("api", f"repos/{repo}/milestones?state=all&per_page=100")
    existing = {m["title"] for m in json.loads(raw)}
    titles: dict[str, str] = {}
    for milestone in seed["milestones"]:
        titles[milestone["id"]] = milestone["title"]
        if milestone["title"] not in existing:
            gh(
                "api",
                f"repos/{repo}/milestones",
                "-f",
                f"title={milestone['title']}",
                "-f",
                f"description={milestone['description']}",
            )
    return titles


def existing_issues() -> dict[str, tuple[int, str]]:
    raw = gh("issue", "list", "--state", "all", "--limit", "1000", "--json", "number,body")
    found: dict[str, tuple[int, str]] = {}
    for issue in json.loads(raw):
        if match := re.search(r"<!-- seed-id: (\S+) -->", issue["body"]):
            found[match[1]] = (issue["number"], issue["body"])
    return found


def link_roadmap(numbers: dict[str, int], repo: str, path: Path = ROADMAP_PATH) -> None:
    def replace(match: re.Match[str]) -> str:
        issue_id = match[1] or match[2]
        if issue_id not in numbers:
            return match[0]
        url = f"https://github.com/{repo}/issues/{numbers[issue_id]}"
        return f"- [`{issue_id}`]({url}) {match[3]}"

    path.write_text(ROADMAP_ITEM.sub(replace, path.read_text(encoding="utf-8")), encoding="utf-8")


def seed_issues(seed: dict[str, Any], milestones: dict[str, str]) -> dict[str, int]:
    found = existing_issues()
    numbers = {issue_id: number for issue_id, (number, _) in found.items()}
    for issue in seed["issues"]:
        if issue["id"] in numbers:
            continue
        args = [
            "issue",
            "create",
            "--title",
            f"{issue['id']} {issue['title']}",
            "--body-file",
            "-",
            "--milestone",
            milestones[prefix(issue["id"])],
        ]
        for label in labels_for(issue):
            args += ["--label", label]
        url = gh(*args, stdin=render_body(issue, {}))
        numbers[issue["id"]] = int(url.rstrip("/").rsplit("/", 1)[-1])
        print(f"created {issue['id']} as #{numbers[issue['id']]}")
        time.sleep(1)

    # Fill dependency numbers only where links are still pending, keeping any manual edits.
    current = existing_issues()
    for issue in seed["issues"]:
        number, body = current[issue["id"]]
        if PENDING in body:
            gh("issue", "edit", str(number), "--body-file", "-", stdin=render_body(issue, numbers))
    return numbers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="validate only")
    args = parser.parse_args(argv)

    seed = load_seed()
    errors = validate(seed) + check_roadmap(seed, ROADMAP_PATH.read_text(encoding="utf-8"))
    for error in errors:
        print(f"error: {error}")
    if errors:
        return 1

    counts = (len(seed["labels"]), len(seed["milestones"]), len(seed["issues"]))
    print("{} labels, {} milestones, {} issues".format(*counts))
    if args.dry_run:
        for milestone in seed["milestones"]:
            ids = [i["id"] for i in seed["issues"] if prefix(i["id"]) == milestone["id"]]
            print(f"  {milestone['title']}: {len(ids)} issues ({', '.join(ids)})")
        return 0

    repo = gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner")
    seed_labels(seed)
    milestones = seed_milestones(seed, repo)
    numbers = seed_issues(seed, milestones)
    link_roadmap(numbers, repo)
    print(f"seeded {len(numbers)} issues in {repo}; commit the updated roadmap.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
