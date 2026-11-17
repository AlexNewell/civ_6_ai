# ruff: noqa: E501  (the Lua has long lines)
"""A one-off probe of the game's per-tile loyalty numbers (issue 239). Read only; not used by the bot.

The settler lens draws a loyalty number on each tile. The shipped UI script
`DLC/Expansion2/UI/Additions/SettlerInfluenceIconManager.lua` gets every number from one call,
`Map.GetContinentPlotsLoyalty()` (plot index to loyalty per turn). This probe runs that call from the
InGame context, the same way `city_tiles.py` sends Lua, and reports what it returns, so a person can
decide whether the bot may use it. It answers the open questions of the research note
(`docs/research/loyalty-pressure-map.md`):

- does the call exist and work from our connection?
- how many plots come back, which continents do they sit on, and are all of them revealed to us?
- what range do the values span (no cap is assumed)?
- does it work with no settler owned, and with the settler lens off?
- how long does it take?

It changes nothing in the game. Run it with `scripts/probe_loyalty_map.py` on a game you own, once
with a settler and once without. The probe is not wired into the bot: any real use must follow the
fog rule in `state/fog.py` (revealed plots only, never work back from values to unseen cities).
"""

from dataclasses import dataclass, field

from civ_mcp.lua._helpers import SENTINEL

SAMPLE_LIMIT = 400  # Plots listed in full, so the reply stays small on a big map.

LOYALTY_PROBE_LUA = f"""
local me = Game.GetLocalPlayer()
local pl = Players[me]
local settlers = 0
pcall(function()
    for _, u in pl:GetUnits():Members() do
        if u:GetUnitType() >= 0 and GameInfo.Units[u:GetUnitType()].UnitType == "UNIT_SETTLER" then settlers = settlers + 1 end
    end
end)
print("LP|settlers|" .. settlers)
local lens = "unknown"
pcall(function() lens = tostring(UILens.IsLayerOn(UILens.CreateLensLayerHash("Hex_Coloring_Water_Availablity"))) end)
print("LP|lens_on|" .. lens)
print("LP|exists|" .. tostring(type(Map.GetContinentPlotsLoyalty)))
local t0 = Automation.GetTime()
local ok, plots = pcall(function() return Map.GetContinentPlotsLoyalty() end)
local dt = Automation.GetTime() - t0
if not ok then
    print("LP|error|" .. tostring(plots))
else
    print("LP|type|" .. type(plots))
    local n, lo, hi, unrevealed, listed = 0, nil, nil, 0, 0
    local continents = {{}}
    local vis = PlayersVisibility[me]
    for idx, val in pairs(plots or {{}}) do
        n = n + 1
        if lo == nil or val < lo then lo = val end
        if hi == nil or val > hi then hi = val end
        local p = Map.GetPlotByIndex(idx)
        if p ~= nil then
            local rev = true
            pcall(function() rev = vis:IsRevealed(p:GetX(), p:GetY()) end)
            if not rev then unrevealed = unrevealed + 1 end
            local c = -1
            pcall(function() c = p:GetContinentType() end)
            continents[c] = (continents[c] or 0) + 1
            if listed < {SAMPLE_LIMIT} then
                listed = listed + 1
                print("LP|plot|" .. p:GetX() .. "|" .. p:GetY() .. "|" .. tostring(val) .. "|" .. tostring(rev))
            end
        end
    end
    print("LP|count|" .. n .. "|" .. tostring(lo) .. "|" .. tostring(hi) .. "|" .. unrevealed)
    for c, k in pairs(continents) do print("LP|continent|" .. c .. "|" .. k) end
end
print("LP|seconds|" .. string.format("%.4f", dt))
print("LPDONE")
print("{SENTINEL}")
"""


@dataclass
class LoyaltyProbe:
    """What the call returned. `error` is set when it failed or the reply was incomplete."""

    settlers: int = 0
    lens_on: str = "unknown"
    exists: str = ""  # Lua type of the function: "function", or "nil" when the game has none.
    error: str = ""
    count: int = 0
    low: float | None = None
    high: float | None = None
    unrevealed: int = 0
    seconds: float = 0.0
    continents: dict[int, int] = field(default_factory=dict)
    plots: list[tuple[int, int, float, bool]] = field(default_factory=list)  # x, y, value, revealed


def _number(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def parse_loyalty_probe(output: str) -> LoyaltyProbe:
    """Parse the reply. A reply without its `LPDONE` line is reported as an error, not as data."""
    probe = LoyaltyProbe()
    if "LPDONE" not in output.split():
        probe.error = "incomplete reply: no end marker"
    for line in output.splitlines():
        parts = line.strip().split("|")
        if parts[0] != "LP" or len(parts) < 3:
            continue
        kind = parts[1]
        if kind == "settlers":
            probe.settlers = int(parts[2]) if parts[2].isdigit() else 0
        elif kind == "lens_on":
            probe.lens_on = parts[2]
        elif kind == "exists":
            probe.exists = parts[2]
        elif kind == "error":
            probe.error = "|".join(parts[2:])
        elif kind == "seconds":
            probe.seconds = _number(parts[2]) or 0.0
        elif kind == "count" and len(parts) >= 6:
            probe.count = int(parts[2]) if parts[2].isdigit() else 0
            probe.low, probe.high = _number(parts[3]), _number(parts[4])
            probe.unrevealed = int(parts[5]) if parts[5].isdigit() else 0
        elif kind == "continent" and len(parts) >= 4:
            try:
                probe.continents[int(parts[2])] = int(parts[3])
            except ValueError:
                continue
        elif kind == "plot" and len(parts) >= 6:
            x, y, value = _number(parts[2]), _number(parts[3]), _number(parts[4])
            if x is not None and y is not None and value is not None:
                probe.plots.append((int(x), int(y), value, parts[5] == "true"))
    return probe


def summarise(probe: LoyaltyProbe) -> str:
    """A short report for a person to read."""
    lines = [
        f"settlers owned: {probe.settlers}; settler lens on: {probe.lens_on}",
        f"Map.GetContinentPlotsLoyalty is a: {probe.exists or '(not reported)'}",
    ]
    if probe.error:
        lines.append(f"ERROR: {probe.error}")
    if probe.count:
        lines.append(
            f"{probe.count} plots, values {probe.low} to {probe.high}, "
            f"{probe.unrevealed} not revealed to us, in {probe.seconds:.4f} s"
        )
        lines.append(f"continents (id: plots): {probe.continents}")
    elif not probe.error:
        lines.append(f"no plots returned, in {probe.seconds:.4f} s")
    return "\n".join(lines)


async def run_loyalty_probe(game) -> LoyaltyProbe:
    """Send the probe to the InGame context. `game` needs `execute_lua`, as in `city_tiles.py`."""
    reply = await game.execute_lua(LOYALTY_PROBE_LUA, "ingame")
    return parse_loyalty_probe(reply if isinstance(reply, str) else "\n".join(reply))
