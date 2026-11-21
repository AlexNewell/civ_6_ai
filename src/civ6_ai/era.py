"""Era outlook: how far the era score is from a Dark or Golden Age, and when to push (issue 311).

At the world era change the game compares the era score with two thresholds: below the Dark one
is a Dark Age, at or above the Golden one a Golden Age. Historic Moments (first district of a
kind, a pantheon, meeting a civ or city-state, clearing a camp, a wonder) are what earn era score.
The player does not plan for them turn by turn. About 10 turns before the era ends they compare
the score with the thresholds, and if the next breakpoint is within reach they favour the cheap
moments. Fresh evidence: STD_T1_03 had era score 1 at turn 50 and STD_T1_04 had 19, against 36
for the player.

Reads that exist: the overview carries the era name, the era score and both thresholds (see
`orchestrator.read_standing`, `state.Empire`). Nothing readable gives the turns left in the era,
so turns left is the turn the era name last changed (`EraClock`; the first era starts on turn 1)
plus `EraSettings.era_length`, the Standard-speed era length scaled by the game speed. A game whose
eras do not follow that length corrects itself at the next change.
"""

import json
import logging
from dataclasses import asdict, dataclass

from civ6_ai.placement import speed_scale

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EraSettings:
    """When the bot pushes for Historic Moments. Zero `window` turns the push off."""

    # Turns per world era at Standard speed. Observed, not read from the game: in both a bot run
    # and the player's game the era name changed on turns 41, 81 and 121 (Ancient to Classical to
    # Medieval to Renaissance), so 40 turns an era. The first guess of 50 was wrong (issue 311).
    era_length: int = 40
    # The player looks at the timer with about 10 turns left, so the push starts there.
    window: int = 10
    # A gap larger than this is out of reach: cheap moments are worth 1 to 3 points each.
    dark_reach: int = 12  # Points short of the Dark threshold that still count as reachable.
    golden_reach: int = 5  # Points short of the Golden threshold (a Golden Age is a bonus).
    # Turn the push stops, so the last turns of a run are not spent on a boundary that no longer
    # matters. 0 means never.
    last_turn: int = 0

    def __post_init__(self) -> None:
        if min(self.era_length, self.window, self.dark_reach, self.golden_reach) < 0:
            raise ValueError("Era settings must not be negative")


@dataclass(frozen=True)
class EraOutlook:
    turn: int
    turns_left: int | None  # None when the era length is not known.
    score: int
    dark: int
    golden: int
    target: str  # "dark" (avoid a Dark Age), "golden", or "" when no breakpoint is ahead.
    gap: int  # Points to the target, 0 when there is none.
    push: bool

    def fields(self) -> dict:
        return asdict(self)


def turns_left_in_era(
    turn: int, game_speed: str, settings: EraSettings, era_start: int | None = None
) -> int | None:
    """Turns until the next era boundary, counted from the turn the era began.

    With no observed start the eras are taken to begin on turn 1, 1 + length, and so on.
    """
    length = round(settings.era_length * speed_scale(game_speed))
    if length <= 0:
        return None
    if era_start is None:
        era_start = 1 + ((turn - 1) // length) * length
    return max(1, era_start + length - turn)


class EraClock:
    """Remembers the turn the era name last changed, so turns left follows the game.

    The first era starts on turn 1. A game loaded part-way has no observed start, so the fixed
    grid stands in until the first change is seen.
    """

    def __init__(self) -> None:
        self.era: str | None = None
        self.start: int | None = None

    def observe(self, turn: int, era: str | None) -> int | None:
        """Record this turn's era name. Returns the observed start turn, or None when unknown."""
        if not era:
            return self.start
        if self.era is None:
            self.era = era
            self.start = 1 if turn <= 1 else None
        elif era != self.era:
            self.era = era
            self.start = turn
        return self.start


def outlook(
    turn: int,
    score: int,
    dark: int,
    golden: int,
    game_speed: str = "",
    settings: EraSettings | None = None,
    era_start: int | None = None,
) -> EraOutlook:
    """The era outlook for this turn. Pure: no reads, no log."""
    settings = settings or EraSettings()
    left = turns_left_in_era(turn, game_speed, settings, era_start)
    if score < dark:
        target, gap, reach = "dark", dark - score, settings.dark_reach
    elif score < golden:
        target, gap, reach = "golden", golden - score, settings.golden_reach
    else:
        target, gap, reach = "", 0, 0
    push = (
        bool(target)
        and left is not None
        and left <= settings.window
        and gap <= reach
        and not (settings.last_turn and turn >= settings.last_turn)
    )
    return EraOutlook(turn, left, score, dark, golden, target, gap, push)


def outlook_from_standing(
    turn: int,
    standing: dict,
    game_speed: str = "",
    settings: EraSettings | None = None,
    clock: EraClock | None = None,
) -> EraOutlook | None:
    """The outlook from the turn loop's standing fields, or None when they were not read."""
    start = clock.observe(turn, standing.get("era")) if clock is not None else None
    try:
        return outlook(
            turn,
            int(standing["era_score"]),
            int(standing["era_dark"]),
            int(standing["era_golden"]),
            game_speed,
            settings,
            start,
        )
    except (KeyError, TypeError, ValueError):
        return None


def log_outlook(result: EraOutlook | None) -> None:
    """One `era_outlook` line per turn, in the same form as the other decision lines."""
    if result is not None:
        log.info("era_outlook %s", json.dumps(result.fields(), sort_keys=True))


def district_nudge(item: str, built: frozenset[str], push: bool, bonus: float) -> float:
    """Extra score for the first district of its kind while the push is on (a Historic Moment).

    `item` and `built` are short district names (`CAMPUS`), as the production context holds them.
    """
    if push and bonus > 0 and item not in built:
        return bonus
    return 0.0
