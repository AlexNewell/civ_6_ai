"""A build ledger and gap tracker (issue 156, part 2).

`BuildLedger` folds the construction events into what the empire has built and tried: districts and
buildings per city, which items were ever started, and in how many cities each was refused. It is
fed as events happen (subscribe it to the bus) and is rebuilt at startup from the event files of
the game's earlier sessions, within a size bound, so a restart keeps its history. Each turn it also
absorbs the city list, so what stood before the first event (or between two sessions) is known.

`find_gaps` compares the ledger with what the plan wants. Targets come from the policy document's
victory aim and `wants_district`. It reports:

- a wanted district type that exists nowhere (and whether it was ever started),
- a district that has stood a while with none of its buildings,
- an item refused in several cities and never started anywhere.

`BuildTracker` is the turn handler. It reads the city list the other handlers already read, so it
makes no game call of its own, and logs one short line a turn to the bot log.
"""

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from civ6_ai.construction import (
    BuildingCompleted,
    ConstructionEvent,
    DistrictCompleted,
    DistrictLost,
    ProductionRefused,
    ProductionSet,
    _districts,
)
from civ6_ai.monitors import CityLost, Event, EventBus, EventLog, event_from_json
from civ6_ai.policies.districts import SPECIAL_DISTRICTS, is_planned, wants_district

log = logging.getLogger(__name__)

# The restore reads at most this much of the earlier event files (newest first), so a long game
# cannot slow a start. The ledger itself holds counts and sets, never the events.
RESTORE_MAX_BYTES = 8_000_000

BASE_TARGETS = ("CAMPUS", "COMMERCIAL_HUB", "HOLY_SITE", "THEATER")
BASE_GRACE_TURN = 40  # A base district is expected by here.
SPECIAL_GRACE_TURN = 120  # A planned special district needs techs, so it is expected later.
EMPTY_AFTER_TURNS = 12  # A district this old with no building of its own is a gap.
REFUSED_CITIES = 3  # An item refused in this many cities and never started is a gap.
LINE_ITEMS = 4  # The gap line names this many gaps, then counts the rest.

# Which buildings stand in which district, for the districts that have buildings of their own.
DISTRICT_BUILDINGS = {
    "CAMPUS": ("LIBRARY", "UNIVERSITY", "MADRASA", "RESEARCH_LAB"),
    "COMMERCIAL_HUB": ("MARKET", "BANK", "STOCK_EXCHANGE"),
    "HOLY_SITE": ("SHRINE", "TEMPLE", "STAVE_CHURCH"),
    "THEATER": ("AMPHITHEATER", "MUSEUM_ART", "MUSEUM_ARTIFACT", "BROADCAST_CENTER", "FILM_STUDIO"),
    "INDUSTRIAL_ZONE": ("WORKSHOP", "FACTORY", "POWER_PLANT", "ELECTRONICS_FACTORY"),
    "ENCAMPMENT": ("BARRACKS", "STABLE", "ARMORY", "MILITARY_ACADEMY"),
    "HARBOR": ("LIGHTHOUSE", "SHIPYARD", "SEAPORT"),
    "ENTERTAINMENT_COMPLEX": ("ARENA", "ZOO"),
}


def bare(name: str) -> str:
    return name.removeprefix("DISTRICT_").removeprefix("BUILDING_")


@dataclass(frozen=True)
class Gap:
    kind: str  # "missing_district", "empty_district" or "refused_never_started"
    subject: str
    detail: str = ""

    def text(self) -> str:
        return f"{self.subject} {self.detail}".strip()


class BuildLedger:
    """What has been built and tried, folded from construction events."""

    def __init__(self) -> None:
        # city id -> {(district, x, y): turn first known}
        self.districts: dict[int, dict[tuple[str, int, int], int]] = {}
        self.buildings: dict[int, set[str]] = {}
        self.names: dict[int, str] = {}
        self.started: set[str] = set()  # Items ever set in production, as bare names.
        self.refused: dict[str, set[int]] = defaultdict(set)  # Item -> cities that refused it.
        self.events_applied = 0

    def subscribe(self, bus: EventBus) -> None:
        bus.subscribe(self.apply, ConstructionEvent, CityLost)

    def apply(self, event: Event) -> None:
        self.events_applied += 1
        if isinstance(event, CityLost):
            self.districts.pop(event.city_id, None)
            self.buildings.pop(event.city_id, None)
            return
        if not isinstance(event, ConstructionEvent):
            return
        self.names[event.city_id] = event.city_name
        if isinstance(event, DistrictCompleted):
            known = self.districts.setdefault(event.city_id, {})
            known.setdefault((bare(event.district), event.district_x, event.district_y), event.turn)
        elif isinstance(event, DistrictLost):
            self.districts.get(event.city_id, {}).pop(
                (bare(event.district), event.district_x, event.district_y), None
            )
        elif isinstance(event, BuildingCompleted):
            self.buildings.setdefault(event.city_id, set()).add(bare(event.building))
        elif isinstance(event, ProductionSet):
            self.started.add(bare(event.item))
        elif isinstance(event, ProductionRefused) and event.is_refusal:
            self.refused[bare(event.item)].add(event.city_id)

    def observe(
        self,
        turn: int,
        cities: Iterable[Any],
        finished: dict[int, set[tuple[str, int, int]]] | None = None,
    ) -> None:
        """Absorb the city list: anything standing that no event told us of is added.

        `finished` (from `ConstructionWatcher`) says which districts are done, so one that is
        being built or paused is not counted. Without it, the city's own current build is skipped.
        """
        for city in cities:
            self.names.setdefault(city.city_id, city.name)
            known = self.districts.setdefault(city.city_id, {})
            if finished is not None:
                found = finished.get(city.city_id, set())
            else:
                building = getattr(city, "currently_building", "") or ""
                found = {d for d in _districts(city.districts) if d[0] != building}
            for name, x, y in found:
                known.setdefault((bare(name), x, y), turn)
            built = self.buildings.setdefault(city.city_id, set())
            built.update(bare(b) for b in city.buildings)

    def reconcile(
        self, cities: Iterable[Any], finished: dict[int, set[tuple[str, int, int]]] | None = None
    ) -> None:
        """Drop restored holdings the current city list does not show. Run once on the first
        complete read, because a loss between sessions left no event to say so."""
        cities = list(cities)
        present = {c.city_id for c in cities}
        for city_id in [c for c in self.districts if c not in present]:
            self.districts.pop(city_id, None)
            self.buildings.pop(city_id, None)
        for city in cities:
            if finished is not None:
                standing = {(bare(n), x, y) for n, x, y in finished.get(city.city_id, set())}
            else:
                standing = {(bare(n), x, y) for n, x, y in _districts(city.districts)}
            known = self.districts.get(city.city_id, {})
            for key in [k for k in known if k not in standing]:
                del known[key]
            self.buildings[city.city_id] = {b for b in self.buildings.get(city.city_id, set())} & {
                bare(b) for b in city.buildings
            }

    def district_types(self) -> set[str]:
        return {d[0] for found in self.districts.values() for d in found}

    def restore(self, paths: Iterable[Path], max_bytes: int = RESTORE_MAX_BYTES) -> int:
        """Replay earlier event files (oldest first), reading the newest `max_bytes` of them.

        Returns how many events were applied. A line that does not parse is skipped.
        """
        chosen: list[tuple[Path, int]] = []
        budget = max_bytes
        for path in reversed(list(paths)):
            if budget <= 0:
                break
            try:
                size = path.stat().st_size
            except OSError:
                continue
            chosen.append((path, min(size, budget)))
            budget -= size
        applied = 0
        for path, take in reversed(chosen):
            try:
                with path.open("rb") as file:
                    size = file.seek(0, 2)
                    start = max(0, size - take)
                    file.seek(start)
                    data = file.read().decode("utf-8", errors="replace")
            except OSError:
                continue
            lines = data.splitlines()
            if start > 0:
                lines = lines[1:]  # The first line is cut.
            for line in lines:
                if not line.strip():
                    continue
                try:
                    self.apply(event_from_json(line))
                    applied += 1
                except Exception:  # Old or damaged lines must not stop a start.
                    continue
        return applied


def earlier_event_files(
    runs: Path, save: str, game_id: str, current_stem: str, game_id_of: Callable[[Path], str | None]
) -> list[Path]:
    """The event files of this game's earlier sessions, oldest first (names carry a UTC stamp)."""
    found = []
    for path in sorted(runs.glob(f"{save}_*_events.jsonl")):
        stem = path.name.removesuffix("_events.jsonl")
        if stem == current_stem:
            continue
        if game_id_of(runs / f"{stem}.jsonl") == game_id:
            found.append(path)
    return found


def wanted_districts(victory: str, turn: int, cities: Iterable[Any], have: set[str]) -> list[str]:
    """District types the plan wants somewhere, once their grace turn has passed."""
    cities = list(cities)
    wanted: list[str] = []
    if turn >= BASE_GRACE_TURN:
        wanted += BASE_TARGETS
    if turn >= SPECIAL_GRACE_TURN:
        for name in sorted(SPECIAL_DISTRICTS):
            if is_planned(name, victory) and any(
                wants_district(
                    name,
                    victory=victory,
                    turn=turn,
                    pop=c.population,
                    housing=c.housing,
                    empire_districts=have,
                )
                for c in cities
            ):
                wanted.append(name)
    return wanted


def find_gaps(ledger: BuildLedger, turn: int, victory: str, cities: Iterable[Any]) -> list[Gap]:
    cities = list(cities)
    have = ledger.district_types()
    gaps = []
    for name in wanted_districts(victory, turn, cities, have):
        if name not in have:
            tried = "started" if name in ledger.started else "never started"
            gaps.append(Gap("missing_district", f"no {name}", f"({tried})"))
    for city_id, found in sorted(ledger.districts.items()):
        built = ledger.buildings.get(city_id, set())
        for (name, _, _), since in sorted(found.items()):
            own = DISTRICT_BUILDINGS.get(name)
            if own and turn - since >= EMPTY_AFTER_TURNS and not built.intersection(own):
                city = ledger.names.get(city_id, str(city_id))
                gaps.append(Gap("empty_district", f"empty {name}", f"in {city} ({turn - since}t)"))
    for item, refusers in sorted(ledger.refused.items()):
        if len(refusers) >= REFUSED_CITIES and item not in ledger.started:
            gaps.append(
                Gap("refused_never_started", f"{item} refused", f"in {len(refusers)} cities")
            )
    return gaps


def gap_line(gaps: list[Gap]) -> str:
    if not gaps:
        return "build gaps: none"
    shown = "; ".join(g.text() for g in gaps[:LINE_ITEMS])
    more = f" (+{len(gaps) - LINE_ITEMS} more)" if len(gaps) > LINE_ITEMS else ""
    return f"build gaps: {shown}{more}"


class BuildTracker:
    """Turn handler: absorb the city list, find gaps, and log one line."""

    name = "build_tracker"

    def __init__(
        self,
        ledger: BuildLedger,
        victory: Callable[[], str] = lambda: "any",
        watcher: Any = None,
    ) -> None:
        self.ledger = ledger
        self._watcher = watcher
        self._reconciled = False
        self._victory = victory
        self.gaps: list[Gap] = []

    async def run(self, game: Any) -> None:
        overview = await game.get_game_overview()
        cities, _ = await game.get_cities()
        if isinstance(getattr(overview, "num_cities", None), int) and len(cities) != (
            overview.num_cities
        ):
            return  # A short read would show districts as missing.
        finished = self._watcher.finished_districts() if self._watcher is not None else None
        if not self._reconciled:
            self.ledger.reconcile(cities, finished)
            self._reconciled = True
        self.ledger.observe(overview.turn, cities, finished)
        self.gaps = find_gaps(self.ledger, overview.turn, self._victory(), cities)
        log.info("%s", gap_line(self.gaps))


class EventFlush:
    """Last handler of a turn: writes the turn's buffered events to the events file."""

    name = "event_flush"

    def __init__(self, event_log: EventLog) -> None:
        self._log = event_log

    async def run(self, game: Any) -> None:
        self._log.flush()
