# ruff: noqa: E501  (the Lua has long lines)
"""Placement by cause: why a district or wonder could not be placed, and what would change it.

PR 193 answered a refusal with a timer (15 turns doubling to 120 for `NO_PLACEMENT`, 10 doubling
to 80 for `CANNOT_START`). That waits too long for a chance that opens in a turn or two, and it
never acted. This module replaces the timer with three things (issues 187 and 149):

1. **A signature of the city's placement state.** One batched Lua read answers for every city: a
   hash over the owned plots within three tiles, with each plot's feature, improvement and
   district. A refusal is remembered with the signature it was made under, and asked again as
   soon as the signature changes (border growth, a tile bought, a feature cleared, a district
   placed or finished). The read is made only when a city is choosing and there is something to
   check, and at most once a turn.
2. **A reason for a `CANNOT_START`.** The reply's hint is a guess, so one small Lua read asks the
   game itself (`CanProduce`, the item's tech, civic and district prerequisites) and sorts the
   refusal into a class, see `classify`.
3. **Chained actions.** When no owned tile is valid, or a tile the city could buy gives clearly
   better adjacency, `Planner` finds the tile (every tile within three of the city that it does
   not own, tested against the game's own placement data), buys it when gold allows, and then the
   caller places the district or wonder. When gold doesn't allow, the plan is handed to the gold
   policy as a saving target (`PlanBook`); when the tile is not buyable yet, it waits for the
   border (the signature then changes).

Each chain logs `placement_plan city=... item=... tile=(x,y) action=... cost=...`.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from civ_mcp.lua._helpers import SENTINEL, _bail, _lua_get_city

log = logging.getLogger(__name__)

SAVE_TURNS = 8  # Most turns to save for a tile. The gold policy's own bound, kept in step.
MIN_BETTER_ADJACENCY = 2  # A tile to buy must beat the best owned one by this much.
GOLD_PER_ADJACENCY_POINT = 25  # Most gold worth paying per net point gained. See `gold_cap`.
NO_TILE_COST_CAP = 200  # Most worth paying for a tile when it is the only way to place the item.
GAME_SPEED_COST_SCALE = 1.0  # Standard speed. Gold costs scale with the game speed (marathon 3x).
MIN_TURNS_LEFT = 40  # A tile bought for adjacency needs this many turns left to pay back.
DEFAULT_LAST_TURN = 400  # The game's last turn when none is known: standard runs end near 325.
RESERVE_TURNS = 30  # How long a chosen tile is held back from other items in the same city.
MAX_PLAN_BUYS = 2  # Tiles bought for one city and item, ever, so a wrong rule cannot drain gold.
PLAN_RADIUS = 3  # A district must be within this many tiles of its city.
PLAN_MAX_AGE = 12  # Turns a saving target is kept without being renewed.
UNKNOWN_WAIT_TURNS = 3  # The most an unknown refusal is waited out without a change to go on.
MAX_UNCHANGED_REFUSALS = 3  # Refusals in a row under one signature before only a change ends it.
SAFETY_RECHECK_TURNS = 50  # A last-resort re-ask for a refusal the signature cannot show ending.

# The districts whose adjacency the advisor (and so the planner) scores. Others have no gain to buy.
ADJACENCY_DISTRICTS = frozenset(
    {
        "DISTRICT_CAMPUS",
        "DISTRICT_HOLY_SITE",
        "DISTRICT_INDUSTRIAL_ZONE",
        "DISTRICT_COMMERCIAL_HUB",
        "DISTRICT_THEATER",
        "DISTRICT_HARBOR",
    }
)

# What a refusal is down to. `Diagnosis.reason`, and the `reason` of `production_refused`.
PLACEMENT = "placement"  # No tile will do: ask again when the city's tiles change.
UNLOCK = "unlock"  # A tech or civic is missing: ask again when it lands.
ELSEWHERE = "elsewhere"  # The wonder is built or under way somewhere else: drop it.
DISTRICT = "district"  # A prerequisite district is missing or pillaged: ask again when it changes.
UNKNOWN = "unknown"  # Nothing says why: a short wait, then only a change.
OTHER = "other"  # A failure that is not about placement (a flaky set).


def _water_names() -> tuple[str, ...]:
    from civ6_ai.district_advisor import WATER_DISTRICTS

    return WATER_DISTRICTS


# ---- Lua ------------------------------------------------------------------------------------


def build_signatures_query(watch: tuple[str, ...] = ()) -> str:
    """InGame Lua: a placement signature for each of the player's cities, and unlock flags.

    The hash covers every plot within `PLAN_RADIUS` of the city that the player owns, with its
    feature, improvement and district, so it changes when the borders grow or a tile is bought,
    cleared, improved or built on. `watch` names techs and civics whose ownership is answered too.
    """
    watched = ", ".join(f'"{name}"' for name in watch if name.replace("_", "").isalnum())
    return f"""
local me = Game.GetLocalPlayer()
local R = {PLAN_RADIUS}
for _, city in Players[me]:GetCities():Members() do
    local cx, cy = city:GetX(), city:GetY()
    local h, n = 17, 0
    for dy = -R, R do for dx = -R, R do
        local plot = Map.GetPlot(cx + dx, cy + dy)
        if plot and plot:GetOwner() == me and Map.GetPlotDistance(cx, cy, cx + dx, cy + dy) <= R then
            n = n + 1
            local v = ((plot:GetIndex() * 7 + plot:GetFeatureType() + 2) * 13 + plot:GetImprovementType() + 2) * 11 + plot:GetDistrictType() + 2
            h = (h * 31 + v) % 2147483629
        end
    end end
    print("PSIG|" .. city:GetID() .. "|" .. h .. "|" .. n)
end
local pTech, pCult = Players[me]:GetTechs(), Players[me]:GetCulture()
for _, name in ipairs({{{watched}}}) do
    local has = 0
    pcall(function()
        if GameInfo.Technologies[name] then
            if pTech:HasTech(GameInfo.Technologies[name].Index) then has = 1 end
        elseif GameInfo.Civics[name] and pCult:HasCivic(GameInfo.Civics[name].Index) then
            has = 1
        end
    end)
    print("PUNLOCK|" .. name .. "|" .. has)
end
print("{SENTINEL}")
"""


def parse_signatures(lines: list[str]) -> tuple[dict[int, str], dict[str, bool]]:
    """City id to signature, and watched unlock name to whether the player has it."""
    signatures: dict[int, str] = {}
    unlocks: dict[str, bool] = {}
    for line in lines:
        parts = line.strip().split("|")
        try:
            if parts[0] == "PSIG" and len(parts) >= 4:
                signatures[int(parts[1])] = f"{parts[2]}:{parts[3]}"
            elif parts[0] == "PUNLOCK" and len(parts) >= 3:
                unlocks[parts[1]] = parts[2] == "1"
        except ValueError:
            continue  # A stray log line.
    return signatures, unlocks


def _table(category: str) -> str:
    return "Districts" if category == "DISTRICT" else "Buildings"


def build_diagnosis_query(city_id: int, category: str, item: str) -> str:
    """InGame Lua: what stops the city starting `item`, from the game's own checks."""
    table = _table(category)
    safe = "".join(ch for ch in item if ch.isalnum() or ch == "_")
    district_check = (
        ""
        if category == "DISTRICT"
        else """
if row.PrereqDistrict then
    local d = GameInfo.Districts[row.PrereqDistrict]
    if d and not pCity:GetDistricts():HasDistrict(d.Index, true) then missingDistrict = row.PrereqDistrict end
end"""
    )
    return f"""
{_lua_get_city(city_id)}
local row = GameInfo.{table}["{safe}"]
if row == nil then {_bail("PDIAG|NOT_FOUND")} end
local pTech, pCult = Players[me]:GetTechs(), Players[me]:GetCulture()
local canProduce = pCity:GetBuildQueue():CanProduce(row.Hash, true)
local missingTech, missingCivic, missingDistrict = "", "", ""
if row.PrereqTech then
    local t = GameInfo.Technologies[row.PrereqTech]
    if t and not pTech:HasTech(t.Index) then missingTech = row.PrereqTech end
end
if row.PrereqCivic then
    local c = GameInfo.Civics[row.PrereqCivic]
    if c and not pCult:HasCivic(c.Index) then missingCivic = row.PrereqCivic end
end{district_check}
print("PDIAG|" .. (canProduce and 1 or 0) .. "|" .. missingTech .. "|" .. missingCivic .. "|" .. missingDistrict)
print("{SENTINEL}")
"""


@dataclass(frozen=True)
class Diagnosis:
    """What the game says stops a city starting an item."""

    can_produce: bool
    missing_tech: str = ""
    missing_civic: str = ""
    missing_district: str = ""

    @property
    def reason(self) -> str:
        if self.missing_tech or self.missing_civic:
            return UNLOCK
        if self.missing_district:
            return DISTRICT
        if not self.can_produce:
            return ELSEWHERE  # Everything it needs is held, yet the game won't produce it.
        return PLACEMENT  # It can be produced: only the chosen tile was refused.

    @property
    def needs(self) -> str | None:
        return self.missing_tech or self.missing_civic or None


def parse_diagnosis(lines: list[str]) -> Diagnosis | None:
    for line in lines:
        parts = line.strip().split("|")
        if parts[0] == "PDIAG" and len(parts) >= 5:
            return Diagnosis(parts[1] == "1", parts[2], parts[3], parts[4])
    return None


def classify(reply: str, diagnosis: Diagnosis | None) -> tuple[str, str | None]:
    """The reason class of a refusal, and the unlock (a tech or civic) it waits for, if any."""
    if "NO_PLACEMENT" in reply:
        return PLACEMENT, None
    if "CANNOT_START" in reply or "CANNOT_PRODUCE" in reply:
        if "pillaged" in reply:
            return DISTRICT, None
        if diagnosis is None:
            return UNKNOWN, None
        return diagnosis.reason, diagnosis.needs
    return OTHER, None


_ADJACENCY_LUA = """
local function adjacencyOf(plot, dType)
    local px, py = plot:GetX(), plot:GetY()
    local mountains, jungles, forests, rivers, wonders = 0, 0, 0, 0, 0
    local mines, quarries, harbors, aqueducts, ent, geo, reefs, nat, sea = 0, 0, 0, 0, 0, 0, 0, 0, 0
    if plot:IsRiver() then rivers = 1 end
    for d = 0, 5 do
        local adj = Map.GetAdjacentPlot(px, py, d)
        if adj then
            if adj:IsMountain() then mountains = mountains + 1 end
            local feat = adj:GetFeatureType()
            if feat >= 0 then
                local fInfo = GameInfo.Features[feat]
                if fInfo then
                    local fn = fInfo.FeatureType
                    if fn == "FEATURE_JUNGLE" then jungles = jungles + 1
                    elseif fn == "FEATURE_FOREST" then forests = forests + 1
                    elseif fn == "FEATURE_GEOTHERMAL_FISSURE" then geo = geo + 1
                    elseif fn == "FEATURE_REEF" then reefs = reefs + 1
                    elseif fInfo.NaturalWonder then nat = nat + 1 end
                end
            end
            local distId = adj:GetDistrictType()
            if distId >= 0 then
                local dInfo = GameInfo.Districts[distId]
                if dInfo then
                    local dn = dInfo.DistrictType
                    if dn == "DISTRICT_HARBOR" then harbors = harbors + 1
                    elseif dn == "DISTRICT_AQUEDUCT" then aqueducts = aqueducts + 1
                    elseif dn == "DISTRICT_ENTERTAINMENT_COMPLEX" or dn == "DISTRICT_WATER_ENTERTAINMENT_COMPLEX" then ent = ent + 1 end
                end
            end
            local imp = adj:GetImprovementType()
            if imp >= 0 then
                local iInfo = GameInfo.Improvements[imp]
                if iInfo then
                    if iInfo.ImprovementType == "IMPROVEMENT_MINE" then mines = mines + 1
                    elseif iInfo.ImprovementType == "IMPROVEMENT_QUARRY" then quarries = quarries + 1 end
                end
            end
            local res = adj:GetResourceType()
            if res >= 0 and adj:IsWater() then sea = sea + 1 end
            if adj:GetWonderType() >= 0 then wonders = wonders + 1 end
        end
    end
    if dType == "DISTRICT_CAMPUS" then return mountains + math.floor(jungles / 2) + geo * 2 + reefs * 2 + nat * 2
    elseif dType == "DISTRICT_HOLY_SITE" then return mountains + math.floor(forests / 2) + nat * 2
    elseif dType == "DISTRICT_INDUSTRIAL_ZONE" then return mines + quarries + aqueducts * 2
    elseif dType == "DISTRICT_COMMERCIAL_HUB" then return (rivers > 0 and 2 or 0) + harbors * 2
    elseif dType == "DISTRICT_THEATER" then return wonders + ent * 2
    elseif dType == "DISTRICT_HARBOR" then return sea end
    return 0
end
"""


def build_plan_query(
    city_id: int, category: str, item: str, owned: tuple[tuple[int, int], ...] = ()
) -> str:
    """InGame Lua: the tiles where `item` could go, and a check of the rules against the game.

    `owned` is what the game's own advisor offered, so the rules used here can be tested against
    it. Each line is one of:

    - `PTILE|x|y|cost|adjacency|displacement`: a tile within three of the city that nobody owns
      and that passes the game's placement data for the item (terrain and feature tables, coast
      and river needs, an adjacent district, no district or wonder on it). Cost is -1 when it
      cannot be bought yet. The adjacency is computed as the district advisor does.
    - `POWN|x|y|displacement|predicted`: a tile in `owned`, with 1 if the rules here agree it is
      valid.
    - `PMISS|x|y`: an owned plot nearest this city that the rules call valid and the game did not
      offer. Any `PMISS`, or a `POWN` that is not predicted, means the rules cannot be trusted.

    The displacement is a rough cost of building on the tile: 3 for a luxury or strategic
    resource, 1 for another resource, 1 for an improvement, 1 for a high-yield tile.
    """
    district = category == "DISTRICT"
    table = "Districts" if district else "Buildings"
    column = "DistrictType" if district else "BuildingType"
    terrains = "District_ValidTerrains" if district else "Building_ValidTerrains"
    features = "District_ValidFeatures" if district else "Building_ValidFeatures"
    safe = "".join(ch for ch in item if ch.isalnum() or ch == "_")
    names = ", ".join(f'["{n}"] = true' for n in _water_names())
    given = ", ".join(f'["{int(x)},{int(y)}"] = true' for x, y in owned)
    return f"""
{_lua_get_city(city_id)}
local row = GameInfo.{table}["{safe}"]
if row == nil then {_bail("PTILE_ERR|NOT_FOUND")} end
local itemType = row.{column}
local isDistrict = {"true" if district else "false"}
{_ADJACENCY_LUA}
local function displacementOf(plot)
    local d = 0
    local res = plot:GetResourceType()
    if res >= 0 then
        local rInfo = GameInfo.Resources[res]
        if rInfo and (rInfo.ResourceClassType == "RESOURCECLASS_LUXURY" or rInfo.ResourceClassType == "RESOURCECLASS_STRATEGIC") then d = d + 3 else d = d + 1 end
    end
    if plot:GetImprovementType() >= 0 then d = d + 1 end
    pcall(function()
        if plot:GetYield(YieldTypes.FOOD) >= 3 or plot:GetYield(YieldTypes.PRODUCTION) >= 3 then d = d + 1 end
    end)
    return d
end
local terrains, features, nRules = {{}}, {{}}, 0
pcall(function() for r in GameInfo.{terrains}() do if r.{column} == itemType then terrains[r.TerrainType] = true; nRules = nRules + 1 end end end)
pcall(function() for r in GameInfo.{features}() do if r.{column} == itemType then features[r.FeatureType] = true; nRules = nRules + 1 end end end)
local waterNames = {{{names}}}
local isWater = isDistrict and ((row.Aquatic == true) or waterNames[itemType] == true)
local needsRiver = (row.RequiresRiver == true)
local needsCoast = (row.Coast == true) and not isWater
local needDistrict = row.AdjacentDistrict
local cx, cy = pCity:GetX(), pCity:GetY()
local given = {{{given}}}
local buyable = {{}}
local targets = CityManager.GetCommandTargets(pCity, CityCommandTypes.PURCHASE, {{[CityCommandTypes.PARAM_PLOT_PURCHASE] = 1}})
if targets then
    for _, v in pairs(targets) do
        if type(v) == "table" then for _, idx in ipairs(v) do buyable[idx] = true end end
    end
end
local function valid(plot)
    if plot:GetDistrictType() >= 0 or plot:GetWonderType() >= 0 then return false end
    local feat = plot:GetFeatureType()
    local fType = nil
    if feat >= 0 then
        local fInfo = GameInfo.Features[feat]
        if fInfo then
            if fInfo.NaturalWonder then return false end
            fType = fInfo.FeatureType
        end
    end
    if isDistrict and plot:IsWater() ~= isWater then return false end
    if not isWater and nRules == 0 and (plot:IsWater() or plot:IsImpassable() or plot:IsMountain()) then return false end
    if isWater and not (plot:GetTerrainType() >= 0 and GameInfo.Terrains[plot:GetTerrainType()].TerrainType == "TERRAIN_COAST") then return false end
    if nRules > 0 then
        local terr = GameInfo.Terrains[plot:GetTerrainType()]
        if not ((terr and terrains[terr.TerrainType]) or (fType and features[fType])) then return false end
    end
    if needsRiver and not plot:IsRiver() then return false end
    if needsCoast and not plot:IsCoastalLand() then return false end
    if needDistrict then
        local found = false
        for d = 0, 5 do
            local adj = Map.GetAdjacentPlot(plot:GetX(), plot:GetY(), d)
            if adj and adj:GetDistrictType() >= 0 then
                local dInfo = GameInfo.Districts[adj:GetDistrictType()]
                if dInfo and dInfo.DistrictType == needDistrict then found = true end
            end
        end
        if not found then return false end
    end
    return true
end
local function nearestOther(x, y)
    local best = Map.GetPlotDistance(cx, cy, x, y)
    for _, c in Players[me]:GetCities():Members() do
        if c:GetID() ~= pCity:GetID() and Map.GetPlotDistance(c:GetX(), c:GetY(), x, y) < best then return true end
    end
    return false
end
for dy = -{PLAN_RADIUS}, {PLAN_RADIUS} do for dx = -{PLAN_RADIUS}, {PLAN_RADIUS} do
    local x, y = cx + dx, cy + dy
    local dist = Map.GetPlotDistance(cx, cy, x, y)
    local plot = Map.GetPlot(x, y)
    if plot and dist >= 1 and dist <= {PLAN_RADIUS} then
        local owner = plot:GetOwner()
        local key = x .. "," .. y
        if owner < 0 and valid(plot) then
            local cost = -1
            if buyable[plot:GetIndex()] then cost = pCity:GetGold():GetPlotPurchaseCost(x, y) end
            local adjacency = 0
            if isDistrict then adjacency = adjacencyOf(plot, itemType) end
            print("PTILE|" .. x .. "|" .. y .. "|" .. cost .. "|" .. adjacency .. "|" .. displacementOf(plot))
        elseif owner == me then
            local ok = valid(plot)
            if given[key] then
                print("POWN|" .. x .. "|" .. y .. "|" .. displacementOf(plot) .. "|" .. (ok and 1 or 0))
            elseif ok and not nearestOther(x, y) then
                print("PMISS|" .. x .. "|" .. y)
            end
        end
    end
end end
print("{SENTINEL}")
"""


@dataclass(frozen=True)
class PlanTile:
    x: int
    y: int
    cost: int  # Gold to buy it now, or -1 when it cannot be bought yet.
    adjacency: int
    displacement: int = 0

    @property
    def net(self) -> int:
        return self.adjacency - self.displacement


@dataclass(frozen=True)
class OwnedTile:
    """A tile the game offered, with its adjacency (the game's) and displacement (ours)."""

    x: int
    y: int
    adjacency: int
    displacement: int = 0

    @property
    def net(self) -> int:
        return self.adjacency - self.displacement


@dataclass
class PlanRead:
    tiles: list[PlanTile] = field(default_factory=list)
    displacement: dict[tuple[int, int], int] = field(default_factory=dict)  # Owned, offered.
    unpredicted: list[tuple[int, int]] = field(default_factory=list)  # Offered, rules said no.
    missed: list[tuple[int, int]] = field(default_factory=list)  # Rules said yes, not offered.

    @property
    def agrees(self) -> bool:
        return not self.unpredicted and not self.missed


def parse_plan(lines: list[str]) -> PlanRead:
    read = PlanRead()
    for line in lines:
        parts = line.strip().split("|")
        try:
            if parts[0] == "PTILE" and len(parts) >= 6:
                read.tiles.append(
                    PlanTile(
                        int(parts[1]),
                        int(parts[2]),
                        int(float(parts[3])),
                        int(parts[4]),
                        int(parts[5]),
                    )
                )
            elif parts[0] == "POWN" and len(parts) >= 5:
                at = (int(parts[1]), int(parts[2]))
                read.displacement[at] = int(parts[3])
                if parts[4] != "1":
                    read.unpredicted.append(at)
            elif parts[0] == "PMISS" and len(parts) >= 3:
                read.missed.append((int(parts[1]), int(parts[2])))
        except ValueError:
            continue  # A stray log line.
    return read


# ---- Per-turn reads --------------------------------------------------------------------------


def _conn(game: Any) -> Any:
    return getattr(game, "conn", None)


class PlacementReads:
    """The placement reads of one turn, each made at most once and only when asked for.

    A fake or half-working game that has no connection answers `None` everywhere, which callers
    treat as "no signature": they then fall back to a short wait instead of a change.
    """

    def __init__(self, game: Any, watch: tuple[str, ...] = ()) -> None:
        self._game = game
        self._watch = watch
        self._loaded = False
        self._signatures: dict[int, str] = {}
        self._unlocks: dict[str, bool] = {}

    def refresh(self) -> None:
        """Forget the signatures, after something (a tile bought) changed them."""
        self._loaded = False

    async def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        conn = _conn(self._game)
        if conn is None:
            return
        try:
            lines = await conn.execute_write(build_signatures_query(self._watch))
        except ConnectionError:
            raise
        except Exception:
            log.exception("placement signature read failed")
            return
        self._signatures, self._unlocks = parse_signatures(list(lines))

    async def signature(self, city_id: int) -> str | None:
        await self._load()
        return self._signatures.get(city_id % 65536) or self._signatures.get(city_id)

    async def unlocked(self, name: str) -> bool:
        await self._load()
        return self._unlocks.get(name, False)

    async def diagnose(self, city_id: int, category: str, item: str) -> Diagnosis | None:
        conn = _conn(self._game)
        if conn is None:
            return None
        try:
            return parse_diagnosis(
                list(await conn.execute_write(build_diagnosis_query(city_id, category, item)))
            )
        except ConnectionError:
            raise
        except Exception:
            log.exception("refusal diagnosis failed for %s", item)
            return None

    async def plan_read(
        self, city_id: int, category: str, item: str, owned: tuple[tuple[int, int], ...]
    ) -> PlanRead | None:
        conn = _conn(self._game)
        if conn is None:
            return None
        try:
            lines = await conn.execute_write(build_plan_query(city_id, category, item, owned))
            return parse_plan(list(lines))
        except ConnectionError:
            raise
        except Exception:
            log.exception("placement plan read failed for %s", item)
            return None


# ---- Chained actions -------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    action: str  # buy_then_place, save or wait_for_border.
    x: int
    y: int
    cost: int


def gold_cap(gain: int | None) -> int:
    """The most worth paying for a tile: `GOLD_PER_ADJACENCY_POINT` for each net point gained.

    With no owned tile (`gain` None) the district or wonder itself is the gain, capped at
    `NO_TILE_COST_CAP`. Both scale with the game speed.
    """
    scale = GAME_SPEED_COST_SCALE
    if gain is None:
        return int(NO_TILE_COST_CAP * scale)
    return int(GOLD_PER_ADJACENCY_POINT * scale * max(gain, 0))


def decide(
    tiles: list[PlanTile], owned: list[OwnedTile], budget: int, income: float
) -> Plan | None:
    """What to do about the tiles the city does not own yet. `owned` holds available tiles only.

    With an owned tile, a tile is bought only when its net value (adjacency minus displacement)
    beats the best owned one by `MIN_BETTER_ADJACENCY`, the gold is within `gold_cap` of that
    gain, and it is affordable now. Equal or better owned value means no buy, and a small gain is
    not worth holding gold back for, so there is no saving. With no owned tile, the best buyable
    tile is bought if it fits the budget and `NO_TILE_COST_CAP`; one that fits only after saving
    becomes a saving target when income closes the gap within `SAVE_TURNS`; a tile that cannot be
    bought yet is waited for (the border must grow, which changes the signature).
    """
    if not tiles:
        return None
    buyable = sorted((t for t in tiles if t.cost > 0), key=lambda t: (-t.net, t.cost, t.x, t.y))
    if owned:
        best = max(t.net for t in owned)
        for tile in buyable:
            gain = tile.net - best
            if gain >= MIN_BETTER_ADJACENCY and tile.cost <= min(budget, gold_cap(gain)):
                return Plan("buy_then_place", tile.x, tile.y, tile.cost)
        return None
    cap = gold_cap(None)
    worth = [t for t in buyable if t.cost <= cap]
    if worth:
        tile = worth[0]
        if tile.cost <= budget:
            return Plan("buy_then_place", tile.x, tile.y, tile.cost)
        for tile in sorted(worth, key=lambda t: (t.cost, -t.net, t.x, t.y)):
            if income > 0 and (tile.cost - budget) / income <= SAVE_TURNS:
                return Plan("save", tile.x, tile.y, tile.cost)
    waiting = sorted((t for t in tiles if t.cost <= 0), key=lambda t: (-t.net, t.x, t.y))
    if waiting:
        return Plan("wait_for_border", waiting[0].x, waiting[0].y, 0)
    return None


@dataclass
class SavingTarget:
    """A tile the production policy wants, handed to the gold policy to buy or save for."""

    city_id: int
    city_name: str
    item: str
    x: int
    y: int
    cost: int
    turn: int


@dataclass
class PlanBook:
    """Saving targets shared by production (which adds them) and gold (which buys them)."""

    targets: dict[tuple[int, str], SavingTarget] = field(default_factory=dict)

    def add(self, target: SavingTarget) -> None:
        self.targets[(target.city_id, target.item)] = target

    def drop(self, city_id: int, item: str) -> None:
        self.targets.pop((city_id, item), None)

    def claimed_by_others(self, city_id: int) -> set[tuple[int, int]]:
        return {(t.x, t.y) for t in self.targets.values() if t.city_id != city_id}

    def current(self, turn: int) -> list[SavingTarget]:
        """The live targets, cheapest first. Targets not renewed for `PLAN_MAX_AGE` turns lapse."""
        for key in [k for k, t in self.targets.items() if turn - t.turn > PLAN_MAX_AGE]:
            del self.targets[key]
        return sorted(self.targets.values(), key=lambda t: (t.cost, t.city_id, t.item))


class Planner:
    """Finds, buys for and records the chained actions. One per game; `begin_turn` each turn.

    A tile is bought for item X in city C only when every rule in `consider` holds. It is asked
    only for the item production chose this turn, never for one that is merely wanted.
    """

    def __init__(
        self,
        book: PlanBook | None = None,
        last_turn: int = DEFAULT_LAST_TURN,
        emit: Callable[[Any], None] | None = None,
    ) -> None:
        self.emit = emit  # Receives a `construction.PlacementPlan` for each plan line.
        self._category = "DISTRICT"
        self.book = book if book is not None else PlanBook()
        self.last_turn = last_turn
        self.budget = 0
        self.income = 0.0
        self.turn = 0
        self._buys: dict[tuple[int, str], int] = {}
        self._bought_turn: dict[int, int] = {}  # City -> the turn it last bought a tile.
        self._bought_at: tuple[int, int, int] = (0, 0, 0)
        self._claimed: dict[tuple[int, int], int] = {}  # Tile -> city, bought this turn.
        self._failed: dict[tuple[int, str], str | None] = {}  # Buy that did not place: signature.
        self._reserved: dict[int, dict[tuple[int, int], tuple[str, int]]] = {}
        self._logged: dict[tuple[int, str], tuple[str, int, int, int]] = {}

    def begin_turn(self, turn: int, budget: int, income: float) -> None:
        self.turn, self.budget, self.income = turn, max(0, budget), income
        self._claimed = {}

    def reserve(self, city_id: int, item: str, x: int, y: int) -> None:
        """Production picked tile (x, y) for `item`: it is not available to another item."""
        held = self._reserved.setdefault(city_id, {})
        for at in [a for a, (it, _) in held.items() if it == item]:
            del held[at]
        held[(x, y)] = (item, self.turn)

    def _available(self, city_id: int, item: str, offered: list[OwnedTile]) -> list[OwnedTile]:
        held = self._reserved.get(city_id, {})
        for at in [a for a, (_, t) in held.items() if self.turn - t > RESERVE_TURNS]:
            del held[at]
        return [t for t in offered if held.get((t.x, t.y), (item, 0))[0] == item]

    async def consider(
        self,
        game: Any,
        reads: PlacementReads,
        city: Any,
        category: str,
        item: str,
        offered: list[OwnedTile],
    ) -> bool:
        """Plan for the chosen district or wonder, buying a tile if every rule allows.

        Returns True if a tile was bought. `offered` is what the game's advisor offered. The rules,
        in order:

        - one tile per city per turn, and at most `MAX_PLAN_BUYS` per city and item;
        - not within `MIN_TURNS_LEFT` turns of the end of the game;
        - not after a bought tile failed to take this item, until the city's signature changes;
        - an owned tile that is available (not reserved for another item) means no buy for an item
          with no adjacency to gain;
        - the rules used to predict placement must agree with the game on the owned tiles;
        - then `decide`: net value, the +2 margin, the gold cap, the budget.

        A tile another city has claimed this turn or is saving for is not considered. The caller
        asks the advisor again after a purchase.
        """
        key = (city.city_id, item)
        self._category = category
        if self._buys.get(key, 0) >= MAX_PLAN_BUYS:
            return False
        if self._bought_turn.get(city.city_id) == self.turn:
            return False
        if self.turn > self.last_turn - MIN_TURNS_LEFT:
            return False
        if key in self._failed:
            if await reads.signature(city.city_id) == self._failed[key]:
                return False
            del self._failed[key]
        available = self._available(city.city_id, item, offered)
        if available and item not in ADJACENCY_DISTRICTS:
            self.book.drop(city.city_id, item)
            return False  # It has a tile, and a better one only counts for adjacency.
        read = await reads.plan_read(
            city.city_id, category, item, tuple((t.x, t.y) for t in offered)
        )
        if read is None:
            return False
        if not read.agrees:
            log.info(
                "placement_plan city=%s item=%s action=rules_mismatch unpredicted=%s missed=%s",
                city.name,
                item,
                read.unpredicted[:5],
                read.missed[:5],
            )
            self._announce(city, item, "rules_mismatch", None, None, 0)
            return False
        owned = [replace(t, displacement=read.displacement.get((t.x, t.y), 0)) for t in available]
        taken = {*self._claimed, *self.book.claimed_by_others(city.city_id)}
        tiles = [t for t in read.tiles if (t.x, t.y) not in taken]
        plan = decide(tiles, owned, self.budget, self.income)
        if plan is None or plan.action != "save":
            self.book.drop(city.city_id, item)  # An older saving target no longer applies.
        if plan is None:
            return False
        self._log(city, item, plan)
        if plan.action == "buy_then_place":
            result = await game.purchase_tile(city.city_id, plan.x, plan.y)
            if isinstance(result, str) and "TILE_PURCHASED" in result:
                self.budget -= plan.cost
                self._buys[key] = self._buys.get(key, 0) + 1
                self._bought_turn[city.city_id] = self.turn
                self._claimed[(plan.x, plan.y)] = city.city_id
                self._bought_at = (plan.x, plan.y, plan.cost)
                self.book.drop(city.city_id, item)
                reads.refresh()
                return True
            log.info(
                "placement_plan city=%s item=%s tile=(%d,%d) action=purchase_refused cost=%d: %s",
                city.name,
                item,
                plan.x,
                plan.y,
                plan.cost,
                str(result)[:100],
            )
            return False
        if plan.action == "save":
            self.book.add(
                SavingTarget(city.city_id, city.name, item, plan.x, plan.y, plan.cost, self.turn)
            )
        return False

    async def placed(
        self, reads: PlacementReads, city: Any, item: str, offered: list[OwnedTile]
    ) -> bool:
        """After a buy: did the game offer the bought tile? If not, no more buys for this pair."""
        x, y, cost = self._bought_at
        if any((t.x, t.y) == (x, y) for t in offered):
            return True
        self._failed[(city.city_id, item)] = await reads.signature(city.city_id)
        log.info(
            "placement_plan city=%s item=%s tile=(%d,%d) action=buy_failed_place cost=%d",
            city.name,
            item,
            x,
            y,
            cost,
        )
        self._announce(city, item, "buy_failed_place", x, y, cost)
        return False

    def _announce(
        self, city: Any, item: str, action: str, x: int | None, y: int | None, cost: int
    ) -> None:
        if self.emit is None:
            return
        from civ6_ai.construction import PlacementPlan

        self.emit(
            PlacementPlan(
                self.turn,
                city.city_id,
                city.name,
                city.x,
                city.y,
                item=item,
                category=self._category,
                action=action,
                tile_x=x,
                tile_y=y,
                cost=cost,
            )
        )

    def _log(self, city: Any, item: str, plan: Plan) -> None:
        """Log a plan, once while it stays the same."""
        shape = (plan.action, plan.x, plan.y, plan.cost)
        key = (city.city_id, item)
        if self._logged.get(key) == shape and plan.action != "buy_then_place":
            return
        self._logged[key] = shape
        self._announce(city, item, plan.action, plan.x, plan.y, plan.cost)
        log.info(
            "placement_plan city=%s item=%s tile=(%d,%d) action=%s cost=%d",
            city.name,
            item,
            plan.x,
            plan.y,
            plan.action,
            plan.cost,
        )
