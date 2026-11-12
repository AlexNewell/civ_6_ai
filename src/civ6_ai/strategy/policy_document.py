"""The policy document: how strategy steers the deterministic policies.

The Phase 2 strategist will write one of these a few times a game. Everything here is offline: a
typed schema, a strict parser, and a resolver that turns a document into the weights each domain
policy already takes. Nothing in this module calls a model or the game.

A document has four parts, all optional:

- `victory`: the win to aim for. It scales the weights of the policies that serve it.
- `posture`: `expand`, `balanced`, `defend` or `conquer`. It scales the same weights and moves the
  city target.
- `expansion`: `target_cities`, which replaces the posture's city target outright.
- `weights`: per domain, exact values for individual weights. These beat the profiles above.

The defaults (`any` victory, `balanced` posture, no overrides) resolve to every policy's own
default weights, so a missing document changes nothing.

An invalid document is rejected whole, with every problem listed, and the caller keeps what it
had. A half-applied document would be harder to reason about than a refused one.
"""

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from typing import Any

from civ6_ai.policies.military import MilitaryWeights
from civ6_ai.policies.production import ProductionWeights
from civ6_ai.policies.research import ResearchWeights
from civ6_ai.policies.secondary import SecondaryWeights

SCHEMA_VERSION = 1
MIN_TARGET_CITIES = 1
MAX_TARGET_CITIES = 24
MAX_WEIGHT = 1000.0  # Far above any sensible weight, so a typo can't make one dominate for good.


class VictoryTarget(StrEnum):
    ANY = "any"  # No lean: play for whatever the game offers.
    SCIENCE = "science"
    CULTURE = "culture"
    DOMINATION = "domination"
    RELIGIOUS = "religious"
    DIPLOMATIC = "diplomatic"


class Posture(StrEnum):
    EXPAND = "expand"
    BALANCED = "balanced"
    DEFEND = "defend"
    CONQUER = "conquer"


# Weights a document may set, by domain. Only plain numeric fields: the tables of named priorities
# stay with their policies.
_DOMAINS: dict[str, type] = {
    "production": ProductionWeights,
    "research": ResearchWeights,
    "secondary": SecondaryWeights,
    "military": MilitaryWeights,
    "gold": ProductionWeights,  # The gold policy values purchases with the production weights.
}
_FRACTION_FIELDS = {
    "military": {
        "heal_below",
        "min_hp_after_attack",
        "garrison_min_hp_after",
        "planned_kill_hp_after",
        "camp_min_hp",
        "damage_variance",
    }
}
_TOP_LEVEL = {"version", "victory", "posture", "expansion", "weights"}


def settable_weights(domain: str) -> dict[str, type]:
    """The fields of a domain's weights that a document may set, with their types."""
    return {item.name: item.type for item in fields(_DOMAINS[domain]) if item.type in (int, float)}


# How a victory or posture scales weights: domain -> field -> multiplier. Only float fields.
_VICTORY_PROFILES: dict[VictoryTarget, dict[str, dict[str, float]]] = {
    VictoryTarget.ANY: {},
    VictoryTarget.SCIENCE: {
        "production": {"science": 1.5},
        "secondary": {"science": 1.5, "gp_scientist": 1.5, "gp_engineer": 1.2},
        "research": {"speed": 1.25},
    },
    VictoryTarget.CULTURE: {
        "production": {"culture": 1.5, "wonders": 1.6},
        "secondary": {
            "culture": 1.5,
            "gp_writer": 2.0,
            "gp_artist": 2.0,
            "gp_musician": 2.0,
        },
    },
    VictoryTarget.DOMINATION: {
        "production": {"military": 1.5, "production": 1.2},
        "secondary": {"production": 1.2, "gp_general": 3.0, "gp_admiral": 2.0},
    },
    VictoryTarget.RELIGIOUS: {
        "production": {"faith": 2.0},
        "secondary": {"faith": 2.0, "gp_prophet": 2.0, "religion_pressure": 2.0},
    },
    VictoryTarget.DIPLOMATIC: {
        "production": {"gold": 1.3, "trade": 1.3},
        "secondary": {"gold": 1.3, "quest": 1.5, "trading_post": 1.5},
    },
}
_POSTURE_PROFILES: dict[Posture, dict[str, dict[str, float]]] = {
    Posture.EXPAND: {"production": {"expansion": 1.5, "builders": 1.2, "military": 0.8}},
    Posture.BALANCED: {},
    Posture.DEFEND: {
        "production": {"military": 1.5, "army_per_city": 1.5, "expansion": 0.5},
        "military": {"heal_below": 1.1, "min_hp_after_attack": 1.25, "min_damage_ratio": 1.3},
    },
    Posture.CONQUER: {
        "production": {"military": 1.8, "army_per_city": 1.8, "expansion": 0.7, "wonders": 0.6},
        "military": {"min_damage_ratio": 0.8, "min_hp_after_attack": 0.8},
    },
}
# Cities added to the default target. An explicit `target_cities` replaces the result.
_POSTURE_CITY_DELTA = {
    Posture.EXPAND: 2,
    Posture.BALANCED: 0,
    Posture.DEFEND: -2,
    Posture.CONQUER: 0,
}


class PolicyDocumentError(ValueError):
    """A document was rejected. `problems` lists every reason, each naming its field."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass(frozen=True)
class Expansion:
    target_cities: int | None = None  # None keeps the posture's target.


@dataclass(frozen=True)
class PolicyDocument:
    """A validated document. Build one with `parse_policy_document`, or use the defaults."""

    victory: VictoryTarget = VictoryTarget.ANY
    posture: Posture = Posture.BALANCED
    expansion: Expansion = field(default_factory=Expansion)
    # Domain to (field, value) pairs, sorted, so the document stays hashable and comparable.
    weights: tuple[tuple[str, tuple[tuple[str, float], ...]], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """The JSON form. `parse_policy_document` reads it back to an equal document."""
        return {
            "version": SCHEMA_VERSION,
            "victory": self.victory.value,
            "posture": self.posture.value,
            "expansion": {"target_cities": self.expansion.target_cities},
            "weights": {domain: dict(pairs) for domain, pairs in self.weights},
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)


DEFAULT_DOCUMENT = PolicyDocument()


@dataclass(frozen=True)
class ResolvedPolicies:
    """The weights each domain policy should use under a document."""

    production: ProductionWeights
    research: ResearchWeights
    secondary: SecondaryWeights
    military: MilitaryWeights
    gold: ProductionWeights


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _check_choice(
    data: Mapping[str, Any], key: str, enum: type[StrEnum], problems: list[str]
) -> StrEnum | None:
    if key not in data:
        return None
    value = data[key]
    try:
        return enum(value)
    except ValueError:
        options = ", ".join(member.value for member in enum)
        problems.append(f"{key}: {value!r} is not one of {options}")
        return None


def _check_weights(raw: object, problems: list[str]) -> tuple:
    if not isinstance(raw, Mapping):
        problems.append("weights: must be an object of domains")
        return ()
    parsed = []
    for domain, table in sorted(raw.items(), key=lambda pair: str(pair[0])):
        if domain not in _DOMAINS:
            problems.append(f"weights.{domain}: unknown domain, expected one of {sorted(_DOMAINS)}")
            continue
        if not isinstance(table, Mapping):
            problems.append(f"weights.{domain}: must be an object of weights")
            continue
        allowed = settable_weights(domain)
        pairs = []
        for name, value in sorted(table.items(), key=lambda pair: str(pair[0])):
            where = f"weights.{domain}.{name}"
            if name not in allowed:
                problems.append(f"{where}: unknown weight")
            elif not _is_number(value) or not math.isfinite(value):
                problems.append(f"{where}: must be a finite number")
            elif value < 0 or value > MAX_WEIGHT:
                problems.append(f"{where}: must be between 0 and {MAX_WEIGHT:g}")
            elif name in _FRACTION_FIELDS.get(domain, ()) and value > 1:
                problems.append(f"{where}: is a share of health and must be at most 1")
            elif allowed[name] is int and value != int(value):
                problems.append(f"{where}: must be a whole number")
            else:
                pairs.append((name, int(value) if allowed[name] is int else float(value)))
        parsed.append((domain, tuple(pairs)))
    return tuple(parsed)


def _check_expansion(raw: object, problems: list[str]) -> Expansion:
    if not isinstance(raw, Mapping):
        problems.append("expansion: must be an object")
        return Expansion()
    extra = sorted(str(key) for key in raw if key != "target_cities")
    if extra:
        problems.append(f"expansion: unknown fields {extra}")
    target = raw.get("target_cities")
    if target is None:
        return Expansion()
    if not _is_number(target) or not math.isfinite(target) or target != int(target):
        problems.append("expansion.target_cities: must be a whole number")
        return Expansion()
    if not MIN_TARGET_CITIES <= target <= MAX_TARGET_CITIES:
        problems.append(
            f"expansion.target_cities: must be {MIN_TARGET_CITIES} to {MAX_TARGET_CITIES}"
        )
        return Expansion()
    return Expansion(target_cities=int(target))


def parse_policy_document(data: object) -> PolicyDocument:
    """Validate a decoded JSON value. Raises `PolicyDocumentError` listing every problem."""
    if not isinstance(data, Mapping):
        raise PolicyDocumentError(["document: must be an object"])
    problems: list[str] = []
    if extra := sorted(str(key) for key in data if key not in _TOP_LEVEL):
        problems.append(f"document: unknown fields {extra}")
    if "version" in data and data["version"] != SCHEMA_VERSION:
        problems.append(f"version: {data['version']!r} is not supported, expected {SCHEMA_VERSION}")
    victory = _check_choice(data, "victory", VictoryTarget, problems)
    posture = _check_choice(data, "posture", Posture, problems)
    expansion = (
        _check_expansion(data["expansion"], problems) if "expansion" in data else Expansion()
    )
    weights = _check_weights(data["weights"], problems) if "weights" in data else ()
    if problems:
        raise PolicyDocumentError(problems)
    return PolicyDocument(
        victory=victory or VictoryTarget.ANY,
        posture=posture or Posture.BALANCED,
        expansion=expansion,
        weights=weights,
    )


def parse_policy_json(text: str) -> PolicyDocument:
    """Like `parse_policy_document`, for text. Bad JSON is a rejection like any other."""
    try:
        data = json.loads(text)
    except ValueError as error:
        raise PolicyDocumentError([f"document: not valid JSON ({error})"]) from error
    return parse_policy_document(data)


def accept_document(
    data: object, current: PolicyDocument = DEFAULT_DOCUMENT
) -> tuple[PolicyDocument, list[str]]:
    """Parse `data` and return it, or return `current` and the problems if it is invalid."""
    try:
        return parse_policy_document(data), []
    except PolicyDocumentError as error:
        return current, error.problems


def _scaled(weights: Any, domain: str, profiles: list[dict[str, dict[str, float]]]) -> Any:
    changes: dict[str, float] = {}
    key = "production" if domain == "gold" else domain  # Gold reads the production weights.
    for profile in profiles:
        for name, factor in profile.get(key, {}).items():
            changes[name] = changes.get(name, getattr(weights, name)) * factor
    return replace(weights, **changes) if changes else weights


def resolve(document: PolicyDocument) -> ResolvedPolicies:
    """The weights for each domain: defaults, scaled by victory and posture, then overrides."""
    profiles = [_VICTORY_PROFILES[document.victory], _POSTURE_PROFILES[document.posture]]
    overrides = {domain: dict(pairs) for domain, pairs in document.weights}
    resolved = {}
    for domain, cls in _DOMAINS.items():
        weights = _scaled(cls(), domain, profiles)
        if domain in ("production", "gold"):
            target = document.expansion.target_cities
            if target is None:
                target = weights.target_cities + _POSTURE_CITY_DELTA[document.posture]
            weights = replace(
                weights, target_cities=max(MIN_TARGET_CITIES, min(MAX_TARGET_CITIES, target))
            )
        if domain in overrides:
            weights = replace(weights, **overrides[domain])
        resolved[domain] = weights
    return ResolvedPolicies(**resolved)
