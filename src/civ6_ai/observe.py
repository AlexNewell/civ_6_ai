"""Watch a game a human is playing, without touching it.

Everything here only reads. It never ends a turn, dismisses a popup, loads a save or moves the
camera, so a person can play while it runs. `ObserverGame` is the whole read surface the observer
is allowed to use; keep mutating calls off it (tests/test_observe.py checks the module source).

Each new turn it writes two files next to each other in `runs/`:
- `<name>.jsonl`: one `TurnRecord` per turn (score, cities, rival scores...), so the existing
  scoring and comparison scripts read it like an AI run.
- `<name>_observed.jsonl`: the full diary snapshot (every player, every city) plus research,
  civic and trade-route state, for the human-vs-AI write-up.
"""

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from civ6_ai.orchestrator import TurnLog, TurnRecord

POLL_SECONDS = 1.5
MAX_FAILURES = 30  # In a row, about a minute, before the watcher gives up.
_RETRYABLE = (OSError, ValueError, TimeoutError)  # ConnectionError is an OSError.


class ObserverGame(Protocol):
    async def get_game_overview(self) -> Any: ...
    async def get_diary_snapshot(self) -> Any: ...
    async def get_tech_civics(self) -> Any: ...
    async def get_trade_routes(self) -> Any: ...


def to_plain(value: Any) -> Any:
    """Turn dataclasses (and sets) into JSON-safe values; leave unknown objects as text."""
    if is_dataclass(value) and not isinstance(value, type):
        return to_plain(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_plain(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def build_turn_record(overview: Any, diary: Any, us: int | None = None) -> TurnRecord:
    """The standing fields of an AI run's turn record, taken from the diary's player rows."""
    players = list(getattr(diary, "players", []) or [])
    our_id = us if us is not None else getattr(overview, "player_id", None)
    ours = next((p for p in players if p.pid == our_id), None)
    rivals = {str(p.pid): p.score for p in players if p.pid != our_id}
    all_scores = sorted((p.score for p in players if p.pid != our_id), reverse=True)
    our_score = ours.score if ours else getattr(overview, "score", None)
    best = max([our_score, *all_scores]) if our_score is not None else None
    rank = (1 + sum(score > our_score for score in all_scores)) if our_score is not None else None
    return TurnRecord(
        turn=overview.turn,
        handler_seconds={},
        handler_errors={},
        end_turn_seconds=0.0,
        total_seconds=0.0,
        score=our_score,
        cities=ours.cities if ours else getattr(overview, "num_cities", None),
        population=ours.pop if ours else getattr(overview, "total_population", None),
        rival_scores=rivals or None,
        era=(ours.era if ours else getattr(overview, "era_name", None)) or None,
        era_score=ours.era_score if ours else getattr(overview, "era_score", None),
        era_dark=getattr(overview, "era_dark_threshold", None),
        era_golden=getattr(overview, "era_golden_threshold", None),
        players_alive=len(players) or None,
        all_scores=all_scores or None,
        rank=rank,
        leader_score=best,
    )


def build_observation(overview: Any, diary: Any, tech: Any, routes: Any) -> dict[str, Any]:
    """Everything worth comparing with an AI run, for one turn, as plain JSON values."""
    return {
        "turn": overview.turn,
        "player_id": getattr(overview, "player_id", None),
        "civ": getattr(overview, "civ_name", None),
        "explored_land": getattr(overview, "explored_land", None),
        "total_land": getattr(overview, "total_land", None),
        "unit_breakdown": to_plain(getattr(overview, "unit_breakdown", None)),
        "current_research": getattr(overview, "current_research", None),
        "current_civic": getattr(overview, "current_civic", None),
        "players": to_plain(getattr(diary, "players", [])),
        "cities": to_plain(getattr(diary, "cities", [])),
        "tech_civics": to_plain(tech),
        "trade_routes": to_plain(routes),
    }


def new_events(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, Any]]:
    """Events between two observations: cities founded, techs and civics finished."""
    if previous is None:
        return []
    us = current.get("player_id")
    turn = current["turn"]
    events: list[dict[str, Any]] = []
    old_cities = {c["city_id"] for c in previous["cities"] if c["pid"] == us}
    for city in current["cities"]:
        if city["pid"] == us and city["city_id"] not in old_cities:
            # Founded by the player during the previous turn; we only see it at the next turn start.
            events.append({"turn": previous["turn"], "kind": "city_gained", "city": city["city"]})
    old = next((p for p in previous["players"] if p["pid"] == us), None)
    new = next((p for p in current["players"] if p["pid"] == us), None)
    if old and new:
        for key, kind in (("techs", "tech_complete"), ("civics", "civic_complete")):
            for name in new[key]:
                if name not in old[key]:
                    events.append({"turn": turn, "kind": kind, "name": name})
    return events


class Observer:
    """Writes a record for each new turn the game reaches."""

    def __init__(
        self,
        game: ObserverGame,
        directory: Path,
        name: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._now = now
        self._game = game
        self._turn_log = TurnLog(directory / f"{name}.jsonl")
        self._observed = directory / f"{name}_observed.jsonl"
        self._events = directory / f"{name}_events.jsonl"
        self._last: dict[str, Any] | None = None
        self.last_turn: int | None = None
        # Resuming a run: carry on from its newest row, so the same turn isn't written twice.
        if self._observed.exists():
            rows = [line for line in self._observed.read_text("utf-8").splitlines() if line]
            if rows:
                self._last = json.loads(rows[-1])
                self.last_turn = self._last["turn"]

    async def observe_once(self) -> bool:
        """Record the current turn if it's new. Returns True when a record was written."""
        overview = await self._game.get_game_overview()
        if overview.turn == self.last_turn:
            return False
        diary = await self._game.get_diary_snapshot()
        tech = await self._game.get_tech_civics()
        routes = await self._game.get_trade_routes()
        observation = build_observation(overview, diary, tech, routes)
        # When we saw the turn begin, so dictated notes can be matched to it (see notes.py).
        observation["observed_at"] = self._now().astimezone(UTC).isoformat()
        self._turn_log.write(build_turn_record(overview, diary))
        self._append(self._observed, [observation])
        self._append(self._events, new_events(self._last, observation))
        self._last = observation
        self.last_turn = overview.turn
        return True

    @staticmethod
    def _append(path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with path.open("a", encoding="utf-8") as file:
            for row in rows:
                file.write(json.dumps(row) + "\n")

    async def watch(
        self,
        until_turn: int | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        """Poll until `until_turn` has been recorded, or forever. Ctrl+C to stop.

        A bad read is retried, not fatal. The tuner shares one channel with the game's own log,
        so a UI event line can arrive where a reply was expected and fail to parse (seen at turn
        67 of a live game). A read writes nothing until all of its calls have succeeded, so a
        retry can't leave half a turn behind. After `MAX_FAILURES` in a row it gives up.
        """
        failures = 0
        while True:
            try:
                if await self.observe_once():
                    log(f"{time.strftime('%H:%M:%S')} recorded turn {self.last_turn}")
                failures = 0
            except _RETRYABLE as error:
                failures += 1
                log(f"read failed ({failures}/{MAX_FAILURES}), will retry: {error!s:.200}")
                if failures >= MAX_FAILURES:
                    raise
            if until_turn is not None and (self.last_turn or 0) >= until_turn:
                return
            await sleep(POLL_SECONDS)
