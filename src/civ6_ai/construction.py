"""Structured construction events (issue 156), on the same bus as the monitors' events.

The production policy publishes `ProductionSet` and `ProductionRefused` as it chooses. The
`ConstructionWatcher` turn handler publishes the rest by comparing each city's district and building
lists from one turn to the next, using the `get_cities` read the monitors already make, so it adds
no game calls. Every event carries the turn, city id, name, city coordinates and a UTC timestamp.

`CityLost` (from the threat monitor) already records a lost city, so it is not repeated here.
`CityGained` marks a city seen for the first time, or one that came back (`regained`), as when a
revolt flips a city back.

The first read only sets the baseline. A turn that goes backwards (a loaded save) resets it, and a
short read (fewer cities than the overview counts) is skipped, so neither can fake a completion.

A district can show in the city's list while it is still being built. So a new district is held
back while the city is still building that district type, and reported once it is not.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from civ6_ai.monitors import Event, EventBus, register_event

log = logging.getLogger(__name__)

_TILE_ITEM = re.compile(r"(?P<name>\w+)@(?P<x>-?\d+),(?P<y>-?\d+)")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ConstructionEvent(Event):
    """A fact about one city. `at` is the wall-clock time it was recorded, in UTC."""

    city_id: int
    city_name: str
    x: int
    y: int
    at: str = field(default_factory=utc_now, kw_only=True)


@dataclass(frozen=True, kw_only=True)
class ProductionSet(ConstructionEvent):
    kind = "production_set"

    item: str
    category: str
    tile_x: int | None = None  # The tile chosen, for a district or wonder.
    tile_y: int | None = None
    placements_offered: int | None = None  # How many tiles the advisor offered.


@dataclass(frozen=True, kw_only=True)
class ProductionRefused(ConstructionEvent):
    kind = "production_refused"

    item: str
    category: str
    reply: str
    is_refusal: bool  # The game said no, as opposed to a read or call that failed.
    placements_offered: int | None = None
    backoff_turns: int = 0  # How long the item is now skipped for this city.


@dataclass(frozen=True, kw_only=True)
class DistrictCompleted(ConstructionEvent):
    kind = "district_completed"

    district: str
    district_x: int
    district_y: int


@dataclass(frozen=True, kw_only=True)
class DistrictLost(ConstructionEvent):
    kind = "district_lost"

    district: str
    district_x: int
    district_y: int


@dataclass(frozen=True, kw_only=True)
class BuildingCompleted(ConstructionEvent):
    kind = "building_completed"

    building: str


@dataclass(frozen=True, kw_only=True)
class CityGained(ConstructionEvent):
    kind = "city_gained"

    regained: bool = False  # We held this city before.


CONSTRUCTION_EVENTS: tuple[type[ConstructionEvent], ...] = (
    ProductionSet,
    ProductionRefused,
    DistrictCompleted,
    DistrictLost,
    BuildingCompleted,
    CityGained,
)
for _event_type in CONSTRUCTION_EVENTS:
    register_event(_event_type)


def _districts(entries: list[str]) -> set[tuple[str, int, int]]:
    found = (_TILE_ITEM.fullmatch(entry) for entry in entries)
    return {(m["name"], int(m["x"]), int(m["y"])) for m in found if m}


def _bare(name: str, prefix: str) -> str:
    return name.removeprefix(prefix)


def _unfinished(city: Any, before: "_Seen | None") -> set[tuple[str, int, int]]:
    """Districts in the list that are new and of the type the city is still building."""
    building = city.currently_building or ""
    if not building.startswith("DISTRICT_"):
        return set()
    known = before.districts if before is not None else set()
    return {d for d in _districts(city.districts) - known if d[0] == building}


@dataclass
class _Seen:
    districts: set[tuple[str, int, int]]
    buildings: set[str]


class ConstructionWatcher:
    """Turn handler that publishes completions, losses and gained cities from city-list changes."""

    name = "construction_watch"

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self.reset()

    def reset(self) -> None:
        self._seen: dict[int, _Seen] | None = None
        self._ever: set[int] = set()
        self._last_turn: int | None = None

    async def run(self, game: Any) -> None:
        overview = await game.get_game_overview()
        cities, _ = await game.get_cities()
        if isinstance(getattr(overview, "num_cities", None), int) and len(cities) != (
            overview.num_cities
        ):
            log.info("Construction watch skipped a short city read")
            return
        if self._last_turn is not None and overview.turn < self._last_turn:
            self.reset()
        self._last_turn = overview.turn
        for event in self.update(overview.turn, cities):
            self._bus.publish(event)

    def update(self, turn: int, cities: list) -> list[Event]:
        """Compare these `CityInfo`s with the last read and return what changed."""
        current: dict[int, _Seen] = {}
        events: list[Event] = []
        previous = self._seen
        for city in cities:
            here = (turn, city.city_id, city.name, city.x, city.y)
            before = previous.get(city.city_id) if previous is not None else None
            districts = _districts(city.districts) - _unfinished(city, before)
            buildings = {_bare(b, "BUILDING_") for b in city.buildings}
            current[city.city_id] = _Seen(districts, buildings)
            if previous is None:
                continue
            if before is None:
                events.append(CityGained(*here, regained=city.city_id in self._ever))
                continue
            for name, dx, dy in sorted(districts - before.districts):
                events.append(DistrictCompleted(*here, district=name, district_x=dx, district_y=dy))
            for name, dx, dy in sorted(before.districts - districts):
                events.append(DistrictLost(*here, district=name, district_x=dx, district_y=dy))
            for name in sorted(buildings - before.buildings):
                events.append(BuildingCompleted(*here, building=name))
        self._ever.update(current)
        self._seen = current
        return events
