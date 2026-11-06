"""Keep the government, its policy cards and the governors filled and reasonable.

Three jobs, each deterministic. The same game state and the same weights give the same actions.

- Government: move up to a better government when one is unlocked. Ranks come from
  `GovernmentWeights.government_ranks`, and a change must beat the current rank by `min_rank_gain`,
  so the policy only ever climbs and can't flip back and forth. Two more bounds apply: at least
  `min_turns_between_changes` turns apart and at most `max_government_changes` in all. civ6-mcp
  lists no unlocked governments, so each candidate is tried best first. One the game calls locked
  is not tried again until another civic is completed, because that is what unlocks one.
- Policy cards: each empty slot takes the best-scoring unused card that fits, one slot at a time,
  so one refused card strands nothing. Slots of a fixed type are filled before wildcard slots,
  so a wildcard slot can't take the card a fixed slot needed. A card in a slot is never swapped.
- Governors: every point is spent, appointing a new governor first and promoting after. A new
  governor is placed in the best free city for its role. A placed governor never moves, because
  moving restarts the time it takes to establish.

After a government change the slots are not read again that turn: the game applies the change
later, so the slots would still look like the old ones. They are filled on the next turn.

`GovernmentWeights` is a plain typed input for the Phase 2 strategist to set.
"""

import logging
import math
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, fields
from typing import Any, Protocol

from civ6_ai.blockers import Blocker, BlockerType, action_succeeded
from civ6_ai.state import City, Government, read_cities, read_government, read_research
from civ6_ai.state.models import Policy, PolicySlot

log = logging.getLogger(__name__)

MAX_GOVERNMENT_REFUSALS = 3  # Refusals other than locked, per turn. Locked ones don't count.
MAX_CARDS_PER_SLOT = 4  # Cards tried per slot before leaving it until next turn.
MAX_GOVERNOR_ACTIONS = 8  # Appointments and promotions per turn. Each one spends a point.
MAX_OPTIONS_PER_POINT = 6  # Governors or promotions tried for one point.
MAX_CITIES_PER_GOVERNOR = 3  # Cities tried for one governor.
_WILDCARD = "SLOT_WILDCARD"
_NO_GOVERNMENT = "NONE"  # civ6-mcp's value when it read none, which may only be a short read.

# Higher is better. Keyed by the government type without its `GOVERNMENT_` prefix. Climbing tiers
# is the point, so each tier sits a clear step above the last. Not tuned against games yet.
DEFAULT_GOVERNMENT_RANKS: tuple[tuple[str, float], ...] = (
    ("AUTOCRACY", 1.0),
    ("CHIEFDOM", 0.0),
    ("CLASSICAL_REPUBLIC", 2.0),
    ("COMMUNISM", 4.2),
    ("CORPORATE_LIBERTARIANISM", 5.2),
    ("DEMOCRACY", 4.5),
    ("DIGITAL_DEMOCRACY", 5.4),
    ("FASCISM", 4.0),
    ("MERCHANT_REPUBLIC", 3.2),
    ("MONARCHY", 3.0),
    ("OLIGARCHY", 1.5),
    ("SYNTHETIC_TECHNOCRACY", 5.3),
    ("THEOCRACY", 2.8),
)

# Cards worth having first, by the type without its `POLICY_` prefix. Anything not listed scores
# `default_policy`, so a slot is never left empty for want of an opinion. Not tuned against games.
DEFAULT_POLICY_PRIORITIES: tuple[tuple[str, float], ...] = (
    ("AGOGE", 1.0),
    ("CRAFTSMEN", 2.0),
    ("DISCIPLINE", 1.5),
    ("GOD_KING", 2.5),
    ("INSPIRATION", 2.5),
    ("LAND_SURVEYORS", 2.0),
    ("MILITARY_TRADITION", 2.0),
    ("RATIONALISM", 3.0),
    ("URBAN_PLANNING", 3.0),
)

# Which governors to appoint first, by the type without its `GOVERNOR_THE_` prefix.
DEFAULT_GOVERNOR_PRIORITIES: tuple[tuple[str, float], ...] = (
    ("AMBASSADOR", 2.0),
    ("BUILDER", 4.0),
    ("CARDINAL", 2.0),
    ("DEFENDER", 1.0),
    ("EDUCATOR", 5.0),
    ("MERCHANT", 3.0),
    ("RESOURCE_MANAGER", 3.0),
)

# What each governor wants in its city: a `City` yield, or `threat` for the most threatened city.
DEFAULT_GOVERNOR_FOCUS: tuple[tuple[str, str], ...] = (
    ("AMBASSADOR", "culture"),
    ("BUILDER", "production"),
    ("CARDINAL", "faith"),
    ("DEFENDER", "threat"),
    ("EDUCATOR", "science"),
    ("MERCHANT", "gold"),
    ("RESOURCE_MANAGER", "production"),
)
_FOCUS_FIELDS = frozenset(
    {"culture", "faith", "food", "gold", "pop", "production", "science", "threat"}
)
_CAPITAL_BONUS = 0.5  # Breaks ties toward the capital, which stays safest and best connected.


@dataclass(frozen=True)
class GovernmentWeights:
    """How much each choice is worth. The defaults make a steady climb through the tiers."""

    government_ranks: tuple[tuple[str, float], ...] = DEFAULT_GOVERNMENT_RANKS  # Pairs: hashable.
    min_rank_gain: float = 1.0  # A change must beat the current rank by this much.
    min_turns_between_changes: int = 20
    max_government_changes: int = 8
    policy_priorities: tuple[tuple[str, float], ...] = DEFAULT_POLICY_PRIORITIES
    default_policy: float = 1.0  # For a card with no entry above.
    governor_priorities: tuple[tuple[str, float], ...] = DEFAULT_GOVERNOR_PRIORITIES
    default_governor: float = 1.0
    governor_focus: tuple[tuple[str, str], ...] = DEFAULT_GOVERNOR_FOCUS

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, bool):
                raise ValueError(f"{item.name} must be a number")
            if isinstance(value, int | float) and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{item.name} must be a finite number of at least 0")
        for table in (self.government_ranks, self.policy_priorities, self.governor_priorities):
            for name, value in table:
                if not isinstance(value, int | float) or not math.isfinite(value):
                    raise ValueError(f"{name} needs a finite number")
        for name, focus in self.governor_focus:
            if focus not in _FOCUS_FIELDS:
                raise ValueError(f"{name} has an unknown focus {focus!r}")


class GovernmentGame(Protocol):
    """The part of civ6-mcp's `GameState` that this policy uses."""

    async def get_game_overview(self) -> Any: ...
    async def get_tech_civics(self) -> Any: ...
    async def get_policies(self) -> Any: ...
    async def get_governors(self) -> Any: ...
    async def get_cities(self) -> Any: ...
    async def get_threat_scan(self) -> Any: ...
    async def get_diplomacy(self) -> Any: ...
    async def get_empire_resources(self) -> Any: ...
    async def change_government(self, government_type: str) -> str: ...
    async def set_policies(self, assignments: dict[int, str]) -> str: ...
    async def appoint_governor(self, governor_type: str) -> str: ...
    async def assign_governor(self, governor_type: str, city_id: int) -> str: ...
    async def promote_governor(self, governor_type: str, promotion_type: str) -> str: ...


def _short(item_type: str) -> str:
    """`GOVERNMENT_MONARCHY` becomes `MONARCHY`, `GOVERNOR_THE_EDUCATOR` becomes `EDUCATOR`."""
    name = item_type.split("_", 1)[-1]
    return name.removeprefix("THE_")


# --- Government ---------------------------------------------------------------------------------


def government_rank(government: str, weights: GovernmentWeights) -> float:
    return dict(weights.government_ranks).get(_short(government), 0.0)


def government_candidates(current: str, weights: GovernmentWeights) -> list[str]:
    """Governments worth changing to, best first. Ties go to the name in alphabetical order."""
    here = government_rank(current, weights)
    ranked = [
        (-rank, name)
        for name, rank in weights.government_ranks
        if rank >= here + weights.min_rank_gain
    ]
    return [f"GOVERNMENT_{name}" for _, name in sorted(ranked)]


# --- Policy cards -------------------------------------------------------------------------------


def card_fits(slot: PolicySlot, card: Policy) -> bool:
    """A wildcard slot takes any card. Any other slot takes only a card of its own type."""
    return slot.slot_type == _WILDCARD or slot.slot_type == card.slot_type


def score_card(card: Policy, weights: GovernmentWeights) -> float:
    return dict(weights.policy_priorities).get(_short(card.policy_type), weights.default_policy)


def rank_cards(slot: PolicySlot, spare: list[Policy], weights: GovernmentWeights) -> list[Policy]:
    """The cards that fit the slot, best first. Ties go to the card type in alphabetical order."""
    fits = [card for card in spare if card_fits(slot, card)]
    return sorted(fits, key=lambda card: (-score_card(card, weights), card.policy_type))


def _policies_set(result: str) -> bool:
    """civ6-mcp reports `POLICIES_SET`, and adds `SILENT_FAILURE` if its own re-read disagrees.

    Text with neither, such as its no-response line, is not taken as a success.
    """
    return action_succeeded(result) and "POLICIES_SET" in result and "SILENT_FAILURE" not in result


async def fill_empty_slots(game: GovernmentGame, weights: GovernmentWeights) -> bool:
    """Fill every empty slot it can. True if none is left empty."""
    government = await read_government(game)
    if government.government == _NO_GOVERNMENT and not government.policy_slots:
        log.warning("No government read, so no policy slots to fill")
        return False
    slotted = {slot.policy for slot in government.policy_slots if slot.policy is not None}
    spare = [card for card in government.unlocked_policies if card.policy_type not in slotted]
    # Fixed-type slots first, so a wildcard slot can't take the only card one of them fits.
    empty = sorted(government.empty_policy_slots, key=lambda s: (s.slot_type == _WILDCARD, s.index))
    filled = 0
    for slot in empty:
        for card in rank_cards(slot, spare, weights)[:MAX_CARDS_PER_SLOT]:
            result = await game.set_policies({slot.index: card.policy_type})
            if _policies_set(result):
                log.info("Slot %s takes %s", slot.index, card.policy_type)
                spare.remove(card)
                filled += 1
                break
            log.warning("The game refused %s in slot %s: %s", card.policy_type, slot.index, result)
    return filled == len(empty)


# --- Governors ----------------------------------------------------------------------------------


def governor_priority(governor_type: str, weights: GovernmentWeights) -> float:
    return dict(weights.governor_priorities).get(_short(governor_type), weights.default_governor)


def _by_priority(types: tuple[str, ...], weights: GovernmentWeights) -> list[str]:
    return sorted(types, key=lambda t: (-governor_priority(t, weights), t))


def city_value(city: City, governor_type: str, weights: GovernmentWeights) -> float:
    """How much a governor of this type is worth in the city."""
    focus = dict(weights.governor_focus).get(_short(governor_type), "pop")
    value = len(city.threats) if focus == "threat" else float(getattr(city, focus))
    return value + (_CAPITAL_BONUS if city.is_capital else 0.0)


def rank_cities(cities: list[City], governor_type: str, weights: GovernmentWeights) -> list[City]:
    """Best city first. Ties go to the lower city id."""
    return sorted(cities, key=lambda c: (-city_value(c, governor_type, weights), c.city_id))


def _took(result: str, marker: str) -> bool:
    """The game's text names what it did. Text without the marker isn't taken as success."""
    return action_succeeded(result) and marker in result


async def spend_governor_points(game: GovernmentGame, weights: GovernmentWeights) -> bool:
    """Appoint, or else promote, until no point is left or none can be spent. True if all spent.

    The state is read again after each action, and the loop stops the first time a point doesn't
    go down, so an action the game accepted but didn't apply can't repeat.
    """
    government = await read_government(game)
    for _ in range(MAX_GOVERNOR_ACTIONS):
        points = government.governor_points
        if points <= 0:
            return True
        if not await _spend_one(game, government, weights):
            log.warning("No governor point could be spent, %s left", points)
            return False
        government = await read_government(game)
        if government.governor_points >= points:
            log.warning("The game took a governor action but kept the point")
            return False
    return government.governor_points <= 0


async def _spend_one(game: GovernmentGame, government: Government, weights: GovernmentWeights):
    """Try appointments, then promotions, until the game accepts one."""
    if government.can_appoint_governor:
        wanted = _by_priority(government.appointable_governors, weights)
        for governor_type in wanted[:MAX_OPTIONS_PER_POINT]:
            result = await game.appoint_governor(governor_type)
            if _took(result, "APPOINT"):  # `APPOINTED`, or `APPOINT_REQUESTED` if still pending.
                log.info("Appointed %s", governor_type)
                return True
            log.warning("The game refused to appoint %s: %s", governor_type, result)
    appointed = {g.governor_type: g for g in government.governors}
    for governor_type in _by_priority(tuple(appointed), weights):
        for promotion in appointed[governor_type].promotions[:MAX_OPTIONS_PER_POINT]:
            result = await game.promote_governor(governor_type, promotion)
            if _took(result, "PROMOTED"):
                log.info("Promoted %s with %s", governor_type, promotion)
                return True
            log.warning("The game refused %s for %s: %s", promotion, governor_type, result)
    return False


async def place_governors(game: GovernmentGame, weights: GovernmentWeights) -> bool:
    """Send each governor without a city to the best free one. True if none is left waiting."""
    government = await read_government(game)
    waiting = [g.governor_type for g in government.governors if g.city_id is None]
    if not waiting:
        return True
    taken = {g.city_id for g in government.governors if g.city_id is not None}
    free = [city for city in await read_cities(game) if city.city_id not in taken]
    placed = 0
    for governor_type in _by_priority(tuple(waiting), weights):
        for city in rank_cities(free, governor_type, weights)[:MAX_CITIES_PER_GOVERNOR]:
            result = await game.assign_governor(governor_type, city.city_id)
            if _took(result, "ASSIGNED"):
                log.info("%s goes to %s", governor_type, city.name)
                free.remove(city)
                placed += 1
                break
            log.warning("The game refused %s in %s: %s", governor_type, city.name, result)
    return placed == len(waiting)


# --- The policy ---------------------------------------------------------------------------------


class GovernmentPolicy:
    """A turn handler for government, cards and governors. Keep one instance for a whole game."""

    name = "government"

    def __init__(self, weights: GovernmentWeights | None = None) -> None:
        self.weights = weights or GovernmentWeights()
        self._changes = 0
        self._last_change_turn: int | None = None
        # Governments the game called locked, with the civics completed when it did.
        self._locked_at: dict[str, int] = {}

    async def change_government(self, game: GovernmentGame) -> bool:
        """Move up one government if a better one is unlocked. True if the game accepted a change.

        A refusal is not an error: most turns most candidates are still locked.
        """
        if self._changes >= self.weights.max_government_changes:
            return False
        turn = (await game.get_game_overview()).turn
        last = self._last_change_turn
        if last is not None and turn - last < self.weights.min_turns_between_changes:
            return False
        government = await read_government(game)
        if government.government == _NO_GOVERNMENT:
            return False  # Possibly a short read, and a change from nothing isn't a climb.
        civics = (await read_research(game)).civics_done
        candidates = [
            c
            for c in government_candidates(government.government, self.weights)
            if self._locked_at.get(c, -1) < civics
        ]
        refused = 0
        for candidate in candidates:
            result = await game.change_government(candidate)
            if _took(result, "GOVERNMENT_CHANGED"):
                log.info("Government changes to %s", candidate)
                self._changes += 1
                self._last_change_turn = turn
                self._locked_at.clear()
                return True
            if "GOV_LOCKED" in result or "GOV_NOT_FOUND" in result:
                self._locked_at[candidate] = civics  # Free: it's remembered, so tried once.
                continue
            log.warning("The game refused %s: %s", candidate, result)
            refused += 1
            if refused >= MAX_GOVERNMENT_REFUSALS:
                break
        return False

    async def run(self, game: GovernmentGame) -> None:
        failures: list[str] = []
        changed = False
        steps: list[tuple[str, Callable[[], Awaitable[bool]]]] = [
            ("policy cards", lambda: fill_empty_slots(game, self.weights)),
            ("governor points", lambda: spend_governor_points(game, self.weights)),
            ("governor placement", lambda: place_governors(game, self.weights)),
        ]
        try:
            changed = await self.change_government(game)
        except ConnectionError:
            raise
        except Exception:
            log.exception("Changing government failed")
            failures.append("government")
        if changed:
            steps = steps[1:]  # The slots still show the old government until next turn.
        for label, step in steps:
            try:
                if not await step():
                    log.warning("Left something unfinished in %s", label)
            except ConnectionError:
                raise
            except Exception:
                log.exception("Handling %s failed", label)
                failures.append(label)
        if failures:
            raise RuntimeError(f"Government policy failed in {', '.join(failures)}")

    def blocker_policies(self) -> Mapping[str, Callable[[Any, Blocker], Awaitable[bool]]]:
        """Policies for `BlockerResolver`. Use the same instance as the turn handler."""

        async def fill(game: Any, blocker: Blocker) -> bool:
            return await fill_empty_slots(game, self.weights)

        async def appoint(game: Any, blocker: Blocker) -> bool:
            return await spend_governor_points(game, self.weights)

        async def idle(game: Any, blocker: Blocker) -> bool:
            return await place_governors(game, self.weights)

        async def consider(game: Any, blocker: Blocker) -> bool:
            await self.change_government(game)
            return True  # Either way `end_turn` clears the notification, as the default does.

        return {
            BlockerType.FILL_CIVIC_SLOT: fill,
            BlockerType.GOVERNOR_APPOINTMENT: appoint,
            BlockerType.GOVERNOR_OPPORTUNITY: appoint,
            BlockerType.GOVERNOR_IDLE: idle,
            BlockerType.CONSIDER_GOVERNMENT_CHANGE: consider,
        }


def blocker_policies(
    policy: GovernmentPolicy | None = None,
) -> Mapping[str, Callable[[Any, Blocker], Awaitable[bool]]]:
    """Policies for `BlockerResolver`. Pass the turn handler's policy so the bounds are shared."""
    return (policy or GovernmentPolicy()).blocker_policies()
