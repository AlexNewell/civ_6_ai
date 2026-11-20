# Settler-mode loyalty preview and other UI-only information: how the bot can read it

Scope: how the Civ VI (Gathering Storm) bot in this repo can get the per-tile loyalty numbers a human sees with a settler selected, without reading the screen, and what other human-visible information is hard to reach from the tuner/MCP connection.

Local sources are cited as file paths. "Game install" means `C:\Program Files (x86)\Steam\steamapps\common\Sid Meier's Civilization VI`, which is present on this machine and was searched read-only.

**Headline finding (corrects PR 240):** the settler loyalty numbers do come from a script call. The shipped UI script `DLC/Expansion2/UI/Additions/SettlerInfluenceIconManager.lua` calls `Map.GetContinentPlotsLoyalty()`, which returns a table of `{plotIndex -> loyalty value}`, and draws each value as the flag-and-number icon. It is a UI-context function, and the bot already runs Lua in the UI (`"ingame"`) context. So one bespoke Lua read, filtered to revealed plots, should give the bot the same numbers the human sees. Two other projects already do this (details below). PR 240's note says "no script call for them was found". That was wrong: its search looked for loyalty and identity names and missed the `Settler*IconManager` files.

---

## 1. How does the game's own UI compute the settler loyalty preview?

### Takeaway
The settler-lens numbers are not computed in Lua. The engine computes them, and `Map.GetContinentPlotsLoyalty()` exposes them as a table of plot index to an integer "loyalty per turn" value. The UI script draws that table only while the settler lens layer (`Hex_Coloring_Water_Availablity`) is on, and redraws it when plot visibility changes.

### Cited Findings
- `SettlerInfluenceIconManager.lua` (Expansion2; Expansion1 has a near-identical copy) has `RealizeInfluenceTiles()`. It returns early unless `UILens.IsLayerOn(m_HexColoringWaterAvail)`. It then runs `local plots = Map.GetContinentPlotsLoyalty()` and `for plotID, loyaltyVal in pairs(plots)`, gets the plot by index and sets `instance.BonusText:SetText(loyaltyVal)` with tooltip `LOC_SETTLER_LOYALTY_WARNING_TOOLTIP`. — game install `DLC\Expansion2\UI\Additions\SettlerInfluenceIconManager.lua` lines 95-126; mirror at [civfanatics/Civ6-UIFiles-Restructured](https://github.com/civfanatics/Civ6-UIFiles-Restructured/blob/master/DLC/Expansion2/UI/Additions/SettlerInfluenceIconManager.lua)
- The redraw is triggered by `Events.LensLayerOn` (only for the settler water-availability layer) and by `Events.PlotVisibilityChanged`. — same file, lines 154-196
- The UI context is registered as `<LuaContext ID="SettlerInfluenceIconManager" FileName="SettlerInfluenceIconManager"/>` inside InGame. — game install `DLC\Expansion2\UI\Replacements\InGame.xml` line 21
- English tooltip text: "The influence of nearby cities would apply {1_LoyaltyPerTurnAmount} Loyalty per turn to a new city settled here. Consider using a [ICON_Governor] Governor in the new city, or settling elsewhere." So each value is loyalty per turn from nearby cities' influence on a city founded on that plot. — game install `DLC\Expansion2\Text\en_US\Expansion1_InGameText.xml` lines 565-566
- The separate Loyalty lens (`Cultural_Identity_Lens`) is different. It is drawn by `MinimapPanel_Expansion1.lua` from existing cities' `pCulturalIdentity:GetCityIdentityPressures()` (pressure waves between cities) and `GetConversionOutcome()`, using flag icons `LoyaltyFlag_Up`/`LoyaltyFlag_Down`. It does not give per-candidate-tile numbers. — game install `DLC\Expansion2\UI\Replacements\MinimapPanel_Expansion1.lua` lines 62-119
- The per-city loyalty APIs the UI uses are `City:GetCulturalIdentity()`, then `GetLoyalty`, `GetMaxLoyalty`, `GetLoyaltyPerTurn`, `GetLoyaltyLevel`, `GetCityIdentityPressures`, `GetIdentitySourcesBreakdown`, `GetIdentitySourcesDetailedBreakdown`, `GetPotentialTransferPlayer`, `GetTurnsToConversion`, `GetConversionOutcome`, and `City:GetLoyaltyAdvice()`. — game install `DLC\Expansion2\UI\Additions\CityPanelCulture.lua` lines 41-174, `CityBanners\CityBannerManager.lua` lines 2195-2262
- A community API knowledge base lists `Map.GetContinentPlotsLoyalty` as **UI: true, Script: false**, with no arguments. — [Sukritact Civ VI Modding Knowledge Base: Map.GetContinentPlotsLoyalty](https://github.com/Sukritact/Civilization-VI-Modding-Knowledge-Base/blob/main/Objects/Map/Map.GetContinentPlotsLoyalty.md)
- A guide (written before Gathering Storm) says the settler numbers "reflect the status at the beginning of your turn and are just raw pressure numbers". — [CivFanatics Civ VI Loyalty Guide](https://forums.civfanatics.com/resources/civ-vi-loyalty-guide.27114)

### Inferences
- The values are the engine's nearby-city pressure term for a hypothetical new city. They are not the final rate after governor (+8), amenities, policies and so on. The CIVVIS project draws the same conclusion (section 4).
- The values are not computed by UI Lua, so we cannot reproduce exactly what the engine does internally. The table read is the authoritative source.

### Gaps
- What "Continent" means is not documented. The table may cover only the continent of the selected settler, every continent with one of our settlers, or every land plot that has non-zero pressure. CIVVIS tested only on a Tiny Pangaea map (one continent), so this is still open. It needs a live probe on a multi-continent map.
- It is unknown whether omitted plots mean zero pressure or "not computed". CIVVIS treats an omitted plot as unknown, not zero.
- Issue 239's screenshot lists a value of -22. That conflicts with the community formula's clamp of +/-20. It may be a misread of the screenshot, or the preview may not be clamped. To verify live.

---

## 2. UI-only vs gameplay context, and how to bridge

### Takeaway
`Map.GetContinentPlotsLoyalty` exists only in UI contexts (InGame and its child contexts), not in GameCore/gameplay scripts. The bot's tuner connection already sends Lua to the InGame state (`execute_lua(code, "ingame")`), and also to any state by index. So no mod is needed. Call it directly in InGame. A bridge (ExposedMembers, LuaEvents, a UI mod) is only needed if a GameCore-side read must see the values.

### Cited Findings
- civ6-mcp finds every Lua state the tuner exposes (`lua_states: index -> name`) and records `gamecore_index` and `ingame_index`. `execute_write` runs Lua "in InGame context". — `.venv\Lib\site-packages\civ_mcp\connection.py` lines 4, 35-82, 125-126
- The bot already runs bespoke Lua in InGame, for example `game.execute_lua(CITY_TILES_LUA, "ingame")` (one call reads all cities' tiles), and in a specific state by index (`execute_lua('pcall(OnClose) ...', str(index))`). — `src\civ6_ai\city_tiles.py` line 113; `src\civ6_ai\blockers.py` lines 244-251
- The repo has already hit context-specific functions. `UnitManager.CanStartOperation` "exists only in InGame", so the GameCore snapshot crashed until the call was guarded. `Game.GetGameSpeedType()` does not exist in InGame. — `src\civ6_ai\fastcalls.py` lines 94-123; `src\civ6_ai\game_speed.py` lines 1-3
- `state/fog.py` marks raw `execute_lua` as BANNED ("Raw game access bypasses every rule here"). Bespoke reads go through their own audited modules instead. — `src\civ6_ai\state\fog.py` lines 147-160
- The repo's spike code already uses `ExposedMembers` as a shared table across Lua contexts. It is sent over the tuner "once per Lua context" and needs no installed mod. — `mods\s1_hold_turn\s1_hooks.lua` lines 1-15
- civ6-mcp warns not to send InGame queries while AI civs are taking their turns ("CRITICAL: Do NOT send InGame queries while AI civs are processing their turns"). — `.venv\Lib\site-packages\civ_mcp\end_turn.py` lines 1159-1160

### Bridging patterns (from the cited code; general Civ VI modding practice)
1. **Direct tuner call into the InGame state (recommended).** Send a short fixed Lua snippet that calls `Map.GetContinentPlotsLoyalty()`, filters by `PlayersVisibility[me]:IsRevealed(idx)`, and prints `idx,value` lines. Cost: one tuner round trip, about the floor of a normal call after `fastcalls.shorten_call_waits` (about 0.07 s of idle drain plus execution). The table is small (land plots near foreign cities). It fits the 10 s/turn budget easily if read once per turn, only while a settler exists or one is planned.
2. **Run in the `SettlerInfluenceIconManager` state itself, by index.** Possible (the `lua_states` index), but not needed. The `Map` function is global in UI contexts, as the Accessibility mod shows by calling it from its own context (section 4).
3. **ExposedMembers cache.** A UI-context snippet registers `Events.PlotVisibilityChanged` / `Events.TurnBegin` / `Events.CityAddedToMap` handlers that write `ExposedMembers.Civ6Ai.loyalty = Map.GetContinentPlotsLoyalty()`. GameCore code, or the end-of-turn snapshot, then reads the cache. Useful only if the read must happen inside the GameCore snapshot. It adds staleness risk and the handlers must be re-installed after a reload, the way the `s1_hooks` spike does it.
4. **LuaEvents / installed UI mod.** An installed mod with a UI `AddUserInterfaces`/`InGame` context can do the same thing persistently. This is more setup, with no speed gain over (1).
5. **Game/Plot `SetProperty`.** UI scripts generally cannot write gameplay properties, so this is not a viable bridge from UI to gameplay. It is not verified in this research; see Gaps.

### Inferences
- Call order matters. Reading during AI turn processing can hang (civ6-mcp's warning above), so read during the bot's own turn, as other InGame reads already do.
- The UI only draws the icons while the settler lens is on, but CIVVIS saw the function return data with no settler selected and no lens change (section 4). So the bot should not need to select a settler or toggle the lens. Toggling would also cost UI time.

### Gaps
- Not live-tested in this repo. The PR 240 probe should now target `Map.GetContinentPlotsLoyalty()` specifically, run with and without a settler owned, selected, or lens-on, and on multi-continent maps.
- Whether UI context can call `Game:SetProperty`/`Plot:SetProperty` was not verified from primary sources.

---

## 3. Can loyalty pressure be reconstructed from formulas?

### Takeaway
Yes, approximately. The repo already has two formula-based estimates (civ6-mcp's `loyalty_pressure` and `policies/settling.py`). The community formula comes from before Gathering Storm and at least one user reports it "doesn't stack up" in practice. The native table should replace the nearby-city term. A model is still needed for the modifiers the preview excludes (governor, amenities, policies, age).

### Cited Findings
- Community formula: Domestic = domestic age factor x sum over domestic cities within 9 tiles of (population x (10 - distance)). Foreign = sum over foreign cities of (population x (10 - distance) x foreign age factor). Pressure = 10 x (Domestic - Foreign) / (min(Domestic, Foreign) + 0.5), clamped to +/-20. Age factors are 1.0 normal, 1.5 golden, 0.5 dark. The capital counts as an extra city of the same population at the same spot (age factor always 1.0). Bread and Circuses doubles population. Free cities and city-states exert only domestic pressure. The guide was written before Gathering Storm. — [CivFanatics Civ VI Loyalty Guide](https://forums.civfanatics.com/resources/civ-vi-loyalty-guide.27114)
- Other modifiers listed there: any governor +8 (Victor's Praetorium +2 more). Amenities: happy +3, ecstatic +6, displeased -3, unhappy or worse -6. Limitanei and Martial Law +2 with a garrison. Police State -2 (as listed in the guide). Occupied -5. A new city starts at 100% loyalty. — [CivFanatics Civ VI Loyalty Guide](https://forums.civfanatics.com/resources/civ-vi-loyalty-guide.27114)
- Disagreement: a forum participant testing the formula said "this just doesn't stack up". — search summary of [CivFanatics thread "Can someone please explain ... loyalty"](https://forums.civfanatics.com/threads/can-someone-please-explain-cheat-sorry-loyalty-to-me.656887) (from a search result; the page was not fetched)
- civ6-mcp's settle-scan estimate: for each city within 9 tiles, `population * (10 - distance) / 10` (capital x1.5), friendly minus enemy, then `10 * diff / (min + 0.5)` clamped to +/-20. `state/fog.py` drops it because it reads fogged cities. — PR 240 diff `docs/research/loyalty-pressure-map.md`; `src\civ6_ai\state\fog.py` lines 147-153
- The repo's own model in `policies/settling.py` (`loyalty_rate`, `safe_loyalty_rate`) adds growth, fog and new-city drag, tuned on earlier runs. — PR 240 diff
- Evidence from another bot: CIVVIS founded a city on a plot with a native **-20** warning. The real initial city rate was -20, a governor brought it to -12, and the city revolted 8 turns later. So the preview value matched the first-turn rate in that case. — [CIVVIS: native settlement loyalty warnings](https://github.com/MartinHalvorson/CIVVIS/blob/HEAD/docs/eval/2026-10-15-native-settlement-loyalty-warnings.md)

### Inferences
- How accurate is the formula? Population, distance and the clamp should get the sign and rough size right near borders. The errors come from the age factor (rival ages are not always known), capital handling, Gathering Storm changes, and unseen cities. The native table removes all of these for the nearby-city term.
- Best hybrid, as CIVVIS did: native value for the nearby-city term, then add the modelled governor, amenity and policy terms for the bot's own plans. Keep the formula as a fallback when the table is empty or the plot is missing.
- The native table also gives calibration data: log both values per plot and fit the formula, closing PR 240's "calibrate against screenshots" step without screenshots.

### Gaps
- No primary (Firaxis) formula source was found, and nothing post-Gathering-Storm verified the community formula.
- The -22 value in issue 239 against the +/-20 clamp is unresolved.

---

## 4. How do community mods and other bots access this data?

### Takeaway
Everyone who shows settler loyalty per tile calls `Map.GetContinentPlotsLoyalty()` from UI context. No mod recomputes it. Two AI or automation projects already read it over a scripting connection and filter by visibility.

### Cited Findings
- **CIVVIS (another Civ VI AI)** exports a `settlement_loyalty` event from `Map.GetContinentPlotsLoyalty()` "for revealed plots only". It queries "only for the local player while an owned Settler exists" and "changes no unit selection or lens state". Over 100 turns: 205 available observations, 163 non-empty tables, no malformed values, at least 17 plots changing value (e.g. (9,15) went from -9 to -12). "Tables were initially empty and supplied negative values from turn 39 without selecting a unit or changing a lens." Omitted plots are treated as unknown. — [CIVVIS: native settlement loyalty warnings](https://github.com/MartinHalvorson/CIVVIS/blob/HEAD/docs/eval/2026-10-15-native-settlement-loyalty-warnings.md)
- CIVVIS then substituted the native value for its modelled nearby-city pressure and still models governors, amenities, policies and starvation. On the captured case its repaired decision rejected the -20 sites ("approximately five turns to revolt"). — [CIVVIS: native settlement pressure forecast](https://github.com/MartinHalvorson/CIVVIS/blob/HEAD/docs/eval/2026-10-15-native-settlement-pressure-forecast.md)
- **auto-civ6 (a civ6-mcp fork)** adds to its settle-scan preamble: `pcall(function() local v=Map.GetContinentPlotsLoyalty(); ... end)`, commented "Same warning getter as SettlerInfluenceIconManager. Missing entry is not evidence of zero pressure". It also limits the city list to our cities or currently visible foreign cities, so "hidden foreign cities must not alter the candidate board". — [Harryleft/auto-civ6 src/civ_mcp/lua/map.py](https://github.com/Harryleft/auto-civ6/blob/HEAD/src/civ_mcp/lua/map.py)
- The installed civ6-mcp in this repo's `.venv` has no reference to `GetContinentPlotsLoyalty`. — grep of `.venv\Lib\site-packages\civ_mcp` (no matches)
- **XenoAmess/civ6_dev** `settlement_warning_info.lua` reads `Map.GetContinentPlotsLoyalty()[index]`. It runs only for a plot that is revealed and visible, unowned or owned by us, and not a city. It re-checks plot state after the read and labels results `KNOWN_NEGATIVE` / `KNOWN_NONNEGATIVE` / `UNKNOWN`. Its rulesets: STANDARD gives "no_loyalty_rules", EXPANSION_1/2 are supported. — [XenoAmess/civ6_dev tools/lua/settlement_warning_info.lua](https://github.com/XenoAmess/civ6_dev/blob/HEAD/tools/lua/settlement_warning_info.lua)
- **Civ VI Accessibility Integration** (a screen-reader mod) reads `Map.GetContinentPlotsLoyalty()[plot:GetIndex()]` in its settler-lens plot info and speaks it as "+N"/"-N". This is a precedent for turning the visual preview into data for a non-visual player. — [flat-arther/Civ-VI-Accessibility-Integration interfaceInfoHelpers_CAI.lua](https://github.com/flat-arther/Civ-VI-Accessibility-Integration/blob/HEAD/src/UI/inGame/interfaceInfoHelpers_CAI.lua)
- **More Lenses / CQUI:** search found an alternate settler lens (Ctrl held: city radius, overlap, resources) and loyalty on city banners, but no per-tile loyalty recomputation. — [More Lenses thread](https://forums.civfanatics.com/threads/more-lenses-now-compatible-with-gathering-storm.606150/), [CQUI Community Edition](https://github.com/civfanatics/CQUI_Community-Edition)
- A gameplay mod (`Civ6HarmonyInDiversity`) replaces `UI/Replacement/SettlerInfluenceIconManager.lua`, so mods can change this UI file. — GitHub code search result for [XHH-Coco/Civ6HarmonyInDiversity](https://github.com/XHH-Coco/Civ6HarmonyInDiversity)

### Inferences
- The accessibility mod is a useful legitimacy precedent: the data is the human-visible preview, delivered as text.
- If a mod that replaces `SettlerInfluenceIconManager` is active, the drawn numbers might differ from the API. Reading the API directly is still the engine's value.

### Gaps
- More Lenses/CQUI source was not checked line by line. Only search summaries were used.

---

## 5. Fairness: what would leak information a human cannot see?

### Takeaway
Reading the table is human-equivalent if the bot keeps only plots the human could see the icon on (revealed plots) and reads it at the same time the human would (during our turn). Raw use of the whole table may leak.

### Cited Findings
- The UI draws every returned plot, but the redraw is tied to `PlotVisibilityChanged`. Unrevealed plots are covered by the map's fog rendering. — `SettlerInfluenceIconManager.lua` lines 177-181, 195
- CIVVIS restricts to revealed plots. XenoAmess requires revealed **and** currently visible, and never queries owner or city on hidden plots. auto-civ6 removes hidden foreign cities from its own scoring. — sources in section 4
- The repo's rule: the settle scan's `loyalty_pressure` is dropped because it reads fogged cities. Any exact per-tile number "may use only cities the player has seen", matching the map's behaviour. — `src\civ6_ai\state\fog.py` lines 147-153; PR 240 diff

### Inferences / flags
- **Flag 1 (unrevealed plots):** the table may contain plots the player has never revealed. Drop them. Their value or presence could reveal a hidden foreign city.
- **Flag 2 (hidden-city influence on revealed plots):** a value on a revealed plot may include pressure from a foreign city the player has not seen. The human sees exactly the same number on that tile, so this matches the human and is acceptable. Record it as a FLAGGED read in `fog.py` with this note. Do not back-solve city positions from the values. A human could do that roughly, but deliberate triangulation goes beyond normal play.
- **Flag 3 (no settler):** a human sees the numbers only with a settler selected. Reading while we own or plan a settler matches what the human can see. CIVVIS reads only while a settler exists, which is the conservative choice. Reading with no settler at all is a minor timing advantage. Gate on "settler exists or is in production" to be safe.
- `GetCityIdentityPressures()` and `GetIdentitySourcesBreakdown()` on **foreign** cities: the UI shows a foreign city banner's loyalty breakdown only for cities the player can see, per `CityBannerManager`. Any bot read must check the city is visible or revealed first.

### Gaps
- It was not verified whether the engine's table excludes unrevealed plots itself.

---

## 6. General: human-visible information that is hard to reach from the bot's connection, and standard workarounds

### Takeaway
The pattern is the same everywhere. Previews, tooltips and lens overlays are computed by engine functions exposed only to UI Lua (InGame), or by UI Lua that combines several such calls. GameCore, where many civ6-mcp reads and the end-of-turn snapshot run, does not have them. The workaround, ordered by preference: (1) call the same function the UI file calls, in InGame, through the tuner; (2) cache it through ExposedMembers if a GameCore read needs it; (3) recompute from documented formulas as a fallback. Never use screen reading or omniscient GameCore reads.

### Cited Findings (UI-side previews and their underlying calls, from shipped UI Lua)
- **Combat preview:** `CombatManager.SimulateAttackVersus(attacker:GetComponentID(), defender:GetComponentID(), combatType)` in `UnitPanel.lua`. — game install `Base\Assets\UI\Panels\UnitPanel.lua` lines 3319-3352
- **District adjacency preview:** `plot:GetAdjacencyYield(localPlayer, cityID, eDistrict, yield)` and `plot:GetAdjacencyBonusTooltip(...)`. — game install `Base\Assets\UI\AdjacencyBonusSupport.lua` lines 188-200
- **Purchase costs:** `pCity:GetGold():GetPurchaseCost(yield, hash[, formation])`. — game install `Base\Assets\UI\Panels\ProductionPanel.lua` lines 1673-1820
- **Operation legality and line of sight:** `UnitManager.CanStartOperation` is InGame-only (the repo hit this). — `src\civ6_ai\fastcalls.py` lines 94-106
- **UI screen state:** `ContextPtr:LookUpControl("/InGame/...")`, `UI.CanEndTurn`, `NotificationManager` are InGame-only and the repo already uses them. — `src\civ6_ai\blockers.py` lines 84-86, 220; `.venv\...\civ_mcp\end_turn.py` line 971
- **Governor effect preview:** `GetIdentitySourcesBreakdown()` before and after a governor change. — game install `DLC\Expansion2\UI\Additions\GovernorAssignmentChooser.lua` lines 281-302
- **Settler flood/volcano warnings** come from a sibling UI context (`SettlerWarningIconManager`, with `RiverManager.CanBeFlooded`), so they are reachable the same way. — game install `DLC\Expansion2\UI\Replacements\InGame.xml` line 33; [Accessibility mod](https://github.com/flat-arther/Civ-VI-Accessibility-Integration/blob/HEAD/src/UI/inGame/interfaceInfoHelpers_CAI.lua)
- **Finding which context has a function:** the Sukritact knowledge base tags every API with UI and Script columns (e.g. `GetContinentPlotsLoyalty`: UI yes, Script no). — [Sukritact KB](https://github.com/Sukritact/Civilization-VI-Modding-Knowledge-Base)
- **Discovery method that worked here:** grep the shipped UI Lua for the tooltip's LOC tag or the lens layer hash, find the drawing context (`*IconManager.lua`, `*Lens*.lua`) and read the engine call it makes. PR 240's search for loyalty and identity names missed it because the file is named "SettlerInfluence".

### Inferences
- Speed: each method is one tuner round trip. Batch several UI-only reads into one InGame snippet, as `city_tiles.py` does, to stay well under the 10 s turn target. ExposedMembers caching saves nothing if the bot reads once per turn anyway. It helps only when GameCore code needs the value mid-snapshot.
- Robustness: wrap calls in `pcall` and check the function exists (the `fastcalls.fix_units_query` pattern). Fail loudly in tests if a pinned function disappears. Treat an empty table as "unknown", not "safe".
- Fairness rule for every such read: the UI function is usually scoped to the local player, which is good. Still filter outputs by revealed or visible state, because the UI relies on fog rendering, not data filtering, to hide things.

### Gaps
- Exact context availability of `CombatManager.SimulateAttackVersus`, `GetAdjacencyYield` and `GetPurchaseCost` (UI-only or also gameplay) was not checked one by one. Use the Sukritact KB or a live `type(fn)` probe in each state.
- No measured latency for `Map.GetContinentPlotsLoyalty()` itself. CIVVIS reports only whole-second clocks ("zero timing rows do not establish the query's fine-grained cost").

---

## Ranked options for the bot (summary for the report writer)
1. **Direct InGame call to `Map.GetContinentPlotsLoyalty()`**, filtered to revealed plots, read once per turn while a settler exists or is planned. Merge it into the settle-site score as the nearby-city term, and add the modelled governor and amenity effects. Highest feasibility: no mod, one call, already done by CIVVIS, auto-civ6, XenoAmess and the Accessibility mod. Register it in `state/fog.py` as FLAGGED (flag 2 above).
2. **ExposedMembers cache** filled by a UI-context event hook, for the GameCore snapshot if needed.
3. **Formula estimate** (existing civ6-mcp and `policies/settling.py`), calibrated against logged native values. Use it as a fallback when the table is empty or a plot is missing.
4. Screen reading or omniscient GameCore reads: excluded (out of scope or unfair).
