# ruff: noqa: E501  (the Lua has long lines)
"""What food each of our cities' tiles give, read for every city in one InGame Lua call (issue 215).

Run 13 logged 143 starving notices. The growth model sized a city from its food surplus alone, so
a city whose workable tiles were all taken (the capital: pop 19, 13 worked, 6 specialists, no free
food tile) still planned to grow, then starved back. Specialists eat 2 food and make none.

The read lists, per city, the food of each worked tile and of each free workable tile (land and
water apart, since a Harbor lifts only water). Only our own plots are read, so nothing here is
hidden information. Everything runs under pcall; a city that fails is simply missing from the
reply, and the call ends with its own `CTDONE` line (civ6-mcp's `execute_lua` strips `---END---`,
so a reply without `CTDONE` is a partial read and is dropped).

Cost: one call for all cities, about the price of the known-city read (~0.4 s with the shortened
call waits) plus a few milliseconds of Lua per plot. The policy refreshes it every
`REFRESH_TURNS` turns, or sooner when a city's size or food sign changes.
"""

from dataclasses import dataclass

from civ_mcp.lua._helpers import SENTINEL

REFRESH_TURNS = 5
_KEEP = 14  # Most free tiles listed per kind; the best ones decide the capacity.

CITY_TILES_LUA = f"""
local me = Game.GetLocalPlayer()
local pl = Players[me]
for _, c in pl:GetCities():Members() do
    pcall(function()
        local cx, cy = c:GetX(), c:GetY()
        local worked, land, water = {{}}, {{}}, {{}}
        for _, idx in ipairs(Map.GetCityPlots():GetPurchasedPlots(c)) do
            local p = Map.GetPlotByIndex(idx)
            if p ~= nil and not (p:GetX() == cx and p:GetY() == cy) then
                local f = p:GetYield(0)
                if p:GetWorkerCount() > 0 then
                    worked[#worked + 1] = f
                elseif p:GetDistrictType() < 0 and f >= 1 and not p:IsImpassable() then
                    if p:IsWater() then water[#water + 1] = f else land[#land + 1] = f end
                end
            end
        end
        table.sort(land, function(a, b) return a > b end)
        table.sort(water, function(a, b) return a > b end)
        local function top(t) local o = {{}} for i = 1, math.min(#t, {_KEEP}) do o[i] = t[i] end return table.concat(o, ",") end
        print("CT|" .. c:GetID() .. "|" .. c:GetPopulation() .. "|" .. top(worked) .. "|" .. top(land) .. "|" .. top(water))
    end)
end
print("CTDONE")
print("{SENTINEL}")
"""


@dataclass(frozen=True)
class CityTiles:
    """Food of a city's tiles. Free lists are best first and cover only tiles giving 1+ food."""

    city_id: int
    pop: int  # Population when read.
    worked: tuple[int, ...]  # Food of each worked tile (the city centre is not counted).
    free_land: tuple[int, ...]
    free_water: tuple[int, ...]

    @property
    def specialists(self) -> int:
        return max(0, self.pop - len(self.worked))

    @property
    def free(self) -> tuple[int, ...]:
        return tuple(sorted((*self.free_land, *self.free_water), reverse=True))

    def focus_gain(self) -> int:
        """Food that rearranging citizens could add now.

        A specialist moved onto a free tile gains that tile's whole food (it made none). Free
        tiles left over replace the worst worked tiles when they are better.
        """
        free = list(self.free)
        moved = free[: self.specialists]
        gain = sum(moved)
        for new, old in zip(free[len(moved) :], sorted(self.worked), strict=False):
            if new <= old:
                break
            gain += new - old
        return gain


def _ints(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split(",") if part.strip())


def parse_city_tiles(output: str) -> dict[int, CityTiles]:
    """Parse the read. Raises ValueError unless its own `CTDONE` line is present."""
    if "CTDONE" not in output.split():
        raise ValueError("city-tile read incomplete: no end marker")
    tiles = {}
    for line in output.splitlines():
        parts = line.strip().split("|")
        if parts[0] != "CT" or len(parts) < 6:
            continue
        try:
            tiles[int(parts[1])] = CityTiles(
                int(parts[1]), int(parts[2]), _ints(parts[3]), _ints(parts[4]), _ints(parts[5])
            )
        except ValueError:
            continue
    return tiles


async def read_city_tiles(game) -> dict[int, CityTiles]:
    """All our cities' tile foods in one call. `game` needs `execute_lua`."""
    reply = await game.execute_lua(CITY_TILES_LUA, "ingame")
    return parse_city_tiles(reply if isinstance(reply, str) else "\n".join(reply))
