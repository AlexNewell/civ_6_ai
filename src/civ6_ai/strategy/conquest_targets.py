"""Rank city-state candidates for a conquest (issue 303, slice 1). Pure: legal reads in, list out.

Nothing calls this yet. It scores each candidate from what the player can see and gives a reason
for every term, so a later playbook can pick a target and a person can read why.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

# Tunables. Weights are in score points; higher is a better target.
WEIGHT_BLOCKS_SETTLING = 30.0  # The reason for the whole epic: it sits on land we want.
WEIGHT_RESOURCES = 6.0  # Per strategic or luxury resource within reach of the city.
WEIGHT_NO_WALLS = 20.0  # Unwalled falls to a small force; no siege units needed.
PENALTY_GARRISON = 8.0  # Per garrison unit.
PENALTY_DISTANCE = 1.5  # Per tile of distance from our nearest city.
PENALTY_ENVOY = 6.0  # Per envoy we have there: a sunk cost and a friend we would lose.
MAX_DISTANCE = 20  # Farther than this is never a target.
MIN_SCORE = 0.0  # Candidates scoring below this are left out of the ranking.


@dataclass(frozen=True)
class CityStateCandidate:
    name: str
    is_ally: bool
    has_walls: bool
    garrison_size: int
    distance: int  # Tiles from our nearest city.
    resources_nearby: int = 0
    blocks_settling: bool = False  # Hook for the loyalty and settle survey.
    our_envoys: int = 0


@dataclass(frozen=True)
class RankedTarget:
    name: str
    score: float
    reasons: list[str] = field(default_factory=list)


def score_candidate(c: CityStateCandidate) -> RankedTarget | None:
    """None when the candidate must not be attacked at all."""
    if c.is_ally:
        return None
    if c.distance > MAX_DISTANCE:
        return None
    score = 0.0
    reasons: list[str] = []

    def add(points: float, why: str) -> None:
        nonlocal score
        score += points
        reasons.append(f"{points:+g}: {why}")

    if c.blocks_settling:
        add(WEIGHT_BLOCKS_SETTLING, "blocks settling sites")
    if c.resources_nearby:
        add(WEIGHT_RESOURCES * c.resources_nearby, f"{c.resources_nearby} resources nearby")
    if not c.has_walls:
        add(WEIGHT_NO_WALLS, "no walls")
    else:
        reasons.append("+0: walls, siege units needed")
    if c.garrison_size:
        add(-PENALTY_GARRISON * c.garrison_size, f"garrison of {c.garrison_size}")
    add(-PENALTY_DISTANCE * c.distance, f"{c.distance} tiles away")
    if c.our_envoys:
        add(-PENALTY_ENVOY * c.our_envoys, f"{c.our_envoys} envoys of ours there")
    return RankedTarget(c.name, score, reasons)


def rank_targets(candidates: Iterable[CityStateCandidate]) -> list[RankedTarget]:
    """Best first; allies and far cities are dropped, ties break on name for determinism."""
    scored = [s for c in candidates if (s := score_candidate(c)) is not None]
    scored = [s for s in scored if s.score >= MIN_SCORE]
    return sorted(scored, key=lambda s: (-s.score, s.name))
