# What the game's map lenses draw, and whether the bot can read it (issue 243)

A human can switch the map between lenses: appeal, settler, religion, loyalty, government, political,
continent, tourism, empire, power, and others. Issue 239 found that the settler lens's loyalty numbers
come from one Lua call the bot can reach (`loyalty-pressure-map.md`). This note asks the same of every
other lens. It only catalogues what exists. No read is added to the bot because it turned out to be
reachable, and "not available" and "available but low value" are results (how the bot would use a
lens is issue 245).

## How the answers were found

The shipped UI Lua (read only, from the game's install: the base `UI` folder plus the two expansions'
`UI/Additions` and `UI/Replacements`) was searched for each lens layer name and tooltip key, then for
what the matching file calls. Lens names come from `MinimapPanel.lua` (the toggles) and
`ModalLensPanel.lua` (the legends); the Loyalty lens is added by `MinimapPanel_Expansion1.lua`, and the
Power lens by `ModalLensPanel_Expansion2.lua` and `PowerLensManager.lua`. The call names below are
from those files. Nothing here has been run; every "works from our connection" is a guess until the
probes in `src/civ6_ai/lens_probe.py` run on a live game (see "Live check" at the end).

## The fairness rule

Whatever is read must be what the player's own screen would show: revealed plots only, and cities of
civs we have not met are not named or used. Each read added to the bot later needs a flagged entry in
`state/fog.py` saying what it leaks, and a missing plot means unknown, not zero. The game's own scripts
already test this for some lenses: the Loyalty lens draws a foreign city only if `IsRevealed` is true
for its plot, and shows an unmet civ's pressure in a neutral "unknown civilization" colour. Others do
no test at all (the appeal and water calls take no arguments and say nothing about reveal), so the
probes count how many returned plots are not revealed. No read is added in this change, so `fog.py` is
unchanged.

## The lenses

"Cost" is a guess from the shape of the call (one call over the map, or one per plot or city); the
probes time it. "Value" is for the bot's current decisions, not a promise.

| Lens | Layer or toggle | Where the number comes from | Readable? | Cost | Value |
|---|---|---|---|---|---|
| Appeal | `Hex_Coloring_Appeal_Level`, `UILens.SetActive("Appeal")` | `MinimapPanel.lua` `SetAppealHexes` calls `Map.GetContinentPlotsAppeal()`, returning five plot lists (breathtaking, charming, average, uninviting, disgusting). A per-plot number is `Plot:GetAppeal()` (used by `AdjacencyBonusSupport.lua` and `PlotToolTip.lua`). The bucket call is made when the layer turns on, so it may or may not work with the layer off. | Likely, from the InGame context. Reveal filtering unknown. | One map call, or one call per plot | Low to medium. Appeal sets neighbourhood and aqueduct-style housing (`AppealHousingChanges`) and tourism for late wonders and parks. Matters for district placement and a late culture or tourism plan, not early settling. |
| Settler (water) | `Hex_Coloring_Water_Availablity` | `SetWaterHexes` calls `Map.GetContinentPlotsWaterAvailability()`: fresh, coastal, none, no-settle lists. | Likely. | One map call | Low. The bot already knows coast and river from tile reads; the no-settle list is the new part (too close, hostile border). |
| Settler (loyalty) | same layer, drawn by `SettlerInfluenceIconManager.lua` | `Map.GetContinentPlotsLoyalty()` (issue 239) | Yes: works with the lens off, 31 plots returned in the first live run | Cheap | Medium to high, used in issue 245. Already probed. |
| Settler (hazard icons) | drawn with the water layer by `SettlerWarningIconManager.lua` (expansion 2) | `RiverManager.CanBeFlooded`, `MapFeatureManager.CanSufferEruption`, `TerrainManager.GetCoastalLowlandType` and `TerrainManager.IsProtected`, over `Map.GetVisibleContinentPlots` | Likely. The script uses visible-plot lists. | One call per plot, a few thousand at most | Medium for sites if disasters are on (floods, volcano, rising sea). Could change which sites to reject. |
| Loyalty (Cultural Identity) | `Cultural_Identity_Lens` | `MinimapPanel_Expansion1.lua` `UpdateLoyaltyLens`: for every revealed city, `City:GetCulturalIdentity():GetConversionOutcome()` (gaining or losing) and `GetCityIdentityPressures()` (each source city, its owner, total pressure) | Likely, since the script runs these on foreign cities; it filters on `IsRevealed` and hides unmet civs. | One call per city; tens of cities | Medium. Shows which foreign cities are about to flip, and what pushes ours. Own-city loyalty is already read through `get_cities`; the new part is foreign cities and the source list. |
| Religion | `Hex_Coloring_Religion` | Mostly engine-drawn colour. The data is `City:GetReligion()` with `GetMajorityReligion`, `GetReligionsInCity`, `GetPressureFromCity` (read in `CityBannerManager.lua`); the legend uses `Game.GetReligion():GetReligions()`. | Likely for revealed cities; `fog.py` already flags religion fields of foreign cities. | One call per city | Low for now. The bot's religion policy runs on its own and met-civ counts. Spread pressure on neighbours could aid missionary targeting. |
| Government | `Hex_Coloring_Government` | `SetGovernmentHexes`: for each player, `Culture:GetCurrentGovernment()`, `IsInAnarchy()`, and `Map.GetCityPlots():GetPurchasedPlots(city)` | Yes, but all of it is already known: government of met civs comes through diplomacy. | Cheap | None. Not worth a read. |
| Political (owning civ) | `Hex_Coloring_Owning_Civ` | `SetOwningCivHexes`: `GetPurchasedPlots` per city | Plot owner is already in `get_map_area` (blanked on fogged tiles by `fog.py`). | Cheap | None. Already available. |
| Continent | `Hex_Coloring_Continent` | `SetContinentHexes`: `Map.GetContinentsInUse()` and `Map.GetVisibleContinentPlots(id)` | Yes, by design a visible-plots call | One call per continent | Low. The continent id of a plot is a per-plot read the bot can already make. Could matter for "is this island ours". |
| Tourism | `Tourist_Tokens` | `TourismBannerManager.lua`: `Culture:GetTouristsAt(plotId)`, `GetTourismAt(plotId)`, `GetTourismTooltipAt(plotId)`, over purchased plots | Only our own cities' plots are read by the UI; the call takes a plot id, so it may answer for others, which would be a leak and must not be used that way. | One call per own plot | Low until a tourism or culture victory plan exists. |
| Empire details | `Empire_Details` | Drawn in `CityBannerManager.lua` and `DistrictPlotIconManager.lua` from our own cities and districts | Own data already read | none | None. |
| Power (expansion 2) | `Power_Lens` | `PowerLensManager.lua`: `City:GetPower():GetPlotsCoveredByRegionalPower()`, `GetPlotsProvidingPower()`, `GetRequiredPower()` | Likely, our own cities only | One call per own city | Low. A late, own-city question. |
| Trade routes | `TradeRoutes` | Set by `TradeRouteChooser.lua` from route data the bot already reads through `get_trade_destinations` | Already available | none | None (no separate map call). |
| Great people | `Hex_Coloring_Great_People` | Set from the selected unit's valid plots in `UnitPanel.lua` and `SelectedUnit.lua`: a unit's operations, not a map call | Already available through unit operations | none | None. |

Not catalogued as lenses: yield icons, adjacency bonus (`Adjacency_Bonus_Districts`, drawn from
`AdjacencyBonusSupport.lua`, relevant to district placement and already mirrored by the city-tile
reads), movement and attack ranges, and purchase plots. They belong to unit and city choosers.

## What the probes ask

`src/civ6_ai/lens_probe.py` has one probe per readable candidate: `appeal`, `water`, `continent`,
`hazards`, `loyalty_lens`, `religion`, `tourism`, `power`, `political`. `scripts/probe_lenses.py` runs
them (`--lens NAME` for one). Each is a fixed Lua string sent to the InGame context, like
`loyalty_probe.py`, and reports: whether the named calls exist; whether the lens layer was on; the
number of plots or cities returned and how many are not revealed; a few sample values; and the time.
None turns a lens on or changes the game, so they cannot answer "does the call need the lens on"
directly; they report the layer state, and a person runs each once with the lens off and once on.
The parsing is unit tested against canned replies. The Lua has not been run, or even loaded by a Lua
interpreter, so the first live run may need small fixes (a wrong return order, for example).

## Live check

For each probe, run it on a game in progress with a few foreign cities revealed, once with the lens
off and once with it on, and record the result in this note:

| Probe | Record |
|---|---|
| `appeal` | Does the call exist and return five lists with the lens off? Plot counts, unrevealed count, whether the bucket matches `GetAppeal`, the time for one map call and for the per-plot reads |
| `water` | Exists, lists non-empty, unrevealed count, whether the no-settle list names plots near foreign cities we have not seen |
| `continent` | Continent ids and visible plot counts |
| `hazards` | The three calls exist, counts of flooded, volcano and lowland plots, and any error from a call used outside the UI context |
| `loyalty_lens` | `foreign_outcome_readable` against `revealed_foreign_cities`, whether unmet civs' cities are readable, pressure sources listed |
| `religion` | Majority religion readable for foreign cities, counts, time |
| `tourism` | Calls exist, own plot counts; whether the tourism call is only meaningful for own plots |
| `power` | Needs expansion 2 and an own city with power; exists, counts |
| `political` | Which governments and plots are readable for civs we have not met (should be none used, only a record) |

Then update the table's "Readable?" and "Cost" columns with what was measured, and file a follow-up
issue for each lens judged worth a read.
