# Civ VI human-facing UI tools and the API calls behind them

Method: the game is installed locally, so the main source is the game's own UI Lua under
`C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI\` (abbreviated `CIV6\`
below; read-only, read 2026-11-17). Community-mod sources are from GitHub. Web references for Civ VI
Lua context rules are thin (see Gaps). Local file citations use `file:///` links.

The repo already reads (from `src/` grep): `PlayersVisibility[me]:IsVisible/IsRevealed`, `Map.GetPlot*`,
`Map.GetCityPlots`, `Map.GetUnitsAt`, `UnitManager.CanStartOperation`, `CityManager.GetOperationTargets/GetCommandTargets`,
`Cities.GetPlotPurchaseCity`, `city:GetCulturalIdentity()` (loyalty policy), `Game.GetWorldCongress`, GameInfo tables.
It does **not** yet use `CombatManager.*`, `UnitManager.GetReachable*`, `UnitManager.GetMoveToPathEx`,
`plot:GetAdjacencyYield`, `plot:GetAppeal`, or `pCityPower:*`.

## 1. Built-in lenses: what API produces each

### Takeaway
Most built-in lenses are drawn by the engine (`UILens.ToggleLayerOn(<layer hash>)`); the Lua only builds the legend. The data behind each is still readable through plot/city/player getters (appeal, continent, owner, religion, loyalty, power), so a bot can rebuild every lens without the overlay. The settler "water availability" colouring itself has no single Lua getter found; it has to be rebuilt from plot flags or replaced by the FOUND_CITY operation test.

### Cited Findings
- The base lens panel declares these engine layers: `Hex_Coloring_Religion`, `Hex_Coloring_Appeal_Level`, `Hex_Coloring_Government`, `Hex_Coloring_Owning_Civ`, `Hex_Coloring_Continent`, `Hex_Coloring_Water_Availablity` (settler lens), `Tourist_Tokens`, `Empire_Details`. The panel's Lua only builds the key (e.g. `ShowSettlerLensKey`, `ShowReligionLensKey` using `Game.GetReligion():GetReligions()/HasBeenFounded/GetName`) — [ModalLensPanel.lua](file:///C:/Program%20Files%20(x86)/Steam/steamapps/common/Sid%20Meier's%20Civilization%20VI/Base/Assets/UI/Panels/ModalLensPanel.lua) lines 10-20, 52-93
- Rise and Fall adds `Cultural_Identity_Lens` (loyalty lens); Gathering Storm adds `Power_Lens` — [ModalLensPanel_Expansion1.lua](file:///C:/Program%20Files%20(x86)/Steam/steamapps/common/Sid%20Meier's%20Civilization%20VI/DLC/Expansion2/UI/Replacements/ModalLensPanel_Expansion1.lua) line 18; [ModalLensPanel_Expansion2.lua](file:///C:/Program%20Files%20(x86)/Steam/steamapps/common/Sid%20Meier's%20Civilization%20VI/DLC/Expansion2/UI/Replacements/ModalLensPanel_Expansion2.lua) line 17
- Continent lens legend uses `Map.GetVisibleContinentPlots(...)` (name suggests it already filters to visible plots) — ModalLensPanel.lua line 195
- Power lens is fully Lua and uses `pCity:GetPower()` then `pCityPower:GetPlotsCoveredByRegionalPower()`, `GetPlotsProvidingPower()`, `GetRequiredPower()`, `IsFullyPowered()`, over `Map.GetCityPlots()` — [PowerLensManager.lua](file:///C:/Program%20Files%20(x86)/Steam/steamapps/common/Sid%20Meier's%20Civilization%20VI/DLC/Expansion2/UI/Additions/PowerLensManager.lua)
- Loyalty / identity: the city panel and banners use `city:GetCulturalIdentity()` with `GetLoyalty`, `GetMaxLoyalty`, `GetLoyaltyPerTurn`, `GetLoyaltyPerTurnStatus`, `GetLoyaltyLevel`, `GetConversionOutcome`, `GetTurnsToConversion`, `IsAlwaysFullyLoyal`, `GetCityIdentityPressures`, `GetIdentitySourcesDetailedBreakdown`, `GetPlayerIdentitiesInCity` — grep of CityPanelCulture.lua, CityBannerManager.lua, LoyaltySupport.lua, GovernorAssignmentChooser.lua under `CIV6\DLC\Expansion*\UI` (e.g. [LoyaltySupport.lua](file:///C:/Program%20Files%20(x86)/Steam/steamapps/common/Sid%20Meier's%20Civilization%20VI/DLC/Expansion2/UI/Additions/LoyaltySupport.lua) lines 25-28)
- Appeal: `plot:GetAppeal()` is read by the plot tooltip, the adjacency support file and the map search panel — [PlotToolTip.lua](file:///C:/Program%20Files%20(x86)/Steam/steamapps/common/Sid%20Meier's%20Civilization%20VI/Base/Assets/UI/ToolTips/PlotToolTip.lua) line 641; [AdjacencyBonusSupport.lua](file:///C:/Program%20Files%20(x86)/Steam/steamapps/common/Sid%20Meier's%20Civilization%20VI/Base/Assets/UI/AdjacencyBonusSupport.lua) line 226
- The plot tooltip (the human "hover" tool) gathers in one place: `GetAppeal, GetContinentType, GetDefenseModifier, GetDistrictID/Type, GetFeatureType, HasFeatureBeenAdded, IsImpassable, GetImprovementType, IsImprovementPillaged, IsCity, IsLake, IsRiver, IsRoute, IsWater, GetMovementCost, GetOwner, Cities.GetPlotPurchaseCity, GetResourceCount/Type, IsRoutePillaged, GetRouteType, GetTerrainType, GetWonderType, GetWorkerCount, IsNWOfCliff/IsWOfCliff/IsNEOfCliff`, fallout via `Game.GetFalloutManager():GetFalloutTurnsRemaining(i)`, plus yields. It gates the tooltip on `PlayersVisibility[Game.GetLocalObserver()]` — PlotToolTip.lua lines 630-720
- `plot:IsFreshWater()` appears only in the map-generation script `AssignStartingPlots.lua` (gameplay/map-script side), not in any UI file — grep of `CIV6\Base`, `CIV6\DLC`
- Settle-ability in the UI comes from `UnitManager.CanStartOperation(pUnit, UnitOperationTypes.FOUND_CITY, nil, false, OperationResultsTypes.ALL)` — [UnitPanel.lua](file:///C:/Program%20Files%20(x86)/Steam/steamapps/common/Sid%20Meier's%20Civilization%20VI/Base/Assets/UI/Panels/UnitPanel.lua) line 630
- Tourism / world rankings use `GetTourismAt`, `GetTouristsFrom`, `GetTouristsTo`, `GetStaycationers`, `GetCategoryScore`, `GetScore`, `GetTurnsUntilVictory`, `GetReligionInMajorityOfCities`, `GetProjectProgress/GetProjectCost`, `GetNumProjectsAdvanced`, `HasTech` — grep of [WorldRankings.lua](file:///C:/Program%20Files%20(x86)/Steam/steamapps/common/Sid%20Meier's%20Civilization%20VI/Base/Assets/UI/PartialScreens/WorldRankings.lua) and ReportScreen.lua / ReportScreen_Expansion2.lua
- The Reports screen (empire overview) uses player/city getters: `GetCities, GetCitizens, GetCultureYield, GetFaithBalance/Yield, GetGoldBalance/Yield, GetScienceYield, GetResourceAmount, GetResourceStockpileCap, GetResourcesExtractedByCity, GetTotalMaintenance, GetMaintDiscountPerUnit, GetTrade, GetTreasury, GetUnits` — grep of [ReportScreen.lua](file:///C:/Program%20Files%20(x86)/Steam/steamapps/common/Sid%20Meier's%20Civilization%20VI/Base/Assets/UI/Screens/ReportScreen.lua) and ReportScreen_Expansion2.lua
- Map tacks are a pure UI feature (`MapTacks.lua`); they store the human's own pins, no derived game data — file exists at `CIV6\Base\Assets\UI\MapTacks.lua` (not read in depth)

### Inferences
- A bot gains nothing from toggling lenses: the engine-drawn layers return no data to Lua. Read the getters directly.
- Settler lens equivalent: combine `plot:IsRiver()`, `plot:IsLake()`, adjacency to coast (`Map.GetAdjacentPlot` + `IsWater`), and the FOUND_CITY `CanStartOperation` test (which already enforces the minimum city distance and legal terrain). `IsFreshWater()` may also work from the gameplay context since map scripts use it (untested here).
- Loyalty lens equivalent: `GetCityIdentityPressures()` / `GetIdentitySourcesDetailedBreakdown()` per city are the loyalty numbers the human sees; the repo's loyalty policy already calls `GetCulturalIdentity()`, so these are cheap additions. A per-plot pressure field for a *prospective* city site was not found in UI Lua (the lens is engine drawn).
- Power, rankings, reports: all one-call-per-city or per-player getters; cheap to batch into one tuner call.

### Gaps
- The exact algorithm behind `Hex_Coloring_Water_Availablity` (fresh vs coast vs none) lives in the engine; no Lua getter for the colour class was found.
- No per-plot "loyalty pressure if a city were here" function found in UI Lua.
- Government lens / Empire_Details / Tourist_Tokens: engine layers; their underlying getters were not traced.

## 2. Combat preview, reachability and pathing

### Takeaway
The combat preview is directly callable: `CombatManager.SimulateAttackVersus(attackerComponentID, defenderComponentID[, combatType])` and `CombatManager.SimulateAttackInto(attacker, combatType, x, y)` return a results table keyed by `CombatResultParameters` (strength, modifiers, damage to each side) without attacking. Reachable tiles, attackable targets, ZOC and paths come from `UnitManager.GetReachableMovement / GetReachableTargets / GetReachableZonesOfControl / GetOperationTargets(RANGE_ATTACK) / GetMoveToPathEx`. These are the highest-value calls for the threat map and military policy. All are used from the UI (InGame) context.

### Cited Findings
- Combat preview calls in the unit panel: `CombatManager.SimulateAttackVersus(pDistrict:GetComponentID(), pDefender:GetComponentID())` for city/district ranged attacks, `CombatManager.SimulateAttackVersus(attackerUnit:GetComponentID(), pDefender:GetComponentID(), eCombatType)` for units, `CombatManager.SimulatePriorityAttackInto(attacker, eCombatType, locX, locY)` / `CombatManager.SimulateAttackInto(attacker, eCombatType, locX, locY)` for hovering a plot, and `CombatManager.CanAttackTarget(attackerID, defenderID, eCombatType)` — UnitPanel.lua lines 3319, 3329, 3352, 3598, 3922-3924
- Firaxis comment at the call site: the preview is only computed when `not UI.IsGameCoreBusy()`, because "the simulation currently needs to run on the Game Core side for accurate results"; it is also skipped for the local player's own units — UnitPanel.lua around line 3305
- Result fields read by the panel: `m_combatResults[CombatResultParameters.COMBAT_TYPE]`, `[ATTACKER][COMBAT_STRENGTH]`, `[ATTACKER][STRENGTH_MODIFIER]`, `[DEFENDER][COMBAT_STRENGTH]`, `[DEFENDER][STRENGTH_MODIFIER]`, `[ATTACKER][DAMAGE_TO]`, plus `INTERCEPTOR`, `ANTI_AIR`, `PREVIEW_TEXT_INTERCEPTOR`, `PREVIEW_TEXT_ANTI_AIR` and modifier lists — UnitPanel.lua lines 1717-1966
- Community confirmation that `CombatManager` methods `CanAttackTarget` and `SimulatePriorityAttackInto` show up in Lua object dumps, and that `CombatResultParameters`/`CombatTypes` keys are integer hashes — [CivFanatics: Lua Objects thread](https://forums.civfanatics.com/threads/lua-objects.601146/page-8); [CivFanatics: accessing combat object attributes](https://forums.civfanatics.com/threads/lua-need-help-accessing-combat-object-attributes.671467/)
- Selected-unit overlays: `UnitManager.GetReachableTargets(kUnit)` (attack plots), `UnitManager.GetReachableMovement(kUnit)` (move plots), `UnitManager.GetReachableZonesOfControl(kUnit, true)` with the Firaxis comment "Only plots visible to the unit", and `UnitManager.GetOperationTargets(kUnit, UnitOperationTypes.RANGE_ATTACK)` — [SelectedUnit.lua](file:///C:/Program%20Files%20(x86)/Steam/steamapps/common/Sid%20Meier's%20Civilization%20VI/Base/Assets/UI/WorldView/SelectedUnit.lua) lines 47-74
- Path preview: `UnitManager.GetMoveToPathEx(kUnit, endPlotId)` returns a table with `.plots` (and per-plot turn data used to draw turn numbers); `UnitManager.GetQueuedDestination(unit)` gives a unit's queued target — [WorldInput.lua](file:///C:/Program%20Files%20(x86)/Steam/steamapps/common/Sid%20Meier's%20Civilization%20VI/Base/Assets/UI/WorldInput.lua) lines 821, 907, 961-975
- Other target lists from the same API family: `GetOperationTargets` for `AIR_ATTACK`, `WMD_STRIKE`, `COASTAL_RAID`, `DEPLOY`, `REBASE`; `GetCommandTargets` for `FORM_CORPS`, `FORM_ARMY`, `AIRLIFT` — WorldInput.lua lines 2004-3046
- `CanStartOperation(unit, op, nil, tParameters, true)` returns `(bCanStart, tResults)`; passing `OperationResultsTypes.NO_TARGETS` is Firaxis's hint to skip "possibly expensive target results" — UnitPanel.lua lines 557-677

### Inferences
- For the threat map, `SimulateAttackVersus(enemy, ourUnit)` gives the game's own expected damage numbers, which could replace the repo's hand-built "strength" scoring for visible enemies. Run it only for defenders/attackers on currently visible tiles.
- Enemy reach: `GetReachableMovement` / `GetReachableTargets` take a unit object. Calling them on a *visible enemy* unit would produce "where can this enemy go/hit next turn" — the same thing a human eyeballs. Whether the engine computes it with the enemy's knowledge or ours (and whether hidden units/terrain affect it) is untested; treat as a fog risk until a live check confirms it only uses revealed terrain.
- Cost: every call needs a unit object and returns tables; batch all units in one tuner execute and return compact plot-index lists. `GetReachableMovement` on 10-20 units per turn should fit the 10 s budget, but this is not measured.
- `UI.IsGameCoreBusy()` must be false before simulating, matching the repo's existing "wait for game core" pattern.

### Gaps
- No public documentation of which Lua context (InGame UI vs GameCore/gameplay script) exposes `CombatManager` and the `UnitManager.GetReachable*` functions; a forum poster says context availability is "hit or miss" — [CivFanatics Lua Objects](https://forums.civfanatics.com/threads/lua-objects.601146/page-5). Needs a live dump in each tuner state.
- Whether `GetMoveToPathEx` routes around hidden enemy units (a fog leak) is unknown.
- Whether `SimulateAttackVersus` includes a random spread or just the expected value was not confirmed; the UI shows a single damage figure.

## 3. Notifications, report screens, diplomacy, adjacency previews

### Takeaway
Adjacency previews are directly callable (`plot:GetAdjacencyYield(player, cityID, district, yield)`, `plot:CanHaveDistrict`, `plot:GetAdjacencyBonusTooltip`). Notifications exist for barbarians sighted and new camps, which is a free, fog-legal event feed. The diplomatic AI's threat/trust functions exist but are not shown in the shipping UI.

### Cited Findings
- District adjacency preview: `plot:GetAdjacencyYield(Game.GetLocalPlayer(), pkCity:GetID(), eDistrict, iBonusYield)`, `plot:GetAdjacencyBonusTooltip(...)`, `plot:GetAdjacencyBonusType(player, cityID, eDistrict, direction)`, and `kPlot:CanHaveDistrict(district.Index, owner, cityID)` over `Map.GetCityPlots():GetPurchasedPlots(pCity)` — AdjacencyBonusSupport.lua lines 33, 188-198, 262-317
- Notification types relevant to threat: `NotificationTypes.BARBARIANS_SIGHTED`, `NEW_BARBARIAN_CAMP`, `CITY_RANGE_ATTACK`, `CONSIDER_DISLOYAL_CITY`, `TRADE_ROUTE_PLUNDERED`, plus spy-mission notifications (`SPY_ENEMY_*`) — grep of `NotificationTypes.*` across `CIV6\Base` and `CIV6\DLC` UI Lua
- Diplomatic AI threat/trust: `Players[me]:GetDiplomaticAI()` with `GetThreatString(playerID)`, `GetThreatFrom(playerID)`, `GetTrustString`, `GetTrustFrom` — [ThreatTrustSupport.lua](file:///C:/Program%20Files%20(x86)/Steam/steamapps/common/Sid%20Meier's%20Civilization%20VI/Base/Assets/UI/ThreatTrustSupport.lua) lines 32-60. No other shipping UI file includes this support file (grep for "ThreatTrustSupport" found no includers).
- Trade route previews: TradeOverview.lua, TradeRouteChooser.lua and TradeSupport.lua (not read in depth); CQUI advertises an "Available Routes" list with tourism and visibility bonus per route — [CQUI README](https://github.com/CQUI-Org/cqui)
- Deal screen: `DiplomacyDealView.lua` exists in `CIV6\Base\Assets\UI` (not traced in this pass)

### Inferences
- The notification feed is the cheapest "something appeared" signal; a mod can listen to `Events.NotificationAdded` and queue them for the bot rather than polling the whole map. (Event name from general Civ VI modding knowledge; not verified in this pass.)
- The repo's district and builder choices could use `GetAdjacencyYield` instead of re-implementing adjacency rules.
- `GetThreatFrom`/`GetTrustFrom` come from the game AI's internal opinion of us. Because no human-facing screen shows them, they fall outside the "a human could see it" rule; do not use.

### Gaps
- Exact notification payloads (plot coordinates for BARBARIANS_SIGHTED) not checked.
- Deal-preview evaluation functions (AI acceptance prediction) not traced.

## 4. Community mods: what they compute and from where

### Takeaway
Community mods compute nothing exotic. They loop plots with standard getters, gated on `PlayersVisibility:IsRevealed`. The repo's threat-map camp read already does what the More Lenses barbarian lens does. Their value is as checklists of useful derived views (city overlap, unimproved resources, goody huts), not as new data.

### Cited Findings
- More Lenses adds Builder (unimproved resources, hills, removable features), Archaeologist (artifacts, shipwrecks), City Overlap (6 or 9), Barbarian (camps), Resource (by class and connected or not), Wonder, Adjacency Yield (gradient), Scout (goody huts). It reuses the Appeal lens layer to draw its own colours — [MoreLenses README](https://github.com/astog/MoreLenses)
- Barbarian lens code: loops `0..(w*h-1)` with `Map.GetPlotByIndex(i)`, checks `PlayersVisibility[localPlayer]:IsRevealed(x, y)`, then `GameInfo.Improvements[pPlot:GetImprovementType()].ImprovementType == "IMPROVEMENT_BARBARIAN_CAMP"`; unrevealed plots are skipped — [ModLens_Barbarian.lua](https://raw.githubusercontent.com/astog/MoreLenses/master/Lenses/Barbarian/ModLens_Barbarian.lua)
- CQUI (archived; continued as CQUI Community Edition) computes: city-banner growth / amenities / border growth, next city plot target and timing, resource dimming for unused resources, trade-route list with tourism and visibility bonus, great-person turns-left, hostile-unit highlighting; it bundles More Lenses and the Unit Report Screen; it removed production/worker recommendations — [CQUI README](https://github.com/CQUI-Org/cqui); successor [CQUI Community Edition](https://github.com/Azurency/CQUI_Community-Edition)

### Inferences
- The barbarian-lens rule matches the repo's rule: a camp on a revealed tile counts, even fogged. The game keeps drawing it, so this is fog-legal by the same logic as `docs/threat-map.md`. Note the repo's `state/fog.py` warning that getters "only test IsRevealed but return live values": on a fogged tile `GetImprovementType()` returns the *current* state, which can differ from the player's remembered map (a camp cleared by someone else). More Lenses has the same leak.
- City Overlap is a cheap settle heuristic: count cities within range 6/9 using `Map.GetPlotDistance`.
- "Next border growth plot" (CQUI) is a candidate read for builder/settle planning; its API was not traced (likely `city:GetCulture():GetNextPlot()`; unverified).

### Gaps
- Better Report Screen and Sukritact mods were not examined (no fetch made); likely the same report-screen getters.
- CQUI CE source files not read; API for next-plot and smart banner not confirmed.

## 5. The game AI's own data exposed to Lua, and fog pitfalls

### Takeaway
The game AI's influence/danger map is exposed: `player:GetInfluenceMap()` with `.Find(plotIndex)`, used only by the FireTuner AI debug panel to highlight "threatened spots". It is an AI-internal, tuner-only view, almost certainly computed with full knowledge, so it must be treated as a cheat. The legal route is: visibility-gate every live read with `PlayersVisibility[me]:IsVisible/IsRevealed`, and use the game's simulation functions only on things currently visible.

### Cited Findings
- `TunerAiPanel.lua`: `local influenceMap = player:GetInfluenceMap()`; for each plot index, `if influenceMap.Find(plotIndex) >= g_DangerThreshold` then the plot is a "threatened spot", drawn with `UILens.SetLayerHexes(PlotHighlightTypes.PLACEMENT, ...)` — [TunerAiPanel.lua](file:///C:/Program%20Files%20(x86)/Steam/steamapps/common/Sid%20Meier's%20Civilization%20VI/Base/Assets/UI/Tuner/TunerAiPanel.lua) lines 1-27
- Visibility API used by shipping UI: `PlayersVisibility[id]:IsRevealed(x,y)` (23 uses), `IsVisible` (21), `IsUnitVisible` (2), `GetVisibilityCount` (1); also `ChangeVisibilityCount` and `RevealAllPlots` (write/cheat functions) — grep across `CIV6\Base`, `CIV6\DLC`
- `GetReachableZonesOfControl(kUnit, true)`: the `true` flag means "only plots visible to the unit" — SelectedUnit.lua line 54
- The combat preview skips the local player's own units and runs only when the game core is idle — UnitPanel.lua around line 3305
- Repo rules: "GameCore can read every player's state. Fog-of-war filtering must be enforced"; "Strict fairness: own fog of war only, no cheat APIs" — [docs/decisions.md](../../decisions.md) lines 13, 68
- Context availability of Civ VI Lua methods differs between UI and gameplay sides and is undocumented; Civ VI's Lua is described as "a truncated implementation" — [CivFanatics Lua Objects](https://forums.civfanatics.com/threads/lua-objects.601146/page-5)

### Inferences
Fog-legality table (L = legal as used by the human UI, G = legal only with a visibility gate, X = leaks, do not use):

| API | Rating | Note |
|---|---|---|
| `player:GetInfluenceMap()` | X | AI debug data, tuner only, almost certainly full knowledge |
| `GetDiplomaticAI():GetThreatFrom/GetTrustFrom` | X | AI opinion, not shown in shipping UI |
| `PlayersVisibility:RevealAllPlots/ChangeVisibilityCount` | X | changes state |
| plot getters (`GetImprovementType`, `GetOwner`, `GetDistrictType` ...) | G | live values; gate on `IsVisible` for current truth, `IsRevealed` only for terrain-like facts |
| `Map.GetUnitsAt`, `Players[i]:GetUnits()` | G | gate each unit on `IsVisible(x,y)` / `IsUnitVisible` |
| `CombatManager.SimulateAttackVersus/Into` | G | only against a visible target; the human UI shows it only on hover over a visible flag |
| `UnitManager.GetReachableMovement/Targets` (own units) | L | the human sees these overlays |
| same on enemy units | G? | not a human UI action; unverified whether it uses hidden info |
| `GetReachableZonesOfControl(u, true)` | L | pass `true` |
| `GetMoveToPathEx` (own units) | L | human path preview |
| `plot:GetAdjacencyYield/CanHaveDistrict/GetAppeal` | L | human previews (own cities) |
| `GetCulturalIdentity():*` own cities | L | city panel |
| WorldRankings getters (`GetScore`, `GetTouristsTo`, `GetTurnsUntilVictory`) | L/G | shown for all players; respect met/unmet (`IsHidden` appears in the screen) |
| `pCity:GetPower()` own cities | L | power lens |

- Speed: the repo already batches Lua in one tuner execute per read. Each new read here should be added to an existing batch (e.g. combat sims for all visible enemy/own pairs within 3 tiles), not one call per pair.

### Gaps
- Not verified live whether `GetInfluenceMap()` respects fog (it is assumed not to).
- Not verified which tuner Lua state (the repo's chosen context index) exposes `CombatManager`, `UnitManager.GetReachable*`, `GetMoveToPathEx`, `GetCityIdentityPressures`, or `GetAdjacencyYield`. Each needs a one-line `print(type(X))` probe in that state before building on it.
- Per-call tuner latency of these functions was not measured.
