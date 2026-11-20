"""Wonder races: estimate whether we finish a wonder before a rival does (issue 323).

An early wonder is a gamble. If a rival finishes first, the production is mostly lost, while a
district is a committed benefit. So the decision is a comparison: the probability of winning
times the wonder's value, against what the same production would give elsewhere. All functions
here are pure and take fixtures; the production policy feeds them what it already reads.

Every constant is an approximation, set by reasoning and not yet by a live race. The live check
(issue 323) reads the `wonder race` log lines against the wonders actually lost.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# How much production the AI puts out relative to a human at the same turn, by difficulty. At
# Prince the AI has no bonus, so a human-equal baseline is the model. Higher difficulties add
# yield and unit bonuses (more settlers and more production per city). APPROXIMATE: the real
# bonuses differ by yield and era, and this folds them into one factor.
DIFFICULTY_AI_FACTOR: dict[str, float] = {
    "SETTLER": 0.6,
    "CHIEFTAIN": 0.75,
    "WARLORD": 0.9,
    "PRINCE": 1.0,
    "KING": 1.2,
    "EMPEROR": 1.4,
    "IMMORTAL": 1.65,
    "DEITY": 1.9,
}
_UNKNOWN_FACTOR = 1.2  # An unreadable difficulty is treated a little above Prince, to be careful.

# What a rival capital makes per turn, and how it grows: a baseline for a rival that races for the
# wonder, before the difficulty factor.
_RIVAL_BASE_PRODUCTION = 4.0
_RIVAL_PRODUCTION_PER_TURN = 0.06
# A rival rarely puts its whole production into one wonder: it builds units, settlers and
# districts too. The share it gives a wonder it has chosen.
_RIVAL_WONDER_SHARE = 0.8
# Chance that a given rival chooses this wonder at all, before difficulty and turn scaling. Only
# a few of the AI's wonders are chosen early. A rival seen building it is certain.
_ATTEMPT_BASE = 0.35
_ATTEMPT_MAX = 0.8
# Width (turns) of the uncertainty around a rival's finish turn; the chance it beats us is a
# logistic of the gap over this width.
_SPREAD_TURNS = 12.0
# Value of finishing the wonder, in the production policy's score units (its `_WONDER_VALUE`).
WONDER_VALUE = 8.0
# The estimate is rough, so expected value must beat the alternative by this factor.
_EDGE = 1.15
# Win probability needed to start a race, and under which a race in progress is dropped. Starting
# needs more than staying, so the bot does not flip each turn (hysteresis).
START_MIN_PROBABILITY = 0.6
ABANDON_PROBABILITY = 0.35


@dataclass(frozen=True)
class RaceEstimate:
    """What the estimator concluded for one wonder in one city."""

    wonder: str
    our_turn: int  # The turn we expect to finish on.
    rival_probability: float  # Chance some rival finishes first.
    win_probability: float
    expected_value: float
    alternative: float
    go: bool
    reasons: tuple[str, ...]

    def log_line(self, city: str) -> str:
        """One line for the bot log: the estimate, the probability and the choice."""
        return (
            f"wonder race {city} {self.wonder}: finish turn {self.our_turn}, "
            f"win {self.win_probability:.2f}, rival {self.rival_probability:.2f}, "
            f"value {self.expected_value:.1f} vs alt {self.alternative:.1f}, "
            f"{'GO' if self.go else 'NO-GO'} ({'; '.join(self.reasons)})"
        )


def ai_factor(difficulty: str) -> float:
    """The AI's production factor for a difficulty name (approximate)."""
    return DIFFICULTY_AI_FACTOR.get(difficulty.upper().removeprefix("DIFFICULTY_"), _UNKNOWN_FACTOR)


def our_finish_turn(turn: int, cost: float, production: float, other_work: float = 0.0) -> int:
    """The turn we finish, with `other_work` production we must spend elsewhere first."""
    if production <= 0:
        return turn + 999
    return turn + math.ceil((max(cost, 0.0) + max(other_work, 0.0)) / production)


def rival_finish_turn(turn: int, cost: float, difficulty: str, progress: float = 0.0) -> float:
    """The turn a rival that races for the wonder finishes, from `progress` (0 to 1) already done.

    Production is the baseline at the current turn, scaled by the difficulty factor and the
    share a rival gives one wonder, so it underestimates a long race a little.
    """
    per_turn = (
        (_RIVAL_BASE_PRODUCTION + _RIVAL_PRODUCTION_PER_TURN * turn)
        * ai_factor(difficulty)
        * _RIVAL_WONDER_SHARE
    )
    remaining = max(cost, 0.0) * (1.0 - min(max(progress, 0.0), 1.0))
    return turn + remaining / max(per_turn, 0.1)


def attempt_probability(turn: int, difficulty: str, rival_wonders_known: int = 0) -> float:
    """Chance that one unobserved rival is building this wonder. Higher for a strong AI and late."""
    base = _ATTEMPT_BASE * ai_factor(difficulty) * (1.0 + turn / 200.0)
    base += 0.02 * min(rival_wonders_known, 5)  # Rivals that built wonders are wonder-minded.
    return min(base, _ATTEMPT_MAX)


def rival_win_probability(
    our_turn: int,
    turn: int,
    cost: float,
    difficulty: str,
    rivals: int,
    progress: tuple[float, ...] = (),
    rival_wonders_known: int = 0,
) -> float:
    """Chance that at least one rival finishes the wonder before `our_turn`.

    `progress` holds the observed fraction (0 to 1) for each rival seen building it; each is
    certain to be racing. The other rivals are modelled by attempt probability and the baseline
    finish turn.
    """

    def beats_us(finish: float) -> float:
        return 1.0 / (1.0 + math.exp(-(our_turn - finish) / _SPREAD_TURNS))

    none_first = 1.0
    for fraction in progress:
        none_first *= 1.0 - beats_us(rival_finish_turn(turn, cost, difficulty, fraction))
    unseen = max(rivals - len(progress), 0)
    if unseen:
        p = attempt_probability(turn, difficulty, rival_wonders_known) * beats_us(
            rival_finish_turn(turn, cost, difficulty)
        )
        none_first *= (1.0 - p) ** unseen
    return 1.0 - none_first


def estimate_race(
    wonder: str,
    cost: float,
    production: float,
    turn: int,
    difficulty: str,
    rivals: int,
    alternative: float,
    other_work: float = 0.0,
    progress: tuple[float, ...] = (),
    rival_wonders_known: int = 0,
    already_started: bool = False,
    value: float = WONDER_VALUE,
) -> RaceEstimate:
    """Estimate the race for one wonder and say go or no-go.

    `alternative` is what the production is worth otherwise (for example the best district's
    score). `already_started` means we are in the race: it is dropped only below
    `ABANDON_PROBABILITY`, while a new start needs `START_MIN_PROBABILITY`.
    """
    ours = our_finish_turn(turn, cost, production, other_work)
    rival = rival_win_probability(
        ours, turn, cost, difficulty, rivals, progress, rival_wonders_known
    )
    win = 1.0 - rival
    expected = win * value
    floor = ABANDON_PROBABILITY if already_started else START_MIN_PROBABILITY
    reasons: list[str] = []
    if production <= 0:
        reasons.append("no production")
    if win < floor:
        reasons.append(f"win {win:.2f} under {floor:.2f}")
    if expected < alternative * _EDGE:
        reasons.append(f"value {expected:.1f} under alternative {alternative:.1f}")
    go = not reasons
    if go:
        reasons.append("favourable")
    return RaceEstimate(wonder, ours, rival, win, expected, alternative, go, tuple(reasons))


def should_abandon(
    win_probability: float, rival_progress: tuple[float, ...], our_progress: float
) -> bool:
    """Drop a race in progress when a rival is well ahead of us, or the win chance is gone.

    Progress values are fractions (0 to 1) of the cost. Past 90% of ours we always finish: the
    rest is cheap and a switch would lose what was spent.
    """
    if our_progress >= 0.9:
        return False
    lead = max(rival_progress, default=0.0) - our_progress
    return win_probability < ABANDON_PROBABILITY or (lead > 0.3 and win_probability < 0.5)
