"""Keep research and civics on a sensible path.

Each choice scores the options that can be started now and takes the best. Nothing is random and
nothing looks at the game outside the `Research` view, so the same view and the same weights give
the same choice. `ResearchWeights` is a plain typed input for the Phase 2 strategist to set.

The score of an option is the sum of:

- speed: `speed / turns`, so a quicker option wins when nothing else separates them.
- eureka: a flat bonus when the boost (eureka or inspiration) is already earned. The option's own
  turns already reflect the discount, so this only decides how much to chase it.
- unlocks: for each locked item that lists this option among its missing prerequisites, a bonus,
  full when this is the only one missing and half otherwise. A boosted locked item adds `eureka`
  on top when this option would open it at once.
- priority: a per-item bonus from `ResearchWeights.priorities`.
- finishing: up to `finishing` for progress already made.
- stickiness: a bonus to the item in progress, so the target doesn't flip each turn.
- commitment: for the civic in progress only, a bonus scaled by its (estimated) progress.

Ties go to the item type in alphabetical order.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Protocol

from civ6_ai.blockers import Blocker, BlockerGame, BlockerType
from civ6_ai.state import Research, read_research
from civ6_ai.state.models import LockedResearch, ResearchOption

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3  # Options tried in score order before giving up until next turn.
_NO_TURNS = 999  # Stands in for an option with no known time to finish.

# Early items worth having first. Keyed by the name without its `TECHNOLOGY_` or `CIVIC_` prefix.
DEFAULT_PRIORITIES: tuple[tuple[str, float], ...] = (
    ("BRONZE_WORKING", 1.0),
    ("CODE_OF_LAWS", 2.0),
    ("ASTRONOMY", 1.0),  # Run 11: researched on turn 182, so science buildings came late.
    ("CHEMISTRY", 1.5),  # The Research Lab. Run 11 never reached it: 0 labs in 5 cities.
    ("CRAFTSMANSHIP", 1.0),
    ("CURRENCY", 1.0),
    ("EDUCATION", 2.5),  # The University. Cheap techs (speed) otherwise run ahead of it.
    ("EARLY_EMPIRE", 1.5),
    ("FOREIGN_TRADE", 1.0),
    ("MASONRY", 0.5),
    ("POLITICAL_PHILOSOPHY", 2.0),
    ("POTTERY", 0.5),
    ("WRITING", 2.0),
)


@dataclass(frozen=True)
class ResearchWeights:
    """How much each reason counts. The defaults make a steady, science-leaning order."""

    speed: float = 4.0
    eureka: float = 3.0
    unlocks: float = 1.5
    finishing: float = 2.0
    stickiness: float = 1.0
    # Extra for the civic in progress, scaled by its estimated progress. Finishing (2.0) and
    # stickiness (1.0) top out at 3, while a boosted prerequisite scores 4.5 or more for its
    # unlock and eureka, so a civic 11 of 13 turns in was dropped for it (#312, PR #307 run).
    # Civics only: tech progress is read exactly and its order is not the problem.
    civic_commitment: float = 6.0
    priorities: tuple[tuple[str, float], ...] = DEFAULT_PRIORITIES  # Pairs, so it stays hashable.


class ResearchGame(Protocol):
    """The part of civ6-mcp's `GameState` that this policy uses."""

    async def get_tech_civics(self): ...
    async def get_game_overview(self): ...
    async def set_research(self, tech_name: str) -> str: ...
    async def set_civic(self, civic_name: str) -> str: ...


def _short(item_type: str) -> str:
    """`TECHNOLOGY_MINING` and `TECH_MINING` both become `MINING`."""
    return item_type.split("_", 1)[-1]


def score_option(
    option: ResearchOption,
    locked: tuple[LockedResearch, ...],
    weights: ResearchWeights,
    current: str | None = None,
) -> float:
    """Score one option. `current` is the localised name of the item in progress."""
    turns = option.turns if option.turns > 0 else _NO_TURNS
    score = weights.speed / turns
    if option.boosted:
        score += weights.eureka
    for item in locked:
        if option.name in item.missing:
            sole = len(item.missing) == 1
            score += weights.unlocks * (1.0 if sole else 0.5)
            if sole and item.boosted:
                score += weights.eureka
    score += dict(weights.priorities).get(_short(option.item_type), 0.0)
    score += weights.finishing * option.progress_pct / 100
    if option.name == current:
        score += weights.stickiness
        if option.item_type.startswith("CIVIC_"):
            score += weights.civic_commitment * option.progress_pct / 100
    return score


def estimate_civic_progress(research: Research, culture: float) -> Research:
    """Fill in the progress of the civic in progress, which civ6-mcp always reads as 0.

    civ6-mcp reads it with a `pcall` around `GetCulturalProgress`, which fails and leaves 0
    (#312). Without it only the stickiness bonus held a civic in progress, so a boost on another
    civic dropped one that was nearly done. The game's own turns left and the culture per turn
    give the points still to find: `progress = max(0, cost - turns_left * culture)`. The turns are
    rounded up, so the estimate errs low. Left as read when the civic isn't known, the turns
    aren't (-1) or the culture isn't positive. Capped at 99 so a civic never scores as finished.
    """
    turns = research.current_civic_turns
    if research.current_civic is None or turns is None or turns <= 0 or culture <= 0:
        return research
    civics = []
    for option in research.civics:
        if option.name == research.current_civic and option.progress_pct == 0 and option.cost > 0:
            left = max(0.0, option.cost - turns * culture)
            pct = max(0, min(99, int(100 * left / option.cost)))
            option = replace(option, progress_pct=pct)
        civics.append(option)
    return replace(research, civics=tuple(civics))


_estimate_logged = False


def _needs_estimate(research: Research) -> bool:
    turns = research.current_civic_turns
    return bool(
        research.current_civic is not None
        and turns is not None
        and turns > 0
        and any(o.name == research.current_civic and o.progress_pct == 0 for o in research.civics)
    )


async def read_research_with_civic_progress(game: ResearchGame) -> Research:
    """`read_research`, plus the civic progress estimate when it is needed (one extra read)."""
    global _estimate_logged
    research = await read_research(game)
    if not _needs_estimate(research):
        return research
    culture = float((await game.get_game_overview()).culture_yield)
    estimated = estimate_civic_progress(research, culture)
    if estimated is not research and not _estimate_logged:
        _estimate_logged = True
        log.info("Civic progress is estimated from cost, turns left and culture (#312)")
    return estimated


def rank_options(
    options: tuple[ResearchOption, ...],
    locked: tuple[LockedResearch, ...],
    weights: ResearchWeights,
    current: str | None = None,
) -> list[ResearchOption]:
    """Best first. Empty if nothing can be started."""
    scored = [(-score_option(o, locked, weights, current), o.item_type, o) for o in options]
    return [option for _, _, option in sorted(scored, key=lambda row: row[:2])]


def choose_tech(research: Research, weights: ResearchWeights) -> list[ResearchOption]:
    return rank_options(research.techs, research.locked_techs, weights, research.current_tech)


def choose_civic(research: Research, weights: ResearchWeights) -> list[ResearchOption]:
    return rank_options(research.civics, research.locked_civics, weights, research.current_civic)


def _succeeded(result: str) -> bool:
    return not result.startswith(("Error", "ERR:"))


async def _start_best(
    ranked: list[ResearchOption], current: str | None, setter: Callable[[str], Awaitable[str]]
) -> bool:
    """Start the best option, unless it's already the one in progress. True if settled."""
    if not ranked:
        return True  # Nothing is open to start, which isn't something a choice can fix.
    for option in ranked[:MAX_ATTEMPTS]:
        if option.name == current:
            return True
        if _succeeded(await setter(option.item_type)):
            log.info("Started %s", option.item_type)
            return True
        log.warning("The game refused %s", option.item_type)
    return False


def research_snapshot(
    options: tuple[ResearchOption, ...], locked: tuple[LockedResearch, ...]
) -> dict[str, list[int]]:
    """Each researchable item as `name: [cost, progress percent, boosted 0 or 1]`.

    Items not yet researchable whose boost is earned come as `[0, 0, 1]`: they have no progress
    and no cost shown yet.
    """
    rows = {o.name: [o.cost, o.progress_pct, int(o.boosted)] for o in options}
    for item in locked:
        if item.boosted and item.name not in rows:
            rows[item.name] = [0, 0, 1]
    return rows


def log_boosts(research: Research) -> None:
    """One line a turn with every tech and civic's cost, progress and boost (Run 15).

    The effectiveness report reads the progress an item had when its boost was earned, so it can
    say how many points the boost saved, not only whether there was one. The read already exists,
    so this costs no game call.
    """
    line = {
        "techs": research_snapshot(research.techs, research.locked_techs),
        "civics": research_snapshot(research.civics, research.locked_civics),
    }
    log.info("research_boosts %s", json.dumps(line, sort_keys=True))


class ResearchPolicy:
    """A turn handler that keeps a tech and a civic in progress and follows the best target."""

    name = "research"

    def __init__(self, weights: ResearchWeights | None = None) -> None:
        self.weights = weights or ResearchWeights()

    async def run(self, game: ResearchGame) -> None:
        research = await read_research_with_civic_progress(game)
        log_boosts(research)
        ok_tech = await _start_best(
            choose_tech(research, self.weights), research.current_tech, game.set_research
        )
        ok_civic = await _start_best(
            choose_civic(research, self.weights), research.current_civic, game.set_civic
        )
        if not (ok_tech and ok_civic):
            raise RuntimeError("The game refused every research or civic option tried")


def blocker_policies(
    weights: ResearchWeights | None = None,
) -> dict[BlockerType, Callable[[BlockerGame, Blocker], Awaitable[bool]]]:
    """Policies for `BlockerResolver`, which takes over the research and civic blockers."""
    weights = weights or ResearchWeights()

    async def choose_research(game: BlockerGame, blocker: Blocker) -> bool:
        research = await read_research(game)
        if research.current_tech is not None:
            return True  # A stale notification, which `end_turn` clears.
        ranked = choose_tech(research, weights)
        return bool(ranked) and await _start_best(ranked, None, game.set_research)

    async def choose_civic_blocker(game: BlockerGame, blocker: Blocker) -> bool:
        research = await read_research_with_civic_progress(game)
        if research.current_civic is not None:
            return True
        ranked = choose_civic(research, weights)
        return bool(ranked) and await _start_best(ranked, None, game.set_civic)

    return {
        BlockerType.RESEARCH: choose_research,
        BlockerType.CIVIC: choose_civic_blocker,
    }
