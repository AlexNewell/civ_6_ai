"""Summarise a run that stopped early or had handler errors, from the logs in `runs/`.

Deterministic: it reads files and applies fixed rules, with no model or game calls. The rules say
whether the logs explain the stop, and so whether a screenshot would add anything. The procedure is
in docs/triage.md.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from civ6_ai.blockers import BlockerType
from civ6_ai.metrics.costs import STAMPED_LOG, game_id_of_log

RECENT_TURNS = 3
LOG_TAIL_LINES = 12
# civ6-mcp names the blocker in the end-turn report, and the bot log repeats it.
BLOCKER_NAMES = re.compile("|".join(re.escape(t.value) for t in BlockerType))

NOT_NEEDED = "not needed"
WORTH_READING = "worth reading"


@dataclass(frozen=True)
class Verdict:
    screenshot: str
    reasons: list[str]


@dataclass
class Triage:
    log: Path
    turn: int | None = None
    stop_reason: str | None = None
    detail: str = ""
    errors: dict[str, str] = field(default_factory=dict)
    recent: list[dict] = field(default_factory=list)
    bot_log_tail: list[str] = field(default_factory=list)
    screenshots: list[Path] = field(default_factory=list)
    verdict: Verdict = field(default_factory=lambda: Verdict(NOT_NEEDED, []))


def newest_turn_log(runs: Path) -> Path | None:
    """The most recently written session turn log (not the _events, _settle or _bot files)."""
    logs = [p for p in runs.glob("*.jsonl") if STAMPED_LOG.match(p.name)]
    return max(logs, key=lambda p: p.stat().st_mtime, default=None)


def assess(records: list[dict], bot_lines: list[str]) -> Verdict:
    """Whether the logs explain the last turn, and so whether a screenshot is worth reading."""
    if not records:
        return Verdict(NOT_NEEDED, ["The turn log has no turns."])
    last = records[-1]
    stop = last.get("stop_reason")
    errors = last.get("handler_errors") or {}
    if stop in ("game_over", "run_aborted"):
        return Verdict(NOT_NEEDED, [f"Stopped with {stop}, and the end-turn report says why."])
    if stop in ("blocked", "hung"):
        text = " ".join([*(r.get("detail", "") for r in records[-RECENT_TURNS:]), *bot_lines])
        if found := sorted(set(BLOCKER_NAMES.findall(text))):
            return Verdict(
                NOT_NEEDED, [f"Stopped {stop} with a named blocker: {', '.join(found)}."]
            )
        return Verdict(
            WORTH_READING,
            [f"Stopped {stop} and no known blocker is named in the report or the bot log."],
        )
    if errors:
        return Verdict(NOT_NEEDED, [f"Handler errors ({', '.join(errors)}); see the bot log."])
    return Verdict(NOT_NEEDED, ["The run did not stop and no handler failed."])


def triage(log: Path) -> Triage:
    records = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    bot_log = log.with_name(f"{log.stem}_bot.log")
    tail = (
        bot_log.read_text(encoding="utf-8").splitlines()[-LOG_TAIL_LINES:]
        if bot_log.exists()
        else []
    )
    result = Triage(log=log, recent=records[-RECENT_TURNS:], bot_log_tail=tail)
    if records:
        last = records[-1]
        result.turn = last.get("turn")
        result.stop_reason = last.get("stop_reason")
        result.detail = last.get("detail", "")
        result.errors = last.get("handler_errors") or {}
    result.verdict = assess(records, tail)
    game_id = game_id_of_log(log.parent, log)
    if game_id is not None and result.turn is not None:
        shots = log.parent / "screenshots"
        result.screenshots = sorted(shots.glob(f"{game_id}_T{result.turn:03d}_*.jpg"))
    return result


def format_report(t: Triage) -> str:
    lines = [
        f"Run: {t.log.name}",
        f"Last turn: {t.turn}, stop reason: {t.stop_reason or 'none'}",
        f"Detail: {t.detail or '(none)'}",
    ]
    if t.errors:
        lines += ["Handler errors:", *(f"  {name}: {err}" for name, err in t.errors.items())]
    lines.append(f"Screenshot: {t.verdict.screenshot}")
    lines += [f"  {reason}" for reason in t.verdict.reasons]
    if t.screenshots:
        lines += ["Screenshots for that turn:", *(f"  {p}" for p in t.screenshots)]
    elif t.verdict.screenshot == WORTH_READING:
        lines.append("No screenshot was taken for that turn. See docs/triage.md for taking one.")
    if t.bot_log_tail:
        lines += ["Bot log tail:", *(f"  {line[:200]}" for line in t.bot_log_tail)]
    return "\n".join(lines)
