# Fog-of-war belief tracking and spatial risk for a Civ VI bot

Scope: techniques for (a) tracking unseen enemies, (b) knowledge age and uncertainty, (c) spatial risk, (d) where to scout, (e) combat outcome prediction, judged against the threat map in `docs/threat-map.md` (issue 238 / PR 241). That design already has: camps kept until a visible tile shows them gone, hostile units kept `unit_memory_turns` after last sighting then dropped, threat faded linearly by distance/turns-to-arrive, a mild fog penalty, and spread by movement cost (stage 1b).

Source note: two primary sources were read in full (Weber et al. 2011, Synnaeve et al. 2018, via PDF text). Civ VI numbers marked "game file" were read from the local install's `Base/Assets/Gameplay/Data/GlobalParameters.xml` (Steam copy on this machine, no DLC XML overrides found for these names; the expansion DLLs reference the same names). Community Civ VI sources are anecdotal and flagged as such.

## (a) Tracking unseen enemies: belief-state models under fog

### Takeaway
The best-evidenced cheap technique is exactly what the threat map almost does: one "particle" per last-seen enemy, with per-class linear confidence decay and removal when its predicted tile is seen empty. The two upgrades with evidence behind them are (1) decay rates per unit class (static things never decay, fast movers decay fast) and (2) moving the particle along a likely path instead of leaving it at the last-seen tile. A full particle filter, occupancy-grid diffusion or a learned "defogger" are possible but the gains over last-seen memory are modest and mostly from movement prediction.

### Cited Findings
- Weber, Mateas, Jhala (AIIDE 2011) use a *simplified* particle filter: a single particle per previously seen enemy unit (not a cloud), because "an opponent may have multiple, indistinguishable units"; they add "a decay function ... which gradually reduces the agent's confidence in estimations over time." — [A Particle Model for State Estimation in RTS Games](https://ojs.aaai.org/index.php/AIIDE/article/view/12424)
- Their per-turn update is four steps: apply movement (constant trajectory), apply linear decay by class, cull particles that are within vision range or have weight <= 0, spawn particles for units that just left vision (weight 1, at last known position). No normalisation, because units are indistinguishable. — [Weber et al. 2011](https://ojs.aaai.org/index.php/AIIDE/article/view/12424)
- Trajectory = weighted sum of unit vectors: observed movement vector, a "chokepoint vector" toward the region exit best aligned with movement, and (cheating, unused in the best model) the true destination. — [Weber et al. 2011](https://ojs.aaai.org/index.php/AIIDE/article/view/12424)
- Learned decay rates (Protoss v Zerg): buildings 0.00, workers 0.00, ground attackers 0.04/update (lifetime ~22 s), air attackers 0.13/update (lifetime ~5.8 s). For ground attackers the chokepoint vector had the largest weight (20.96 px/s vs movement 5.35). — [Weber et al. 2011](https://ojs.aaai.org/index.php/AIIDE/article/view/12424)
- Parameters were fitted offline with Nelder-Mead (500 iterations) against an error function: sum over map regions of |predicted threat - actual threat|, where predicted threat = visible enemies + sum of particle weights in the region. — [Weber et al. 2011](https://ojs.aaai.org/index.php/AIIDE/article/view/12424)
- Accuracy (0 = never remember, 1 = perfect tracker): "Default" last-known-position with no decay = 0.669 overall; optimized model = 0.739. Letting the model identify individual units across sightings did *not* help (0.709). — [Weber et al. 2011](https://ojs.aaai.org/index.php/AIIDE/article/view/12424)
- In-game win rate: optimized particle model 78% vs 67% for null, default and even perfect-information settings (144 games). Authors attribute the perfect-info result partly to the bot no longer scouting. — [Weber et al. 2011](https://ojs.aaai.org/index.php/AIIDE/article/view/12424)
- Related-work summary in the same paper: occupancy maps (Isla 2006) diffuse each node's probability to neighbours each update, zero out visible nodes not containing the target, renormalise; a stated drawback is grid resolution vs visibility cost. Tozour (2004) keeps a visited list with decay so the agent re-checks old areas. Hladky & Bulitko (2008) learn non-uniform transitions with hidden semi-Markov models. — [Weber et al. 2011](https://ojs.aaai.org/index.php/AIIDE/article/view/12424)
- Hladky & Bulitko (CIG 2008) compared HSMMs and particle filters for opponent positions in Counter-Strike: Source and found they can match or beat average human experts. — [IEEE CIG 2008 record](https://search.isc.ac/inventory/49/1840242.htm); [Hladky thesis, U. Alberta](https://era.library.ualberta.ca/items/85d65107-ed06-4745-9e67-ff3392bad546)
- Isla's Probabilistic Occupancy Map (with Blumberg, MIT) and later the stealth game *Third Eye Crime* were built around occupancy maps: probability spreads from the last-known position into unseen areas. — [Isla & Blumberg, object persistence](https://characters.media.mit.edu/Papers/objectPersistence.pdf); [Third Eye Crime, AIIDE](https://ojs.aaai.org/index.php/AIIDE/article/view/12663)
- Synnaeve et al. (NeurIPS 2018) "defogger": conv + LSTM encoder-decoder trained on 65,000 human StarCraft games (STARDATA), predicting unit counts per coarse grid cell (32 or 64 walk tiles) now and 5/15/30 s ahead. — [Forward Modeling for Partial Observation Strategy Games](https://arxiv.org/abs/1812.00054)
- Its main baseline, "Previous Seen" (last seen position per hidden unit, reset to 0 when the location is revealed empty) "is what most rule based bots do in real games". For predicting which building types exist, "the baselines already do very well, it is hard to beat the best baseline"; the model's gains are mainly on *moving unit* positions. — [Synnaeve et al. 2018](https://arxiv.org/abs/1812.00054)
- Plugged into a rule-based bot, the defogger raised average win rate from 61% to 66% (1820 games per setting), but it "seems to hurt the existing Tactics module more than help it" while improving build decisions. — [Synnaeve et al. 2018](https://arxiv.org/abs/1812.00054)
- Synnaeve & Bessière's Bayesian work (CIG 2011) targets build/opening prediction from fogged observations with parameters learned from replays (Weber & Mateas dataset of 9,316 logs), i.e. *what* the opponent has, not *where*. — [Opening prediction, CIG 2011](https://bibtex.github.io/CIG-2011-SynnaeveB11a.html); [arXiv 1111.3735](https://arxiv.org/pdf/1111.3735)
- Occupancy-grid log-odds: a binary Bayes filter per cell, updated by adding the inverse-sensor-model log-odds and subtracting the prior; each cell just keeps a running log-odds value (Probabilistic Robotics ch. 4 and 9). Known limitation: it assumes cells are independent. — [Edinburgh lecture slides](https://homepages.inf.ed.ac.uk/msridhar/Teaching/MobileRobotics/Slides/S9_Mapping.pdf); [Thrun, forward sensor models](https://typeset.io/pdf/learning-occupancy-grid-maps-with-forward-sensor-models-1brojkm7wv.pdf)

### Inferences
- The current design (keep N turns, then drop; drop on resight within `resight_radius`) is roughly Weber's "Default" model with a step-function lifetime. The cheapest evidence-backed upgrade is Weber's: replace the hard cutoff with a weight that falls linearly per turn, *per class* (camp 0; settlers/builders/siege low; scouts, horsemen and ships high), and multiply threat by that weight. Removal stays "visible tile shows it absent" plus "weight <= 0".
- Weber's culling rule also covers a gap the doc implies but does not state: a remembered unit should be dropped (or its weight cut sharply) when its *predicted* tile, not just its last-seen tile, becomes visible and is empty.
- In Civ VI, turn-based movement makes a "reachable set" better than a single moved particle: after k unseen turns, a unit can be on any tile within k * moves of movement cost (the terrain store already gives costs). Spreading the source's weight over that set, minus currently visible tiles, is an occupancy map restricted to one source; it is a Dijkstra per source, cheap at 2-5k tiles and tens of sources. This is a principled version of "threat faded by turns-to-arrive".
- A directional bias is available legally: barbarian raiders head for the city their scout found (anecdotal, below), so a hostile unit last seen moving toward a city can be biased along its shortest path to that city, Weber's "chokepoint vector" analogue.
- Learned defoggers need thousands of logged games and the published gain was ~5 win-rate points with mixed tactical effect; overkill for this project now. The run logs (`*_threatmap.jsonl`) could later serve as data to fit decay rates offline with Nelder-Mead exactly as Weber did, using "was the remembered unit actually there when the tile was next seen" as the error.

### Gaps
- No published Civ-specific (turn-based hex) evaluation of particle vs occupancy models was found.
- Isla's GAIPW3 chapter and Darken & Anderegg's "simulacra" chapter were not available in full text; details above come from Weber et al.'s summary.

## (b) Knowledge age and uncertainty quantification

### Takeaway
Treat each tile's knowledge as a probability that decays with time since last seen, and each remembered source as a weight that decays with time. Bayesian search theory gives a one-line, exact update for "looked and saw nothing", which is the right tool for both camp-spawn belief and unit belief.

### Cited Findings
- Bayesian search theory update after searching a cell and not finding the object: p' = p(1-q)/(1-pq), where q is detection probability; all other cells rise to r' = r/(1-pq). Search next where the probability-per-cost is highest. Used for USS Scorpion (1968), Palomares H-bomb (1966), AF447 recorders (2009). — [Bayesian search theory (Wikipedia)](https://en.wikipedia.org/wiki/Bayesian_search_theory)
- Occupancy grids store per-cell log-odds; a cell at its prior is "unknown" (e.g. p = 0.5), and frontier cells are known-free cells next to unknown ones. — [CTU lecture slides on frontier exploration](https://cw.fel.cvut.cz/b221/_media/courses/uir/lectures/b4m36uir-lec04-slides.pdf); [Edinburgh slides](https://homepages.inf.ed.ac.uk/msridhar/Teaching/MobileRobotics/Slides/S9_Mapping.pdf)
- Influence-map "momentum" blends the stored value toward the newly computed one by linear interpolation: high momentum keeps history (good for statistics of past attacks), low momentum tracks the present. — [Champandard, The Core Mechanics of Influence Mapping](https://gamedev.net/tutorials/programming/artificial-intelligence/the-core-mechanics-of-influence-mapping-r2799)
- Tozour's variant keeps a visited list with decay so old areas are re-investigated. — [Weber et al. 2011 (related work)](https://ojs.aaai.org/index.php/AIIDE/article/view/12424)

### Inferences
- A per-tile `last_seen_turn` array (int, one per tile) is the minimal knowledge-age store and is what `oldest_knowledge` needs. Age alone treats a tile next to a city the same as a far one; a better "staleness" is P(tile now holds something new | age), e.g. 1 - (1 - h)^age where h is a per-tile hazard (camp spawn hazard, unit-arrival hazard). This is just a Poisson/Bernoulli process per tile and costs one vectorised exponent.
- For camps, Civ VI's spawn rule (below) makes this exact in spirit: a tile under your vision has hazard 0; a fogged eligible tile accumulates hazard each turn. Seeing a tile empty resets it (q close to 1 for a visible tile in Civ, since visibility is exact).
- Uncertainty of a remembered unit is its reachable-set size (tiles it could be on). That number is a natural "confidence" and can gate decisions (e.g. do not commit a garrison release while a strong unit's reachable set covers the city).

### Gaps
- No source gives empirical Civ VI rates for camp spawns per fogged tile per turn; it would have to be measured from the project's own runs.

## (c) Spatial risk estimation and barbarian spawn modelling

### Takeaway
Risk = sum over sources of (presence probability x strength x reach factor), plus a prior hazard for things not yet seen. Civ VI's game files pin the camp prior well: camps need fog, at least 4 tiles from any city and 7 from another camp, with a per-major-civ cap. Linear distance fade is fine; the main improvements are using movement-cost reach (already planned) and replacing the "mild linear fog penalty" with a camp-spawn eligibility mask.

### Cited Findings
- Game file values (local `GlobalParameters.xml`): `BARBARIAN_CAMP_MINIMUM_DISTANCE_CITY` = 4, `BARBARIAN_CAMP_MINIMUM_DISTANCE_ANOTHER_CAMP` = 7, `BARBARIAN_CAMP_MAX_PER_MAJOR_CIV` = 3, `BARBARIAN_CAMP_ODDS_OF_NEW_CAMP_SPAWNING` = 2, `BARBARIAN_CAMP_EXTRA_DISTANCE_PER_LOW_DIFFICULTY` = 1, `BARBARIAN_CAMP_FIRST_TURN_PERCENT_OF_TARGET_TO_ADD` = 33, `BARBARIAN_CAMP_COASTAL_SPAWN_ROLL` = 6, `BARBARIAN_TECH_PERCENT` = 50, boldness: +2 per turn, +15 per kill, -10 per unit lost, -5 per scout lost, -30 per camp attack, `BARBARIAN_RELEASE_VARIATION` = 20. The same 4/7/3 values appear in a 2016 community reference. — local game file; [r/civ map settings reference](https://lr.eu.psf.lt/r/civ/comments/59lsbf/map_settings_reference_everything_ive_found)
- Players widely report camps spawn only on tiles in fog for all civs and city-states, and recommend "fog-busting" with units posted near cities. — [Steam discussion](https://steamcommunity.com/app/289070/discussions/0/2993170476558139211); [gamepressure guide](https://www.gamepressure.com/sidmeierscivilization6/barbarians/z49335); [CivFanatics thread](https://forums.civfanatics.com/threads/rules-for-barbarian-spawns.654941/latest) (anecdotal; that thread itself calls fog-only "obvious" without a source)
- Reported behaviour (anecdotal, no official source): a camp sends a scout; if the scout finds a city and returns, the camp triggers and produces a raiding party (reported 3-6 units over a few turns) that heads to the spotted city; killing the scout before it returns is the standard counter; a camp next to the border can trigger without travel. Coastal camps may produce galleys. — [CivFanatics: Barbarian scouting](https://forums.civfanatics.com/threads/barbarian-scouting.652484/); [gamepressure](https://www.gamepressure.com/sidmeierscivilization6/barbarians/z49335); [substack guide](https://theahura.substack.com/p/on-towards-the-stars-dealing-with)
- Destroyed camps are replaced in unmonitored areas; one poster saw a replacement the next turn (anecdote). — [CivFanatics](https://forums.civfanatics.com/threads/rules-for-barbarian-spawns.654941/latest)
- Influence maps: decay with distance (exponential or linear), propagated over the walkable graph; lower decay for strategic maps, higher for tactical ones. — [Champandard](https://gamedev.net/tutorials/programming/artificial-intelligence/the-core-mechanics-of-influence-mapping-r2799); [Dave Mark, Modular Tactical Influence Maps (GameAI Pro 2)](https://www.gameaipro.com/GameAIPro2/GameAIPro2_Chapter30_Modular_Tactical_Influence_Maps.pdf)
- Weber's error metric is region threat = visible enemies + sum of particle weights per region, compared with ground truth. — [Weber et al. 2011](https://ojs.aaai.org/index.php/AIIDE/article/view/12424)

### Inferences
- Replace the generic fog penalty with a **spawn-eligibility mask**: fogged (not currently visible to us; note the real rule is "visible to nobody", which we cannot observe, so our mask overestimates — conservative), land, >= 4 tiles from any known city (ours or rivals'), >= 7 from any known camp. Weight each eligible tile by a hazard per turn and by its movement distance to our cities. This gives "where will the next camp appear", turns the fog layer into a real probability, and directly ranks fog-busting posts (place a unit where it removes the most hazard-weighted eligible tiles from the mask).
- A Poisson-process view is enough: expected new camps in a region over T turns ~= hazard x eligible-tile count x T. Fit the hazard from the project's own runs (count camp first-sightings per eligible fogged tile-turn). Kernel density on historical camp sightings is a lighter alternative but ignores the eligibility rule, so the rule-based mask should come first.
- Barbarian scouts are a leading indicator: a barbarian scout seen near a city and heading back toward fog means a raid in a few turns. A "triggered camp" flag (camp whose scout saw us and was not killed) deserves a strength multiplier and is a high-value kill target. Boldness values (+2/turn, +15 per kill) suggest unattacked camps grow more aggressive over time, supporting acting early on camps near cities (inference from parameter names; semantics unverified).
- Linear-with-distance fade is acceptable; what matters more is that distance is movement turns, not hex distance, which stage 1b covers.

### Gaps
- The exact per-turn spawn probability formula, how "visible to any player" is tested, and what `ODDS_OF_NEW_CAMP_SPAWNING` = 2 and boldness thresholds mean live in the game DLL; no authoritative public description found.
- The scout-trigger/raid mechanic is only community-described. Barbarian Clans mode (if enabled) changes behaviour and was not researched.

## (d) Choosing where to scout

### Takeaway
Frontier/information-gain exploration and Bayesian search theory both reduce to one score per candidate target: (expected value of what you would learn) / (cost to get there and risk). For this bot "value" should be hazard- and threat-weighted staleness near our cities, not raw unknown-tile counts.

### Cited Findings
- Yamauchi (CIRA 1997) defined frontiers as boundaries between known and unknown space in an occupancy grid and drove a robot to the nearest frontier; later work (Wavefront Frontier Detector) finds frontiers by BFS over known space only. — [CTU slides](https://cw.fel.cvut.cz/b221/_media/courses/uir/lectures/b4m36uir-lec04-slides.pdf); [WFD, arXiv 1806.03581](https://arxiv.org/pdf/1806.03581)
- Information-driven frontier selection typically scores U(f) = a*Gain(f) - b*Cost(f), with gain as unknown-cell count or entropy reduction and cost as travel distance/time. — [Dai et al., arXiv 2002.04440](https://arxiv.org/pdf/2002.04440); [Fung et al., ARL report](https://apps.dtic.mil/sti/pdfs/AD1008850.pdf)
- Bayesian search: search where p*detection/cost is highest; failed searches lower that cell and raise others. — [Bayesian search theory](https://en.wikipedia.org/wiki/Bayesian_search_theory)
- Weber's bot needed active scouting to populate its particle memory; the perfect-information variant scouted less and won less. — [Weber et al. 2011](https://ojs.aaai.org/index.php/AIIDE/article/view/12424)

### Inferences
- Concrete score for recon target tile t: value(t) = sum over tiles s revealed from t of [P(new threat at s | age_s) x relevance(s)], where relevance falls with movement distance to our nearest city and rises with remembered-source weight nearby; divide by turns-to-reach and multiply by survival probability of the scout (from the threat layer). One Dijkstra from each scout plus a sum over sight radius per candidate; candidates can be limited to frontier tiles and remembered-source predicted tiles. Easily sub-second.
- "Oldest knowledge" alone (planned) will send scouts to distant irrelevant corners; weighting by the spawn mask and city distance fixes that. Revisiting remembered camps whose last confirmation is old is a special case with high p.
- Static fog-busting (a unit parked to deny camp spawns) and roaming recon are separable: fog-busting maximises hazard removed per unit; recon maximises information per turn.

### Gaps
- No game-AI paper found that evaluates scouting policies in a turn-based 4X; recommendations are transferred from robotics and RTS.

## (e) Predicting combat outcomes and force sizing

### Takeaway
Civ VI's damage roll is known closely enough to compute exact outcome distributions rather than simulate: damage = (24 + U{0..12}) x exp(0.04 x strength difference), 100 HP, plus modifiers (wounded penalty, support/flanking, terrain, fortify, river, walls). A per-fight lookup table or a small Monte Carlo over a planned sequence of attacks is cheap; Lanchester models are a poor fit for duel-based, one-attack-per-unit combat.

### Cited Findings
- Game file values: `COMBAT_BASE_DAMAGE` = 24, `COMBAT_MAX_EXTRA_DAMAGE` = 12, `COMBAT_POWER_SCALING` = 0.04, `COMBAT_MAX_HIT_POINTS` = 100, `COMBAT_MINIMUM_DAMAGE` = 1, `COMBAT_WOUNDED_DAMAGE_MULTIPLIER` = 10, `COMBAT_SUPPORT_BONUS_MODIFIER` = 2, `COMBAT_FLANKING_BONUS_MODIFIER` = 2, `COMBAT_RIVER_DEFENSE` = 5, `COMBAT_CORPS_STRENGTH_MODIFIER` = 10, `COMBAT_ARMY_STRENGTH_MODIFIER` = 17, `COMBAT_DEFENSE_DAMAGE_PERCENT_MELEE` = 15, `..._RANGED` = 50, `..._BOMBARD` = 100, `COMBAT_RANGED_VS_DISTRICT_STRENGTH_MODIFIER` = 17, `COMBAT_DISTRICT_STRENGTH_REDUCTION` = 15, `COMBAT_DAMAGE_MULTIPLIER_MINIMUM` = 0.25, `BARBARIAN_DEFENSE_ALL_UNITS` = 4. — local game file
- Community fit from combat logs: Damage = 30 x e^(diff/25) x Random(0.75, 1.25) (Hans Lemurson), later restated as [24 + rand(0,12)] x exp(0.04 x diff); a reviewer noted 3 of 193 logged combats did not fit. Each strength point is ~4% damage; ~+10 strength = 1.5x damage dealt and taken; beyond ~30 difference fights are effectively one-hit kills. City/district attacks fit worse and may differ. — [CivFanatics: Hans Lemurson combat formula](https://forums.civfanatics.com/threads/hans-lemurson-figures-out-the-combat-formula.606147/); [CivFanatics formula thread](https://forums.civfanatics.com/threads/formula-thread.600534/)
- Wounded units: community sources say a damaged unit gets roughly -1 to -10 strength depending on HP lost (cities: -1 per 10% damage); the National Identity civic reduces the penalty. — [Steam discussion](https://steamcommunity.com/app/289070/discussions/0/1742265965886558501); [CivFanatics city combat resource](https://forums.civfanatics.com/resources/city-combat.27737/); [Civ Wiki: National Identity](https://civilization.fandom.com/wiki/National_Identity_(Civ6))
- Lanchester square law (dA/dt = -bB, dB/dt = -aA) assumes continuous attrition with all units engaging; it "fails when combatants kill each other almost instantly" and is not meant for whole armies where not all troops engage. — [Lanchester's laws (Wikipedia)](https://en.wikipedia.org/wiki/Lanchester%27s_laws)

### Inferences
- With a uniform integer roll of 13 values, the outcome of one attack is a 13-point distribution; a sequence of k attacks on one defender is a convolution of k such distributions (with the wounded penalty making later hits slightly stronger). That gives exact P(kill) and P(attacker dies) in microseconds, no Monte Carlo needed; Monte Carlo (a few hundred rolls) is fine for multi-unit plans with retaliation order.
- Wounded penalty: `COMBAT_WOUNDED_DAMAGE_MULTIPLIER` = 10 is consistent with the community "-1 to -10" penalty, i.e. strength minus about 10 x (damage taken / 100). This is an inference; confirm against a combat log or the in-game combat preview.
- The game's own combat preview (if the scripting API exposes it, e.g. a combat simulation call) would be strictly better than any reimplementation because it includes all modifiers; worth checking the API before building a model.
- Force sizing so no unit dies: choose the smallest set of attacks with P(target dies) >= target and, for each melee attacker, P(retaliation kills it) <= tolerance, computing retaliation damage with the defender's (wounded) strength. Barbarians get +4 defence vs all units (`BARBARIAN_DEFENSE_ALL_UNITS`, likely a strength bonus; semantics unverified) — relevant when sizing camp clears.
- Garrison sizing per city can use the same machinery: expected incoming damage per turn = sum over sources with P(source reaches city in 1-2 turns) x expected damage; garrison is enough when city + garrison survives the worst plausible turn (e.g. 90th percentile) rather than the mean.
- Because the threat map's strength unit is "one barbarian warrior", a more principled common unit is "expected damage per attack against a reference defender", which already includes the exponential and would make cover and threat directly comparable (the exponential means summing raw strengths overstates many weak units against one strong one).

### Gaps
- Exact wounded-penalty formula, fortification and terrain values were not confirmed from game files in this pass (they live in modifier tables, not GlobalParameters).
- Whether the bot's scripting API exposes the game's combat preview was not checked.

## Opponent / behaviour modelling (cross-cutting)

### Takeaway
For barbarians, rule-based target prediction (raid goes to the city the scout found; untriggered units wander and pillage) is enough; for AI civs, learned models need data the project does not have yet.

### Cited Findings
- Raiding parties reportedly head straight for the spotted city; units spawned outside a raid wander and pillage (anecdotal). — [CivFanatics: Barbarian scouting](https://forums.civfanatics.com/threads/barbarian-scouting.652484/)
- Weber found a terrain-derived chokepoint bias outweighed observed motion for ground units, and the true-destination vector did not improve accuracy. — [Weber et al. 2011](https://ojs.aaai.org/index.php/AIIDE/article/view/12424)
- Hladky & Bulitko learned movement transitions from logs (HSMM); a later critique notes it lacked behaviour learning. — [Hladky thesis](https://era.library.ualberta.ca/items/85d65107-ed06-4745-9e67-ff3392bad546)

### Inferences
- Bias each remembered barbarian's reachable set toward the nearest city by path cost (a softmax over cities by distance), and toward roads/passable corridors. This is cheap and matches both the reported behaviour and Weber's chokepoint result.

### Gaps
- No measured data on Civ VI barbarian or AI target selection was found.

## Cost summary for a 2-5k tile hex map in Python (inference, not measured)

- Cheap (well under 100 ms/turn with numpy or plain dicts): per-class linear decay; `last_seen_turn` grid; spawn-eligibility mask (multi-source BFS from cities and camps); Bayesian "seen empty" updates; exact damage distributions; recon scoring over frontier candidates.
- Moderate (still sub-second): one Dijkstra per remembered source for reachable sets (tens of sources x ~5k tiles); multi-source Dijkstra for movement-time distances; small Monte Carlo for multi-unit attack plans.
- Overkill now: true particle clouds per unit (adds little over reachable sets on a discrete turn-based map); full-map log-odds occupancy grids per enemy; HSMM or neural defoggers (need large logged datasets; published gains were modest, about +5 to +11 win-rate points in StarCraft).
