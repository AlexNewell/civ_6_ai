"""Playbooks: named responses to a recurring situation (issue 244). The format, parser, combining.

A playbook is a versioned JSON file. It says how to spot a situation (`trigger`), what to do at each
urgency level (`levels`), when to move between levels (`raise_when`, `lower_when`, with a hold in
turns so the level does not flip every turn) and when the situation is over (`end`). Nothing here
calls a model or the game: conditions are structured data over named readings, never evaluated
text, and the parser rejects a file whole with every problem listed, like the policy document.

A level steers the bot in two ways, both through the policy document mechanism:

- `settings`: a partial policy document (posture, expansion, weights), resolved by `resolve`.
- `nudges`: a bounded relative step on one named setting, for posture dials (issue 321): the
  resolved value is scaled by `1 + step` and clamped to `min`..`max`. The step is not cumulative,
  so a level always means the same thing.

`combine` is the rule for several playbooks active at once (docs/decisions.md).
"""

import json
import math
import operator
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from civ6_ai.strategy.policy_document import (
    _DOMAINS,
    _FRACTION_FIELDS,
    DEFAULT_DOCUMENT,
    MAX_TARGET_CITIES,
    MIN_TARGET_CITIES,
    Expansion,
    PolicyDocument,
    PolicyDocumentError,
    Posture,
    ResolvedPolicies,
    parse_policy_document,
    resolve,
    settable_weights,
)

PLAYBOOK_SCHEMA_VERSION = 1
MAX_LEVELS = 6
MAX_CONDITION_DEPTH = 4  # Nesting of all/any/not, so a file cannot build something unreadable.
MAX_CONDITION_CHILDREN = 8
MAX_HOLD_TURNS = 30
MAX_STEP = 3.0  # A nudge may at most quadruple a setting.
MIN_STEP = -0.9  # And at most cut it to a tenth, so a step can never zero a setting.
MAX_BOUND = 1000.0  # The same ceiling the policy document puts on a weight.

# Readings a trigger or rule may name, with what each means. The signal provider supplies them from
# legal reads; one it does not supply is missing, and a condition on a missing reading is false.
KNOWN_SIGNALS: dict[str, str] = {
    "turn": "the game turn",
    "cities": "our city count",
    "camps_near_city": "barbarian camps within the threat map's near-city distance of a city",
    "nearest_camp_distance": "tiles from our nearest city to the nearest known camp",
    "city_danger_max": "the highest threat-map danger at any of our cities",
    "hostile_units_near": "foreign or barbarian military units seen near our cities",
    "turns_since_hostile_seen": "turns since a hostile unit was last seen near a city",
    "weak_neighbour_distance": "tiles to the nearest weak, unwalled city-state",
    "army_ratio": "our army divided by the army the posture wants",
}
_OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
    "!=": operator.ne,
}
_TOP_LEVEL = {
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
}
_REQUIRED = ("name", "playbook_version", "trigger", "levels", "end")
_LEVEL_KEYS = {"name", "urgency", "settings", "nudges", "raise_when", "lower_when"}
_NUDGE_KEYS = {"setting", "step", "min", "max"}


class PlaybookError(ValueError):
    """A playbook was rejected. `problems` lists every reason, each naming its field."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


# A condition is a plain tuple tree, hashable and comparable:
#   ("cmp", signal, op, value) | ("all", (c, ...)) | ("any", (c, ...)) | ("not", c)
Condition = tuple


@dataclass(frozen=True)
class Gate:
    """A condition that must hold for `hold` consecutive turns before it counts."""

    when: Condition
    hold: int = 1


@dataclass(frozen=True)
class Nudge:
    setting: str  # "domain.field", a field `settable_weights` allows.
    step: float  # Relative: the value is scaled by 1 + step.
    low: float
    high: float

    @property
    def domain(self) -> str:
        return self.setting.split(".", 1)[0]

    @property
    def field(self) -> str:
        return self.setting.split(".", 1)[1]


@dataclass(frozen=True)
class Level:
    name: str
    urgency: int  # Strictly rising through a playbook. Compared across playbooks when combining.
    settings: PolicyDocument = DEFAULT_DOCUMENT
    sets: frozenset[str] = frozenset()  # Which of posture/victory/expansion/weights it names.
    nudges: tuple[Nudge, ...] = ()
    raise_when: Condition | None = None
    lower_when: Condition | None = None


@dataclass(frozen=True)
class Playbook:
    name: str
    version: str
    trigger: Gate
    levels: tuple[Level, ...]
    end: Gate
    example: bool = False
    description: str = ""
    raise_hold: int = 2  # Turns a raise condition must hold: the buffer against flipping.
    lower_hold: int = 4  # Longer than raise_hold, so a level is slower to drop than to rise.
    min_dwell: int = 2  # Turns a level stays after a move before it may move again.
    outcome_signals: tuple[str, ...] = ()  # Readings kept in the occurrence record.


# ----- conditions -----


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def parse_condition(raw: object, where: str, problems: list[str], depth: int = 0) -> Condition:
    """A condition from JSON: a comparison, or all/any/not over conditions. Never evaluated text."""
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: must be an object")
        return ()
    if depth >= MAX_CONDITION_DEPTH:
        problems.append(f"{where}: nested more than {MAX_CONDITION_DEPTH} deep")
        return ()
    if "signal" in raw:
        extra = sorted(str(key) for key in raw if key not in ("signal", "op", "value"))
        if extra:
            problems.append(f"{where}: unknown fields {extra}")
        signal, op, value = raw.get("signal"), raw.get("op"), raw.get("value")
        if not isinstance(signal, str) or signal not in KNOWN_SIGNALS:
            problems.append(f"{where}.signal: {signal!r} is not one of {sorted(KNOWN_SIGNALS)}")
        if not isinstance(op, str) or op not in _OPS:
            problems.append(f"{where}.op: {op!r} is not one of {sorted(_OPS)}")
        if not _is_number(value) or not math.isfinite(value):
            problems.append(f"{where}.value: must be a finite number")
        return ("cmp", signal, op, value)
    keys = [key for key in ("all", "any", "not") if key in raw]
    if len(keys) != 1 or len(raw) != 1:
        problems.append(f"{where}: must have exactly one of signal, all, any, not")
        return ()
    key = keys[0]
    if key == "not":
        return ("not", parse_condition(raw["not"], f"{where}.not", problems, depth + 1))
    children = raw[key]
    if not isinstance(children, list) or not children:
        problems.append(f"{where}.{key}: must be a non-empty list")
        return ()
    if len(children) > MAX_CONDITION_CHILDREN:
        problems.append(f"{where}.{key}: more than {MAX_CONDITION_CHILDREN} conditions")
        return ()
    return (
        key,
        tuple(
            parse_condition(child, f"{where}.{key}[{i}]", problems, depth + 1)
            for i, child in enumerate(children)
        ),
    )


def evaluate(condition: Condition, readings: Mapping[str, float]) -> bool:
    """Whether a condition holds. A comparison on a missing reading is false, so `not` of one is
    true: write absence checks with care."""
    if not condition:
        return False  # Only an unparsed placeholder, which a parsed playbook never holds.
    kind = condition[0]
    if kind == "cmp":
        _, signal, op, value = condition
        reading = readings.get(signal)
        return reading is not None and _OPS[op](reading, value)
    if kind == "all":
        return all(evaluate(child, readings) for child in condition[1])
    if kind == "any":
        return any(evaluate(child, readings) for child in condition[1])
    return not evaluate(condition[1], readings)


def condition_signals(condition: Condition) -> set[str]:
    """Every reading a condition names."""
    if not condition:
        return set()
    kind = condition[0]
    if kind == "cmp":
        return {condition[1]}
    if kind == "not":
        return condition_signals(condition[1])
    return {name for child in condition[1] for name in condition_signals(child)}


# ----- parsing -----


def _check_hold(value: object, where: str, problems: list[str], minimum: int) -> int:
    if not _is_number(value) or value != int(value) or not minimum <= value <= MAX_HOLD_TURNS:
        problems.append(f"{where}: must be a whole number of turns, {minimum} to {MAX_HOLD_TURNS}")
        return minimum
    return int(value)


def _check_gate(raw: object, where: str, problems: list[str]) -> Gate:
    if not isinstance(raw, Mapping) or "when" not in raw:
        problems.append(f"{where}: must be an object with a `when` condition")
        return Gate(())
    extra = sorted(str(key) for key in raw if key not in ("when", "hold"))
    if extra:
        problems.append(f"{where}: unknown fields {extra}")
    hold = _check_hold(raw.get("hold", 1), f"{where}.hold", problems, minimum=1)
    return Gate(parse_condition(raw["when"], f"{where}.when", problems), hold)


def _check_nudge(raw: object, where: str, problems: list[str]) -> Nudge | None:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: must be an object")
        return None
    if extra := sorted(str(key) for key in raw if key not in _NUDGE_KEYS):
        problems.append(f"{where}: unknown fields {extra}")
    if missing := sorted(key for key in _NUDGE_KEYS if key not in raw):
        problems.append(f"{where}: missing {missing}")
        return None
    setting, step, low, high = raw["setting"], raw["step"], raw["min"], raw["max"]
    before = len(problems)
    domain, _, name = str(setting).partition(".")
    if domain not in _DOMAINS or name not in settable_weights(domain):
        problems.append(f"{where}.setting: {setting!r} is not a domain.field the weights allow")
    for key, value in (("step", step), ("min", low), ("max", high)):
        if not _is_number(value) or not math.isfinite(value):
            problems.append(f"{where}.{key}: must be a finite number")
    if len(problems) > before:
        return None
    if not MIN_STEP <= step <= MAX_STEP:
        problems.append(f"{where}.step: must be {MIN_STEP:g} to {MAX_STEP:g}")
    if low < 0 or high > MAX_BOUND:
        problems.append(f"{where}: min and max must lie between 0 and {MAX_BOUND:g}")
    if low > high:
        problems.append(f"{where}: min is above max")
    if name in _FRACTION_FIELDS.get(domain, ()) and high > 1:
        problems.append(f"{where}.max: {setting} is a share of health and must be at most 1")
    if name == "target_cities" and (low < MIN_TARGET_CITIES or high > MAX_TARGET_CITIES):
        problems.append(
            f"{where}: target_cities bounds must lie in {MIN_TARGET_CITIES} to {MAX_TARGET_CITIES}"
        )
    if len(problems) > before:
        return None
    return Nudge(str(setting), float(step), float(low), float(high))


def _check_level(raw: object, where: str, problems: list[str]) -> Level | None:
    if not isinstance(raw, Mapping):
        problems.append(f"{where}: must be an object")
        return None
    if extra := sorted(str(key) for key in raw if key not in _LEVEL_KEYS):
        problems.append(f"{where}: unknown fields {extra}")
    name, urgency = raw.get("name"), raw.get("urgency")
    if not isinstance(name, str) or not name:
        problems.append(f"{where}.name: must be a non-empty string")
    if not _is_number(urgency) or urgency != int(urgency) or urgency < 1:
        problems.append(f"{where}.urgency: must be a whole number from 1")
        urgency = 0
    settings, sets = DEFAULT_DOCUMENT, frozenset()
    if "settings" in raw:
        try:
            settings = parse_policy_document(raw["settings"])
            sets = frozenset(key for key in raw["settings"] if key != "version")
        except PolicyDocumentError as error:
            problems.extend(f"{where}.settings: {text}" for text in error.problems)
    nudges: list[Nudge] = []
    seen: set[str] = set()
    raw_nudges = raw.get("nudges", [])
    if not isinstance(raw_nudges, list):
        problems.append(f"{where}.nudges: must be a list")
        raw_nudges = []
    for i, item in enumerate(raw_nudges):
        nudge = _check_nudge(item, f"{where}.nudges[{i}]", problems)
        if nudge is not None and nudge.setting in seen:
            problems.append(f"{where}.nudges[{i}]: {nudge.setting} is nudged twice in a level")
        elif nudge is not None:
            seen.add(nudge.setting)
            nudges.append(nudge)
    rules = {
        key: parse_condition(raw[key], f"{where}.{key}", problems) if key in raw else None
        for key in ("raise_when", "lower_when")
    }
    return Level(
        name=name if isinstance(name, str) else "",
        urgency=int(urgency),
        settings=settings,
        sets=sets,
        nudges=tuple(nudges),
        raise_when=rules["raise_when"],
        lower_when=rules["lower_when"],
    )


def _check_levels(raw: object, problems: list[str]) -> list[Level]:
    if not isinstance(raw, list) or not raw:
        problems.append("levels: must be a non-empty list")
        return []
    if len(raw) > MAX_LEVELS:
        problems.append(f"levels: more than {MAX_LEVELS}")
        raw = raw[:MAX_LEVELS]
    levels = [
        level
        for i, item in enumerate(raw)
        if (level := _check_level(item, f"levels[{i}]", problems)) is not None
    ]
    names = [level.name for level in levels]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"levels: name {dup!r} used twice")
    for before, after in zip(levels, levels[1:], strict=False):
        if after.urgency <= before.urgency:
            problems.append(f"levels: urgency must rise, but {after.name!r} does not pass it")
    for i, level in enumerate(levels):
        if i == len(levels) - 1 and level.raise_when is not None:
            problems.append(f"levels[{i}].raise_when: the top level cannot be raised")
        if i == 0 and level.lower_when is not None:
            problems.append(f"levels[{i}].lower_when: the lowest level cannot be lowered")
        if i < len(levels) - 1 and level.raise_when is None:
            problems.append(f"levels[{i}].raise_when: missing, so level {level.name!r} is a trap")
    return levels


def parse_playbook(data: object) -> Playbook:
    """Validate a decoded JSON value. Raises `PlaybookError` listing every problem."""
    if not isinstance(data, Mapping):
        raise PlaybookError(["playbook: must be an object"])
    problems: list[str] = []
    if extra := sorted(str(key) for key in data if key not in _TOP_LEVEL):
        problems.append(f"playbook: unknown fields {extra}")
    if data.get("version") != PLAYBOOK_SCHEMA_VERSION:
        problems.append(
            f"version: {data.get('version')!r} is not supported, expected {PLAYBOOK_SCHEMA_VERSION}"
        )
    for key in _REQUIRED:
        if key not in data:
            problems.append(f"playbook: missing {key}")
    name, version = data.get("name"), data.get("playbook_version")
    if "name" in data and (not isinstance(name, str) or not name.replace("_", "").isalnum()):
        problems.append("name: must be letters, digits and underscores")
    if "playbook_version" in data and (not isinstance(version, str) or not version):
        problems.append("playbook_version: must be a non-empty string")
    if "example" in data and not isinstance(data["example"], bool):
        problems.append("example: must be true or false")
    if "description" in data and not isinstance(data["description"], str):
        problems.append("description: must be text")
    trigger = _check_gate(data["trigger"], "trigger", problems) if "trigger" in data else Gate(())
    end = _check_gate(data["end"], "end", problems) if "end" in data else Gate(())
    holds = {
        key: _check_hold(data[key], key, problems, minimum) if key in data else default
        for key, default, minimum in (
            ("raise_hold", 2, 1),
            ("lower_hold", 4, 1),
            ("min_dwell", 2, 0),
        )
    }
    levels = _check_levels(data["levels"], problems) if "levels" in data else []
    outcome = data.get("outcome_signals", [])
    if not isinstance(outcome, list) or any(item not in KNOWN_SIGNALS for item in outcome):
        problems.append(f"outcome_signals: must be a list drawn from {sorted(KNOWN_SIGNALS)}")
        outcome = []
    if problems:
        raise PlaybookError(problems)
    return Playbook(
        name=name,
        version=version,
        trigger=trigger,
        levels=tuple(levels),
        end=end,
        example=bool(data.get("example", False)),
        description=data.get("description", ""),
        raise_hold=holds["raise_hold"],
        lower_hold=holds["lower_hold"],
        min_dwell=holds["min_dwell"],
        outcome_signals=tuple(outcome),
    )


def parse_playbook_json(text: str) -> Playbook:
    """Like `parse_playbook`, for text. Bad JSON is a rejection like any other."""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise PlaybookError([f"playbook: not valid JSON ({error})"]) from error
    return parse_playbook(data)


def load_playbooks(
    directory: Path, include_examples: bool = False
) -> tuple[list[Playbook], dict[str, list[str]]]:
    """The playbooks in a folder, and why each refused file was refused (by file name).

    Files marked `"example": true` are skipped unless asked for: they are design material, not
    shipped behaviour. Two playbooks with one name keep the first by file name.
    """
    found: list[Playbook] = []
    refused: dict[str, list[str]] = {}
    for path in sorted(directory.glob("*.json")):
        try:
            playbook = parse_playbook_json(path.read_text(encoding="utf-8"))
        except PlaybookError as error:
            refused[path.name] = error.problems
            continue
        except OSError as error:
            refused[path.name] = [f"playbook: cannot be read ({error})"]
            continue
        if playbook.example and not include_examples:
            continue
        if any(item.name == playbook.name for item in found):
            refused[path.name] = [f"name: {playbook.name!r} is already loaded"]
            continue
        found.append(playbook)
    return found, refused


# ----- combining -----


@dataclass(frozen=True)
class ActiveLevel:
    """One playbook's current level, as the combining rule sees it."""

    playbook: str
    level: Level
    started_turn: int  # When the occurrence began: the tie-break.


def _priority(active: ActiveLevel) -> tuple:
    # Ascending priority: a later entry overrides an earlier one. Higher urgency wins; on a tie the
    # earlier-started occurrence wins; the name keeps even that deterministic.
    return (active.level.urgency, -active.started_turn, active.playbook)


def _nudged(value: float, nudge: Nudge, whole: bool) -> float:
    scaled = max(nudge.low, min(nudge.high, value * (1 + nudge.step)))
    return int(round(scaled)) if whole else scaled


def combine(base: PolicyDocument, active: Sequence[ActiveLevel] = ()) -> ResolvedPolicies:
    """The weights under a base document and the active playbook levels.

    The rule (docs/decisions.md): the base document's posture comes first, then each active level
    over it. Where two levels set the same field the higher urgency wins, and a tie goes to the
    occurrence that started earlier. Explicit `weights` and `target_cities` in the base document
    win over everything, nudges included. Nudges apply last, on the resolved weights, and for one
    setting only the winning level's nudge counts. With nothing active this is `resolve(base)`.
    """
    posture: Posture = base.posture
    target: int | None = None
    merged: dict[str, dict[str, float]] = {}
    nudges: dict[str, Nudge] = {}
    for item in sorted(active, key=_priority):
        level = item.level
        if "posture" in level.sets:
            posture = level.settings.posture
        if level.settings.expansion.target_cities is not None:
            target = level.settings.expansion.target_cities
        for domain, pairs in level.settings.weights:
            merged.setdefault(domain, {}).update(pairs)
        for nudge in level.nudges:
            nudges[nudge.setting] = nudge  # A later (higher priority) level replaces.
    if base.expansion.target_cities is not None:
        target = base.expansion.target_cities
        for domain in ("production", "gold"):  # A level's weight must not undo the pinned target.
            merged.get(domain, {}).pop("target_cities", None)
    for domain, pairs in base.weights:
        merged.setdefault(domain, {}).update(pairs)
    document = replace(
        base,
        posture=posture,
        expansion=Expansion(target_cities=target),
        weights=tuple(
            (domain, tuple(sorted(pairs.items()))) for domain, pairs in sorted(merged.items())
        ),
    )
    resolved = resolve(document)
    explicit = {(domain, name) for domain, pairs in base.weights for name, _ in pairs}
    if base.expansion.target_cities is not None:
        explicit |= {("production", "target_cities"), ("gold", "target_cities")}
    changes: dict[str, dict[str, float]] = {}
    for nudge in nudges.values():
        if (nudge.domain, nudge.field) in explicit:
            continue
        whole = settable_weights(nudge.domain)[nudge.field] is int
        value = _nudged(getattr(getattr(resolved, nudge.domain), nudge.field), nudge, whole)
        changes.setdefault(nudge.domain, {})[nudge.field] = value
        if nudge.setting == "production.target_cities" and ("gold", nudge.field) not in explicit:
            changes.setdefault("gold", {})[nudge.field] = value  # Gold reads the same table.
    return replace(
        resolved,
        **{domain: replace(getattr(resolved, domain), **c) for domain, c in changes.items()},
    )
