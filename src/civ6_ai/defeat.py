"""Tell whether the local player has been defeated, in a way that works with no city.

civ6-mcp's `check_game_over` can't do this at the pin: its InGame query fails with a nil
comparison while we have no city, and its fallback calls a builder that isn't exported. So a
defeat shows up as a turn that never ends. This is a read of our own player only, run from one
fixed Lua string, so no model ever supplies or sees Lua.

What the game offers, from civ6-mcp's own game-over query and the game's `EndGameMenu.lua`
(read only): `Players[me]:IsAlive()` is the test the end-game screen itself uses for "eliminated",
`Game.GetWinningTeam()` is set once any team has won, and the screen is the control
`/InGame/EndGameMenu`. The game pauses while that screen is up, which is why `end_turn` waits.

Whether `IsAlive` is already false for a player who simply hasn't founded a city yet is not
known (see `docs/live-checks.md`). So "not alive" alone is not trusted: it needs the screen up or
no units and no cities left.
"""

import logging
from dataclasses import dataclass
from typing import Protocol

from civ_mcp.lua import SENTINEL

log = logging.getLogger(__name__)

_MARKER = "DEFEAT_STATE|"

# Fixed text. Every read is guarded, so it prints something even where a call doesn't exist, such
# as the UI lookup in GameCore. The same text runs in both contexts.
_STATUS_LUA = f"""
local me = Game.GetLocalPlayer()
local alive, winTeam, myTeam, units, cities, screen = "?", -1, -1, -1, -1, "unknown"
pcall(function() alive = tostring(Players[me]:IsAlive()) end)
pcall(function() winTeam = Game.GetWinningTeam() or -1 end)
pcall(function() myTeam = Players[me]:GetTeam() or -1 end)
pcall(function() units = Players[me]:GetUnits():GetCount() end)
pcall(function() cities = Players[me]:GetCities():GetCount() end)
pcall(function()
    local menu = ContextPtr:LookUpControl("/InGame/EndGameMenu")
    if menu then screen = menu:IsHidden() and "hidden" or "shown" end
end)
print("{_MARKER}" .. alive .. "|" .. tostring(winTeam) .. "|" .. tostring(myTeam) .. "|"
    .. tostring(units) .. "|" .. tostring(cities) .. "|" .. screen)
print("{SENTINEL}")
"""


class LuaGame(Protocol):
    """The part of civ6-mcp's `GameState` that this read uses."""

    async def execute_lua(self, code: str, context: str = "gamecore") -> str: ...


@dataclass(frozen=True)
class PlayerStatus:
    alive: bool
    winning_team: int  # -1 while no team has won.
    team: int
    units: int  # -1 if the read failed.
    cities: int
    end_game_screen: bool | None  # None where the UI can't be read.

    @property
    def is_game_over(self) -> bool:
        if self.winning_team >= 0:
            return True  # Someone has won, so the game is over for us whether or not we lost.
        if self.alive:
            return False
        # Eliminated. Without the screen, only an empty player is certain enough to stop on.
        return bool(self.end_game_screen) or (self.units == 0 and self.cities == 0)


def parse_status(output: str) -> PlayerStatus | None:
    """Read the status line. Returns None if it's missing or the player couldn't be read."""
    for line in output.splitlines():
        if not line.startswith(_MARKER):
            continue
        parts = line[len(_MARKER) :].split("|")
        if len(parts) != 6 or parts[0] not in ("true", "false"):
            return None
        try:
            winning_team, team, units, cities = (int(part) for part in parts[1:5])
        except ValueError:
            return None
        screen = {"shown": True, "hidden": False}.get(parts[5])
        return PlayerStatus(parts[0] == "true", winning_team, team, units, cities, screen)
    return None


async def read_player_status(game: LuaGame) -> PlayerStatus | None:
    """Read the local player's status, from the UI context first and GameCore if that fails."""
    for context in ("ingame", "gamecore"):
        try:
            status = parse_status(await game.execute_lua(_STATUS_LUA, context))
        except ConnectionError:
            raise
        except Exception:
            log.debug("Player status read failed in %s", context, exc_info=True)
            continue
        if status is not None:
            return status
    return None


async def is_defeated(game: LuaGame) -> bool:
    """True once the game is over for the local player. False if that can't be established."""
    status = await read_player_status(game)
    if status is None:
        log.warning("Could not read the player's status to check for a defeat")
        return False
    if status.is_game_over:
        log.warning("Game over for the local player: %s", status)
    return status.is_game_over
