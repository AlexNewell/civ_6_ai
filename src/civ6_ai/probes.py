"""Probe screenshots: a few pictures taken when an outcome under test happens.

A run that tests an outcome (say, why districts are refused a tile) turns on a probe kind with
`run_game.py --probe placement`. When a matching event is published on the bus, the next turn
handler moves the camera to the event's tile and saves a screenshot with a JSON sidecar beside it,
so the analysis can pair the image with the data. Probes are off unless asked for.

Budget, so a probe never costs a stop its screenshot or slows the run:

- `PROBE_CAP` probe shots per game, counted from files on disk, inside the game's overall cap.
- One probe shot a turn, and one per (city, item, reason class).
- A probe never uses the slots reserved for stops, and never marks its turn as shot, so a stop
  later on the same turn still gets its screenshot. A turn that matched a `blocked` event gets no
  other probe.
- Any failure logs a warning and ends the probe. It never fails the handler or the turn.

The camera move is civ6-mcp's own: `UI.LookAtPlot` in the InGame context, then a dwell of the same
length it uses (`CAMERA_DWELL`) before the capture. The same Lua call reads the game's own valid
targets for the item (`CityManager.GetOperationTargets`, as civ6-mcp's advisors do) for the sidecar.

The game's placement view (the valid tiles highlighted) is available only with `overlay=True`
(`--probe-overlay`), which is off by default. Its parameter shape follows the game's own production
panel Lua and has not been checked live. It always restores `InterfaceModeTypes.SELECTION`
afterwards, and checks that it did, since a stuck mode could block the end of turn.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from civ6_ai.monitors import Event, EventBus
from civ6_ai.screenshots import Screenshotter

log = logging.getLogger(__name__)

PROBE_CAP = 12  # Per game, inside the overall cap.
CAMERA_DWELL = 1.0  # Seconds, as civ6-mcp's spectator camera waits at each location.
SLOW_PROBE_SECONDS = 2.0  # Dwell plus capture should come in under this.
MAX_TARGETS = 40
SENTINEL = "---END---"
PROBE_PREFIX = "probe-"

# Runs Lua in the game's InGame context and returns its printed lines (`execute_write`).
LuaRunner = Callable[[str], Awaitable[list[str]]]

_ITEM = re.compile(r"[A-Z0-9_]+")


@dataclass(frozen=True)
class ProbeRequest:
    kind: str
    turn: int
    city_id: int
    city: str
    x: int  # Where to look: the event's tile, else the city's.
    y: int
    item: str = ""
    category: str = ""
    reply: str = ""
    reason_class: str = ""
    tile: tuple[int, int] | None = None
    wants_targets: bool = False  # A district or wonder: read its valid tiles.


def reason_class_of(event: Event) -> str:
    """The reason class the placement redesign puts on `production_refused`, or one read from the
    reply when the event has none yet."""
    declared = getattr(event, "reason_class", None)
    if declared:
        return str(declared)
    reply = str(getattr(event, "reply", ""))
    if "NO_PLACEMENT" in reply:
        return "placement"
    if "CANNOT_START" in reply:
        return "start"
    return "other"


def _tile_of(event: Event) -> tuple[int, int] | None:
    x, y = getattr(event, "tile_x", None), getattr(event, "tile_y", None)
    return (x, y) if x is not None and y is not None else None


def _request(kind: str, event: Event, **extra: Any) -> ProbeRequest:
    tile = _tile_of(event)
    city_x, city_y = getattr(event, "x", 0), getattr(event, "y", 0)
    return ProbeRequest(
        kind=kind,
        turn=event.turn,
        city_id=getattr(event, "city_id", -1),
        city=str(getattr(event, "city_name", "")),
        x=tile[0] if tile else city_x,
        y=tile[1] if tile else city_y,
        tile=tile,
        **extra,
    )


def _placement(event: Event) -> ProbeRequest | None:
    if event.kind == "placement_plan":
        # The placement redesign's plan record. Only the failed buy is a probe.
        if getattr(event, "action", "") != "buy_failed_place":
            return None
        item = str(getattr(event, "item", ""))
        return _request(
            "placement",
            event,
            item=item,
            category=str(getattr(event, "category", "")),
            reply=str(getattr(event, "reply", "")),
            reason_class="buy_failed_place",
            wants_targets=bool(_ITEM.fullmatch(item)),
        )
    if event.kind != "production_refused":
        return None
    reason = reason_class_of(event)
    reply = str(getattr(event, "reply", ""))
    category = str(getattr(event, "category", ""))
    if reason not in ("placement", "start") and not (
        "NO_PLACEMENT" in reply or "CANNOT_START" in reply
    ):
        return None
    item = str(getattr(event, "item", ""))
    return _request(
        "placement",
        event,
        item=item,
        category=category,
        reply=reply,
        reason_class=reason,
        # A district, or a building that needed a tile (a wonder), has tiles to read.
        wants_targets=category in ("DISTRICT", "BUILDING") and bool(_ITEM.fullmatch(item)),
    )


def _city_lost(event: Event) -> ProbeRequest | None:
    if event.kind != "city_lost":
        return None
    return ProbeRequest(
        kind="city_lost",
        turn=event.turn,
        city_id=getattr(event, "city_id", -1),
        city=str(getattr(event, "city_name", "")),
        x=getattr(event, "x", 0),
        y=getattr(event, "y", 0),
        reason_class="city_lost",
    )


def _blocked(event: Event) -> ProbeRequest | None:
    # No event carries a blocked turn yet (a stop gets its own screenshot). Anything that
    # publishes a "blocked" event with a city and tile is picked up here.
    if event.kind != "blocked":
        return None
    reply = str(getattr(event, "reply", ""))
    return _request("blocked", event, reason_class="blocked", reply=reply)


# To add a kind: write a matcher from an event to a request (or None) and register it here.
Matcher = Callable[[Event], ProbeRequest | None]
PROBE_KINDS: dict[str, Matcher] = {
    "placement": _placement,
    "city_lost": _city_lost,
    "blocked": _blocked,
}


def parse_kinds(text: str) -> list[str]:
    """`placement,city_lost` to a list of kinds. Raises ValueError on an unknown one."""
    kinds = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [kind for kind in kinds if kind not in PROBE_KINDS]
    if unknown:
        raise ValueError(f"Unknown probe kind {unknown}; choose from {sorted(PROBE_KINDS)}")
    return kinds


def _look_lua(request: ProbeRequest) -> str:
    """Move the camera and, for a district or wonder, print the game's own valid tiles."""
    lua = (
        f"local p=Map.GetPlot({request.x},{request.y}) "
        "if p then pcall(function() UI.LookAtPlot(p) end) end "
    )
    if request.wants_targets and _ITEM.fullmatch(request.item):
        lua += (
            "pcall(function() "
            f"local c=Players[Game.GetLocalPlayer()]:GetCities():FindID({request.city_id}) "
            "if not c then return end "
            f"local d=GameInfo.Districts['{request.item}'] "
            "local params "
            "if d then params={[CityOperationTypes.PARAM_DISTRICT_TYPE]=d.Hash} else "
            f"local b=GameInfo.Buildings['{request.item}'] "
            "if b then params={[CityOperationTypes.PARAM_BUILDING_TYPE]=b.Hash} end end "
            "if not params then return end "
            "local t=CityManager.GetOperationTargets(c,CityOperationTypes.BUILD,params) "
            "local out={} "
            "if t then for _,v in pairs(t) do if type(v)=='table' then "
            "for _,i in ipairs(v) do local q=Map.GetPlotByIndex(i) "
            "if q then table.insert(out,q:GetX()..','..q:GetY()) end end end end end "
            f"print('TARGETS|'..table.concat(out,';',1,math.min(#out,{MAX_TARGETS}))..'|'..#out) "
            "end) "
        )
    return lua + f"print('{SENTINEL}')"


def _overlay_lua(request: ProbeRequest) -> str:
    """Open the game's placement view for the item, so its valid tiles are highlighted."""
    return (
        "pcall(function() "
        f"local c=Players[Game.GetLocalPlayer()]:GetCities():FindID({request.city_id}) "
        "if not c then return end "
        "UI.SelectCity(c) "
        f"local d=GameInfo.Districts['{request.item}'] "
        "if d then UI.SetInterfaceMode(InterfaceModeTypes.DISTRICT_PLACEMENT,"
        "{[CityOperationTypes.PARAM_DISTRICT_TYPE]=d.Hash}) return end "
        f"local b=GameInfo.Buildings['{request.item}'] "
        "if b then UI.SetInterfaceMode(InterfaceModeTypes.BUILDING_PLACEMENT,"
        "{[CityOperationTypes.PARAM_BUILDING_TYPE]=b.Hash}) end "
        f"end) print('{SENTINEL}')"
    )


_RESTORE_LUA = (
    "pcall(function() UI.SetInterfaceMode(InterfaceModeTypes.SELECTION) end) "
    "print('MODE|'..tostring(UI.GetInterfaceMode()==InterfaceModeTypes.SELECTION)) "
    f"print('{SENTINEL}')"
)


def _targets_from(lines: list[str]) -> tuple[list[list[int]], int] | None:
    for line in lines:
        if line.startswith("TARGETS|"):
            _, tiles, total = (line.split("|") + ["", "0"])[:3]
            pairs = [[int(n) for n in t.split(",")] for t in tiles.split(";") if t]
            return pairs, int(total or 0)
    return None


@dataclass
class ProbeShots:
    """Turn handler (and bus subscriber) that takes probe screenshots. Off unless it has kinds."""

    shots: Screenshotter
    run_lua: LuaRunner
    kinds: list[str] = field(default_factory=list)
    overlay: bool = False
    dwell: float = CAMERA_DWELL
    cap: int = PROBE_CAP
    name = "probe_shots"

    def __post_init__(self) -> None:
        self._pending: list[ProbeRequest] = []
        self._seen: set[tuple[str, str, str]] = set()
        self._blocked_turns: set[int] = set()
        self._shot_turns: set[int] = set()
        self.seconds: list[float] = []

    @property
    def enabled(self) -> bool:
        return bool(self.kinds)

    def subscribe(self, bus: EventBus) -> None:
        if self.enabled:
            bus.subscribe(self.on_event)

    def on_event(self, event: Event) -> None:
        for kind in self.kinds:
            try:
                request = PROBE_KINDS[kind](event)
            except Exception:
                log.warning("Probe matcher %s failed on %s", kind, event.kind, exc_info=True)
                continue
            if request is None:
                continue
            if request.kind == "blocked":
                self._blocked_turns.add(request.turn)
            self._pending.append(request)

    async def run(self, game: Any) -> None:
        """Take at most one probe shot for the events seen since the last turn."""
        pending, self._pending = self._pending, []
        try:
            for request in pending:
                if self._allowed(request):
                    await self._shoot(request)
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Probe screenshot failed", exc_info=True)

    @staticmethod
    def _key(request: ProbeRequest) -> tuple[str, str, str]:
        return (request.city or str(request.city_id), request.item, request.reason_class)

    def _allowed(self, request: ProbeRequest) -> bool:
        if request.turn in self._shot_turns:
            return False
        if request.turn in self._blocked_turns and request.kind != "blocked":
            return False
        if self._key(request) in self._seen:
            return False
        return self.shots.probe_room(self.cap) > 0

    async def _shoot(self, request: ProbeRequest) -> Path | None:
        started = time.perf_counter()
        info: dict[str, Any] = {}
        overlay_opened = False
        path: Path | None = None
        try:
            found = _targets_from(await self.run_lua(_look_lua(request)))
            if found is not None:
                info["targets"], info["target_count"] = found
            if self.overlay and request.wants_targets:
                overlay_opened = True
                await self.run_lua(_overlay_lua(request))
            await asyncio.sleep(self.dwell)
            label = f"{PROBE_PREFIX}{request.kind}-{request.city}-{request.item}".rstrip("-")
            path = await asyncio.to_thread(self.shots.take_probe, request.turn, label, self.cap)
        except Exception:
            log.warning("Probe %s failed", request.kind, exc_info=True)
        finally:
            if overlay_opened:
                info["overlay"] = await self._restore_selection()
        elapsed = time.perf_counter() - started
        self.seconds.append(elapsed)
        log.info(
            "Probe %s %s %s took %.2fs (dwell plus capture)",
            request.kind,
            request.city,
            request.item,
            elapsed,
        )
        if elapsed > SLOW_PROBE_SECONDS:
            log.warning("Probe took %.2fs, over the %.0fs target", elapsed, SLOW_PROBE_SECONDS)
        if path is not None:
            self._seen.add(self._key(request))
            self._shot_turns.add(request.turn)
            self._write_sidecar(path, request, info, elapsed)
        return path

    async def _restore_selection(self) -> str:
        """Put the interface back to SELECTION and check. Tries twice; never raises."""
        for _ in range(2):
            try:
                lines = await self.run_lua(_RESTORE_LUA)
            except Exception:
                log.warning("Restoring the interface mode failed", exc_info=True)
                continue
            if "MODE|true" in lines:
                return "restored"
        log.error("Interface mode could not be confirmed as SELECTION after a probe")
        return "restore_unconfirmed"

    def _write_sidecar(
        self, image: Path, request: ProbeRequest, info: dict[str, Any], seconds: float
    ) -> None:
        data = {
            **asdict(request),
            **info,
            "image": image.name,
            "seconds": round(seconds, 2),
            "overlay_requested": self.overlay,
        }
        try:
            image.with_suffix(".json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            log.warning("Could not write the probe sidecar for %s", image.name, exc_info=True)
