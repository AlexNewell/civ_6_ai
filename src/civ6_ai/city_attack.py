# ruff: noqa: E501  (the Lua has long lines)
"""A working city ranged attack, replacing the Lua builder in civ6-mcp (live failure, run 5).

civ6-mcp's `build_city_attack` words its line-of-sight error with `{target_x}` inside a plain
(not f-) string, so the Lua that is sent holds the literal text `{target_x}`, which Lua reads as
an empty table constructor. The first time the target is not in `CityManager.GetCommandTargets`,
the script dies with `operator .. is not supported for table .. string` at line 56 instead of
answering `ERR:NO_LOS`. The strike never fired, and the error escaped as a `LuaError`.

The builder here keeps civ6-mcp's order of checks and its answers (`OK:CITY_RANGE_ATTACK|...`
and `ERR:<REASON>|...`, which `GameState.city_attack` and its follow-up read), with two changes:

- the message is built from numbers written into the Lua text, so it can't hit that bug;
- being absent from `GetCommandTargets` no longer stops the strike. Which shape that call
  returns hasn't been seen in a live game, so it is used only to word the error, and
  `CanStartCommand` (which checks range and line of sight itself) decides. If it refuses, the
  answer is still `ERR:NO_LOS` or `ERR:ALREADY_FIRED`, as before.

Only integers are written into the Lua. `GameState.city_attack` itself is unchanged: this
replaces the builder it calls through the `civ_mcp.lua` package.
"""

from civ_mcp import lua as lq
from civ_mcp.lua._helpers import SENTINEL, _bail, _bail_lua, _lua_get_city


def build_city_attack(city_id: int, target_x: int, target_y: int) -> str:
    """InGame Lua that fires a city's ranged attack at a tile."""
    city_id, target_x, target_y = int(city_id), int(target_x), int(target_y)
    return f"""
{_lua_get_city(city_id)}
local cx, cy = pCity:GetX(), pCity:GetY()
local dist = Map.GetPlotDistance(cx, cy, {target_x}, {target_y})
local enemy = nil
local pu = Map.GetUnitsAt({target_x}, {target_y})
if pu then for other in pu:Units() do if other:GetOwner() ~= me then enemy = other end end end
if not enemy then {_bail("ERR:NO_ENEMY|No hostile unit at target tile")} end
local eInfo = GameInfo.Units[enemy:GetType()]
local eName = eInfo and eInfo.UnitType or "UNKNOWN"
local eHP = enemy:GetMaxDamage() - enemy:GetDamage()
local params = {{}}
params[CityCommandTypes.PARAM_X] = {target_x}
params[CityCommandTypes.PARAM_Y] = {target_y}
local ccIdx = GameInfo.Districts["DISTRICT_CITY_CENTER"].Index
local hasWalls = false
for _, d in pCity:GetDistricts():Members() do
    if d:GetType() == ccIdx then
        local wHP = d:GetMaxDamage(DefenseTypes.DISTRICT_OUTER)
        if wHP and wHP > 0 then hasWalls = true end
        break
    end
end
if not hasWalls then
    {_bail("ERR:NO_WALLS|City has no walls - build Ancient Walls first")}
end
if dist > 2 then
    {_bail_lua('"ERR:OUT_OF_RANGE|Target is " .. dist .. " tiles away (city attack range is 2)"')}
end
-- Only for wording the error: the shape of GetCommandTargets is not confirmed live.
local targetPlotIdx = {target_y} * Map.GetGridSize() + {target_x}
local inTargets = false
local totalTargets = 0
local validTargets = CityManager.GetCommandTargets(pCity, CityCommandTypes.RANGE_ATTACK)
if validTargets then
    for _, tbl in pairs(validTargets) do
        if type(tbl) == "table" then
            totalTargets = totalTargets + #tbl
            for _, idx in ipairs(tbl) do
                if idx == targetPlotIdx then inTargets = true end
            end
        end
    end
end
local canAttack = CityManager.CanStartCommand(pCity, CityCommandTypes.RANGE_ATTACK, true, params, false)
if not canAttack then
    if inTargets then
        {_bail("ERR:CANNOT_ATTACK|City cannot attack this target (unknown reason)")}
    elseif totalTargets == 0 then
        {_bail("ERR:ALREADY_FIRED|City already attacked this turn")}
    else
        {_bail_lua(f'"ERR:NO_LOS|Line of sight to ({target_x},{target_y}) is blocked from (" .. cx .. "," .. cy .. ")"')}
    end
end
CityManager.RequestCommand(pCity, CityCommandTypes.RANGE_ATTACK, params)
print("OK:CITY_RANGE_ATTACK|" .. Locale.Lookup(pCity:GetName()) .. " -> " .. eName .. "@{target_x},{target_y}|pre_hp:" .. eHP .. "/" .. enemy:GetMaxDamage())
print("{SENTINEL}")
"""


def fix_city_attack() -> None:
    """Make `GameState.city_attack` use the builder above. Raises if civ6-mcp has changed."""
    if not callable(getattr(lq, "build_city_attack", None)):
        raise RuntimeError("civ6-mcp has no build_city_attack to replace")
    from civ_mcp import game_state

    if game_state.lq is not lq:
        raise RuntimeError("civ6-mcp game_state no longer builds its Lua through civ_mcp.lua")
    lq.build_city_attack = build_city_attack  # type: ignore[attr-defined]
