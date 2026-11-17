# ruff: noqa: E501  (the Lua has long lines)
"""One-off probes of the data behind the game's map lenses (issue 243). Read only; not used by the bot.

`docs/research/map-lenses.md` lists each lens and where the shipped UI scripts get its numbers. This
module holds one probe per readable candidate, in the style of `loyalty_probe.py`: a fixed Lua string
sent to the InGame context (as `city_tiles.py` does), and a parser for the reply. Each probe reports
whether the call exists, whether it works from our connection, whether the lens layer was on, how
many plots or cities it returns, how many of those are not revealed to us, a few sample values, and
how long it took. None of them turns a lens on or changes anything in the game.

They have been tested only against a fake game, for their parsing. A person runs them with
`scripts/probe_lenses.py` on a game they own. Any read that later joins the bot must follow the fog
rule in `state/fog.py`: revealed plots only, a flagged entry per read, and a missing plot means
unknown, not zero.
"""

from dataclasses import dataclass, field

from civ_mcp.lua._helpers import SENTINEL

SAMPLES_PER_BUCKET = 5  # Values listed per bucket, so the reply stays small on a big map.
COST_PLOT_LIMIT = 300  # Plots read one by one to time a per-plot call.

# Shared by every probe. `tally` counts a list of plot indices (and reads a value on a few of them).
_PRELUDE = f"""
local me = Game.GetLocalPlayer()
local vis = PlayersVisibility[me]
local function rev(p) local r = true; pcall(function() r = vis:IsRevealed(p:GetX(), p:GetY()) end) return r end
local function note(k, v) print("LN|note|" .. k .. "|" .. tostring(v)) end
local function layer(name) pcall(function() note("lens_on", UILens.IsLayerOn(UILens.CreateLensLayerHash(name))) end) end
local function tally(label, list, valfn)
    local n, unrev, listed = 0, 0, 0
    for _, idx in ipairs(list or {{}}) do
        n = n + 1
        local p = Map.GetPlotByIndex(idx)
        if p ~= nil then
            local r = rev(p)
            if not r then unrev = unrev + 1 end
            if valfn ~= nil and listed < {SAMPLES_PER_BUCKET} then
                listed = listed + 1
                local ok, v = pcall(valfn, p)
                print("LN|sample|" .. label .. "|" .. p:GetX() .. "|" .. p:GetY() .. "|" .. tostring(ok and v or "error") .. "|" .. tostring(r))
            end
        end
    end
    print("LN|bucket|" .. label .. "|" .. n .. "|" .. unrev)
end
local t0 = Automation.GetTime()
"""


def _probe(name: str, body: str) -> str:
    """Wrap a probe body: a failure is reported as an error line, and the reply always ends cleanly."""
    return (
        _PRELUDE
        + f'print("LN|probe|{name}")\n'
        + "local ok_, err_ = pcall(function()\n"
        + body.strip("\n")
        + '\nend)\nif not ok_ then print("LN|error|" .. tostring(err_)) end\n'
        + 'print("LN|seconds|" .. string.format("%.4f", Automation.GetTime() - t0))\n'
        + f'print("LNDONE")\nprint("{SENTINEL}")\n'
    )


_APPEAL = f"""
layer("Hex_Coloring_Appeal_Level")
note("Map.GetContinentPlotsAppeal", type(Map.GetContinentPlotsAppeal))
note("Plot.GetAppeal", type(Map.GetPlotByIndex(0).GetAppeal))
local b, c, a, u, d = Map.GetContinentPlotsAppeal()
local function appeal(p) return p:GetAppeal() end
tally("breathtaking", b, appeal)
tally("charming", c, appeal)
tally("average", a, appeal)
tally("uninviting", u, appeal)
tally("disgusting", d, appeal)
local t1, read, n = Automation.GetTime(), 0, 0
for i = 0, Map.GetPlotCount() - 1 do
    local p = Map.GetPlotByIndex(i)
    if p ~= nil and rev(p) then
        read = read + p:GetAppeal()
        n = n + 1
        if n >= {COST_PLOT_LIMIT} then break end
    end
end
note("per_plot_reads", n)
note("per_plot_seconds", string.format("%.4f", Automation.GetTime() - t1))
"""

_WATER = """
layer("Hex_Coloring_Water_Availablity")
note("Map.GetContinentPlotsWaterAvailability", type(Map.GetContinentPlotsWaterAvailability))
local f, c, n, s = Map.GetContinentPlotsWaterAvailability()
tally("fresh_water", f)
tally("coastal", c)
tally("no_water", n)
tally("no_settle", s)
"""

_CONTINENT = """
layer("Hex_Coloring_Continent")
note("Map.GetContinentsInUse", type(Map.GetContinentsInUse))
note("Map.GetVisibleContinentPlots", type(Map.GetVisibleContinentPlots))
for _, id in ipairs(Map.GetContinentsInUse()) do
    tally("continent_" .. id, Map.GetVisibleContinentPlots(id))
end
"""

_HAZARDS = """
layer("Hex_Coloring_Water_Availablity")
note("RiverManager.CanBeFlooded", type(RiverManager and RiverManager.CanBeFlooded))
note("MapFeatureManager.CanSufferEruption", type(MapFeatureManager and MapFeatureManager.CanSufferEruption))
note("TerrainManager.GetCoastalLowlandType", type(TerrainManager and TerrainManager.GetCoastalLowlandType))
local flood, volcano, lowland = {}, {}, {}
for _, id in ipairs(Map.GetContinentsInUse()) do
    for _, idx in ipairs(Map.GetVisibleContinentPlots(id) or {}) do
        local p = Map.GetPlotByIndex(idx)
        if p ~= nil then
            if RiverManager.CanBeFlooded(p) then table.insert(flood, idx) end
            if MapFeatureManager.CanSufferEruption(p) then table.insert(volcano, idx) end
            if not TerrainManager.IsProtected(p) and TerrainManager.GetCoastalLowlandType(p) > -1 then table.insert(lowland, idx) end
        end
    end
end
local function owner(p) return p:GetOwner() end
tally("flood", flood, owner)
tally("volcano", volcano, owner)
tally("coastal_lowland", lowland, owner)
"""

_LOYALTY_LENS = """
layer("Cultural_Identity_Lens")
local pdip = Players[me]:GetDiplomacy()
local cities, foreign, unmet, readable, pressures = 0, 0, 0, 0, 0
for _, pl in ipairs(Game.GetPlayers()) do
    for _, c in pl:GetCities():Members() do
        local plot = Map.GetPlotIndex(c:GetX(), c:GetY())
        if vis:IsRevealed(plot) then
            cities = cities + 1
            if pl:GetID() ~= me then
                foreign = foreign + 1
                if not pdip:HasMet(pl:GetID()) then unmet = unmet + 1 end
                local ok, outcome = pcall(function() return c:GetCulturalIdentity():GetConversionOutcome() end)
                if ok then
                    readable = readable + 1
                    local _, ps = pcall(function() return c:GetCulturalIdentity():GetCityIdentityPressures() end)
                    pressures = pressures + (type(ps) == "table" and #ps or 0)
                    if readable <= 5 then print("LN|sample|foreign_city_outcome|" .. c:GetX() .. "|" .. c:GetY() .. "|" .. tostring(outcome) .. "|true") end
                end
            end
        end
    end
end
note("revealed_cities", cities)
note("revealed_foreign_cities", foreign)
note("revealed_foreign_cities_of_unmet_civs", unmet)
note("foreign_outcome_readable", readable)
note("foreign_pressure_sources_listed", pressures)
note("UILens.CreatePressureWaves", type(UILens.CreatePressureWaves))
"""

_RELIGION = """
layer("Hex_Coloring_Religion")
note("Game.GetReligion", type(Game.GetReligion))
local pdip = Players[me]:GetDiplomacy()
local cities, foreign, readable, unmet, withpressure = 0, 0, 0, 0, 0
for _, pl in ipairs(Game.GetPlayers()) do
    for _, c in pl:GetCities():Members() do
        if vis:IsRevealed(Map.GetPlotIndex(c:GetX(), c:GetY())) then
            cities = cities + 1
            if pl:GetID() ~= me then
                foreign = foreign + 1
                if not pdip:HasMet(pl:GetID()) then unmet = unmet + 1 end
                local ok, maj = pcall(function() return c:GetReligion():GetMajorityReligion() end)
                if ok then
                    readable = readable + 1
                    local _, inbound = pcall(function() return c:GetReligion():GetReligionsInCity() end)
                    local _, outward = pcall(function() return c:GetReligion():GetPressureFromCity() end)
                    if type(inbound) == "table" and #inbound > 0 then withpressure = withpressure + 1 end
                    if readable <= 5 then print("LN|sample|foreign_city_majority_religion|" .. c:GetX() .. "|" .. c:GetY() .. "|" .. tostring(maj) .. ";outward=" .. tostring(outward) .. "|true") end
                end
            end
        end
    end
end
note("revealed_cities", cities)
note("revealed_foreign_cities", foreign)
note("revealed_foreign_cities_of_unmet_civs", unmet)
note("foreign_majority_readable", readable)
note("foreign_cities_with_religions_listed", withpressure)
"""

_TOURISM = """
layer("Tourist_Tokens")
local culture = Players[me]:GetCulture()
note("GetTourismAt", type(culture.GetTourismAt))
note("GetTouristsAt", type(culture.GetTouristsAt))
note("GetTourismTooltipAt", type(culture.GetTourismTooltipAt))
local tourism, tourists = {}, {}
for _, c in Players[me]:GetCities():Members() do
    for _, idx in ipairs(Map.GetCityPlots():GetPurchasedPlots(c)) do
        if culture:GetTourismAt(idx) > 0 then table.insert(tourism, idx) end
        if culture:GetTouristsAt(idx) > 0 then table.insert(tourists, idx) end
    end
end
tally("own_plots_with_tourism", tourism, function(p) return culture:GetTourismAt(p:GetIndex()) end)
tally("own_plots_with_tourists", tourists, function(p) return culture:GetTouristsAt(p:GetIndex()) end)
"""

_POWER = """
layer("Power_Lens")
local covered, providing, cities = {}, {}, 0
for _, c in Players[me]:GetCities():Members() do
    cities = cities + 1
    local power = c:GetPower()
    note("City.GetPower", type(c.GetPower))
    for _, idx in ipairs(power:GetPlotsCoveredByRegionalPower() or {}) do table.insert(covered, idx) end
    for _, idx in ipairs(power:GetPlotsProvidingPower() or {}) do table.insert(providing, idx) end
end
note("own_cities", cities)
tally("covered_by_regional_power", covered)
tally("providing_power", providing)
"""

_POLITICAL = """
layer("Hex_Coloring_Government")
note("Map.GetCityPlots.GetPurchasedPlots", type(Map.GetCityPlots().GetPurchasedPlots))
local pdip = Players[me]:GetDiplomacy()
local cities, plots, unmetcities, govreadable, unmetgov = 0, 0, 0, 0, 0
for _, pl in ipairs(Game.GetPlayers()) do
    local met = pl:GetID() == me or pdip:HasMet(pl:GetID())
    local ok, gov = pcall(function() return pl:GetCulture():GetCurrentGovernment() end)
    if ok then govreadable = govreadable + 1; if not met then unmetgov = unmetgov + 1 end end
    for _, c in pl:GetCities():Members() do
        if vis:IsRevealed(Map.GetPlotIndex(c:GetX(), c:GetY())) then
            cities = cities + 1
            if not met then unmetcities = unmetcities + 1 end
            plots = plots + table.count(Map.GetCityPlots():GetPurchasedPlots(c))
        end
    end
end
note("revealed_cities", cities)
note("revealed_cities_of_unmet_civs", unmetcities)
note("purchased_plots_of_revealed_cities", plots)
note("governments_readable", govreadable)
note("governments_of_unmet_civs_readable", unmetgov)
"""

# One probe per candidate lens. Lenses with no map-data call of their own (trade routes, great
# people, empire details) have none; see the note.
PROBES: dict[str, str] = {
    "appeal": _probe("appeal", _APPEAL),
    "water": _probe("water", _WATER),
    "continent": _probe("continent", _CONTINENT),
    "hazards": _probe("hazards", _HAZARDS),
    "loyalty_lens": _probe("loyalty_lens", _LOYALTY_LENS),
    "religion": _probe("religion", _RELIGION),
    "tourism": _probe("tourism", _TOURISM),
    "power": _probe("power", _POWER),
    "political": _probe("political", _POLITICAL),
}


@dataclass
class Bucket:
    """One list of plots a call returned (for example the Charming plots of the appeal lens)."""

    count: int = 0
    unrevealed: int = 0
    samples: list[tuple[int, int, str, bool]] = field(default_factory=list)  # x, y, value, revealed


@dataclass
class LensProbe:
    """What one probe returned. `error` is set when it failed or the reply was incomplete."""

    name: str = ""
    error: str = ""
    seconds: float = 0.0
    notes: dict[str, str] = field(default_factory=dict)
    buckets: dict[str, Bucket] = field(default_factory=dict)


def parse_lens_probe(output: str) -> LensProbe:
    """Parse a reply. A reply without its `LNDONE` line is reported as an error, not as data."""
    probe = LensProbe()
    if "LNDONE" not in output.split():
        probe.error = "incomplete reply: no end marker"
    for line in output.splitlines():
        parts = line.strip().split("|")
        if parts[0] != "LN" or len(parts) < 3:
            continue
        kind = parts[1]
        if kind == "probe":
            probe.name = parts[2]
        elif kind == "error":
            probe.error = "|".join(parts[2:])
        elif kind == "seconds":
            try:
                probe.seconds = float(parts[2])
            except ValueError:
                continue
        elif kind == "note" and len(parts) >= 4:
            probe.notes[parts[2]] = "|".join(parts[3:])
        elif kind == "bucket" and len(parts) >= 5:
            if parts[3].isdigit() and parts[4].isdigit():
                bucket = probe.buckets.setdefault(parts[2], Bucket())
                bucket.count, bucket.unrevealed = int(parts[3]), int(parts[4])
        elif (
            kind == "sample"
            and len(parts) >= 7
            and parts[3].lstrip("-").isdigit()
            and parts[4].lstrip("-").isdigit()
        ):
            bucket = probe.buckets.setdefault(parts[2], Bucket())
            bucket.samples.append((int(parts[3]), int(parts[4]), parts[5], parts[6] == "true"))
    return probe


def summarise(probe: LensProbe) -> str:
    """A short report for a person to read."""
    lines = [f"probe: {probe.name or '(not reported)'}; {probe.seconds:.4f} s"]
    if probe.error:
        lines.append(f"ERROR: {probe.error}")
    lines += [f"  {key}: {value}" for key, value in probe.notes.items()]
    for label, bucket in probe.buckets.items():
        shown = ", ".join(f"({x},{y})={v}" for x, y, v, _ in bucket.samples)
        lines.append(
            f"  {label}: {bucket.count} plots, {bucket.unrevealed} not revealed"
            + (f"; {shown}" if shown else "")
        )
    return "\n".join(lines)


async def run_lens_probe(game, name: str) -> LensProbe:
    """Send one probe to the InGame context. `game` needs `execute_lua`, as in `city_tiles.py`."""
    reply = await game.execute_lua(PROBES[name], "ingame")
    return parse_lens_probe(reply if isinstance(reply, str) else "\n".join(reply))
