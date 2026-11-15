"""Load a save and write checkpoint saves without anyone at the keyboard (issue 17).

What the game does (checked live on 2026-11-15, Civ VI build 15296837, Windows):

- `Network.LoadGame` from the InGame Lua state loads a save from a running game, including from
  the defeat screen: the InGame state still answers there, so no trip through the main menu.
- The load stops on the leader screen until "Continue Game", and the tuner stays closed until
  then. The game's own `LoadScreen.lua` skips that button when
  `Automation.IsAutoStartEnabled()` is true, and `Automation.SetAutoStartEnabled(true)` from
  InGame Lua sets it before the load. With it, STD_T1_01 was back on the tuner in 24 s with no
  input at all.
- Without it, a synthetic mouse click on the button did nothing (DPI-aware, foreground, not
  elevated), but an Escape key-up does what the button does once loading is complete
  (`LoadScreen.lua` `OnInput`). `press_escape` is kept as the fallback.
- `Network.SaveGame` writes a named save into the normal Single saves folder.

Loading from the main menu (no game open) is not verified: the InGame state does not exist
there. `load_save` then uses the first Lua state, which may not have `Network.LoadGame`.
"""

import asyncio
import contextlib
import logging
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from civ_mcp.connection import GameConnection
from civ_mcp.lua import SENTINEL

log = logging.getLogger(__name__)

LOAD_TIMEOUT_SECONDS = 300  # Late-game saves on a slow disk take longer than turn-1 ones.
ESCAPE_AFTER_SECONDS = 90  # No tuner by now: the loading screen may be waiting for input.
POLL_SECONDS = 3
QUERY_WAIT_SECONDS = 15

_LOAD_LUA = """
if not ExposedMembers then ExposedMembers = {{}} end
ExposedMembers.Civ6AiLoad = "PENDING"
pcall(function() Automation.SetAutoStartEnabled(true) end)
local function OnResults(fileList, queryId)
  UI.CloseFileListQuery(queryId)
  LuaEvents.FileListQueryResults.Remove(OnResults)
  for _, save in ipairs(fileList) do
    if save.Name == "{name}" or save.Name == "{name}.Civ6Save" then
      ExposedMembers.Civ6AiLoad = "FOUND"
      {leave}
      Network.LoadGame(save, ServerType.SERVER_TYPE_NONE)
      return
    end
  end
  ExposedMembers.Civ6AiLoad = "NOT_FOUND"
end
LuaEvents.FileListQueryResults.Add(OnResults)
local options = SaveLocationOptions.NORMAL + SaveLocationOptions.AUTOSAVE
UI.QuerySaveGameList(SaveLocations.LOCAL_STORAGE, SaveTypes.SINGLE_PLAYER, options)
print("QUERY_SENT")
print("{sentinel}")
"""

_STATUS_LUA = 'print(tostring(ExposedMembers.Civ6AiLoad))\nprint("{sentinel}")'

_TURN_LUA = 'print("TURN|" .. Game.GetCurrentGameTurn())\nprint("{sentinel}")'

_SAVE_LUA = """
local gf = {{}}
gf.Name = "{name}"
gf.Location = SaveLocations.LOCAL_STORAGE
gf.Type = SaveTypes.SINGLE_PLAYER
gf.IsAutosave = false
gf.IsQuicksave = false
Network.SaveGame(gf)
print("SAVED|{name}")
print("{sentinel}")
"""


class LoadError(RuntimeError):
    """The save could not be loaded, or the game did not come back on the tuner."""


@dataclass(frozen=True)
class Loaded:
    name: str
    turn: int
    seconds: float
    escape_pressed: bool


def checkpoint_name(scenario: str, turn: int) -> str:
    """The save name for a checkpoint: `CKPT_STD_T1_01_T100`. Sorts by turn."""
    return f"CKPT_{scenario}_T{turn:03d}"


def _safe_name(name: str) -> str:
    if not name or any(ch in name for ch in '"\\\n\r'):
        raise ValueError(f"unsafe save name {name!r}")
    return name


def build_load_lua(name: str, in_game: bool) -> str:
    leave = "Network.LeaveGame()" if in_game else ""
    return _LOAD_LUA.format(name=_safe_name(name), leave=leave, sentinel=SENTINEL)


def build_save_lua(name: str) -> str:
    return _SAVE_LUA.format(name=_safe_name(name), sentinel=SENTINEL)


def press_escape() -> bool:
    """Bring the game window forward and send Escape. Windows only; False if it can't."""
    if sys.platform != "win32":
        return False
    try:
        import win32api
        import win32con
        import win32gui
    except ImportError:
        return False
    found: list[int] = []

    def visit(hwnd: int, _: object) -> None:
        title = win32gui.GetWindowText(hwnd).lower()
        if win32gui.IsWindowVisible(hwnd) and "civilization vi" in title:
            found.append(hwnd)

    win32gui.EnumWindows(visit, None)
    if not found:
        return False
    try:
        win32gui.SetForegroundWindow(found[0])
    except Exception:  # Windows may refuse focus; the key may still reach the game.
        log.debug("Could not bring the game window forward", exc_info=True)
    time.sleep(0.4)
    win32api.keybd_event(win32con.VK_ESCAPE, 0, 0, 0)
    time.sleep(0.08)
    win32api.keybd_event(win32con.VK_ESCAPE, 0, win32con.KEYEVENTF_KEYUP, 0)
    return True


async def _connect(factory: Callable[[], GameConnection], timeout: float) -> GameConnection:
    connection = factory()
    await asyncio.wait_for(connection.connect(), timeout)
    return connection


async def current_turn(connection: GameConnection) -> int:
    lines = await connection.execute_in_state(
        connection.ingame_index, _TURN_LUA.format(sentinel=SENTINEL)
    )
    for line in lines:
        if line.startswith("TURN|"):
            return int(line.split("|", 1)[1])
    raise LoadError(f"no turn in {lines!r}")


async def load_save(
    name: str,
    factory: Callable[[], GameConnection] = GameConnection,
    escape: Callable[[], bool] = press_escape,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = LOAD_TIMEOUT_SECONDS,
) -> Loaded:
    """Load `name` from whatever is open (a game, the defeat screen) and wait until it is ready.

    Returns the turn of the loaded game. Raises LoadError if the save is not found or the game
    does not come back on the tuner within `timeout`. The caller connects afresh afterwards: the
    connection used here is closed, since a load drops every Lua state.
    """
    started = clock()
    connection = await _connect(factory, 15)
    in_game = connection.ingame_index is not None
    state = connection.ingame_index if in_game else 0
    try:
        await connection.execute_in_state(state, build_load_lua(name, in_game))
        status = "PENDING"
        deadline = clock() + QUERY_WAIT_SECONDS
        while status == "PENDING" and clock() < deadline:
            await sleep(0.5)
            try:
                lines = await connection.execute_in_state(
                    state, _STATUS_LUA.format(sentinel=SENTINEL)
                )
            except Exception:  # The load has started and taken the Lua state with it.
                status = "FOUND"
                break
            status = next((ln for ln in lines if ln in ("FOUND", "NOT_FOUND", "PENDING")), status)
        if status == "NOT_FOUND":
            raise LoadError(f"no save named {name!r} in the game's save list")
        if status == "PENDING":
            raise LoadError(f"the save list did not answer within {QUERY_WAIT_SECONDS}s")
    finally:
        with contextlib.suppress(Exception):
            await connection.disconnect()

    escaped = False
    while clock() - started < timeout:
        await sleep(POLL_SECONDS)
        try:
            back = await _connect(factory, 5)
        except Exception:
            if not escaped and clock() - started > ESCAPE_AFTER_SECONDS:
                escaped = escape()
                log.info(
                    "No tuner %.0fs after the load: Escape sent (%s)", clock() - started, escaped
                )
            continue
        try:
            if back.ingame_index is None:
                continue
            turn = await current_turn(back)
        except Exception:
            continue
        finally:
            with contextlib.suppress(Exception):
                await back.disconnect()
        seconds = clock() - started
        log.info("Loaded %s at turn %d in %.0fs", name, turn, seconds)
        return Loaded(name, turn, seconds, escaped)
    raise LoadError(f"{name!r} did not come back on the tuner within {timeout:.0f}s")


async def save_checkpoint(execute: Callable[[str], Awaitable[list[str]]], name: str) -> bool:
    """Write a named save of the open game. `execute` runs InGame Lua and returns its lines."""
    lines = await execute(build_save_lua(name))
    saved = any(line == f"SAVED|{name}" for line in lines)
    log.info("Checkpoint %s %s", name, "saved" if saved else f"not confirmed: {lines!r}")
    return saved
