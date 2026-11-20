# ruff: noqa: E501  (the Lua has long lines)
"""Unique replacements read from the game's own tables, once per game (issue 354).

`game_data.py` holds hand tables of which unique unit, district and building replaces which base
one. The building table was written from recall and covers five entries, so a civilization's other
unique buildings missed their boosts (Codex found Seowon, Pitati, Bireme and Madrasa on PR 351).
The game states them in `GameInfo.BuildingReplaces`, `DistrictReplaces` and `UnitReplaces`; one
read-only Lua call lists the rows and `apply_replacements` merges them over the hand tables, which
stay as the fallback when the read fails.

The reply ends with its own `RPDONE` line: civ6-mcp's `execute_lua` strips `---END---`, so a reply
without `RPDONE` is a partial read and is dropped.
"""

from dataclasses import dataclass

from civ_mcp.lua._helpers import SENTINEL

from civ6_ai.policies import game_data

READ_TRIES = (
    2  # A failed read is tried once more next turn, then the hand tables stand for the game.
)

REPLACEMENTS_LUA = f"""
local function rows(tag, tbl, a, b)
    pcall(function()
        for row in GameInfo[tbl]() do print(tag .. "|" .. row[a] .. "|" .. row[b]) end
    end)
end
rows("BR", "BuildingReplaces", "CivUniqueBuildingType", "ReplacesBuildingType")
rows("DR", "DistrictReplaces", "CivUniqueDistrictType", "ReplacesDistrictType")
rows("UR", "UnitReplaces", "CivUniqueUnitType", "ReplacesUnitType")
print("RPDONE")
print("{SENTINEL}")
"""


@dataclass(frozen=True)
class Replacements:
    """What the game says replaces what. Buildings and districts are keyed without their
    `BUILDING_` / `DISTRICT_` prefix, as in `game_data`; units keep their full names."""

    buildings: dict[str, str]
    districts: dict[str, str]
    units: dict[str, str]


def parse_replacements(output: str) -> Replacements:
    """Parse the read. Raises ValueError unless its own `RPDONE` line is present."""
    if "RPDONE" not in output.split():
        raise ValueError("replacement read incomplete: no end marker")
    buildings: dict[str, str] = {}
    districts: dict[str, str] = {}
    units: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.strip().split("|")
        if len(parts) < 3 or not parts[1] or not parts[2]:
            continue
        if parts[0] == "BR":
            buildings[parts[1].removeprefix("BUILDING_")] = parts[2].removeprefix("BUILDING_")
        elif parts[0] == "DR":
            districts[parts[1].removeprefix("DISTRICT_")] = parts[2].removeprefix("DISTRICT_")
        elif parts[0] == "UR":
            units[parts[1]] = parts[2]
    return Replacements(buildings, districts, units)


def apply_replacements(read: Replacements) -> None:
    """Merge the read over the hand tables in place, so every importer of them sees it."""
    game_data.UNIQUE_BUILDINGS.update(read.buildings)
    game_data.UNIQUE_DISTRICTS.update(read.districts)
    game_data.REPLACES.update(read.units)


async def read_replacements(game) -> Replacements:
    """The three replacement tables in one call. `game` needs `execute_lua`."""
    reply = await game.execute_lua(REPLACEMENTS_LUA, "ingame")
    return parse_replacements(reply if isinstance(reply, str) else "\n".join(reply))
