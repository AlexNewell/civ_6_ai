# ruff: noqa: E501  (the Lua has long lines)
"""A working city yield focus, replacing the Lua builder in civ6-mcp (Run 12 starvation).

civ6-mcp's `build_set_yield_focus` sends `SET_FOCUS` with `PARAM_FLAGS = 1` and no `PARAM_DATA0`.
The game's own city panel (`CityPanel.lua`, `SetYieldFocus` / `SetYieldIgnore`) uses
`PARAM_FLAGS = 0` for "favored" and `PARAM_FLAGS = 1` for "ignored", and always sets
`PARAM_DATA0` to 1 (turn it on) or 0 (turn it off). So civ6-mcp's "food focus" was a command to
*ignore* food with an unset on/off flag: at best a no-op, at worst citizens avoiding food tiles.
That fits Run 12, where cities set to a food focus kept starving for 50 turns.

The builder here follows the panel: `food` favors food (and first un-ignores it), `default`
un-favors and un-ignores every yield. The reply is one `OK:` line that also reads the state
back (`favored=`, `ignored=`) and counts worked tiles, specialists and free food tiles, so the
log shows whether the focus took and why a city starves. Reads are wrapped in pcall: a missing
API gives `?`, never an error. Only integers and fixed names are written into the Lua.
"""

from civ_mcp import lua as lq
from civ_mcp.lua._helpers import SENTINEL, _bail, _lua_get_city


def build_set_yield_focus(city_id: int, yield_type: str) -> str:
    """InGame Lua that favors a yield for a city, or (for DEFAULT) clears every focus."""
    city_id = int(city_id)
    name = yield_type.upper()
    default = name == "DEFAULT"
    if not default and not name.startswith("YIELD_"):
        name = f"YIELD_{name}"
    if not default and not all(c.isalnum() or c == "_" for c in name):
        raise ValueError(f"bad yield type {yield_type!r}")
    want = "nil" if default else f'GameInfo.Yields["{name}"]'
    return f"""
{_lua_get_city(city_id)}
local citz = pCity:GetCitizens()
local want = {want}
if not {"true" if default else "(want ~= nil)"} then {_bail(f"ERR:YIELD_NOT_FOUND|{name}")} end
local function send(flags, yIndex, on)
    local tp = {{}}
    tp[CityCommandTypes.PARAM_FLAGS] = flags
    tp[CityCommandTypes.PARAM_YIELD_TYPE] = yIndex
    tp[CityCommandTypes.PARAM_DATA0] = on
    CityManager.RequestCommand(pCity, CityCommandTypes.SET_FOCUS, tp)
end
for yr in GameInfo.Yields() do
    if citz:IsDisfavoredYield(yr.Index) then send(1, yr.Index, 0) end
    if citz:IsFavoredYield(yr.Index) and (want == nil or want.Index ~= yr.Index) then send(0, yr.Index, 0) end
end
if want ~= nil and not citz:IsFavoredYield(want.Index) then send(0, want.Index, 1) end
local fav, ign = {{}}, {{}}
for yr in GameInfo.Yields() do
    if citz:IsFavoredYield(yr.Index) then fav[#fav + 1] = yr.YieldType end
    if citz:IsDisfavoredYield(yr.Index) then ign[#ign + 1] = yr.YieldType end
end
local worked, free, foodWorked = 0, 0, 0
local ok = pcall(function()
    local cx, cy = pCity:GetX(), pCity:GetY()
    for _, idx in ipairs(Map.GetCityPlots():GetPurchasedPlots(pCity)) do
        local plot = Map.GetPlotByIndex(idx)
        if plot ~= nil and not (plot:GetX() == cx and plot:GetY() == cy) then
            local f = plot:GetYield(0)
            if plot:GetWorkerCount() > 0 then
                worked = worked + 1
                foodWorked = foodWorked + f
            elseif f >= 2 and not plot:IsWater() then
                free = free + 1
            end
        end
    end
end)
local pop = pCity:GetPopulation()
local spec = ok and math.max(0, pop - worked) or "?"
print("OK:FOCUS|favored=" .. table.concat(fav, ",") .. "|ignored=" .. table.concat(ign, ",") .. "|pop=" .. pop .. "|worked=" .. (ok and worked or "?") .. "|specialists=" .. tostring(spec) .. "|worked_food=" .. (ok and foodWorked or "?") .. "|free_food_tiles=" .. (ok and free or "?"))
print("{SENTINEL}")
"""


def fix_set_yield_focus() -> None:
    """Make `GameState.set_city_focus` use the builder above. Raises if civ6-mcp has changed."""
    if not callable(getattr(lq, "build_set_yield_focus", None)):
        raise RuntimeError("civ6-mcp has no build_set_yield_focus to replace")
    from civ_mcp import game_state

    if game_state.lq is not lq:
        raise RuntimeError("civ6-mcp game_state no longer builds its Lua through civ_mcp.lua")
    lq.build_set_yield_focus = build_set_yield_focus  # type: ignore[attr-defined]
