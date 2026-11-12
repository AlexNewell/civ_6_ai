# ruff: noqa: E501  (the Lua in the query has long lines)
"""A working great-person advisor, replacing the one in civ6-mcp (issue 15).

civ6-mcp's `get_gp_advisor` reads `GameInfo.Units[...].GreatPersonClass`, a column this game
build doesn't have (it is nil), so the query bails with `ERR:UNKNOWN_GP_CLASS` for every great
person. The policies read that as "no district yet", and great people piled up unused (Run 5:
18 stacked in one city). It also looks only for the base district, so a civilization's unique
district (the Maya Observatory for the Campus, the German Hansa for the Industrial Zone) never
counted.

The query here finds the class through `GameInfo.GreatPersonClasses` (which names the unit type
and its district) and accepts the base district or any district that replaces it, per
`GameInfo.DistrictReplaces`. It answers in the same lines civ6-mcp's parser reads. Cultural
great-work slots aren't read: the policies don't use them, so they are reported as -1.
"""

from civ_mcp.game_state import GameState
from civ_mcp.lua import great_people as lua_gp
from civ_mcp.lua._helpers import SENTINEL, _bail, _lua_get_unit


def build_gp_advisor_query(unit_index: int) -> str:
    """InGame Lua that lists each city with a district the great person can be used in."""
    return f"""
{_lua_get_unit(unit_index)}
local uInfo = GameInfo.Units[unit:GetType()]
if not uInfo then {_bail("ERR:UNIT_INFO_NOT_FOUND")} end
local gpClass, baseDist = nil, nil
for row in GameInfo.GreatPersonClasses() do
    if row.UnitType == uInfo.UnitType then gpClass = row.GreatPersonClassType; baseDist = row.DistrictType end
end
if not gpClass then {_bail("ERR:NOT_A_GREAT_PERSON")} end
if not baseDist or not GameInfo.Districts[baseDist] then {_bail("ERR:DISTRICT_NOT_FOUND")} end
local candidates = {{ GameInfo.Districts[baseDist].Index }}
for row in GameInfo.DistrictReplaces() do
    if row.ReplacesDistrictType == baseDist and GameInfo.Districts[row.CivUniqueDistrictType] then
        candidates[#candidates + 1] = GameInfo.Districts[row.CivUniqueDistrictType].Index
    end
end
local classToYield = {{
    GREAT_PERSON_CLASS_SCIENTIST = "YIELD_SCIENCE",
    GREAT_PERSON_CLASS_ENGINEER = "YIELD_PRODUCTION",
    GREAT_PERSON_CLASS_MERCHANT = "YIELD_GOLD",
    GREAT_PERSON_CLASS_WRITER = "YIELD_CULTURE",
    GREAT_PERSON_CLASS_ARTIST = "YIELD_CULTURE",
    GREAT_PERSON_CLASS_MUSICIAN = "YIELD_CULTURE",
    GREAT_PERSON_CLASS_PROPHET = "YIELD_FAITH",
    GREAT_PERSON_CLASS_GENERAL = "YIELD_PRODUCTION",
    GREAT_PERSON_CLASS_ADMIRAL = "YIELD_GOLD",
}}
local charges = -1
local gp = unit:GetGreatPerson()
if gp then pcall(function() charges = gp:GetActionCharges() end) end
print("GP_INFO|" .. Locale.Lookup(unit:GetName()) .. "|" .. gpClass .. "|" .. baseDist .. "|" .. unit:GetX() .. "|" .. unit:GetY() .. "|" .. charges)
local validPlotSet = {{}}
if gp then
    pcall(function()
        local plots = gp:GetActivationHighlightPlots()
        if plots then
            for _, pIdx in ipairs(plots) do validPlotSet[pIdx] = true end
        end
    end)
end
-- A Great Engineer is used on a wonder (or district) being built, not in its own district. The game
-- lists those tiles as its highlight plots, so they are the only places it offers.
if gpClass == "GREAT_PERSON_CLASS_ENGINEER" then
    for plotIdx, _ in pairs(validPlotSet) do
        pcall(function()
            local plot = Map.GetPlotByIndex(plotIdx)
            local x, y = plot:GetX(), plot:GetY()
            local dist = Map.GetPlotDistance(unit:GetX(), unit:GetY(), x, y)
            local owner = ""
            pcall(function() owner = Locale.Lookup(Cities.GetPlotPurchaseCity(x, y):GetName()) end)
            print("GP_CITY|" .. (owner:gsub("|", "/")) .. "|-1|" .. x .. "|" .. y .. "|true|" .. dist .. "|0|-1|-1")
        end)
    end
    print("{SENTINEL}")
    do return end
end
local yieldIdx = -1
local yieldType = classToYield[gpClass]
if yieldType then pcall(function() yieldIdx = GameInfo.Yields[yieldType].Index end) end
for i, city in Players[me]:GetCities():Members() do
    pcall(function()
        local districts = city:GetDistricts()
        for _, distIdx in ipairs(candidates) do
            if districts:HasDistrict(distIdx, true) then
                local dObj = districts:GetDistrict(distIdx)
                if dObj then
                    local dx, dy = dObj:GetX(), dObj:GetY()
                    local plotIdx = Map.GetPlot(dx, dy):GetIndex()
                    local dist = Map.GetPlotDistance(unit:GetX(), unit:GetY(), dx, dy)
                    local cityYield = 0
                    if yieldIdx >= 0 then pcall(function() cityYield = city:GetYield(yieldIdx) end) end
                    local cn = (Locale.Lookup(city:GetName()):gsub("|", "/"))
                    print("GP_CITY|" .. cn .. "|" .. city:GetID() .. "|" .. dx .. "|" .. dy .. "|" .. tostring(validPlotSet[plotIdx] == true) .. "|" .. dist .. "|" .. cityYield .. "|-1|-1")
                    break
                end
            end
        end
    end)
end
print("{SENTINEL}")
"""


async def get_gp_advisor(self: GameState, unit_index: int):
    """Stands in for `GameState.get_gp_advisor`."""
    lines = await self.conn.execute_write(build_gp_advisor_query(unit_index))
    return lua_gp.parse_gp_advisor_response(lines)


def fix_gp_advisor() -> None:
    """Replace civ6-mcp's advisor with the one above. Raises if civ6-mcp no longer has it."""
    if not callable(getattr(GameState, "get_gp_advisor", None)) or not callable(
        getattr(lua_gp, "parse_gp_advisor_response", None)
    ):
        raise RuntimeError("civ6-mcp has no get_gp_advisor or parser to build on")
    GameState.get_gp_advisor = get_gp_advisor  # type: ignore[method-assign]
