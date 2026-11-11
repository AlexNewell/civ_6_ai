"""Trade routes, pantheon and religion choices, and great people, with no model calls.

Each choice is a pure ranking over what the game offers, so the same offers and weights always
give the same order. `SecondaryPolicy` is a turn handler that applies them. It also takes over
`BlockerType.PANTHEON` and `BlockerType.GREAT_PERSON_CLAIM` through `blocker_policies()`, with the
resolver's defaults left behind as a fallback.

What it does each turn:

- Trade: sends every idle trader on the best route the game accepts. Production builds the
  traders, see `ProductionContext.trade_capacity`.
- Pantheon: picks the best belief once faith allows and confirms it took.
- Religion: after a Great Prophet is activated, founds a religion with the best beliefs.
- Great people: recruits the best class that has enough points, then walks each great person to
  the district it activates in and activates it.

Missionaries and apostles, patronising great people with gold or faith, and the enhancer and
worship beliefs added after founding are not handled yet.

Fog of war: every read here is own-state or filtered, see `READ_AUDIT`. Trade destinations come
from a query whose fallback lists every living civ's cities, so foreign destinations are used
only for civs and city-states we have met. Rival religion data on a destination is never used.

`SecondaryWeights` is the plain input the Phase 2 strategist will set.
"""

import logging
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields
from typing import Any, Protocol

from civ6_ai.blockers import Blocker, BlockerGame, BlockerType, move_succeeded
from civ6_ai.policies.game_data import REPLACES
from civ6_ai.state import GameReads, Unit, read_units

log = logging.getLogger(__name__)

_YIELDS = ("food", "production", "gold", "science", "culture", "faith")
_YIELD_LABELS = {
    "Food": "food",
    "Prod": "production",
    "Gold": "gold",
    "Sci": "science",
    "Cul": "culture",
    "Faith": "faith",
}
_YIELD_PATTERN = re.compile(r"(Food|Prod|Gold|Sci|Cul|Faith):(-?\d+(?:\.\d+)?)")

MAX_DESTINATIONS = 4  # Routes tried per trader per turn. Every refusal costs a call.
TRADE_WAIT_TURNS = 3  # A trader with nowhere to go is left alone this long.
MAX_BELIEF_TRIES = 3
MAX_RECRUITS = 6  # Recruits per call. A refused person is skipped, so this only bounds a loop.
MAX_FOUND_ATTEMPTS = 6  # Religion founding attempts in a game. Each rotates the beliefs.
PROPHET_WINDOW = 3  # Turns after activating a prophet in which the religion is looked for.
NO_DISTRICT_WAIT = 3  # Turns before asking again where a great person can activate.
MAX_MOVE_TARGETS = 3
GP_STUCK_TURNS = 6  # Turns without getting closer, or failing, before a great person is set aside.
GP_SET_ASIDE_TURNS = 10

_PROPHET = "UNIT_GREAT_PROPHET"
_ACTIVATABLE = frozenset(
    {
        "UNIT_GREAT_SCIENTIST",
        "UNIT_GREAT_ENGINEER",
        "UNIT_GREAT_MERCHANT",
        "UNIT_GREAT_WRITER",
        "UNIT_GREAT_ARTIST",
        "UNIT_GREAT_MUSICIAN",
        _PROPHET,
    }
)
_RELIGION_NOTES = frozenset({"NOTIFICATION_CHOOSE_RELIGION", "NOTIFICATION_CHOOSE_BELIEF"})
_FOLLOWER = "BELIEF_CLASS_FOLLOWER"
_FOUNDER = "BELIEF_CLASS_FOUNDER"
_UNCLAIMED = "Unclaimed"


@dataclass(frozen=True)
class SecondaryWeights:
    """What each outcome is worth. Zero turns it off.

    The six yield weights scale trade routes and beliefs alike. The `gp_` weights rank which
    class of great person is recruited first, and zero never recruits that class.
    """

    food: float = 1.0
    production: float = 1.0
    gold: float = 1.0
    science: float = 1.0
    culture: float = 1.0
    faith: float = 0.5
    quest: float = 3.0  # A city-state that wants a trade route.
    trading_post: float = 0.5  # A foreign city where we have no trading post yet.
    religion_pressure: float = 0.05  # Per point of our religion's pressure on the destination.
    gp_scientist: float = 3.0
    gp_engineer: float = 2.5
    gp_merchant: float = 2.0
    gp_prophet: float = 2.0
    gp_writer: float = 1.0
    gp_artist: float = 1.0
    gp_musician: float = 1.0
    gp_general: float = 0.5
    gp_admiral: float = 0.5
    pantheon_min_faith: float = 10.0  # Below this the pantheon isn't tried outside a blocker.
    retry_turns: int = 3  # Wait after a pantheon the game didn't accept.

    def __post_init__(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if value < 0 or not math.isfinite(value):
                raise ValueError(f"Weight {item.name} must be a finite number of at least 0")


def canonical(item: str) -> str:
    """The standard unit a unique unit replaces."""
    return REPLACES.get(item, item)


# ----- trade routes -----


def parse_yields(text: str) -> dict[str, float]:
    """Read civ6-mcp's `Food:3 Prod:2 Gold:4` into yield amounts. Unknown text gives nothing."""
    found = {_YIELD_LABELS[label]: float(amount) for label, amount in _YIELD_PATTERN.findall(text)}
    return {name: found.get(name, 0.0) for name in _YIELDS if name in found}


def score_destination(dest: Any, weights: SecondaryWeights) -> float:
    """What a route is worth to us. Only our side's yields count, never the destination's."""
    value = sum(
        amount * getattr(weights, name) for name, amount in parse_yields(dest.origin_yields).items()
    )
    if dest.is_domestic:
        # A domestic route feeds our own city too, which the preview reports separately.
        value += sum(
            amount * getattr(weights, name)
            for name, amount in parse_yields(dest.dest_yields).items()
            if name in ("food", "production")
        )
    else:
        if not dest.has_trading_post:
            value += weights.trading_post
        value += weights.religion_pressure * max(dest.pressure_out, 0.0)
    if dest.is_city_state and dest.has_quest:
        value += weights.quest
    return value


def rank_destinations(
    destinations: Iterable[Any], weights: SecondaryWeights, met_owners: frozenset[str]
) -> list[Any]:
    """Eligible routes, best first. Ties break on name and tile, never on input order.

    A foreign city is eligible only if its owner is a civ or city-state we have met, because the
    fallback in civ6-mcp's destination query lists every living civ's cities.
    """
    eligible = [d for d in destinations if d.is_domestic or d.owner_name in met_owners]
    return sorted(eligible, key=lambda d: (-score_destination(d, weights), d.city_name, d.x, d.y))


# ----- beliefs -----

# Value of a belief to each yield it grows: (food, production, gold, science, culture, faith).
# Names are from the base game and Gathering Storm. Anything missing scores `_UNKNOWN_BELIEF`.
_BELIEFS: dict[str, tuple[float, float, float, float, float, float]] = {
    "BELIEF_CITY_PATRON_GODDESS": (0, 3.0, 0, 0, 0, 0),
    "BELIEF_DANCE_OF_THE_AURORA": (0, 0, 0, 0, 0, 1.5),
    "BELIEF_DESERT_FOLKLORE": (0, 0, 0, 0, 0, 2.0),
    "BELIEF_DIVINE_SPARK": (0, 0, 0, 2.0, 2.0, 0),
    "BELIEF_FERTILITY_RITES": (3.0, 0, 0, 0, 0, 0),
    "BELIEF_GOD_OF_CRAFTSMEN": (0, 2.5, 0, 0, 0, 0),
    "BELIEF_GOD_OF_THE_FORGE": (0, 2.5, 0, 0, 0, 0),
    "BELIEF_GOD_OF_THE_OPEN_SKY": (0, 0, 0, 0, 2.0, 0),
    "BELIEF_GOD_OF_THE_SEA": (0, 2.0, 0, 0, 0, 0),
    "BELIEF_GODDESS_OF_FESTIVALS": (0, 0, 0, 0, 2.0, 0),
    "BELIEF_GODDESS_OF_THE_HARVEST": (2.5, 0, 0, 0, 0, 0),
    "BELIEF_GODDESS_OF_THE_HUNT": (2.5, 0, 0, 0, 0, 0),
    "BELIEF_LADY_OF_THE_REEDS_AND_MARSHES": (0, 2.0, 0, 0, 0, 0),
    "BELIEF_MONUMENT_TO_THE_GODS": (0, 2.5, 0, 0, 0, 0),
    "BELIEF_ORAL_TRADITION": (0, 0, 0, 0, 2.5, 0),
    "BELIEF_RELIGIOUS_IDOLS": (0, 0, 0, 0, 0, 2.5),
    "BELIEF_RELIGIOUS_SETTLEMENTS": (2.5, 0, 0, 0, 0, 0),
    "BELIEF_RIVER_GODDESS": (0, 0, 0, 0, 2.0, 0),
    "BELIEF_SACRED_PATH": (0, 0, 0, 0, 0, 2.0),
    "BELIEF_STONE_CIRCLES": (0, 0, 0, 0, 0, 2.0),
    # Follower beliefs.
    "BELIEF_CHORAL_MUSIC": (0, 0, 0, 0, 3.0, 0),
    "BELIEF_DIVINE_INSPIRATION": (0, 0, 0, 0, 0, 3.0),
    "BELIEF_FEED_THE_WORLD": (3.0, 0, 0, 0, 0, 0),
    "BELIEF_HEATHEN_CONVERSION": (0, 1.0, 0, 0, 0, 0),
    "BELIEF_HOLY_WATERS": (2.0, 0, 0, 0, 0, 0),
    "BELIEF_JESUIT_EDUCATION": (0, 0, 0, 3.0, 0, 0),
    "BELIEF_WORK_ETHIC": (0, 3.5, 0, 0, 0, 0),
    "BELIEF_ZEN_MEDITATION": (0, 0, 0, 0, 3.0, 0),
    # Founder beliefs.
    "BELIEF_CHURCH_PROPERTY": (0, 0, 4.0, 0, 0, 0),
    "BELIEF_CROSS_CULTURAL_DIALOGUE": (0, 0, 0, 4.0, 0, 0),
    "BELIEF_LAY_MINISTRY": (0, 0, 2.0, 0, 0, 2.0),
    "BELIEF_PILGRIMAGE": (0, 0, 0, 0, 0, 3.0),
    "BELIEF_STEWARDSHIP": (0, 0, 3.0, 0, 0, 0),
    "BELIEF_TITHE": (0, 0, 3.5, 0, 0, 0),
    "BELIEF_WORLD_CHURCH": (0, 0, 0, 0, 4.0, 0),
}
_UNKNOWN_BELIEF = 1.0


def score_belief(belief_type: str, weights: SecondaryWeights) -> float:
    profile = _BELIEFS.get(belief_type)
    if profile is None:
        return _UNKNOWN_BELIEF
    return sum(
        amount * getattr(weights, name) for amount, name in zip(profile, _YIELDS, strict=True)
    )


def rank_beliefs(options: Iterable[Any], weights: SecondaryWeights) -> list[Any]:
    """Beliefs best first. Ties break on belief type, never on input order."""
    return sorted(options, key=lambda b: (-score_belief(b.belief_type, weights), b.belief_type))


# ----- great people -----

_GP_WEIGHTS = {
    "Great Scientist": "gp_scientist",
    "Great Engineer": "gp_engineer",
    "Great Merchant": "gp_merchant",
    "Great Prophet": "gp_prophet",
    "Great Writer": "gp_writer",
    "Great Artist": "gp_artist",
    "Great Musician": "gp_musician",
    "Great General": "gp_general",
    "Great Admiral": "gp_admiral",
}
_UNKNOWN_GP_CLASS = 0.5


def rank_recruits(
    people: Iterable[Any],
    weights: SecondaryWeights,
    refused: frozenset[int] = frozenset(),
    prophets: bool = True,
) -> list[Any]:
    """People we can recruit now, best class first. Claimed people and zero-weight classes are out.

    A person someone else has claimed can't be recruited whatever our points. Within a class
    the earliest era's person comes first, then by name.
    """

    def class_weight(person: Any) -> float:
        name = _GP_WEIGHTS.get(person.class_name)
        return getattr(weights, name) if name else _UNKNOWN_GP_CLASS

    wanted = [
        p
        for p in people
        if p.can_recruit
        and p.claimant == _UNCLAIMED
        and p.individual_id not in refused
        and class_weight(p) > 0
        and (prophets or p.class_name != "Great Prophet")
    ]
    return sorted(
        wanted, key=lambda p: (-class_weight(p), p.cost, p.individual_name, p.individual_id)
    )


# ----- the policy -----


class SecondaryGame(GameReads, BlockerGame, Protocol):
    """What the policy needs from civ6-mcp's `GameState`."""

    async def get_trade_destinations(self, unit_index: int) -> list[Any]: ...
    async def make_trade_route(self, unit_index: int, target_x: int, target_y: int) -> str: ...
    async def get_religion_founding_status(self) -> Any: ...
    async def found_religion(
        self, religion_type: str, follower_belief: str, founder_belief: str
    ) -> str: ...
    async def get_gp_advisor(self, unit_index: int) -> Any: ...
    async def activate_great_person(self, unit_index: int) -> str: ...


@dataclass
class _GreatPersonState:
    stuck: int = 0
    last_distance: int | None = None
    last_target: tuple[int, int] | None = None
    skip_until: int = -1


@dataclass
class SecondaryPolicy:
    """Runs trade, religion and great people. Run it before the blocker resolver."""

    weights: SecondaryWeights = field(default_factory=SecondaryWeights)
    name: str = "secondary"
    _trade_wait: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _great: dict[int, _GreatPersonState] = field(default_factory=dict, init=False, repr=False)
    _pantheon_retry: int = field(default=-1, init=False, repr=False)
    _found_attempts: int = field(default=0, init=False, repr=False)
    _prophet_turn: int | None = field(default=None, init=False, repr=False)
    _can_found: bool = field(default=True, init=False, repr=False)

    async def run(self, game: SecondaryGame) -> None:
        """Run every part so one failing doesn't strand the rest, then raise the first error."""
        turn = (await game.get_game_overview()).turn
        errors: list[Exception] = []
        can_found = False
        for step in ("pantheon", "religion", "recruit", "activate", "trade"):
            try:
                if step == "pantheon":
                    await self._pantheon(game, turn)
                elif step == "religion":
                    can_found = self._can_found = await self._religion(game, turn)
                elif step == "recruit":
                    await self._recruit(game)
                elif step == "activate":
                    await self._activate_all(game, turn, can_found)
                else:
                    await self._trade(game, turn)
            except Exception as error:  # noqa: BLE001 - each part is independent.
                log.exception("Secondary step %s failed", step)
                errors.append(error)
        if errors:
            raise errors[0]

    def blocker_policies(self) -> Mapping[str, Any]:
        """Policies to pass to `BlockerResolver`."""
        return {
            BlockerType.PANTHEON: self.resolve_pantheon,
            BlockerType.GREAT_PERSON_CLAIM: self.resolve_great_people,
        }

    async def resolve_pantheon(self, game: SecondaryGame, blocker: Blocker) -> bool:
        turn = (await game.get_game_overview()).turn
        return await self._pantheon(game, turn, force=True)

    async def resolve_great_people(self, game: SecondaryGame, blocker: Blocker) -> bool:
        await self._recruit(game)
        return True  # Whatever is left can't be recruited, so the claim isn't ours to clear.

    # ----- pantheon -----

    async def _pantheon(self, game: SecondaryGame, turn: int, force: bool = False) -> bool:
        """Choose a pantheon belief and confirm it took. True once we have one."""
        status = await game.get_pantheon_status()
        if status.has_pantheon:
            return True
        if not status.available_beliefs:
            return False
        if not force and (
            status.faith_balance < self.weights.pantheon_min_faith or turn < self._pantheon_retry
        ):
            return False
        for belief in rank_beliefs(status.available_beliefs, self.weights)[:MAX_BELIEF_TRIES]:
            result = await game.choose_pantheon(belief.belief_type)
            if "PANTHEON_FOUNDED" not in result:
                log.info("Pantheon %s refused: %s", belief.belief_type, result[:120])
                continue
            if (await game.get_pantheon_status()).has_pantheon:
                log.info("Pantheon chosen: %s", belief.belief_type)
                return True
            break  # Accepted but not applied, which means not enough faith. Others would fail too.
        self._pantheon_retry = turn + self.weights.retry_turns
        return False

    # ----- religion -----

    async def _religion(self, game: SecondaryGame, turn: int) -> bool:
        """Found a religion once a prophet has been activated. Returns whether we may still found.

        The game prints success whether or not founding took, so it is confirmed by reading the
        status on the next turn, not from the result.
        """
        status = await game.get_religion_founding_status()
        if status.has_religion or not status.available_religions:
            return False
        notified = any(n.type_name in _RELIGION_NOTES for n in await game.get_notifications())
        recent = self._prophet_turn is not None and turn - self._prophet_turn <= PROPHET_WINDOW
        if (notified or recent) and self._found_attempts < MAX_FOUND_ATTEMPTS:
            followers = rank_beliefs(status.beliefs_by_class.get(_FOLLOWER, []), self.weights)
            founders = rank_beliefs(status.beliefs_by_class.get(_FOUNDER, []), self.weights)
            if followers and founders:
                religions = sorted(status.available_religions)
                n = self._found_attempts
                self._found_attempts += 1
                result = await game.found_religion(
                    religions[n % len(religions)][0],
                    followers[n % len(followers)].belief_type,
                    founders[n % len(founders)].belief_type,
                )
                log.info("Founding religion (attempt %d): %s", n + 1, result[:120])
            else:
                log.warning("Religion is ready but the game offers no follower or founder belief")
        # Once attempts run out nothing will found, so prophets must not be spent. There is no
        # choose-religion blocker type (docs/blockers.md), so giving up can't leave one stuck.
        return self._found_attempts < MAX_FOUND_ATTEMPTS

    # ----- great people -----

    async def _recruit(self, game: SecondaryGame) -> None:
        """Recruit until nothing is left, reading again after each one, because points are spent."""
        refused: set[int] = set()
        for _ in range(MAX_RECRUITS):
            people = rank_recruits(
                await game.get_great_people(),
                self.weights,
                frozenset(refused),
                prophets=self._can_found,  # A prophet is only worth points if it will be used.
            )
            if not people:
                return
            person = people[0]
            result = await game.recruit_great_person(person.individual_id)
            if "RECRUITED" not in result:
                log.info("Can't recruit %s: %s", person.individual_name, result[:120])
                refused.add(person.individual_id)

    async def _activate_all(self, game: SecondaryGame, turn: int, can_found: bool) -> None:
        units = await read_units(game)
        alive = {u.unit_id for u in units}
        self._great = {k: v for k, v in self._great.items() if k in alive}
        for unit in sorted(units, key=lambda u: u.unit_id):
            kind = canonical(unit.unit_type)
            if kind not in _ACTIVATABLE or not unit.can_move:
                continue
            if kind == _PROPHET and not can_found:
                continue  # A religion exists already, or none is left to found.
            await self._activate(game, unit, turn)
        self._log_waiting(units, turn, can_found)

    def _log_waiting(self, units: Iterable[Unit], turn: int, can_found: bool) -> None:
        """Say which great people are still on the map, and why each one isn't being used.

        Run 4 piled up great people in one city and the log never mentioned them (issue 15).
        The reason is a guess from the policy's own state: a unit it never activates (generals,
        admirals), a prophet with no religion to found, one set aside, or one it is working on.
        """
        waiting = []
        for unit in sorted(units, key=lambda u: u.unit_id):
            if not unit.unit_type.startswith("UNIT_GREAT_"):
                continue
            kind = canonical(unit.unit_type)
            state = self._great.get(unit.unit_id)
            if kind not in _ACTIVATABLE:
                reason = "never activated by this policy"
            elif kind == _PROPHET and not can_found:
                reason = "no religion left to found"
            elif state is not None and state.skip_until > turn:
                reason = f"set aside until turn {state.skip_until}"
            else:
                reason = "working on it"
            waiting.append(f"{unit.name} ({kind}) at {unit.x},{unit.y}: {reason}")
        if waiting:
            log.info(
                "great_people turn %d: %d on the map: %s", turn, len(waiting), "; ".join(waiting)
            )

    async def _activate(self, game: SecondaryGame, unit: Unit, turn: int) -> None:
        state = self._great.setdefault(unit.unit_id, _GreatPersonState())
        if state.skip_until > turn:
            return
        advisor = await game.get_gp_advisor(unit.unit_index)
        if advisor is None or not advisor.cities:
            # No matching district yet, or an unreadable unit. Ask again later, not every turn.
            log.info("%s has no district to activate in, asking again later", unit.name)
            state.skip_until = turn + NO_DISTRICT_WAIT
            return
        cities = sorted(
            advisor.cities,
            key=lambda c: (not c.can_activate, c.distance, -c.city_yield, c.city_id),
        )
        target = cities[0]
        if target.distance == 0:
            result = await game.activate_great_person(unit.unit_index)
            if "GP_ACTIVATED" in result:
                if canonical(unit.unit_type) == _PROPHET:
                    self._prophet_turn = turn
                self._great.pop(unit.unit_id, None)
                return
            log.info("%s can't activate at %s: %s", unit.name, target.city_name, result[:120])
            self._no_progress(state, turn, unit.name)
            return
        for city in cities[:MAX_MOVE_TARGETS]:
            result = await game.move_unit(unit.unit_index, city.district_x, city.district_y)
            if move_succeeded(result):
                moved_to = city
                break
            log.info("%s can't move to %s: %s", unit.name, city.city_name, result[:120])
        else:
            self._no_progress(state, turn, unit.name)
            return
        # Progress is measured against the district it actually moved toward.
        goal = (moved_to.district_x, moved_to.district_y)
        if (
            state.last_target == goal
            and state.last_distance is not None
            and moved_to.distance >= state.last_distance
        ):
            self._no_progress(state, turn, unit.name)
            if state.skip_until > turn:
                return  # Set aside: the reset in `_no_progress` must stand.
        else:
            state.stuck = 0
        state.last_target, state.last_distance = goal, moved_to.distance

    @staticmethod
    def _no_progress(state: _GreatPersonState, turn: int, name: str) -> None:
        """Count a turn without progress. Past the limit, set the unit aside for a while."""
        state.stuck += 1
        if state.stuck >= GP_STUCK_TURNS:
            log.warning("%s is stuck, leaving it for %d turns", name, GP_SET_ASIDE_TURNS)
            state.skip_until = turn + GP_SET_ASIDE_TURNS
            state.stuck = 0
            state.last_distance = state.last_target = None

    # ----- trade -----

    async def _met_owners(self, game: SecondaryGame) -> frozenset[str]:
        civs = await game.get_diplomacy()
        states = await game.get_city_states()
        return frozenset(
            [c.civ_name for c in civs if c.has_met] + [s.name for s in states.city_states]
        )

    async def _trade(self, game: SecondaryGame, turn: int) -> None:
        """Send each idle trader on a route. Capacity is counted down as routes start."""
        status = await game.get_trade_routes()
        seen = {t.unit_id for t in status.traders}
        self._trade_wait = {k: v for k, v in self._trade_wait.items() if k in seen}
        idle = sorted(
            (t for t in status.traders if not t.on_route and t.has_moves), key=lambda t: t.unit_id
        )
        free = status.capacity - status.active_count
        if not idle or free <= 0:
            return
        met = await self._met_owners(game)
        used: set[tuple[int, int, int, int]] = set()
        for trader in idle:
            if free <= 0:
                return
            if self._trade_wait.get(trader.unit_id, -1) > turn:
                continue
            index = trader.unit_id % 65536
            ranked = rank_destinations(await game.get_trade_destinations(index), self.weights, met)
            ranked = [d for d in ranked if (trader.x, trader.y, d.x, d.y) not in used]
            started = False
            for dest in ranked[:MAX_DESTINATIONS]:
                result = await game.make_trade_route(index, dest.x, dest.y)
                if "TRADE_ROUTE_STARTED" in result:
                    used.add((trader.x, trader.y, dest.x, dest.y))
                    free -= 1
                    started = True
                    break
                log.info("Trade route to %s refused: %s", dest.city_name, result[:120])
                if "CAPACITY_FULL" in result:
                    return
            if not started:
                self._trade_wait[trader.unit_id] = turn + TRADE_WAIT_TURNS
