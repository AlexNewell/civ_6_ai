# Influence maps, threat maps and spatial reasoning in game AI (for the Civ VI bot threat map)

Scope: established game-AI techniques that apply to a turn-based hex 4X bot that reads structured,
fog-respecting API data. Each section ends with what the project already does (per
`docs/threat-map.md`, issue 238 / PR 241) and what is new. Sources were fetched where possible;
several primary sources (GameDev.net, AAAI OJS, Civ fandom wiki) blocked automated fetches, so their
content comes only from search snippets and is flagged as such.

## 1. Canonical influence-map design: propagation, falloff, momentum, layer combination

### Takeaway
The best-documented modern design is Dave Mark's "Modular Tactical Influence Maps" (Game AI Pro 2,
ch. 30): per-faction base maps (location/proximity and threat), normalized precomputed "templates"
stamped in and scaled by unit strength, falloff by a response curve (linear for proximity, exponent-4
polynomial for threat, a ring for ranged units), path distance instead of straight distance when
obstacles matter, and small "working maps" combined with add / multiply / normalize / inverse and
queried for the highest point. Champandard's tutorial adds the momentum (history vs current blend)
and decay (exponential distance falloff) parameters. "Tension" and "vulnerability" as named layers
could not be confirmed in any fetched primary source.

### Cited Findings
- Author is Dave Mark; the chapter lists uses including "Determining the relative threat that an agent was under at a given moment", "Determining safe locations to evade to or withdraw to", positioning defenders between enemies and vulnerable allies, and "Determining when there enough allies near a prospective target to avoid 'piling on'". Developed for two online RPG prototypes but said to apply to strategy games. — [Mark, Game AI Pro 2 ch. 30 (PDF)](http://www.gameaipro.com/GameAIPro2/GameAIPro2_Chapter30_Modular_Tactical_Influence_Maps.pdf)
- Linear propagation: `Influence = MaxValue - MaxValue * (Distance / MaxDistance)` (eq. 30.1); inverse polynomial: `Influence = MaxValue - MaxValue * (Distance / MaxDistance)^2` (eq. 30.2). — [Mark, GAIP2 ch. 30](http://www.gameaipro.com/GameAIPro2/GameAIPro2_Chapter30_Modular_Tactical_Influence_Maps.pdf)
- "Threat maps are best calculated with a polynomial formula that reflects that the threat is similar across most of the distance and only drops off as it reaches its furthest point", using exponent 4 (eq. 30.3). Location/proximity maps use linear falloff. — [Mark, GAIP2 ch. 30](http://www.gameaipro.com/GameAIPro2/GameAIPro2_Chapter30_Modular_Tactical_Influence_Maps.pdf)
- Ranged threat need not peak at the agent: "a catapult is fairly innocuous at close range but has a ring of influence that it projects at its firing range." — [Mark, GAIP2 ch. 30](http://www.gameaipro.com/GameAIPro2/GameAIPro2_Chapter30_Modular_Tactical_Influence_Maps.pdf)
- For movement threat ("what could that tiger bite in the next 3 seconds?") use path distance, not straight-line distance; "a common solution is to precompute cell path-distances ... using an undirected Dijkstra search starting from the agent's location", with nonnegligible run-time and engineering cost. — [Mark, GAIP2 ch. 30](http://www.gameaipro.com/GameAIPro2/GameAIPro2_Chapter30_Modular_Tactical_Influence_Maps.pdf)
- Overlapping threat sums: a cell threatened by several agents can exceed any one agent's value. One base map per type (proximity, threat) per faction. — [Mark, GAIP2 ch. 30](http://www.gameaipro.com/GameAIPro2/GameAIPro2_Chapter30_Modular_Tactical_Influence_Maps.pdf)
- Templates are precomputed, normalized to 1.0, and multiplied by a magnitude when stamped, so a strength-3 unit stamps 3x the template; this replaces repeated distance/curve evaluation with additions. Map edges must be clipped. — [Mark, GAIP2 ch. 30](http://www.gameaipro.com/GameAIPro2/GameAIPro2_Chapter30_Modular_Tactical_Influence_Maps.pdf)
- Signed combination: enemy maps inverted and added to ally maps give a topography from "strongly ours" to "weakly ours", usable to move toward the front, avoid conflict or flank. — [Mark, GAIP2 ch. 30](http://www.gameaipro.com/GameAIPro2/GameAIPro2_Chapter30_Modular_Tactical_Influence_Maps.pdf)
- Working maps: small temporary maps around the querying agent, built e.g. `AddMap(EnemyLocationMap, 1.0); AddMap(AllyLocationMap, -0.5); MultiplyMap(InterestTemplate, 1.0); GetHighestPoint()`. Helpers: normalize (min-max to 0..1) and inverse (`1 - value`); adding an inverse instead of subtracting keeps "get highest point" usable. — [Mark, GAIP2 ch. 30](http://www.gameaipro.com/GameAIPro2/GameAIPro2_Chapter30_Modular_Tactical_Influence_Maps.pdf)
- "Personal interest template": multiply a result by a distance falloff around the agent so near cells win over slightly better far ones ("there is something interesting over there, but it is too far away for me to care"); points outside go to 0. "Safer spot" = inverse(enemy location map) x interest template, take highest. "Threat axis": point along the line between agent and enemy center of mass, used for blocking/positioning. — [Mark, GAIP2 ch. 30](http://www.gameaipro.com/GameAIPro2/GameAIPro2_Chapter30_Modular_Tactical_Influence_Maps.pdf)
- Champandard's 2011 GameDev.net tutorial "The Core Mechanics of Influence Mapping": set source values then repeatedly blur/spread to neighbours. Momentum near 1.0 biases toward historical values (good for statistics about past attacks); near 0.0 favours the current calculation (low-level positioning). Lower decay for large strategic maps, higher decay for local tactical maps. (Page itself returned 403; content from search snippets only.) — [GameDev.net r2799 (search snippet)](https://www.gamedev.net/articles/programming/artificial-intelligence/the-core-mechanics-of-influence-mapping-r2799)
- Same spread/momentum framing reused in a MOBA (League of Legends) paper: spread controls reach, momentum controls how long information persists. — [arXiv 1706.02789](https://arxiv.org/pdf/1706.02789.pdf) (search snippet)
- Frontier = where influence changes sign between adjacent tiles; zero = no-man's-land; gradient = influence difference between neighbours; RTS lecture example halves value per tile of distance. — [UMich EECS 494 lecture 19 (search snippet)](https://web.eecs.umich.edu/~sugih/courses/eecs494/fall06/lectures/lecture19-strategy.pdf)
- "Degree of control" = winning side's influence minus second side's; a large margin means a secure position. — [SDU DM810 lecture 12 (search snippet)](https://imada.sdu.dk/u/march/Teaching/AY2012-2013/DM810/Slides/dm810-lec12.pdf)

### Inferences
- Already in the project: sources with strength scaled per unit ("one point = a barbarian warrior", like Mark's template magnitude), summed overlap, movement-cost spread (Mark's path-distance recommendation), and a separate own-forces layer (cover), which together are Mark's two-faction setup.
- New / worth adopting:
  - Curve shape. The note says threat fades linearly with tile distance. Mark recommends a flat-then-drop (exponent 4) shape for threat. For a turn-based game the natural equivalent is a step function on turns-to-arrive: full threat if the source can attack this turn (move + attack range), reduced next turn, ~0 beyond the horizon. That is more meaningful in Civ VI than distance falloff.
  - Ranged units (archers, slingers once promoted, catapults, cities) project a ring at attack range from every reachable tile, not a blob at their location (the Civ V Vox Populi code does exactly this; see section 2).
  - Derived layers, cheap with numpy once threat and cover are arrays: `control = cover - threat` (signed; frontier where the sign flips between neighbours), `tension = cover + threat` (where fighting is likely), `vulnerability = tension - |control|` (contested spots). These three formulas are the commonly taught set, but I could not confirm their original author in a fetched source (see Gaps); treat as convention.
  - Momentum for slow-changing strategic layers (e.g. a per-tile "barbarian spawn history" or "where we lost units"), blending `new = m*old + (1-m)*current`. The current design instead keeps discrete sources with ages, which is better for the tactical layer; momentum is a cheap add for a separate history layer.
  - Interest-template multiply is the right shape for recon targeting and garrison choice: score = layer x falloff(turns from the unit), take argmax.

### Gaps
- Could not fetch Champandard's tutorial text (403); the exact formula (I recall `exp(-distance * decay)` blended with momentum) is not verified here.
- Could not find a fetched primary source that names "tension" and "vulnerability" maps with those formulas. Commonly attributed to Ian Millington's *AI for Games* or to AiGameDev material; not verified.
- Paul Tozour's "Influence Mapping" (Game Programming Gems 2, 2001) and "Strategic Assessment Techniques" (AI Game Programming Wisdom) and Dave Mark's GDC 2015 influence-map talk were not fetched; contents unverified.

## 2. How shipping strategy games build threat/danger maps (Civ V in particular)

### Takeaway
The most concrete, inspectable reference is Civ V's `CvDangerPlots` as maintained in the open-source
Community Patch (Vox Populi) DLL: danger per plot = simulated expected damage from every hostile that
can reach (and, for ranged, shoot) that plot this turn, a fog penalty around our units and cities,
a one-turn memory of units that vanished into fog, discounted damage for non-visible plots, full
rebuild on turn or war change. It is strikingly close to what the project has built, and shows a few
refinements the project lacks. No GDC talk on Civ V/VI tactical maps was found.

### Cited Findings
- `CvDangerPlots` stores one `CvDangerPlotContents` per plot; `UpdateDangerInternal()` rebuilds from scratch over living players on other teams, skipping those at peace (`ShouldIgnorePlayer()`); enemy cities mark every plot in bombard range. Plot/feature turn damage and adjacent citadel damage are also added. — [Community-Patch-DLL CvDangerPlots.cpp](https://raw.githubusercontent.com/LoneGazebo/Community-Patch-DLL/master/CvGameCoreDLL_Expansion2/CvDangerPlots.cpp)
- Reach: `UpdateDangerSingleUnit()` calls `TacticalAIHelpers::GetAllPlotsInReachThisTurn()` with the unit's full moves (one-turn reach, not multi-turn), ignoring stacking and enemies; melee units threaten every reachable tile; ranged units threaten tiles found by `GetPlotsUnderRangedAttackFrom()` from their reachable tiles. — [CvDangerPlots.cpp](https://raw.githubusercontent.com/LoneGazebo/Community-Patch-DLL/master/CvGameCoreDLL_Expansion2/CvDangerPlots.cpp)
- Danger is expected damage, not raw strength: `GetDanger(const CvUnit*)` sums simulated attack damage with no RNG (so tooltips cannot change game state); melee uses attacker max strength vs an assumed defender equal to the attacker's base strength. Civilians: an enemy that can reach the plot (capture threat) returns `MAX_INT`. For a city, damage is split across the garrison's HP. — [CvDangerPlots.cpp](https://raw.githubusercontent.com/LoneGazebo/Community-Patch-DLL/master/CvGameCoreDLL_Expansion2/CvDangerPlots.cpp)
- Visibility: units invisible to the team or on invisible plots are ignored (with exceptions for AI players on cities and barbarian camps); if the target plot is not visible, simulated damage is cut to 80%. — [CvDangerPlots.cpp](https://raw.githubusercontent.com/LoneGazebo/Community-Patch-DLL/master/CvGameCoreDLL_Expansion2/CvDangerPlots.cpp)
- Fog: `AddFogDanger()` adds `FOG_DEFAULT_DANGER` (= 1) to each hidden plot within 2 of a unit or 3 of a city. A revealed but not visible barbarian camp triggers fog danger at range 3. — [CvDangerPlots.cpp](https://raw.githubusercontent.com/LoneGazebo/Community-Patch-DLL/master/CvGameCoreDLL_Expansion2/CvDangerPlots.cpp)
- Memory: `m_knownUnits` remembers attackers; `m_vanishedUnits` keeps units seen last turn but now out of sight for one turn only. Only player ID and unit memory are serialized; loading forces a rebuild. — [CvDangerPlots.cpp](https://raw.githubusercontent.com/LoneGazebo/Community-Patch-DLL/master/CvGameCoreDLL_Expansion2/CvDangerPlots.cpp)
- Update: full rebuild on turn change or war-state change; `SetDirty()` marks for later rebuild. Optional two-pass mode recomputes ignoring ZOC from our units likely to die (danger > HP). — [CvDangerPlots.cpp](https://raw.githubusercontent.com/LoneGazebo/Community-Patch-DLL/master/CvGameCoreDLL_Expansion2/CvDangerPlots.cpp)
- A December 2014 Civ V changelog says major civs "see" barbarian units on revealed tiles, which it calls a knowledge cheat; a forum post says Civ V AI danger evaluation pulls knowledge directly from the game, not the player's information. — [Steam changelog / CivFanatics (search snippets)](https://steamcommunity.com/sharedfiles/filedetails/changelog/312060464?p=2)
- Civ IV: `CvPlayerAI::AI_getPlotDanger` takes a range capped at a `DANGER_RANGE` constant; a modder found it "a lot less robust than I thought". — [CivFanatics modding thread (search snippet)](https://forums.civfanatics.com/threads/quick-modding-questions-thread.351519/page-271)
- Civ V's AI has four layers, with the tactical layer controlling individual units; Ed Beach described subsystems that create and execute strategies (IGN preview, quoted on forum). — [CivFanatics](https://forums.civfanatics.com/threads/civ-ai-discussion.356510)
- Supreme Commander 2: influence map reportedly a coarse grid without propagation to neighbours; AI groups units by the target's threat. Sorian (Planetary Annihilation) said mobile-unit threat values decay over time. — [FAForever / PA forums (search snippets)](https://forum.faforever.xyz/post/611)
- Killzone (Straatman & van der Sterren, GDC Europe 2005): position picking as a weighted sum of dozens of dynamic factors (cover, lines of fire, proximity, preferred range); tactical pathfinding adds cost for crossing friendly lines of fire. — [Guerrilla GDCE05 slides](https://www.guerrilla-games.com/media/News/Files/gdce05_killzone_ai.pdf) (search snippet)

### Inferences
- Already in the project and matching Civ V/VP: fog penalty near our cities (VP: 1 per hidden plot, radius 2 for units, 3 for cities), memory of unseen units (VP: one turn; project: `unit_memory_turns`), camps on revealed tiles kept as threats, legal-read stance (stricter than shipping Civ V which cheated on barbarians).
- New / worth adopting:
  - Express danger as expected HP damage to a specific defender using the Civ VI formula (section 4), not summed strength. This makes "will this unit/city survive next turn" a direct comparison of danger vs HP, which is what garrison need and retreat need.
  - Separate "can attack us this turn" (one-turn reach + attack range) from "can arrive in N turns". VP uses only one-turn reach for tactical danger; the project's turns-to-arrive field already supports both; a policy should read the turn-1 slice for safety and the multi-turn field for strategy.
  - Civilian rule: any tile a hostile melee unit can enter this turn is infinite danger for settlers/builders (capture). Cheap and high-value.
  - Discount for non-visible: VP multiplies by 0.8 for non-visible target plots. The project's stale-unit decay serves the same purpose; a per-age multiplier (e.g. 1.0 now, decaying with age) is the continuous version.
  - Rebuild-on-dirty: rebuild only when the turn changes or a read reveals tiles; matches the planned "mid-turn refresh".

### Gaps
- Firaxis vanilla Civ V/VI danger code (not the community patch) was not inspected; the VP file may differ from shipped Civ V. Civ VI's own AI (Lua behaviour trees + C++ tactical) has no published danger-map source that I found.
- No GDC talk or postmortem on Civ V/VI tactical AI, Total War, Paradox, Age of Empires or Company of Heroes influence maps was found in this pass.

## 3. Terrain analysis, Dijkstra maps, safety/flee maps, chokepoints, regions

### Takeaway
Multi-source Dijkstra ("distance-to-any") fields are the workhorse: one pass gives distance to the
nearest of many goals plus which goal is nearest, fields combine by min/sum/weighted sum, and
"flee" maps come from scaling and re-relaxing a distance field. For regions and chokepoints the
StarCraft line (Perkins 2010 BWTA, BWTA2, BWEM) is the reference; on a hex 4X map the cheap analogue
is connected-component labelling and narrow-passage detection on the walkable graph.

### Cited Findings
- Dijkstra map (Brogue author, RogueBasin): goals 0, others high, relax each cell to 1 + lowest neighbour until stable; agents "roll downhill". Many goals may be seeded at once (e.g. all unexplored tiles for autoexplore). — [RogueBasin, The Incredible Power of Dijkstra Maps](https://www.roguebasin.com/index.php/The_Incredible_Power_of_Dijkstra_Maps)
- Flee map: take the map toward the threat, multiply by about -1.2, re-run the scan; a more negative coefficient makes agents willing to break past the threat rather than corner themselves. Desire maps: one map per interest, per-agent coefficients, move to the neighbour maximizing sum(coef x map). Recompute only when goals change; many agents share maps. — [RogueBasin](https://www.roguebasin.com/index.php/The_Incredible_Power_of_Dijkstra_Maps)
- Red Blob Games: multi-source BFS queues every seed at distance 0; tracking `started_at` gives each tile's nearest seed (Voronoi-like territories); "The combined distance field to the closest source is the `min()` of multiple distance fields"; max/sum/difference build influence maps; switch to Dijkstra for variable movement costs; connected components (islands) in one pass, and tiles where neighbours come from two different islands are potential bridge points; BFS runs "around 1 million nodes per second". — [Red Blob Games, distance to any](https://www.redblobgames.com/pathfinding/distance-to-any/)
- Perkins (RPI, 2010): integrated chokepoint detection and region decomposition; found 97% of chokepoints humans found plus some they missed. BWTA builds regions from a pruned Voronoi diagram of walkable terrain; chokes are region boundaries; BWTA2 (Uriarte & Ontañón) at least 10x faster; BWEM a later tool. — [AITopics record](https://aitopics.org/doc/conferences:FBE37864); [Uriarte & Ontañón, AIIDE](https://ojs.aaai.org/index.php/AIIDE/article/view/12889); [Richoux 2022, arXiv 2205.08683](https://ar5iv.labs.arxiv.org/html/2205.08683)

### Inferences
- Already in the project: turns-to-arrive spread over a terrain store (a Dijkstra map per source, or multi-source).
- New / worth adopting:
  - One multi-source Dijkstra with "nearest source id" instead of per-source passes when only "nearest threat and its turns" is needed (see section 5 for scipy `min_only`).
  - Flee/safety map for civilians and wounded units: `safety = -1.2 * dist_to_threat`, re-relaxed, combined with distance to our nearest city.
  - Recon targeting by oldest knowledge is a desire map: seed tiles weighted by age-since-seen (or unexplored), multiply by interest falloff from the scout.
  - Chokepoints on hex land: tiles whose removal disconnects (articulation points of the walkable graph, computed once per terrain change), or tiles where the multi-source "nearest city" labels from two of our cities meet with a narrow width. Garrison/blocker placement and `path_clear` both benefit. BWTA-style Voronoi is overkill for a hex grid of a few thousand tiles.
  - Region labels (land masses, separated by water/mountains) let threat spread skip land walkers across water cheaply, and let "is this camp on my continent" be an O(1) check.

### Gaps
- No hex-specific chokepoint paper was found; the articulation-point approach above is my inference, not a cited method from a shipping game.

## 4. Combat outcome estimation for sizing forces (Civ VI formula, Lanchester, simulators)

### Takeaway
Civ VI combat is a deterministic-ish exponential in strength difference, so threat and cover should
be compared in expected damage, not added as raw strength. Lanchester-law models are a fast,
established alternative to full combat simulation in RTS bots; for one-unit-per-tile sequential
Civ combat, a short sequential damage simulation using the Civ VI formula is likely both cheap and
more accurate.

### Cited Findings
- Fan-derived Civ VI formula: `Damage = 30 * e^(StrengthDifference / 25) * Random(0.75, 1.25)`, randomness per unit. A later thread citing GlobalParameters.xml gives `Damage = [24 + rand(0,12)] * exp(0.04 * StrengthDifference)`, minimum 1 (equivalent to base 30 with +/-20%). Equal strength gives about 25-30 damage each side; +10 gives about 150% dealt, two-thirds taken; a ~30 difference is almost always a one-shot. — [CivFanatics: Hans Lemurson figures out the combat formula](https://forums.civfanatics.com/threads/hans-lemurson-figures-out-the-combat-formula.606147/latest); [Steam discussion](https://steamcommunity.com/app/289070/discussions/0/154642447914672165) (search snippets; fan measurements, treat constants as approximate)
- Stanescu, Barriga & Buro, "Using Lanchester Attrition Laws for Combat Prediction in StarCraft" (AIIDE): simulation-based prediction is popular but slow and needs extensive engine modelling; a Lanchester-based model is faster. Square law = all units engage at once; linear law = series of duels. (AAAI page blocked; abstract from search snippet.) — [AAAI AIIDE](https://ojs.aaai.org/index.php/AIIDE/article/view/12780)
- Caveat: a National Academies analysis notes Lanchester equations are largely irrelevant to modern DoD combat modelling, which uses simulation. — [NAP (search snippet)](https://nap.nationalacademies.org/read/5869/chapter/19)
- Civ V VP danger uses deterministic simulated damage with no RNG and an assumed defender strength. — [CvDangerPlots.cpp](https://raw.githubusercontent.com/LoneGazebo/Community-Patch-DLL/master/CvGameCoreDLL_Expansion2/CvDangerPlots.cpp)

### Inferences
- Not yet in the project: the cover and threat layers add raw combat strength ("cover from real combat strength, no cap"). Because damage is exponential in the difference, two warriors (20+20) are not "worth" one strength-40 unit; additive strength over-states swarms of weak units and under-states one strong unit. Proposed plug-in:
  - Per tile, threat to a defender of strength D = sum over sources that can attack that tile this turn of `30 * exp((S_attacker - D) / 25)` (ignore RNG; maybe use the 1.2x upper bound for safety). Compare to defender HP (100 units; cities have walls/garrison).
  - Opportunity for attacking a source: expected damage we deal minus expected damage taken, run as a short sequential simulation (attacks happen one at a time in Civ, closer to Lanchester's linear/duel law than the square law). A handful of exp() calls per candidate is far under 1 ms.
  - Wounded penalties and terrain/fortify/support modifiers shift S; apply them where known from legal reads, else treat as unknown and use a margin.
- "One point = one barbarian warrior" can stay as the display unit, but decisions should go through the damage formula.

### Gaps
- Exact Civ VI modifiers (wounded penalty, flanking/support values, fortify, terrain) and the authoritative GlobalParameters values were not verified; the Civ fandom wiki returned 402.
- Full text of the Lanchester StarCraft paper (accuracy numbers, fitted attrition rates) could not be fetched.

## 5. Hex-grid specifics and fast multi-source Dijkstra in Python

### Takeaway
At Civ VI map sizes (a few thousand tiles) a cached sparse adjacency graph plus
`scipy.sparse.csgraph.dijkstra(indices=sources, min_only=True, limit=horizon)` gives a multi-source
turns-to-arrive field with nearest-source labels in one compiled call; per-source fields use the same
call without `min_only`. Templates/stamping and numpy array algebra make every derived layer
vectorized. Rebuild only when sources or terrain change.

### Cited Findings
- `scipy.sparse.csgraph.dijkstra`: `indices` restricts sources; `limit` stops at a max distance (beyond it returns inf), which "speeds up computation"; `min_only=True` (SciPy 1.3.0+) returns one 1D array of distance to the nearest of the `indices` and is "substantially faster"; with `return_predecessors=True` it also returns `sources`, the index of the nearest source per node (-9999 if none within limit). `directed=False` is wrong when edge weights are asymmetric. — [SciPy docs](https://docs.scipy.org/doc/scipy/reference/generated/scipy.sparse.csgraph.dijkstra.html)
- Multi-source BFS/Dijkstra and min-combination of fields; BFS ~1M nodes/s; Union-Find useful when only adding land. — [Red Blob Games](https://www.redblobgames.com/pathfinding/distance-to-any/)
- Precomputed normalized templates turn per-unit propagation into additions; working maps limit work to the area of interest. — [Mark, GAIP2 ch. 30](http://www.gameaipro.com/GameAIPro2/GameAIPro2_Chapter30_Modular_Tactical_Influence_Maps.pdf)
- Dijkstra maps need recomputing only when their goals change; many agents share one map. — [RogueBasin](https://www.roguebasin.com/index.php/The_Incredible_Power_of_Dijkstra_Maps)

### Inferences
- Pitfall: movement cost in Civ VI is asymmetric (cost to enter a tile depends on the destination tile; rivers add cost on crossing). Use a directed graph whose edge (a->b) weight is the cost to enter b, and run Dijkstra from the threat source along that direction (threat moves toward us). Do not use `directed=False`.
- Pitfall: Civ movement is turn-quantized (a unit with 1 move left can always enter one more tile; leftover moves are lost at turn end). Plain summed movement cost / max moves under-estimates turns for slow units in rough terrain. Options: accept the approximation (cheap), or run a small custom Dijkstra with state (turns, moves left) for the few sources within a short horizon.
- Pitfall: ZOC (zones of control) stops movement next to enemy units; VP's danger code ignores or selectively applies ZOC. Ignoring it over-estimates threat reach (safe direction).
- Grouping by source type: sources of the same movement class and the same strength can share one multi-source call; sources of different strength need separate fields (or per-source fields with `limit` = horizon), or the min-only field plus nearest-source label, with strength looked up from the label (loses overlap/summing). Summing threat from several sources requires per-source fields; with `limit` small (2-4 turns) each is tiny.
- 0.36 s/turn for the handler is high for a few thousand tiles; a cached CSR adjacency (rebuilt only when `terrain_new` is non-empty) plus one csgraph call per strength class should be in the low milliseconds. Measure before changing.
- Hex coordinates: Civ VI uses offset coordinates; convert to axial/cube for distance and neighbour arithmetic (Red Blob hexagon guide is the standard reference; not fetched in this pass).

### Gaps
- No measured benchmark of csgraph on a Civ-sized hex graph was found; timing claims above are inference.
- Red Blob's hexagon-grid guide (offset vs axial conversions) was not fetched in this pass.
