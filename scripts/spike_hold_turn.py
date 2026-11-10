"""S.1 spike: find out whether an AI player's turn can be held from outside the game.

Hooks turn events over the tuner, ends the local turn with Civ's own end-turn action, and records
what fires and when. Nothing is installed: the hooks live in the running game's memory.

Only runs on a standard save, recognised by seed. It ends turns, which can't be undone, and makes no
saves of its own. It doesn't use civ6-mcp's `end_turn`, because that writes `0_MCP_` saves. It sends
raw Lua, so none of this may ever be exposed to a model.
"""

import argparse
import asyncio
import json
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from civ_mcp.connection import GameConnection, LuaError
from civ_mcp.lua import SENTINEL

from civ6_ai.saves import disable_mcp_saves, use_real_save_dirs
from run_game import identify_save

ROOT = Path(__file__).parent.parent
MANIFEST = ROOT / "config" / "standard_saves.json"
HOOKS = ROOT / "mods" / "s1_hold_turn" / "s1_hooks.lua"
CONTEXTS = {"GameCore": "gamecore_index", "InGame": "ingame_index"}
HOLD_KINDS = ("none", "spin", "ui_event", "pause")
POLL_SECONDS = 0.25
READ_BATCH = 300
LOG_PREFIX = "S1LOG|"
MOVE_EVENTS = ("Events.UnitMoved", "Events.UnitMoveComplete")

STATUS_LUA = """
print("S1STAT|turn|" .. tostring(Game.GetCurrentGameTurn()))
print("S1STAT|local|" .. tostring(Game.GetLocalPlayer()))
print("S1STAT|seed|" .. tostring(GameConfiguration.GetValue("GAME_SYNC_RANDOM_SEED")))
print("S1STAT|local_active|" .. tostring(Players[Game.GetLocalPlayer()]:IsTurnActive()))
print("S1STAT|paused|" .. tostring(GameConfiguration.IsPaused()))
print("{sentinel}")
"""

POLL_LUA = """
local s = ExposedMembers.Civ6AiS1
print("S1POLL|" .. tostring(Automation.GetTime()) .. "|" .. tostring(Game.GetCurrentGameTurn())
  .. "|" .. tostring(Players[0]:IsTurnActive()) .. "|" .. tostring(s and s.hold.state)
  .. "|" .. tostring(s and s.seq))
print("{sentinel}")
"""

ARM_LUA = """
ExposedMembers.Civ6AiS1.hold = {{ state = "armed", kind = "{kind}", context = "{context}",
  event = "{event}", player = {player}, max_seconds = {max_seconds}, release = false }}
print("{sentinel}")
"""

END_TURN_LUA = """
UI.RequestAction(ActionTypes.ACTION_ENDTURN)
print("{sentinel}")
"""

RELEASE_BODY = """
local hold = ExposedMembers.Civ6AiS1.hold
hold.release = true
if hold.kind == "ui_event" and hold.event_id then
  UI.ReleaseEventID(hold.event_id)
  hold.state = "released"
elseif hold.kind == "pause" then
  PlayerConfigurations[Game.GetLocalPlayer()]:SetWantsPause(false)
  Network.BroadcastPlayerInfo()
  hold.state = "released"
end
"""

RELEASE_LUA = (
    RELEASE_BODY
    + """
print("{sentinel}")
"""
)

READ_LOG_LUA = """
local s = ExposedMembers.Civ6AiS1
for i = {first}, math.min({first} + {count} - 1, s.seq) do print("{prefix}" .. s.log[i]) end
print("{sentinel}")
"""

REMOVE_LUA = """
local s = ExposedMembers.Civ6AiS1
if s then
  if s.hold.state == "held" then
    {release}
  end
  for _, cleanup in ipairs(s.cleanups) do pcall(cleanup) end
  ExposedMembers.Civ6AiS1 = nil
end
print("{sentinel}")
"""


@dataclass(frozen=True)
class Event:
    seq: int
    time: float
    context: str
    name: str
    args: str
    turn: str
    active: str
    digest: str


def parse_event(line: str) -> Event | None:
    """Parse one log line from `s1_hooks.lua`. Returns None for anything else."""
    if not line.startswith(LOG_PREFIX):
        return None
    fields = line.removeprefix(LOG_PREFIX).split("|")
    if len(fields) != 8:
        return None
    seq, stamp, context, name, args, turn, active, digest = fields
    return Event(int(seq), float(stamp), context, name, args, turn, active, digest)


def first_of_player(events: list[Event], player: int) -> list[Event]:
    """Return the first time each event fired for this player, in order of firing."""
    seen: set[tuple[str, str]] = set()
    picked = []
    for event in events:
        key = (event.context, event.name)
        if event.args.split(",")[0] == str(player) and key not in seen:
            seen.add(key)
            picked.append(event)
    return picked


def moves_by_player(events: list[Event]) -> dict[str, int]:
    """Count unit-move events per player, from the player ID each event starts with."""
    counts: dict[str, int] = {}
    for event in events:
        if event.name in MOVE_EVENTS:
            player = event.args.split(",")[0]
            counts[player] = counts.get(player, 0) + 1
    return counts


def fill_hooks(source: str, context: str, target: int) -> str:
    """Fill in the hook template. The target goes into Lua as code, so it must be an integer."""
    if context not in CONTEXTS:
        raise ValueError(f"unknown context: {context}")
    if isinstance(target, bool) or not isinstance(target, int):
        raise ValueError("target must be an integer")
    return source.replace("__CONTEXT__", context).replace("__TARGET__", str(target))


def parse_status(lines: list[str]) -> dict[str, str]:
    """Turn `S1STAT|key|value` output lines into a dictionary."""
    pairs = (line.split("|")[1:3] for line in lines if line.startswith("S1STAT|"))
    return {pair[0]: pair[1] for pair in pairs if len(pair) == 2}


class Spike:
    """The Lua calls this spike makes, each aimed at a named context."""

    def __init__(self, connection: GameConnection):
        self.connection = connection

    def state(self, context: str) -> int:
        return getattr(self.connection, CONTEXTS[context])

    async def lua(self, context: str, code: str, timeout: float = 5) -> list[str]:
        return await self.connection.execute_in_state(self.state(context), code, timeout)

    async def status(self) -> dict[str, str]:
        return parse_status(await self.lua("InGame", STATUS_LUA.format(sentinel=SENTINEL), 8))

    async def install(self, target: int) -> None:
        source = HOOKS.read_text(encoding="utf-8")
        await self.remove()
        for context in CONTEXTS:
            for line in await self.lua(context, fill_hooks(source, context, target), 10):
                if line.endswith("|false"):
                    print(f"Not hooked: {line}")

    async def remove(self) -> None:
        """Release any hold and unhook. Does nothing when no game is open."""
        if self.connection.ingame_index is None:
            return
        await self.lua("InGame", REMOVE_LUA.format(release=RELEASE_BODY, sentinel=SENTINEL))

    async def read_log(self) -> list[Event]:
        events: list[Event] = []
        first = 1
        while True:
            code = READ_LOG_LUA.format(
                first=first, count=READ_BATCH, prefix=LOG_PREFIX, sentinel=SENTINEL
            )
            lines = await self.lua("GameCore", code, 15)
            batch = [event for event in map(parse_event, lines) if event]
            events += batch
            if len(batch) < READ_BATCH:
                return events
            first += READ_BATCH


async def poll_once(spike: Spike, context: str) -> dict:
    """Ask one Lua context for the game's state and time how long it takes to answer."""
    sent = time.time()
    try:
        lines = await spike.lua(context, POLL_LUA.format(sentinel=SENTINEL), 3)
    except (ConnectionError, LuaError, OSError, TimeoutError) as error:
        return {"sent": sent, "context": context, "error": repr(error)}
    entry = {"sent": sent, "context": context, "latency": round(time.time() - sent, 3)}
    reply = next((line for line in lines if line.startswith("S1POLL|")), None)
    if reply:
        _, _, turn, local_active, hold, seq = reply.split("|")
        entry |= {"turn": turn, "local_active": local_active, "hold": hold, "seq": seq}
    return entry


async def run_turn(spike: Spike, args: argparse.Namespace) -> list[dict]:
    """End the local turn and watch until it comes back, releasing any hold on schedule."""
    before = await spike.status()
    polls: list[dict] = []
    await spike.lua("InGame", END_TURN_LUA.format(sentinel=SENTINEL))
    polls.append({"sent": time.time(), "action": "end_turn_requested"})

    started = time.time()
    held_at = None
    released = False
    while time.time() - started < args.timeout:
        for context in args.poll:
            polls.append(await poll_once(spike, context))
        last = polls[-1]
        if last.get("hold") == "held" and held_at is None:
            held_at = time.time()
        due = held_at is not None and time.time() - held_at >= args.release_after
        if due and not released and last.get("hold") == "held":
            await spike.lua(args.hold_context, RELEASE_LUA.format(sentinel=SENTINEL))
            polls.append({"sent": time.time(), "action": "release_sent"})
            released = True
        if last.get("turn") not in (None, before["turn"]) and last.get("local_active") == "true":
            return polls
        await asyncio.sleep(POLL_SECONDS)
    polls.append({"sent": time.time(), "action": "watch_timed_out"})
    return polls


def write_report(name: str, events: list[Event], polls: list[dict]) -> Path:
    """Keep the full evidence in `runs/`, which git ignores."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = ROOT / "runs" / f"s1_{name}_{stamp}.json"
    path.parent.mkdir(exist_ok=True)
    report = {"events": [asdict(event) for event in events], "polls": polls}
    path.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return path


def print_summary(events: list[Event], polls: list[dict], target: int) -> None:
    print(f"Events: {len(events)}. Unit moves by player: {moves_by_player(events)}")
    for event in first_of_player(events, target):
        print(f"  {event.time:.3f} {event.context} {event.name} ({event.args}) {event.digest}")
    for event in events:
        if event.name.startswith("HOLD_"):
            print(f"  {event.time:.3f} {event.context} {event.name} {event.args} {event.digest}")
    answered = [poll["latency"] for poll in polls if "latency" in poll]
    if answered:
        print(f"Polls: {len(answered)}, slowest {max(answered)}s")
    for poll in polls:
        if "action" in poll or "error" in poll:
            print(f"  {poll}")


async def run(args: argparse.Namespace) -> int:
    use_real_save_dirs()
    disable_mcp_saves()
    connection = GameConnection()
    await connection.connect()
    spike = Spike(connection)
    try:
        if connection.ingame_index is None:
            print("FAILED: no game is open")
            return 1
        status = await spike.status()
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        name = identify_save(manifest, int(status["seed"]))
        print(f"{name}: {status}")
        if name is None:
            print("FAILED: the open game isn't a standard save")
            return 1
        if args.status_only:
            return 0
        if status["local_active"] != "true":
            print("FAILED: the local turn isn't active")
            return 1

        await spike.install(args.target)
        if args.hold != "none":
            arm = ARM_LUA.format(
                kind=args.hold,
                context=args.hold_context,
                event=args.hold_event,
                player=args.target if args.hold_player is None else args.hold_player,
                max_seconds=args.max_seconds,
                sentinel=SENTINEL,
            )
            await spike.lua("GameCore", arm)
        polls = await run_turn(spike, args)
        events = await spike.read_log()
        print(f"Report: {write_report(name, events, polls).relative_to(ROOT)}")
        print_summary(events, polls, args.target)
        print(f"After: {await spike.status()}")
        return 0
    finally:
        try:
            await spike.remove()
        finally:
            await connection.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-only", action="store_true", help="read the game and stop")
    parser.add_argument("--target", type=int, default=1, help="AI player to study")
    parser.add_argument("--hold", choices=HOLD_KINDS, default="none")
    parser.add_argument("--hold-context", choices=tuple(CONTEXTS), default="GameCore")
    parser.add_argument("--hold-event", default="PlayerTurnStarted")
    parser.add_argument("--hold-player", type=int, help="default: the target. -1 means any")
    parser.add_argument("--max-seconds", type=float, default=3, help="a spin ends by itself here")
    parser.add_argument("--release-after", type=float, default=2)
    parser.add_argument("--poll", nargs="+", choices=tuple(CONTEXTS), default=["GameCore"])
    parser.add_argument("--timeout", type=float, default=120, help="seconds to watch the turn")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
