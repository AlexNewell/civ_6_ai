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
  live stats, nor `military_strength`. One exception: to name who took a lost city, the tile of
  that city (which we owned, so we know it) is looked up in `visible_cities`. No other tile is read.
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

from civ6_ai.state.reader import IncompleteRead, check_cities_complete

log = logging.getLogger(__name__)

THREAT_PERIOD = 1
RESOURCE_PERIOD = 5
VICTORY_PERIOD = 5

BARBARIAN_ID = 63
THREAT_RADIUS = 4  # Tiles from our nearest city or unit.
ABSENT_CHECKS = 3  # A hostile owner is forgotten after this many checks without it nearby.
TAKER_RADIUS = 3  # A hostile unit this near a lost city's tile is taken to have taken it.
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
    # Who has the city now, and why it left us. Older logs have neither, so both have defaults.
    # `cause` is "loyalty" (the last read had it flipping within a turn or two), "capture" (an enemy
    # holds it or stands beside it) or "unknown". A taker of None means no read shows who.
    taker_id: int | None = None
    taker_name: str = ""
    cause: str = "unknown"


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
class CityLoyaltyResponse(Event):
    """What the bot did about a city that is losing loyalty (roadmap item 1.37)."""

    kind = "city_loyalty_response"

    city_id: int
    city_name: str
    loyalty: float
    loyalty_per_turn: float
    actions: tuple[str, ...]


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


def register_event(event_type: type[Event]) -> None:
    """Let `event_from_json` read an event that is published but not by a monitor."""
    _EVENTS_BY_KIND[event_type.kind] = event_type


def event_from_json(line: str) -> Event:
    fields = json.loads(line)
    kind = fields.pop("kind")
    if kind not in _EVENTS_BY_KIND:
        # Events from other modules register themselves on import. Import them now, so a process
        # that only reads event logs can still read them.
        import civ6_ai.construction  # noqa: F401
        import civ6_ai.strategy.tasks  # noqa: F401
    return _EVENTS_BY_KIND[kind](**fields)


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
    """Writes one JSON line per event. Lines are buffered and written by `flush`, which the turn
    loop calls once a turn and the run calls when it stops (including on a crash), so the file is
    opened once a turn and not once per event. If the process dies without reaching either, up to
    one turn's events are lost."""

    path: Path
    max_buffered: int = 500  # A burst this large is written at once rather than held.

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._buffer: list[str] = []

    def write(self, event: Event) -> None:
        self._buffer.append(event.to_json() + "\n")
        if len(self._buffer) >= self.max_buffered:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        lines, self._buffer = self._buffer, []
        try:
            with self.path.open("a", encoding="utf-8") as file:
                file.writelines(lines)
        except OSError:
            self._buffer = lines + self._buffer  # Kept for the next flush.
            raise


class MonitorGame(Protocol):
    """The part of civ6-mcp's `GameState` that monitors may use. See the module docstring."""

    async def get_game_overview(self): ...

    async def get_cities(self): ...

    async def get_diplomacy(self): ...

    async def get_threat_scan(self): ...

    async def get_empire_resources(self): ...

    async def get_victory_progress(self): ...


class Monitor(Protocol):
    name: str
    period: int  # Turns between checks.

    async def check(self, game: MonitorGame, overview) -> list[Event]:
        """Return the events that start now. Reads come first, so a failure changes no state."""
        ...

    def reset(self) -> None:
        """Forget everything seen. Called when the turn goes backwards, as after loading a save."""
        ...


@dataclass(frozen=True)
class _KnownCity:
    """What the last read of one of our cities said, kept to explain it if it goes."""

    name: str
    x: int
    y: int
    loyalty: float
    loyalty_per_turn: float
    turn: int


def _distance(x1: int, y1: int, x2: int, y2: int) -> int:
    """Tiles between two tiles, roughly (offset rows, so a slight overestimate)."""
    dy = abs(y1 - y2)
    return dy + max(abs(x1 - x2) - dy // 2, 0)


class ThreatMonitor:
    """Watches wars, lost cities, falling loyalty and hostile units.

    With a `state_path`, the city set is also kept in that JSON file, so a restart of the same game
    still sees a city lost while the bot was down. A file from a later turn (another game or an
    earlier save) is ignored, as the war tracker does.
    """

    name = "threat"
    period = THREAT_PERIOD

    def __init__(self, state_path: Path | None = None) -> None:
        self.state_path = state_path
        self.reset()

    def reset(self) -> None:
        self._at_war: dict[int, bool] = {}
        self._cities: dict[int, _KnownCity] | None = None
        self._disloyal: set[int] = set()
        self._hostile_owners: dict[int, int] = {}  # Owner to checks since it was last near.

    async def check(self, game: MonitorGame, overview) -> list[Event]:
        civs = await game.get_diplomacy()
        cities, _ = await game.get_cities()
        scan = await game.get_threat_scan()
        check_cities_complete(cities, overview)

        turn = overview.turn
        if self._cities is None:
            self._cities = self._load(turn)
        events = [
            *self._wars(turn, civs),
            *self._lost_cities(turn, cities, civs, scan),
            *self._loyalty(turn, cities),
            *self._hostile_units(turn, scan),
        ]
        self._save(turn)
        return events

    def _load(self, turn: int) -> dict[int, _KnownCity] | None:
        """The city set of an earlier run of this game, or None if there is none to trust."""
        if self.state_path is None:
            return None
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
            if saved["turn"] > turn:
                log.info(
                    "threat: saved cities are from turn %s, after %s, so not used",
                    saved["turn"],
                    turn,
                )
                return None
            return {int(cid): _KnownCity(**city) for cid, city in saved["cities"].items()}
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            log.warning("threat: could not read %s, starting without it", self.state_path)
            return None

    def _save(self, turn: int) -> None:
        """Write the city set. A failure only costs the restart check."""
        if self.state_path is None or self._cities is None:
            return
        saved = {
            "turn": turn,
            "cities": {str(cid): asdict(city) for cid, city in self._cities.items()},
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.state_path.with_name(self.state_path.name + ".tmp")
            temp.write_text(json.dumps(saved, sort_keys=True), encoding="utf-8")
            temp.replace(self.state_path)
        except OSError:
            log.warning("threat: could not save %s", self.state_path, exc_info=True)

    def _wars(self, turn: int, civs: list) -> list[Event]:
        """Fire when a civ we've seen goes to war with us, including at first contact.

        Unmet civs are listed too, so they're recorded as at peace. The first read, or the first
        after `reset()`, has no record and so only sets the baseline: wars already running aren't
        new. A civ missing from a short read keeps its last state, and one first seen only when
        already met is a baseline, so neither can fake a new war.
        """
        events: list[Event] = []
        for civ in civs:
            if not civ.has_met:
                self._at_war.setdefault(civ.player_id, False)
                continue
            if civ.is_at_war and self._at_war.get(civ.player_id) is False:
                events.append(WarDeclared(turn, civ.player_id, civ.civ_name))
            self._at_war[civ.player_id] = civ.is_at_war
        return events

    def _lost_cities(self, turn: int, cities: list, civs: list, scan: list) -> list[Event]:
        current = {
            city.city_id: _KnownCity(
                city.name, city.x, city.y, city.loyalty, city.loyalty_per_turn, turn
            )
            for city in cities
        }
        previous = self._cities if self._cities is not None else current
        self._cities = current
        return [
            self._explain(turn, city_id, known, civs, scan)
            for city_id, known in previous.items()
            if city_id not in current
        ]

    def _explain(
        self, turn: int, city_id: int, known: _KnownCity, civs: list, scan: list
    ) -> CityLost:
        """A `CityLost` with its taker and cause, from reads this check already made.

        The taker is the met civ whose visible cities (`get_diplomacy`) include the city's tile,
        else the owner of a hostile unit within `TAKER_RADIUS` of it (`get_threat_scan`). The cause
        is loyalty when the last read had the loyalty reaching zero by now, a capture when the
        taker is at war with us or a hostile unit stands beside the tile, else unknown. A razed
        city leaves no city on its tile, so it falls to the unit, or to unknown.
        """
        taker: tuple[int, str] | None = None
        for civ in civs:
            if any((vc.x, vc.y) == (known.x, known.y) for vc in civ.visible_cities):
                taker = (civ.player_id, civ.civ_name)
                break
        near = sorted(
            (
                unit
                for unit in scan
                if unit.unit_type not in STEALTH_UNITS
                and _distance(unit.x, unit.y, known.x, known.y) <= TAKER_RADIUS
            ),
            key=lambda unit: _distance(unit.x, unit.y, known.x, known.y),
        )
        hostile = [unit for unit in near if self._is_hostile(unit)]
        if taker is None and hostile:
            taker = (hostile[0].owner_id, hostile[0].owner_name)
        gap = max(turn - known.turn, 1)
        if known.loyalty_per_turn < 0 and known.loyalty + known.loyalty_per_turn * gap <= 0:
            cause = "loyalty"
        elif hostile or (taker is not None and self._at_war.get(taker[0], False)):
            cause = "capture"
        else:
            cause = "unknown"
        taker_id, taker_name = taker if taker is not None else (None, "")
        return CityLost(turn, city_id, known.name, known.x, known.y, taker_id, taker_name, cause)

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
        if not progress.players and overview.num_cities == 0:
            # The game doesn't count a player with no city as alive, so the read lists nobody,
            # not even us. There is nothing to compare until the first city is founded.
            return []
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
