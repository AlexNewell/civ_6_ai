"""Re-rank a run's production decisions by yield-turn value, offline (issue 400, slice 2).

A static program: it reads the bot log of each run and prints. It makes no game or network calls
and changes no live pick. A run is given as `runs/<name>` (the turn log's stem or file name, with or
without `.jsonl`); the `_bot.log` beside it is read.

For each `production_options {json}` line (one per production decision, with every offered option,
its score and the inputs `build_value` reads) the options are valued with
`civ6_ai.policies.build_value` and the best valued option is compared with the pick the policy
ranked first. Reported:

- every changed pick: turn, city, old pick and score, new pick and value, and the reason;
- how many options had no value (and the reasons), since those cannot be compared;
- for each changed pick, the assumed settings its value rests on, with the old and new pick's
  value at half and double each and whether the pick still changes (the first line lists them all);
- any change that would displace a settler, or a defender or walls in a threatened city.

An old pick with no value is never replaced. A new pick always has a higher value than the old one
under the model, so a changed pick is a gain by construction: the question to read is which
picks change and what they displace. The "old pick" is the option ranked first, which is the pick
unless the game refused it and the policy went down its list. The weights and settings are the
defaults, since the log does not record a posture's weights.

A run whose log has no such line (older code) prints "no structured options; cannot replay". The
top three options that older logs carry as text are not used to reconstruct anything.

    replay_builds.py runs/<name> [runs/<other> ...] [--limit 60]
"""

import argparse
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from civ6_ai.policies.build_value import (
    ASSUMED_SETTINGS,
    LOG_TAG,
    BuildValueSettings,
    DecisionInputs,
    OptionInput,
    is_defence_option,
    options_from_record,
    rerank,
    scaled,
    value_option,
)
from civ6_ai.policies.production import canonical
from yield_report import _bot_lines, resolve

NO_OPTIONS = "no structured options; cannot replay"


@dataclass
class Change:
    turn: int
    city: str
    old: str
    old_score: float | None
    new: str
    new_value: float
    old_value: float | None
    reason: str
    flags: list[str] = field(default_factory=list)
    sensitivity: list[str] = field(default_factory=list)


@dataclass
class Replay:
    label: str
    decisions: int = 0
    unvalued_old: int = 0  # Decisions whose old pick had no value, so nothing changed.
    options_total: int = 0
    options_unvalued: int = 0
    unvalued_reasons: Counter = field(default_factory=Counter)
    changes: list[Change] = field(default_factory=list)

    @property
    def displaced(self) -> list[Change]:
        return [c for c in self.changes if c.flags]


def _short_reason(reason: str) -> str:
    """The reason without its numbers, so the same cause counts together."""
    return reason.split(":")[0].strip()[:60]


def _flags(old: OptionInput, new: OptionInput, inputs: DecisionInputs) -> list[str]:
    flags = []
    if canonical(old.item) == "UNIT_SETTLER":
        flags.append("displaces a settler")
    if is_defence_option(old) and not is_defence_option(new) and inputs.under_threat:
        flags.append("displaces a defender in a threatened city")
    return flags


def sensitivity(
    options: list[OptionInput],
    inputs: DecisionInputs,
    old: OptionInput,
    new: OptionInput,
    defence_only: bool,
) -> list[str]:
    """For each assumed setting that moves the old or new pick's value: both values at half and
    double the setting, and whether the pick still changes."""
    base = BuildValueSettings()
    lines = []
    for name in ASSUMED_SETTINGS:
        parts = []
        moved = False
        for factor in (0.5, 2.0):
            s = scaled(base, name, factor)
            a, b = value_option(old, inputs, s), value_option(new, inputs, s)
            a0, b0 = value_option(old, inputs, base), value_option(new, inputs, base)
            if (a.value, b.value) != (a0.value, b0.value):
                moved = True
            again = rerank(options, inputs, s, defence_only=defence_only)
            holds = "holds" if again.changed and again.new.item == new.item else "changes"
            av = "n/a" if a.value is None else f"{a.value:+.2f}"
            bv = "n/a" if b.value is None else f"{b.value:+.2f}"
            parts.append(f"x{factor:g} ({getattr(s, name):g}): old {av}, new {bv}, pick {holds}")
        if moved:
            lines.append(f"{name} = {getattr(base, name):g}: " + "; ".join(parts))
    return lines


def replay_records(label: str, records: list[dict[str, Any]]) -> Replay:
    """Re-rank each logged decision."""
    report = Replay(label)
    for record in records:
        options = options_from_record(record)
        inputs = DecisionInputs.from_dict(record.get("inputs", {}))
        result = rerank(options, inputs, defence_only=bool(record.get("defence_only")))
        if result.old is None:
            continue
        report.decisions += 1
        for value in result.values.values():
            report.options_total += 1
            if value.value is None:
                report.options_unvalued += 1
                report.unvalued_reasons[_short_reason(value.reason)] += 1
        if result.old_value is not None and result.old_value.value is None:
            report.unvalued_old += 1
        if not result.changed or result.new is None or result.new_value is None:
            continue
        assert result.old is not None
        old_value = result.old_value.value if result.old_value is not None else None
        report.changes.append(
            Change(
                turn=int(record.get("turn", 0)),
                city=str(record.get("city", "?")),
                old=result.old.item,
                old_score=result.old.score,
                new=result.new.item,
                new_value=float(result.new_value.value or 0.0),
                old_value=old_value,
                reason=result.new_value.reason,
                flags=_flags(result.old, result.new, inputs),
                sensitivity=sensitivity(
                    options, inputs, result.old, result.new, bool(record.get("defence_only"))
                ),
            )
        )
    return report


def analyse(spec: str) -> Replay | None:
    """The replay of one run, or None when its log has no structured options."""
    turn_log = resolve(spec)
    bot = turn_log.with_name(f"{turn_log.stem}_bot.log")
    records = _bot_lines(bot).get(LOG_TAG, [])
    if not records:
        return None
    return replay_records(turn_log.stem, records)


def render(label: str, report: Replay | None, limit: int) -> str:
    if report is None:
        return f"{label}: {NO_OPTIONS}"
    base = BuildValueSettings()
    lines = [
        "assumed settings: " + ", ".join(f"{n}={getattr(base, n):g}" for n in ASSUMED_SETTINGS),
        f"{label}: {report.decisions} decisions, {len(report.changes)} changed picks, "
        f"{len(report.displaced)} displacing a settler or a defender",
        f"  options with no value: {report.options_unvalued} of {report.options_total}; "
        f"decisions whose old pick had no value (kept): {report.unvalued_old}",
    ]
    for reason, count in report.unvalued_reasons.most_common(8):
        lines.append(f"    {count:5d}  {reason}")
    for change in report.changes[:limit]:
        old_score = "n/a" if change.old_score is None else f"{change.old_score:.1f}"
        old_value = "n/a" if change.old_value is None else f"{change.old_value:+.2f}"
        flag = f"  [{'; '.join(change.flags)}]" if change.flags else ""
        lines.append(
            f"  T{change.turn} {change.city}: {change.old} (score {old_score}, value {old_value})"
            f" -> {change.new} (value {change.new_value:+.2f}) because {change.reason}{flag}"
        )
        if change.sensitivity:
            lines.append("    rests on assumed settings:")
            lines += [f"      {text}" for text in change.sensitivity]
        else:
            lines.append("    rests on no assumed setting")
    if len(report.changes) > limit:
        lines.append(f"  ... {len(report.changes) - limit} more changed picks (--limit)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("runs", nargs="+", help="runs/<name> (the turn log's stem)")
    parser.add_argument("--limit", type=int, default=60, help="most changed picks to list per run")
    args = parser.parse_args(argv)
    for spec in args.runs:
        print(render(Path(spec).name, analyse(spec), args.limit))
    return 0


if __name__ == "__main__":
    sys.exit(main())
