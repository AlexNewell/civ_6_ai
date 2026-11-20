"""The playbook runner: a turn-loop handler that follows situations from start to end (issue 244).

Each turn it reads a set of named readings, and for each playbook it starts an occurrence when the
trigger holds, moves the level up or down (a rule must hold for `raise_hold` or `lower_hold`
consecutive turns, and a level stays `min_dwell` turns after a move, so it does not flip), and
closes the occurrence when the end condition has held for its hold. When the set of active levels
changes it applies `combine(base document, active levels)` through `apply_resolved`, the same way
the policy document handler does. It makes no model calls and is off unless `enabled`: an off
runner, or one with no occurrence ever open, never touches a policy.

Every occurrence is a record in a JSON lines file, rewritten whole when one changes (the file is
small), so an occurrence still open at the end of the game is already there as unresolved.
"""

import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from civ6_ai.strategy.apply import apply_resolved
from civ6_ai.strategy.playbook import (
    ActiveLevel,
    Playbook,
    combine,
    evaluate,
)
from civ6_ai.strategy.policy_document import DEFAULT_DOCUMENT, PolicyDocument

log = logging.getLogger(__name__)

UNRESOLVED = "unresolved at game end"
RESOLVED = "resolved"


@dataclass(frozen=True)
class Readings:
    """What the signal provider saw this turn: the turn, and each reading it could take."""

    turn: int
    values: Mapping[str, float] = field(default_factory=dict)


SignalProvider = Callable[[Any], Awaitable[Readings]]


@dataclass
class Occurrence:
    """One open or closed run of a playbook, from its trigger to its end."""

    playbook: Playbook
    start_turn: int
    level: int = 0
    last_move: int = 0
    raise_streak: int = 0
    lower_streak: int = 0
    end_streak: int = 0
    end_turn: int | None = None
    levels: list[tuple[str, int]] = field(default_factory=list)  # Level name and the turn reached.
    signals: dict[str, dict[str, float]] = field(default_factory=dict)

    def active(self) -> ActiveLevel:
        return ActiveLevel(self.playbook.name, self.playbook.levels[self.level], self.start_turn)

    def note_signals(self, values: Mapping[str, float]) -> None:
        for name in self.playbook.outcome_signals:
            if name not in values:
                continue
            seen = self.signals.setdefault(name, {"start": values[name], "max": values[name]})
            seen["end"] = values[name]
            seen["max"] = max(seen["max"], values[name])

    def to_record(self) -> dict[str, Any]:
        order = [level.name for level in self.playbook.levels]
        peak = max(order.index(name) for name, _ in self.levels)
        return {
            "playbook": self.playbook.name,
            "playbook_version": self.playbook.version,
            "start_turn": self.start_turn,
            "levels": [{"level": name, "turn": turn} for name, turn in self.levels],
            "peak_level": self.playbook.levels[peak].name,
            "end_turn": self.end_turn,
            "status": UNRESOLVED if self.end_turn is None else RESOLVED,
            "outcome": {"signals": self.signals},
        }


class PlaybookRunner:
    name = "playbooks"

    def __init__(
        self,
        playbooks: list[Playbook],
        signals: SignalProvider,
        *,
        enabled: bool = False,  # Off by default: a run plays as it always did.
        base_document: Callable[[], PolicyDocument] = lambda: DEFAULT_DOCUMENT,
        records: Path | None = None,
        **policies: Any,
    ) -> None:
        """`policies` are the policies to steer, by the keyword names of `apply_resolved`."""
        self.playbooks = playbooks
        self.enabled = enabled
        self._signals = signals
        self._base = base_document
        self._records = records
        self._policies = policies
        self.open: dict[str, Occurrence] = {}
        self.closed: list[Occurrence] = []
        self._applied: tuple | None = None  # What was last applied, to apply only on a change.
        self._last_turn: int | None = None
        self._written = ""  # The records file as last written, so a write happens only on change.
        self._streaks: dict[str, int] = {}  # Consecutive turns each trigger has held.

    async def run(self, game: Any) -> None:
        if not self.enabled:
            return
        readings = await self._signals(game)
        self.step(readings)

    def step(self, readings: Readings) -> None:
        """Advance every playbook one turn on these readings. Pure apart from applying weights."""
        if self._last_turn is not None and readings.turn < self._last_turn:
            # The turn went backwards: a save was loaded, so open occurrences are another game's.
            self.open.clear()
            self._streaks.clear()
        self._last_turn = readings.turn
        changed = False
        for playbook in self.playbooks:
            changed |= self._advance(playbook, readings)
        if changed or self.open:
            self._write_records()  # Open occurrences' signals move without any transition.
        self._apply()

    def _advance(self, playbook: Playbook, readings: Readings) -> bool:
        """Move one playbook. True if its record changed."""
        turn, values = readings.turn, readings.values
        occurrence = self.open.get(playbook.name)
        if occurrence is None:
            gate = playbook.trigger
            streak = self._streaks.get(playbook.name, 0) + 1 if evaluate(gate.when, values) else 0
            self._streaks[playbook.name] = streak
            if streak < gate.hold:
                return False
            self._streaks[playbook.name] = 0
            occurrence = Occurrence(playbook, start_turn=turn, last_move=turn)
            occurrence.levels.append((playbook.levels[0].name, turn))
            occurrence.note_signals(values)
            self.open[playbook.name] = occurrence
            log.info("Playbook %s v%s started on turn %d", playbook.name, playbook.version, turn)
            return True
        occurrence.note_signals(values)
        occurrence.end_streak = (
            occurrence.end_streak + 1 if evaluate(playbook.end.when, values) else 0
        )
        if occurrence.end_streak >= playbook.end.hold:
            occurrence.end_turn = turn
            del self.open[playbook.name]
            self.closed.append(occurrence)
            log.info("Playbook %s ended on turn %d", playbook.name, turn)
            return True
        level = playbook.levels[occurrence.level]
        raising = level.raise_when is not None and evaluate(level.raise_when, values)
        lowering = level.lower_when is not None and evaluate(level.lower_when, values)
        occurrence.raise_streak = occurrence.raise_streak + 1 if raising else 0
        occurrence.lower_streak = occurrence.lower_streak + 1 if lowering else 0
        if turn - occurrence.last_move < playbook.min_dwell:
            return False
        if occurrence.raise_streak >= playbook.raise_hold:
            return self._move(occurrence, occurrence.level + 1, turn)
        if occurrence.lower_streak >= playbook.lower_hold:
            return self._move(occurrence, occurrence.level - 1, turn)
        return False

    def _move(self, occurrence: Occurrence, to: int, turn: int) -> bool:
        occurrence.level = to
        occurrence.last_move = turn
        occurrence.raise_streak = occurrence.lower_streak = 0
        name = occurrence.playbook.levels[to].name
        occurrence.levels.append((name, turn))
        log.info("Playbook %s moved to %s on turn %d", occurrence.playbook.name, name, turn)
        return True

    def active(self) -> list[ActiveLevel]:
        return [self.open[p.name].active() for p in self.playbooks if p.name in self.open]

    def _apply(self) -> None:
        active = self.active()
        base = self._base()
        signature = (tuple(active), base)
        if signature == self._applied or (self._applied is None and not active):
            return
        self._applied = signature
        apply_resolved(combine(base, active), **self._policies)

    def _write_records(self) -> None:
        if self._records is None:
            return
        everything = [*self.closed, *self.open.values()]
        everything.sort(key=lambda o: (o.start_turn, o.playbook.name))
        text = "".join(json.dumps(o.to_record(), sort_keys=True) + "\n" for o in everything)
        if text == self._written:
            return
        try:
            self._records.write_text(text, encoding="utf-8")
        except OSError:
            log.warning("Playbook records could not be written to %s", self._records)
            return
        self._written = text


# ----- records -----


@dataclass(frozen=True)
class OccurrenceRecord:
    """One line of a run's playbook records, read back."""

    playbook: str
    version: str
    start_turn: int
    levels: tuple[tuple[str, int], ...]  # Level name and the turn it was reached.
    peak_level: str
    end_turn: int | None
    status: str
    outcome: Mapping[str, Any]

    @property
    def resolved(self) -> bool:
        return self.end_turn is not None

    @property
    def turns_open(self) -> int | None:
        return None if self.end_turn is None else self.end_turn - self.start_turn


def read_occurrences(path: Path) -> list[OccurrenceRecord]:
    """The occurrences in a records file, oldest first. A missing file is no occurrences, and a
    line that is not a record is skipped, so one bad line does not hide the rest."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
            out.append(
                OccurrenceRecord(
                    playbook=row["playbook"],
                    version=row["playbook_version"],
                    start_turn=row["start_turn"],
                    levels=tuple((item["level"], item["turn"]) for item in row["levels"]),
                    peak_level=row["peak_level"],
                    end_turn=row["end_turn"],
                    status=row["status"],
                    outcome=row.get("outcome", {}),
                )
            )
        except (ValueError, KeyError, TypeError):
            continue
    return out


def summarise(records: list[OccurrenceRecord]) -> dict[str, dict[str, Any]]:
    """Per playbook version: occurrences, how many resolved, mean turns to resolve, peak levels."""
    table: dict[str, dict[str, Any]] = {}
    for record in records:
        row = table.setdefault(
            f"{record.playbook} v{record.version}",
            {"occurrences": 0, "resolved": 0, "turns": [], "peaks": {}},
        )
        row["occurrences"] += 1
        row["resolved"] += record.resolved
        if record.turns_open is not None:
            row["turns"].append(record.turns_open)
        row["peaks"][record.peak_level] = row["peaks"].get(record.peak_level, 0) + 1
    for row in table.values():
        turns = row.pop("turns")
        row["mean_turns_to_resolve"] = sum(turns) / len(turns) if turns else None
    return table
