"""Strategic checks that run on a fixed schedule and publish typed events.

`Monitors` is one turn-loop handler. Each monitor has a period, and a check that fails is retried
next turn, so a check can't be forgotten. Events fire when a condition starts, not on every check
while it holds, because the strategist will spend model calls on them.

Events carry only what the player can legally see. civ6-mcp's reads don't enforce that, so the
limits are set here, field by field. Each read is used only for:

- `get_game_overview`: our own turn, gold and city count.
- `get_cities`: our own cities. Not `attack_targets`, which lists hidden units.
- `get_empire_resources`: our own stockpiles.
- `get_diplomacy`: met civs' war state with us, and the count of living majors, which the game
  shows as unmet entries. Not `num_cities` or `visible_cities`, which cover hidden cities and give
  live stats, nor `military_strength`.
- `get_threat_scan`: units on visible tiles, minus `STEALTH_UNITS`.

civ6-mcp reads that time out return the lines received so far, with no error and no marker, so a
short answer can look like an empty one. Monitors cross-check what they can (city counts) and
otherwise wait for a change to repeat before acting on a disappearance.
- `get_victory_progress`: met civs' World Rankings figures, meaning science and diplomatic points,
  majority religion and original capitals. Not `demographics`, which covers unmet civs, nor
  `spaceports` or yields.

`get_rival_snapshot` is never used, because it reads rivals' stockpiles, gold and faith directly.
"""

import json
import logging
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Protocol

log = logging.getLogger(__name__)

THREAT_PERIOD = 1
RESOURCE_PERIOD = 5
VICTORY_PERIOD = 5

BARBARIAN_ID = 63
THREAT_RADIUS = 4  # Tiles from our nearest city or unit.
ABSENT_CHECKS = 3  # A hostile owner is forgotten after this many checks without it nearby.
THREAT_UNITS = 3  # Fewer than this is a job for the military policy, not the strategist.
# GameCore's scan checks the tile, not the unit, so it also reports units the player can't see.
# These are the units tagged CLASS_STEALTH in the game's Units data, base game and Gathering Storm.
STEALTH_UNITS = frozenset(
    {
        "UNIT_ENGLISH_SEADOG",
        "UNIT_GERMAN_UBOAT",
        "UNIT_NUCLEAR_SUBMARINE",
        "UNIT_OTTOMAN_BARBARY_CORSAIR",
        "UNIT_PRIVATEER",
        "UNIT_SUBMARINE",
    }
)
SHORTAGE_TURNS = 10  # Warn when a falling stockpile or treasury would run out this soon.
LOYALTY_TURNS = 10  # Warn when a falling city would flip this soon.
DIPLOMATIC_POINTS_TO_WIN = 20
# Each share of a win warns once. On diplomatic points these are 10, 15 and 18.
VICTORY_WARNING_SHARES = (0.5, 0.75, 0.9)


class VictoryPath(StrEnum):
    """A way for a rival to win, valued by the game's own victory type."""

    SCIENCE = "VICTORY_TECHNOLOGY"
    DIPLOMATIC = "VICTORY_DIPLOMATIC"
    RELIGIOUS = "VICTORY_RELIGIOUS"
    CONQUEST = "VICTORY_CONQUEST"


@dataclass(frozen=True)
class Event:
    kind: ClassVar[str]

    turn: int

    def to_json(self) -> str:
        return json.dumps({"kind": self.kind, **asdict(self)})


@dataclass(frozen=True)
class WarDeclared(Event):
    kind = "war_declared"

    rival_id: int
    rival_name: str


@dataclass(frozen=True)
class CityLost(Event):
    kind = "city_lost"

    city_id: int
    city_name: str
    x: int
    y: int


@dataclass(frozen=True)
class HostileUnitsNear(Event):
    kind = "hostile_units_near"

    owner_id: int
    owner_name: str
    units: int
    nearest_distance: int


@dataclass(frozen=True)
class CityLoyaltyWarning(Event):
    kind = "city_loyalty_warning"

    city_id: int
    city_name: str
    loyalty: float
    loyalty_per_turn: float


@dataclass(frozen=True)
class RivalVictoryWarning(Event):
    """A rival has passed a share of what a win needs.

    `subject` is the rival's name, a religion type on the religious path, or "original capitals"
    on the conquest path. `rival_id` is None when the reads don't say who is responsible, as with
    who took a capital.
    """

    kind = "rival_victory_warning"

    path: VictoryPath
    rival_id: int | None
    subject: str
    progress: int
    needed: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", VictoryPath(self.path))


@dataclass(frozen=True)
class StrategicResourceShortage(Event):
    kind = "strategic_resource_shortage"

    resource: str
    amount: int
    net_per_turn: int


@dataclass(frozen=True)
class GoldShortage(Event):
    kind = "gold_shortage"

    gold: float
    gold_per_turn: float


EVENT_TYPES: tuple[type[Event], ...] = (
    WarDeclared,
    CityLost,
    HostileUnitsNear,
    CityLoyaltyWarning,
    RivalVictoryWarning,
    StrategicResourceShortage,
    GoldShortage,
)
_EVENTS_BY_KIND = {event_type.kind: event_type for event_type in EVENT_TYPES}


def event_from_json(line: str) -> Event:
    fields = json.loads(line)
    return _EVENTS_BY_KIND[fields.pop("kind")](**fields)


class EventBus:
    """Delivers each event to its subscribers as it's published, on the turn loop's thread."""

    def __init__(self) -> None:
        self._subscribers: list[tuple[Callable[[Event], None], tuple[type[Event], ...]]] = []

    def subscribe(self, subscriber: Callable[[Event], None], *event_types: type[Event]) -> None:
        """Call `subscriber` with events of these types, or with all events if none are given."""
        self._subscribers.append((subscriber, event_types or (Event,)))

    def publish(self, event: Event) -> None:
        for subscriber, event_types in self._subscribers:
            if not isinstance(event, event_types):
                continue
            try:
                subscriber(event)
            except Exception:  # A bad subscriber must not cost the others this event.
                log.exception("Subscriber failed on %s", event.kind)


@dataclass
class EventLog:
    """Writes one JSON line per event, flushed as it goes so a crash loses nothing."""

    path: Path

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: Event) -> None:
        with self.path.open("a", encoding="utf-8") as file:
            file.write(event.to_json() + "\n")


class MonitorGame(Protocol):
    """The part of civ6-mcp's `GameState` that monitors may use. See the module docstring."""

    async def get_game_overview(self): ...

    async def get_cities(self): ...

    async def get_diplomacy(self): ...

    async def get_threat_scan(self): ...

    async def get_empire_resources(self): ...

    async def get_victory_progress(self): ...


class IncompleteRead(Exception):
    """civ6-mcp returns what it has when a read times out, so a short answer can't be trusted."""


class Monitor(Protocol):
    name: str
    period: int  # Turns between checks.

    async def check(self, game: MonitorGame, overview) -> list[Event]:
        """Return the events that start now. Reads come first, so a failure changes no state."""
        ...

    def reset(self) -> None:
        """Forget everything seen. Called when the turn goes backwards, as after loading a save."""
        ...


class ThreatMonitor:
    name = "threat"
    period = THREAT_PERIOD

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._at_war: dict[int, bool] = {}
        self._cities: dict[int, tuple[str, int, int]] | None = None
        self._disloyal: set[int] = set()
        self._hostile_owners: dict[int, int] = {}  # Owner to checks since it was last near.

    async def check(self, game: MonitorGame, overview) -> list[Event]:
        civs = await game.get_diplomacy()
        cities, _ = await game.get_cities()
        scan = await game.get_threat_scan()
        if len(cities) != overview.num_cities:
            raise IncompleteRead(f"{len(cities)} of {overview.num_cities} cities read")

        turn = overview.turn
        return [
            *self._wars(turn, civs),
            *self._lost_cities(turn, cities),
            *self._loyalty(turn, cities),
            *self._hostile_units(turn, scan),
        ]

    def _wars(self, turn: int, civs: list) -> list[Event]:
        """A civ missing from a short read keeps its last state, so it can't fake a new war."""
        events: list[Event] = []
        for civ in civs:
            if not civ.has_met:
                continue
            if civ.is_at_war and self._at_war.get(civ.player_id) is False:
                events.append(WarDeclared(turn, civ.player_id, civ.civ_name))
            self._at_war[civ.player_id] = civ.is_at_war
        return events

    def _lost_cities(self, turn: int, cities: list) -> list[Event]:
        current = {city.city_id: (city.name, city.x, city.y) for city in cities}
        previous = self._cities if self._cities is not None else current
        self._cities = current
        return [
            CityLost(turn, city_id, name, x, y)
            for city_id, (name, x, y) in previous.items()
            if city_id not in current
        ]

    def _loyalty(self, turn: int, cities: list) -> list[Event]:
        falling = [
            city
            for city in cities
            if city.loyalty_per_turn < 0
            and city.loyalty + city.loyalty_per_turn * LOYALTY_TURNS <= 0
        ]
        events: list[Event] = [
            CityLoyaltyWarning(turn, city.city_id, city.name, city.loyalty, city.loyalty_per_turn)
            for city in falling
            if city.city_id not in self._disloyal
        ]
        self._disloyal = {city.city_id for city in falling}
        return events

    def _hostile_units(self, turn: int, scan: list) -> list[Event]:
        """Warn once per owner, until it has been gone for `ABSENT_CHECKS` checks.

        A timed-out scan returns fewer lines without saying so, so one empty scan isn't a retreat.
        """
        near = defaultdict(list)
        for unit in scan:
            if (
                unit.distance <= THREAT_RADIUS
                and unit.unit_type not in STEALTH_UNITS
                and self._is_hostile(unit)
            ):
                near[unit.owner_id].append(unit)

        events: list[Event] = []
        for owner_id, units in near.items():
            if len(units) < THREAT_UNITS:
                continue
            is_new = owner_id not in self._hostile_owners
            self._hostile_owners[owner_id] = 0
            if is_new:
                nearest = min(unit.distance for unit in units)
                events.append(
                    HostileUnitsNear(turn, owner_id, units[0].owner_name, len(units), nearest)
                )
        for owner_id in list(self._hostile_owners):
            if owner_id in near and len(near[owner_id]) >= THREAT_UNITS:
                continue
            self._hostile_owners[owner_id] += 1
            if self._hostile_owners[owner_id] >= ABSENT_CHECKS:
                del self._hostile_owners[owner_id]
        return events

    def _is_hostile(self, unit) -> bool:
        """The scan only lists city-states at war with us. An unmet civ is never at war here."""
        return (
            unit.owner_id == BARBARIAN_ID
            or unit.is_city_state
            or self._at_war.get(unit.owner_id, False)
        )


class ResourceMonitor:
    name = "resource"
    period = RESOURCE_PERIOD

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._short: set[str] = set()
        self._gold_short = False

    async def check(self, game: MonitorGame, overview) -> list[Event]:
        stockpiles, _, _, _ = await game.get_empire_resources()

        events: list[Event] = []
        for stockpile in stockpiles:
            net = stockpile.per_turn - stockpile.demand + stockpile.imported
            short = net < 0 and stockpile.amount + net * SHORTAGE_TURNS < 0
            if short and stockpile.name not in self._short:
                events.append(
                    StrategicResourceShortage(overview.turn, stockpile.name, stockpile.amount, net)
                )
            if short:
                self._short.add(stockpile.name)
            else:
                self._short.discard(stockpile.name)

        gold, per_turn = overview.gold, overview.gold_per_turn
        gold_short = per_turn < 0 and gold + per_turn * SHORTAGE_TURNS < 0
        if gold_short and not self._gold_short:
            events.append(GoldShortage(overview.turn, gold, per_turn))
        self._gold_short = gold_short
        return events


class VictoryMonitor:
    """Warns as rivals pass shares of a win.

    There is no culture warning: the reads give our tourists from each rival, but not a rival's
    tourists from anyone else, so its culture progress can't be worked out.
    """

    name = "victory"
    period = VICTORY_PERIOD

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._shares_passed: dict[tuple[VictoryPath, str], int] = {}
        self._most_majors = 0
        self._majors = 0  # Living majors, once two reads in a row agree.
        self._majors_seen = 0

    async def check(self, game: MonitorGame, overview) -> list[Event]:
        progress = await game.get_victory_progress()
        civs = await game.get_diplomacy()
        if not civs or not progress.players:
            raise IncompleteRead("no civs in victory or diplomacy read")

        living_majors = self._confirmed_majors(len(civs) + 1)  # Unmet civs are counted, no more.
        self._most_majors = max(self._most_majors, living_majors)

        enabled = progress.enabled_victories  # Empty means all of them.
        events: list[Event] = []
        for path, rival_id, subject, value, needed in [
            *self._points(progress, overview.player_id),
            *self._religions(progress, overview.civ_name, living_majors),
            *self._capitals(progress, living_majors),
        ]:
            if enabled and path not in enabled:
                continue
            shares = sum(value >= share * needed for share in VICTORY_WARNING_SHARES)
            if shares > self._shares_passed.get((path, subject), 0):
                events.append(
                    RivalVictoryWarning(overview.turn, path, rival_id, subject, value, needed)
                )
            self._shares_passed[(path, subject)] = shares
        return events

    def _confirmed_majors(self, seen: int) -> int:
        """A short diplomacy read would look like eliminated civs, so a drop must repeat.

        A rise is believed at once: a short read can't add civs.
        """
        if seen >= self._majors or seen == self._majors_seen:
            self._majors = seen
        self._majors_seen = seen
        return self._majors

    @staticmethod
    def _points(progress, our_id: int) -> list[tuple]:
        rows = []
        for rival in progress.players:
            if rival.player_id == our_id:
                continue
            if rival.science_vp_needed > 0:
                rows.append(
                    (
                        VictoryPath.SCIENCE,
                        rival.player_id,
                        rival.name,
                        rival.science_vp,
                        rival.science_vp_needed,
                    )
                )
            rows.append(
                (
                    VictoryPath.DIPLOMATIC,
                    rival.player_id,
                    rival.name,
                    rival.diplomatic_vp,
                    DIPLOMATIC_POINTS_TO_WIN,
                )
            )
        return rows

    @staticmethod
    def _religions(progress, our_name: str, living_majors: int) -> list[tuple]:
        """A win needs a religion to be the majority in every civ. Only met civs can be counted.

        Majorities come as types and founders as display names. A pair that doesn't match, such as
        a renamed religion, is reported with no rival, which at worst warns about our own.
        """
        majorities = Counter(
            religion for religion in progress.religion_majority.values() if religion != "none"
        )
        ids = {player.name: player.player_id for player in progress.players}
        rows = []
        for religion, count in majorities.items():
            founder = next(
                (
                    civ
                    for civ, name in progress.religion_founded_names.items()
                    if _is_same_religion(religion, name)
                ),
                None,
            )
            if founder != our_name:
                rows.append(
                    (VictoryPath.RELIGIOUS, ids.get(founder), religion, count, living_majors)
                )
        return rows

    def _capitals(self, progress, living_majors: int) -> list[tuple]:
        """A conqueror needs every other original capital. Eliminated civs have lost theirs."""
        eliminated = self._most_majors - living_majors
        lost = sum(not held for held in progress.capitals_held.values())
        needed = self._most_majors - 1
        return [(VictoryPath.CONQUEST, None, "original capitals", lost + eliminated, needed)]


def _is_same_religion(religion_type: str, display_name: str) -> bool:
    name = religion_type.removeprefix("RELIGION_").replace("_", " ")
    return name.casefold() == display_name.casefold()


def default_monitors() -> list[Monitor]:
    return [ThreatMonitor(), ResourceMonitor(), VictoryMonitor()]


class MonitorError(Exception):
    """One or more monitors failed. The turn loop records this. They run again next turn."""


class Monitors:
    """Runs each monitor that's due.

    A monitor is due once `period` turns have passed since it last succeeded, not on multiples of
    its period, so a skipped turn or failed read delays it by a turn rather than a whole period.
    """

    name = "monitors"

    def __init__(self, bus: EventBus, monitors: list[Monitor] | None = None) -> None:
        self._bus = bus
        self._monitors = default_monitors() if monitors is None else monitors
        self._last_success: dict[str, int] = {}
        self._last_turn: int | None = None

    async def run(self, game: MonitorGame) -> None:
        overview = await game.get_game_overview()
        if self._last_turn is not None and overview.turn < self._last_turn:
            # The turn went backwards, so a save was loaded. Old baselines describe another game.
            self._last_success.clear()
            for monitor in self._monitors:
                monitor.reset()
        self._last_turn = overview.turn
        failures: list[str] = []
        for monitor in self._monitors:
            if not self._is_due(monitor, overview.turn):
                continue
            try:
                events = await monitor.check(game, overview)
            except ConnectionError:
                raise  # Same rule as the turn loop: the game is gone.
            except Exception as error:
                log.exception("Monitor %s failed", monitor.name)
                failures.append(f"{monitor.name}: {error!r}")
                continue
            self._last_success[monitor.name] = overview.turn
            for event in events:
                self._bus.publish(event)
        if failures:
            raise MonitorError("; ".join(failures))

    def _is_due(self, monitor: Monitor, turn: int) -> bool:
        last = self._last_success.get(monitor.name)
        return last is None or turn - last >= monitor.period
