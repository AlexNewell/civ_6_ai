"""Load each standard save in a running Civ VI and record or check its seeds and settings.

Loading replaces whatever game is open. It uses Lua only, unlike civ6-mcp's `load_game_save`, whose
fallback restarts the game and clicks through its menus. That means someone has to click the button
on Civ's loading screen for each save, because the tuner stays closed until then.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from civ_mcp.connection import GameConnection, LuaError
from civ_mcp.lua import SENTINEL

MANIFEST = Path(__file__).parent.parent / "config" / "standard_saves.json"
LOAD_TIMEOUT_SECONDS = 600
POLL_SECONDS = 2
STATUS_CHECKS = 60
STATUS_POLL_SECONDS = 0.5
PREFIX = "SAVEINFO|"

LOAD_LUA = """
if not ExposedMembers then ExposedMembers = {{}} end
ExposedMembers.Civ6AiLoad = "PENDING"
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
local options = SaveLocationOptions.NORMAL
UI.QuerySaveGameList(SaveLocations.LOCAL_STORAGE, SaveTypes.SINGLE_PLAYER, options)
print("QUERY_SENT")
print("{sentinel}")
"""

STATUS_LUA = """
print(tostring(ExposedMembers.Civ6AiLoad))
print("{sentinel}")
"""

READ_LUA = """
local human = PlayerConfigurations[Game.GetLocalPlayer()]
local function emit(key, read)
  local ok, value = pcall(read)
  print("{prefix}" .. key .. "|" .. (ok and tostring(value) or "ERROR"))
end
local function city_states()
  local count = 0
  for _, player in ipairs(PlayerManager.GetAliveMinors()) do
    local name = PlayerConfigurations[player:GetID()]:GetCivilizationTypeName()
    local civilization = GameInfo.Civilizations[name]
    if civilization.StartingCivilizationLevelType == "CIVILIZATION_LEVEL_CITY_STATE" then
      count = count + 1
    end
  end
  return count
end
emit("turn", function() return Game.GetCurrentGameTurn() end)
emit("game_seed", function() return GameConfiguration.GetValue("GAME_SYNC_RANDOM_SEED") end)
emit("map_seed", function() return MapConfiguration.GetValue("RANDOM_SEED") end)
emit("ruleset", function() return GameConfiguration.GetValue("RULESET") end)
emit("difficulty", function()
  return GameInfo.Difficulties[human:GetHandicapTypeID()].DifficultyType
end)
emit("game_speed", function()
  return GameInfo.GameSpeeds[GameConfiguration.GetGameSpeedType()].GameSpeedType
end)
emit("start_era", function() return GameInfo.Eras[GameConfiguration.GetStartEra()].EraType end)
emit("map_size", function() return GameInfo.Maps[MapConfiguration.GetMapSize()].MapSizeType end)
emit("map_script", function() return MapConfiguration.GetScript() end)
emit("major_players", function() return PlayerManager.GetAliveMajorsCount() end)
emit("city_states", city_states)
emit("civilization", function() return human:GetCivilizationTypeName() end)
emit("leader", function() return human:GetLeaderTypeName() end)
print("{sentinel}")
"""


def parse_info(lines: list[str]) -> dict[str, str]:
    """Turn `SAVEINFO|key|value` output lines into a dictionary."""
    pairs = (line.removeprefix(PREFIX).split("|", 1) for line in lines if line.startswith(PREFIX))
    return dict(pairs)


def find_differences(expected: dict, actual: dict) -> list[str]:
    """List every recorded value that doesn't match what the save holds."""
    return [
        f"{key}: expected {value}, found {actual.get(key)}"
        for key, value in expected.items()
        if actual.get(key) != value
    ]


async def request_load(connection: GameConnection, name: str) -> None:
    await connection.reconnect()
    in_game = connection.ingame_index is not None
    state = (
        connection.ingame_index
        if in_game
        else next(i for i, state in connection.lua_states.items() if state == "MainMenu")
    )
    leave = "Network.LeaveGame()" if in_game else ""
    await connection.execute_in_state(
        state, LOAD_LUA.format(name=name, leave=leave, sentinel=SENTINEL)
    )
    await wait_for_teardown(connection, state)


async def wait_for_teardown(connection: GameConnection, state: int) -> None:
    """Return once Civ tears down the open Lua state, the only proof that the load has started."""
    for _ in range(STATUS_CHECKS):
        await asyncio.sleep(STATUS_POLL_SECONDS)
        try:
            lines = await connection.execute_in_state(
                state, STATUS_LUA.format(sentinel=SENTINEL), timeout=2
            )
        except TimeoutError:  # Must come first, because it's a kind of OSError.
            continue
        except (ConnectionError, LuaError, OSError):
            return
        if "NOT_FOUND" in lines:
            raise FileNotFoundError("Civ doesn't list this save")
    raise TimeoutError("Civ never started loading the save")


async def read_when_loaded(connection: GameConnection, turn_must_be: int) -> dict[str, str]:
    """Wait for the new game's Lua states, then read it. Raises TimeoutError if it never appears."""
    print("Waiting: click the button on Civ's loading screen", flush=True)
    deadline = time.monotonic() + LOAD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS)
        try:
            await connection.reconnect()
            if connection.ingame_index is None:
                continue
            lines = await connection.execute_write(
                READ_LUA.format(prefix=PREFIX, sentinel=SENTINEL), timeout=10
            )
        except (ConnectionError, LuaError, OSError, TimeoutError):
            continue
        info = parse_info(lines)
        if info.get("turn") == str(turn_must_be) and "leader" in info:
            return info
    raise TimeoutError("the save didn't finish loading")


async def connect_when_ready(connection: GameConnection) -> None:
    """Connect, waiting for the tuner in case Civ is on its loading screen."""
    deadline = time.monotonic() + LOAD_TIMEOUT_SECONDS
    while True:
        try:
            return await connection.connect()
        except ConnectionError:
            if time.monotonic() > deadline:
                raise
            print("Waiting for the tuner: is Civ on its loading screen?", flush=True)
            await asyncio.sleep(POLL_SECONDS)


async def run(write: bool, first_is_open: bool) -> int:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    connection = GameConnection()
    await connect_when_ready(connection)
    failed = False
    seeds = set()
    for save in manifest["saves"]:
        name = save["name"]
        try:
            if not (first_is_open and save is manifest["saves"][0]):
                await request_load(connection, name)
            info = await read_when_loaded(connection, turn_must_be=1)
        except (ConnectionError, FileNotFoundError, TimeoutError) as error:
            print(f"FAILED {name}: {error}")
            failed = True
            continue
        info = {key: value for key, value in info.items() if key != "turn"}
        settings = {key: info.pop(key) for key in manifest["settings"]}
        problems = find_differences(manifest["settings"], settings)
        if not write:
            problems += find_differences(save["recorded"], info)
        if (info["game_seed"], info["map_seed"]) in seeds:
            problems.append("seeds repeat an earlier save")
        seeds.add((info["game_seed"], info["map_seed"]))
        save["recorded"] = info
        print(f"{'FAILED' if problems else 'OK'} {name}: {info}")
        for problem in problems:
            print(f"  {problem}")
        failed = failed or bool(problems)
    if write and not failed:
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {MANIFEST.name}")
    return 1 if failed else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="record seeds in the manifest")
    parser.add_argument(
        "--first-is-open", action="store_true", help="the first save is already open, so read it"
    )
    args = parser.parse_args()
    return asyncio.run(run(args.write, args.first_is_open))


if __name__ == "__main__":
    sys.exit(main())
