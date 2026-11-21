# Live checks

What was verified against a running Civ VI game, and what wasn't reached. Newest run first. Run 2 updates the status of earlier checks it reached.

## To check in the next live run: victory-threat check no longer fails on decimals

`civ6_ai.victory_check.fix_victory_check` is applied at startup. The console log should have no `Victory proximity check failed` / `invalid literal for int() ... '33.5'` tracebacks. Not reached: a game with a rival holding space-race points.

## To check live: city strikes (fix for a civ6-mcp Lua bug)

Run 6, turn 282: `City strike from Palenque failed` with `LuaError: ERR:Runtime Error: ... :56: operator .. is not supported for table .. string`. The cause, read from the Lua text (not seen in the game): civ6-mcp's `build_city_attack` writes its `ERR:NO_LOS` message with `{target_x}` in a plain string, so the script holds the literal `{target_x}`, an empty table in Lua. It was reached because the target was not in `CityManager.GetCommandTargets`. `civ6_ai.city_attack.fix_city_attack` (called in `run_game.py`) swaps in a builder without the bug that also no longer stops when the target is missing from that list, and lets `CanStartCommand` decide.

| Check | Status |
|---|---|
| A strike logs `city_strike` with a result starting `CITY_RANGE_ATTACK|` and `damage dealt:` and no `City strike from ... failed` | Not reached |
| The shape of `CityManager.GetCommandTargets` (whether the target plot index `y * width + x` is in it) | Not reached |
| A refused strike now answers `Error: NO_LOS|Line of sight to (x,y) ...`, `ALREADY_FIRED` or `CANNOT_ATTACK`, and is not repeated for `retry_turns` | Not reached |
| Why Palenque's target was not in the list (line of sight, or the list's shape) | Unknown |

## To check in the next live run: no per-turn MCP saves

`disable_mcp_saves` is applied at startup. No new `0_MCP_*` files should appear in the `Single` saves folder, and Civ's own `AutoSave_*` files in `auto` should keep rolling as before.

## To check live: military policy (issue 13, first slice)

Written offline against civ6-mcp's source, so none of this has run in a game yet.

| Check | Status |
|---|---|
| Damage rule `24 * e^(0.04 * strength difference)` matches the "your HP: a -> b" line of a melee attack and the damage dealt by a ranged one | Not reached |
| `assumed_defender_bonus` (6) covers fortification and terrain on barbarian units that stand on hills or fortified | Not reached |
| The damage roll stays within `damage_variance` (20%) | Not reached |
| `NOTIFICATION_UNIT_PROMOTION_AVAILABLE` is the notification type that was listed from turn 80, and `get_unit_promotions` then lists options | Not reached |
| The promotion names in `promotion_preference` exist (`PROMOTION_GARRISON`, `PROMOTION_TORTOISE`, ...); unknown names are skipped for the first offered | Not reached |
| `heal_unit` and `fortify_unit` answer as read in the source (`HEALING|HP:..`, `FORTIFIED`, `ALREADY_FORTIFIED`, `Error: CANNOT_HEAL`) | Not reached |
| A refused `attack_unit` answers with the combat estimate followed by `Error: ...`, which `attack_landed` treats as a failure | Not reached |
| A move into a city to garrison it answers `now_at:x,y` equal to the city tile when it arrives | Not reached |
| `get_map_area` shows `IMPROVEMENT_BARBARIAN_CAMP` on a visible camp tile, and melee units stepping onto an emptied camp clear it | Not reached |
| A melee unit that kills its target does not move onto the tile (so a city garrison stays put) | Not reached |
| Ranged units are not hit back by melee defenders, and ranged defenders do hit back (`assess_attack` assumes both) | Not reached |
| `Unit.targets` lists only targets in range with line of sight, and ranged units fire at adjacent targets | Not reached |

## Run 20: turns 1 to 150, STD_T1_03 with PR 265, a regression run (2026-11-18)

STD_T1_03 on main from 28e31bc (PR 265: defence "short" from the threat map's city-tile reading, city cover, heal read, resumed builds, new logging), logs `20261118T144707Z` (turns 1 to 14) and `20261118T145249Z` (turns 15 to 150, resumed once with no `--load` because the game was already on turn 15), consoles `runs/run_live20.out` and `runs/run_live20b.out`. The first session stopped `blocked` on turn 14 after 251 s: "Turn did not read as advanced", but the screenshot showed turn 15 with the Next Turn button, so the stop looks like a false read; nothing was named in blockers.md. The resumed session stopped on `turn_limit` after 2,612 s with no handler errors. Decision time mean 9.8 s over both logs (11.4 s resumed session; p90 14.5 s, max 47.7 s); end-turn wait mean 7.9 s; total per turn mean 19.2 s, max 66.0 s. Resumed session's G1 gate clean, 7 warnings (gold nothing bought 88%, no spare unit 73%, settlers used 1 of 7, no site found 89% of settling decisions, one city lost, one Dark Age, trade routes used 8 of 36). The first session's gate failed on the one stop. $0.00 in model spend.

Compared with Run 19 (same save, previous code):

| | Run 19 | Run 20 |
|---|---|---|
| Score turn 50 / 100 / 150 | 35 / 110 / 223 | 52 / 142 / 161 |
| Rank, alive, leader, gap at 150 | 6/6, 6, 482, -259 | 6/6, 6, 543, -382 |
| CivBench gap 100 / 150 (LLM, AI medians) | -199 / -259 (-129, -146 / -144, -194) | -172 / -382 (-97, -208 / -112, -256) |
| Cities founded (turns) | 6 (2, 48, 54, 106, 119, 134) | 2 (2, 30); city 2 lost on turn 129 |
| Districts started | Campus 4, Commercial Hub 4, Diplomatic Quarter 1, Industrial Zone 1, Aqueduct 2 | Campus 5, Commercial Hub 1, Diplomatic Quarter 1, Aqueduct 2 |
| Districts completed | Commercial Hub 89 and 141, Diplomatic Quarter 114, Industrial Zone 147, Aqueduct 150 (plus wonder districts 109, 115) | Campus 33 and 69, Commercial Hub 63, Diplomatic Quarter 94, Aqueduct 100 and 105 |
| First district completed | turn 89 | turn 33 |
| Theater, Holy Site | none | none |
| "defence turn" lines / SHORT | 113 / 26 | 213 / 38 |
| "production: switched to" / "resumed" | 6 / 0 | 4 / 2 |
| Units started (production_set) | Slinger 9 (7 in Constantinople), Settler 7, Builder 15, Trader 4, Spearman 3, Archer 3, Heavy Chariot 2, Horseman 3, Warrior 2, Scout 1 | Slinger 4 (4 in Constantinople), Settler 10, Builder 8, Trader 7, Archer 6, Horseman 3, Warrior 2, Scout 1 |

What PR 265 did right: the two Campuses finished (turns 33 and 69; Run 19 finished none), the first district came 56 turns earlier, Constantinople built 4 Slingers instead of 7, and the score was ahead of Run 19 through turn 100 (142 against 110) and level at turn 125 (169 against 170), then fell behind after the capture (161 against 223 at turn 150). The heal read worked: both sessions log `Threat map: combat heal CombatHeal(city_garrison=20.0, outer_defences=10.0, outer_defence_cooldown=3.0)` and there is no `Combat heal read failed` warning (the values equal the table fallback, so the log cannot tell a read from the fallback by value alone). Two builds were resumed: Thessalonica turn 111 and Constantinople turn 145, both Settlers.

What went wrong, the regression: `ThreatMap.city_reading` (`src/civ6_ai/threat_map.py`) computes each attacker's damage as `expected_damage(strength, cover)`, where `cover` is the sum of all allied cover on the city tile (city plus units, 125 to 139 here). It treats that sum as one defender's combat strength, so five barbarians come out at 2 to 5 damage a turn and a city is almost never "short" however many hostile units stand around it. Evidence:

| Turn | City | Reading | Result |
|---|---|---|---|
| 108 | Thessalonica | 6 hostile, 1 defender, threat 216.3, cover 80.0, damage 27.4: SHORT | the right verdict |
| 110 to 117 | Thessalonica | 3 to 5 hostile, 1 defender, threat 160 to 209, cover 101 to 147, damage 1.4 to 11.5: not short | built a Trader and a Campus district; captured by barbarians on turn 129 (`city_lost`, cause `capture`) |
| 120 | Constantinople | threat 223 against cover 125, damage 4.5: not short | built Traders |

Consequences seen in the run: the army fell to 2 combat units from about turn 85 while 6 to 9 barbarians were near; `camp_orders` was 0 all game and the camp at (55,7), known since turn 61, was never attacked; 7 settlers created but only 1 used for a city; settling holds were "escort could not move" 24 times and "every route passes a hostile unit" 22 times, with `no_site` on 67 decisions. Trade routes are chosen by yield with no check for barbarians on the path (8 of 36 used). The short verdicts that did fire were right on spot checks (turn 14: 3 hostile, damage 45.2 against heal 20, cover 23.0; turn 34: threat 74.9, cover 21.9, damage 88.5), and the not-short ones with a real garrison near few units were right too (turn 12: one Warrior against two defenders; turn 52: 3 hostile, 2 in the city, damage 10.2), so the fault is summed cover against many attackers, not the idea. Top-3 production logging works, for example: `Constantinople options: UNIT_SLINGER 21.3, UNIT_WARRIOR 20.0, BUILDING_MONUMENT 1.5`. A follow-up issue is filed for the cover fix.

## Run 19: turns 1 to 150, a fresh 150-turn run from STD_T1_03 (Byzantium) (2026-11-18)

STD_T1_03 on main from c299773, log `20261118T045839Z`, `--turns 150` (no `--save-at`), console in `runs/run35.out`. One session, no resume: stopped on `turn_limit` after 2,996 s with no handler errors and no blockers (the Run 18 "Unit needs orders" stop did not recur; settling found a site on 59 of 60 decisions). Decision time mean 12.1 s, median 11.6 s, p90 16.1 s, max 48.6 s; end-turn wait mean 7.9 s; total per turn mean 20.0 s, max 55.1 s. G1 gate clean, 3 warnings (gold: nothing bought on 88% of turns above the reserve; builders: 12% of builder actions refused; one Dark Age, the Ancient Era at 1 point against a Dark threshold of 9). $0.00 in model spend.

| Turn | Score | Rank (true) | Alive | Leader | Gap | Cities |
|---|---|---|---|---|---|---|
| 25 | 19 | 6/6 | 6 | 34 | -15 | 1 |
| 50 | 35 | 6/6 | 6 | 117 | -82 | 2 |
| 75 | 82 | 6/6 | 6 | 196 | -114 | 3 |
| 100 | 110 | 6/6 | 6 | 309 | -199 | 3 |
| 125 | 170 | 6/6 | 6 | 414 | -244 | 5 |
| 150 | 223 | 6/6 | 6 | 482 | -259 | 6 |

CivBench gap table: turn 100 -199 (LLM median -129, AI median -144), turn 150 -259 (LLM -146, AI -194). The turn-150 leader is Rome (482), with another civ at 478; we were last of six all game, and no victory threat beyond Norway's religious progress (0.4). Cities: 6 (Constantinople turn 2, then cities 2 to 6 on turns 48, 54, 106, 119, 134), the slowest early expansion of the three standard saves. No city or settler lost; a wonder district tile in Constantinople was lost on turn 121. Wonder: Temple of Artemis turn 122.

| Check | Result |
|---|---|
| Settling | 60 decisions: move 26, escort_move 12, hold 7, found 6, flee 4, escort_gather 4, no_site 1 (turn 42, "no acceptable site on the revealed map"). The holds were "the escort could not move" (6) and "route is threatened, so the escort joins first" (1), mostly turns 43 to 50 while barbarians were near; city 2 came only on turn 48. |
| Barbarians | Per-turn counters summed: `camps_gone` 3, `kills_barb` 38, `attacks_barb` 88, `camp_orders` 99. Barbarians near on 65 early turns (from turn 5); 10 of 65 had no spare unit and 23 of 55 had a spare unit but no attack or camp order (both within the gate). Several camp orders were refused (blocked tile, water tile). |
| Faith effectiveness | "no data" (no faith lines judged), no failure. Faith was saved toward Artemisia (Great Admiral, 550) at about 4 a turn, 290 held at turn 150. No Holy Site was ever started ("no HOLY_SITE (never started)"). Correction (Run 20): Run 19 did complete Commercial Hubs (turns 89 in Constantinople and 141 in Amaseia), a Diplomatic Quarter (114) and an Industrial Zone (147); what it lacked was a Campus, Theater and Holy Site (four Campuses were started and abandoned through defence switches). |
| Handler errors | None. |

## Run 18: turns 1 to 150, a fresh 150-turn run from STD_T1_02 (Khmer), stopped once and resumed (2026-11-17)

STD_T1_02 on main from deb78ca, logs `20261118T040000Z` (turns 1 to 27) and `20261118T040823Z` (turns 27 to 150, resumed once with `--load AutoSave_0028 --turns 123`), consoles `runs/run33.out` and `runs/run34.out`. The first session stopped `blocked` on turn 27 after 384 s: the screenshot showed the end-turn button reading "Unit needs orders" with a Settler idle, and the settling log had `no_site` for 13 of 14 decisions (the only candidates were rejected "likely to flip to a rival (loyalty)"). No known blocker was named, so this is a new blocker, not yet in blockers.md. The resumed session loaded the turn-27 autosave, moved on, and stopped on `turn_limit` after 2,594 s with no handler errors. Total wall time about 2,980 s. Decision time mean 8.7 s then 12.2 s (p90 10.5 s then 15.4 s, max 62.8 s); end-turn wait mean 8.9 s; total per turn mean 21.1 s. The resumed session's G1 gate was clean with 5 warnings (gold nothing bought 80%, builder refusals 11%, settling no_site 64%, 2 Dark Ages, deals 2 of 13 accepted). The stopped session's gate failed on the one stop. $0.00 in model spend.

| Turn | Score | Rank (true) | Alive | Leader | Gap |
|---|---|---|---|---|---|
| 25 | 28 | 6/6 | 6 | 40 | -12 |
| 50 | 78 | 4/6 | 6 | 115 | -37 |
| 75 | 151 | 4/6 | 6 | 211 | -60 |
| 100 | 193 | 5/6 | 6 | 303 | -110 |
| 125 | 298 | 4/6 | 6 | 438 | -140 |
| 150 | 347 | 5/6 | 6 | 513 | -166 |

CivBench gap table: turn 100 -110 (LLM median -46, AI median -61), turn 150 -166 (LLM -22, AI -70). The turn-150 leader is Aztec (513), then Maya (400); we were fifth of six. Cities: 4 (Angkor Thom turn 2, then cities 2 to 4 on turns 33, 50, 62); no fifth city, 21 of 33 settling decisions found no site. No city or settler lost. Wonders: Hanging Gardens turn 100, Great Bath 111, Machu Picchu 122.

| Check | Result |
|---|---|
| Barbarians | `camps_gone` 1, `kills_barb` 0, `attacks_barb` 2, `camp_orders` 6; barbarians near on turns 20, 33 and 103. The stopped session's offense gate warned 7 of 7 early barbarian turns unanswered. |
| Faith effectiveness | No faith warning in the report; great people bought (Zhang Qian 310, Piero de' Bardi 320, John the Baptist 550). No Holy Site was ever started ("no HOLY_SITE (never started)"). |
| PR 247, peace-offer late acceptance | Reached: the turn-71 war with Hungary was offered peace on turn 81, refused at once, and the game ended the war on turn 82 ("lasted 11 turns, after our peace offer on turn 81, the game answers late"). A second war from turn 134 had no offer (the game waits 10 turns). |
| Handler errors | None in either session. |

New against STD_T1_01: the turn-27 stop. An idle Settler with no acceptable site (every candidate rejected on loyalty) leaves "Unit needs orders" and the run stops `blocked`. Reloading the turn-27 autosave got past it (the settler founded city 2 on turn 33), so the cause is not a stuck popup. Not fixed in this run.

## Run 17: turns 1 to 150, a repeat of Run 16 with PRs 257 to 259 (2026-11-17)

STD_T1_01 on main from b120efb, log `20261118T025353Z`, `--turns 150` (no `--save-at`), console in `runs/run32.out`. Stopped on `turn_limit` after 3,487 s, no handler errors, no blockers. Decision time mean 14.4 s, p90 20.2 s, max 51.0 s; end-turn wait mean 8.9 s; total per turn mean 23.2 s. G1 gate clean, 4 warnings (gold nothing bought on 64% of turns above the reserve, one city lost, boosts 15%, 2 Dark Ages). $0.00 in model spend.

| Turn | Score | Rank (true) | Alive | Leader | Gap | Era score |
|---|---|---|---|---|---|---|
| 25 | 33 | 4/6 | 6 | 46 | -13 | |
| 50 | 68 | 4/6 | 6 | 118 | -50 | |
| 75 | 132 | 5/6 | 6 | 199 | -67 | |
| 100 | 235 | 5/5 | 5 | 324 | -89 | |
| 125 | 306 | 5/5 | 5 | 438 | -132 | |
| 150 | 391 | 4/5 | 5 | 587 | -196 | |

The run played the same as Run 16 (scores 33, 68, 132, 235, 306, 391 match exactly; cities 2 to 5 on turns 21, 54, 77, 96; a sixth, Copan, on 142 and lost to Free Cities by loyalty on turn 150; no settler lost). CivBench gap table: turn 100 -89 (LLM median -4, AI median -19), turn 150 -196 (LLM +22, AI -26). The true rank is fourth of five: one civ dropped out on turn 87 and two unmet civs lead (587 and 492 at turn 150), matching the Run 16 screen reading of about -206.

| Check | Result |
|---|---|
| PR 259, issue 256, `world_rankings:` line each turn | Pass: 150 lines. Rank, alive and leader score are plausible (alive 6 until turn 86, 5 from 87; leader 587 on turn 150, so rank 4/5 and gap -196, not the old "second of three"). `unmet=` counts 5 early, 2 from turn 50. Unmet civs show as `unmet=N` in the score list. |
| PR 259, `victory_leaders` | Reads: science `us 0` throughout, culture and religious show `unmet 0.04 to 0.07` and `unmet 0.25` on 31 turns (turn 125 on), `Khmer` religious on 5 turns, never `[none]`. |
| PR 259, `unmet_leaders` in victory_threat | Pass: `[none]` on 112 turns, `[culture 0.04 to 0.07; religious 0.25]` on the rest. |
| PR 258, offense counters | Pass: `camps_gone` 1 on turn 44 and turn 121; `kills_barb` 1 on 6 turns, `attacks_barb` or kills nonzero on 19 turns (39 to 104). Run 16 read 0 throughout. |
| PR 257, faith idle only when affordable | Pass: 14 "above reserve but nothing bought" lines, every one with the cheapest great person out of reach (500 to 550 faith against 3 to 78 above the reserve); the effectiveness report has no faith warning. No Holy Site was ever started ("no HOLY_SITE (never started)"). |
| PR 247, peace-offer late acceptance | Not reached: no peace talk in the log. |
| Barbarians near | `hostile_units_near` 5 events, camps acted on; one `district_lost` (wonder district, Wak Kab'nal, turn 27) not examined. |

## Run 16: turns 1 to 150, a fresh 150-turn run from STD_T1_01 (2026-11-17)

STD_T1_01 on main from 9685c99, log `20261117T233448Z`, `--turns 150 --save-at 25,50,75,100,125`, console in `runs/run31.out`. Stopped on `turn_limit` after 3,489 s with no handler errors and no blockers. Decision time mean 14.3 s, p90 19.8 s, max 50.9 s (under the 60 s bar, over the 10 s goal); end-turn wait mean 9.0 s; total per turn mean 23.3 s. Checkpoints `CKPT_STD_T1_01_T025`, `T050`, `T075`, `T100` and `T125` were all written. $0.00 in model spend.

| Turn | Score | Cities | Best rival | Era score |
|---|---|---|---|---|
| 25 | 33 | 2 | 46 | 4 |
| 50 | 68 | 2 | 88 | 15 |
| 75 | 132 | 3 | 178 | 33 |
| 100 | 235 | 5 | 254 | 44 |
| 125 | 306 | 5 | 331 | 53 |
| 150 | 391 | 5 | 417 | 64 |

Second, third and fourth cities on turns 21, 54 and 77 (fifth on 96, a sixth on 142). We finished second of three *met* living players (391 against 417 and 385). That rank and the gap below were wrong (issue 256): the World Rankings screen on turn 151 showed five civs, with two unmet players at 590 and 497, so we were fifth and about 206 behind the leader. The Khmer (player 2) did drop out of every met-civ read on turn 87 and never returned, and a six-civ game with five on the screen means one civ really was eliminated, before turn 100. Against the 17 Nov 30-turn slices (score 203 to 213 and 4 cities on turn 100) this run is ahead (235 and 5 cities). Run 14 was 121 and 229 on turns 75 and 100 under a different build. Copan (founded turn 142) was lost on turn 150 to Free Cities by loyalty after a warning on turn 142 (-19 a turn, governor sent, nothing more to do); no settler was lost. G1 gate clean with five warnings: faith idle (1 of 15 used), gold unspent on 64% of turns above the reserve, one city lost, boosts saved 18% of civic points, two Dark Ages. Score gap table (ours minus theirs): turn 100 leader -19 (LLM median -4, AI median -19), turn 150 leader -26 (LLM +22, AI -26).

| Check | Result |
|---|---|
| PR 247, suzerain change lines | Pass: five lines, one per change (turns 58, 72, 90, 144; "Caguana suzerain changed from none to Sumeria (5) (held since turn 87)"), none repeated. |
| PR 247, peace-offer late acceptance | Not reached: no peace talk in the log (we were at war with no one). |
| PR 249, city_lost taker and cause | Pass: `city_lost` Copan, turn 150, `taker_name: Free Cities`, `cause: loyalty`. |
| PR 249, `runs/<name>_threat.json` each turn | Pass: written, last turn 150. It is named `STD_T1_01_threat.json` (the save name, not the timestamped game name). |
| PR 252, score gap table | Pass: printed at the end, rows for turns 100 and 150 only. |
| PR 253, wonder_completed beside building_completed | Pass: turn 98, Tikal, both `building_completed` and `wonder_completed` for ORACLE. |
| PR 254, victory_threat line each turn, age 0-4, science_rank | Pass: 150 lines, ages 0 to 4 in turn with the age counter cycling, `science_rank=3/3` at turn 146, one religious flag (Sumeria). |
| PR 241, threat map camps acted on | Pass: `camp_orders` above 0 on 19 turns and 72 `military_decision` lines with `"action": "camp"`; `kills_barb` stayed 0, which read as no camp cleared and no barbarian killed. Reread (issue 238): the metric was blind, not the play. Two of the three camps left the map after a unit stepped onto the emptied tile (turn 43 at (69,27), era score +3 next turn; turn 120 at (59,36)); the third stood 15 tiles from a city, past the 13-tile limit. Kills were lost because only a reply saying "killed" counted, and the game's replies carry an estimate ("Post-combat: ~0/46"); a later attack on the same tile got `NO_ENEMY`. The offense line now counts estimated kills and `camps_gone`. |
| PR 241, threat map dumps | Pass: `_threatmap.jsonl` has 150 lines, with camps (strength 45, ages, city distance) and unit sources. |
| PR 237, era score in turn lines | Pass: `era_score`, `era_dark` and `era_golden` in all 150 rows (4, 15, 33, 44, 53, 64 on turns 25 to 150). |

## Run 14: turns 1 to 296, with PRs 225 to 228, stopped once (PR 229); eliminated

STD_T1_01 on main from 5736c59, 2026-11-15, two sessions on one game: `20261115T162807Z` (turns 1 to 53) and `20261115T165303Z` (53 to 296). Result: eliminated on turn 296. Sumeria declared war on turn 241 and took the capital on turn 249, Xunantunich on turn 289 and Coba on turn 291, and then the last city. Final standing: Germany 1,377, Sumeria 1,208, Brazil 870, Mapuche 815, Khmer 791, Maya 385 (40 techs against 63 to 65). $0.00 in model spend. Decision time mean 12.1 s.

| Check | Result |
|---|---|
| Expansion | Stalled on turns 42 to 53: every site near the pop-2 capital failed PR 228's robust test and was refused as "needs a look" while a scout lived, but the scout was on automatic exploration and never looked. Fixed live by PR 229 (needs-a-look marks a site down, never refuses it). The 2nd city came on turn 75, and there were 4 cities by turn 103. From turn 130 the survey refused every remaining site for loyalty. |
| No new-city loyalty loss (228) | Fail: El Mirador at (60,29), Run 13's Uxmal site, founded on turn 114 with robust +11.9 and 81% of its rival side unexplored, flipped on turn 127. The unseen Mapuche capital (pop 13, 6 tiles) pressed 10.4. My change sizing the unseen city like our median city (pop 6 here) made the test too lenient; fixed by PR 230 (largest known city, as a capital in an unexplored heartland). |
| Defence (225) | Fail: the "wary" walls rule never fired, and Sumeria's war took every city. Military strength from civ6-mcp's `get_diplomacy` (`GetStats():GetMilitaryStrength()`) read 426 for us with no units after elimination, so the input is suspect. It is not logged per turn yet. |
| Science | 2 Observatories (turns 53 and 110), 40 techs at the end. |
| Production refused | 13 of 141. Wonders 13. |
| Gold and faith | 6 purchases, 13 upgrades; gold unspent on 82% of turns above the reserve; faith idle up to 557. |
| Score | 44, 121, 229, 340, 398 on turns 50 to 250, behind Runs 12 and 13 throughout; 385 at elimination. |

## Run 13: turns 1 to 327, with PRs 216 to 219, stopped twice and fixed mid-run (PRs 220 to 222)

STD_T1_01 on main from e4ee537, 2026-11-15, three sessions on one game: `20261115T132807Z` (turns 1 to 27), `20261115T134719Z` (27 to 130) and `20261115T141921Z` (130 to 327). A first start (`20261115T035715Z`) was abandoned at turn 51 after a reload. Result: defeat on turn 327, `winning_team=4` (Mapuche: 77 techs, 4 spaceports, and they conquered the Khmer outright), almost certainly science. Final scores: Mapuche 1,492, Brazil 1,293, Sumeria 1,285, Germany 1,092, Maya 778 (5th of 5 left). $0.00 in model spend. Decision time mean 12.5 s (p90 14.8 s).

| Check | Result |
|---|---|
| Expansion | Two stalls, each found live and fixed. First start: barbarians near the capital made the survey refuse every site ("hostile unit near the site"), so PR 209's gate built no settler by turn 51. Fixed by PR 220 (survey as an escorted settler; settlers gather an escort for such sites). Second start: from turn 15 every site was refused for loyalty because a border phantom stood on the nearest rival border tile; fixed by PR 221 (phantom behind the border, map seam handled). Turns 76 to 104: all sites refused again with real rivals known; Run 12's Palenque site read safe -1.0 and a settler was disbanded on turn 95; fixed by PR 222 (drag 3, floor 0). Cities: 2 on turn 49, 6 by turn 147, 4 at the end. |
| No city lost | Fail: Uxmal (60,29), founded on turn 146 at raw +11.1 (safe +4.1), flipped on turn 185 at about -3.5 a turn even with Amani. The breakdown showed Mapuche cities never seen: (55,30) pop 19 pressing 15, (57,27) pop 6. Their borders were "explained" by a known Mapuche city, so no phantom stood in. Coba (64,28, pop 13) fell to the Mapuche on turn 300, six turns after their second declaration of war. |
| Food focus (217) | Works: cities now show `favored=YIELD_FOOD` the turn after it is set. The starving cities have no free food tiles: the capital at pop 17 to 19 ran 6 specialists with 0 unworked food tiles and ended at -10 food. 143 starving notices. |
| Valuation (219) | 21 valuation lines, 23 gold purchases, 27 upgrades, 9 faith purchases. Faith saved for a Great Scientist about 185 turns away because the game's length read as 500 turns: civ6-mcp never reported the speed (Quick, 330 turns). Fixed by PR 223. |
| Production refused | Pass: 8 of 209. Wonders 15. Observatories completed on turns 27, 77 and 173. |
| Blockers | Gate fail: 3 turns held by `ENDTURN_BLOCKING_RELIGION` with no handler. |
| Score | Ahead of Runs 11 and 12 from turn 100 to 250 (147, 309, 487, 673), then 748 on turn 300 after losing two cities. Share of the leader's score 0.67 on turn 200. |

## Run 12: turns 1 to 331, with the Run 11 fixes (PRs 207, 209 and 210)

STD_T1_01 from turn 1 on main at 0e686cf, 2026-11-14, in one session (`20261114T225042Z`) with `--probe placement`, no hands-on help and no stop other than game over. Result: defeat at the turn limit, `winning_team=1` (Germany, on score). Final scores: Germany 1,435, Brazil 1,072, Sumeria 1,072, Maya 924 (4th), Mapuche 889, Khmer 642. $0.00 in model spend. 331 turns in 7,364 s; decision time mean 11.4 s (p90 14.9 s), end-turn wait mean 10.9 s.

| Check | Result |
|---|---|
| Science (210) | Pass: Observatories completed on turns 79, 91, 162 and 168 (Run 11: first one started on turn 184). At the end: 67 techs (Run 11: 48; Germany 74), science 203 a turn (Run 11: 78), 5 Libraries, 5 Universities and 5 Research Labs. No rival won early: the game reached the turn limit (Run 11 ended on turn 301). |
| Expansion (209) | Better: 7 cities at the end (Run 11: 5), the 7th captured from the Mapuche on turn 300. The second city came only on turn 57: barbarians held the first settlers, the escort could never join (`STACKING_CONFLICT` every turn, turns 29 to 39), and the settler that left alone on turn 40 was lost (fixed by PR 211). |
| No new-city loyalty loss (205, 209) | Fail: Tikal at (61,20), the site lost in Runs 9 and 11, founded on turn 83 at estimate +20 (safe +17), lost on turn 88. |
| Loyalty breakdown (207) | Pass: logged on turn 84. Pressure -13.3, happiness -3, a rival Diplomat governor -4. The settle record showed `rival_cities: []`: the estimate knew no rival city, and the Sumerian city 5 tiles away (pop 7) pressed with 7, double the distance formula, as did our own capital, so capitals count twice. Fixed by PR 212 (rival cities from visible tiles, kept in memory; capital weight; border phantoms) and PR 213 (its Lua read never completed in a live game, because `execute_lua` strips the end marker). |
| Production refused | Pass: 16 of 248 (6%). |
| Wonders | 13. |
| Faith idle at most 250 | Fail: up to 2,872; 18 purchases. |
| Gold used | Fail: nothing bought on 71% of turns above the reserve. |
| Builders idle under 50% | Fail: 69%. Starving notices 124. |
| Score | 924 on turn 331, 4th of 6. Behind Run 11 on turns 100 to 200 (slow start) and level from turn 250 (632 each); 814 on turn 300 (Run 11: 759). |

## Run 11: turns 1 to 301, with the Run 10 fixes (PRs 204 to 206)

STD_T1_01 from turn 1 on main at 69f758d, 2026-11-14, in one session (`20261114T152535Z`) with `--probe placement`, no hands-on help and no stop other than game over. Result: defeat on turn 301, `winning_team=1` (Germany), before the turn limit. The end screen names no victory; Germany, Brazil, Mapuche and Sumeria each had 4 spaceports and 65 to 68 techs against our 48, so it was almost certainly a science victory. The bot never raised a science warning (issue 182). Final scores: Brazil 1,117, Sumeria 1,092, Germany 1,034, Mapuche 964, Maya 760 (5th), Khmer 348. First run with per-turn scores (PR 202): 49, 132, 267, 480, 632 and 759 on turns 50 to 300. $0.00 in model spend. 301 turns in 6,429 s; decision time mean 11.1 s (p90 14.5 s), end-turn wait mean 10.3 s.

| Check | Result |
|---|---|
| Production refused under 30% (204) | Pass: 14 of 191 (7%; Run 10: 31%). 9 Bolshoi Theatre, 5 Cristo Redentor. |
| Wonders | 10 (Run 10: 17). |
| No new-city loyalty loss (205) | Fail: Chichen Itza (61,20), founded on turn 71 at an estimate of +16.5 (safe +1.9), read -27 the next turn and flipped on turn 76. Retaken on turn 116, lost again on turn 127 at -12 to -16 with Amani in the city. Run 9 lost the same site. PR 207 now logs the game's breakdown on a warning. |
| No city lost to war (206) | Pass: the Khmer declared war on turn 192; no city fell. 9 upgrades by turn 178. |
| Faith idle at most 250 | Fail, better: up to 890 (Run 10: 1,613); 11 purchases. |
| Gold used | Fail: nothing bought on 69% of turns above the reserve. |
| Builders idle under 50% | Fail: 63%. |
| Settlers | 10 built, 6 founded a city; 141 of 200 settle decisions found no site. |
| Score | Level with Runs 8 and 9 through turn 250 (632; Run 9: 623), 759 on turn 300. Our share of the leader's score peaked at 0.72 on turn 200 and fell to 0.69. |

The main finding is science. We had 48 techs to the leaders' 65 to 68 at the end (Run 10: 55 to 75), and the game ended 29 turns before the turn limit on a victory the bot did not see coming.

## Run 10: turns 1 to 330, with the Run 9 fixes (PRs 199 to 201)

STD_T1_01 from turn 1 on main at 5a5e504, 2026-11-14, in one session (`20261114T125429Z`) with `--probe placement`, no hands-on help and no stop other than game over. Result: defeat at the turn limit, `winning_team=1` (Germany, on score). Final scores read from the game afterwards: Germany 1,471, Sumeria 1,372, Mapuche 1,117, Maya 921 (4th), Brazil 718, Khmer 622. Tech count 55 against Germany's 75. $0.00 in model spend. 329 turns in 7,229 s; decision time mean 12.1 s (p90 16.7 s), end-turn wait mean 9.9 s, so the per-turn rival reads of PR 199 cost no visible time.

| Check | Result |
|---|---|
| Preflight shows 10 patches (201) | Pass. |
| Production refused under 30% (201) | Near: 111 of 362 (31%; Run 9: 62%). 31 were an Aqueduct for a city with no water source, and 54 were one boxed-in coastal city (Yokib) with no free land tile. |
| Wonders built (201) | Pass: 17 (Run 9: 8). |
| `placement_plan` buy-then-place (201) | Fail: all 6 plans were `rules_mismatch` with `game_buy=0`, so no tile was bought for a district. |
| Faith idle at most 250 (200) | Fail, better: up to 1,613 (Run 9: 4,519); 16 purchases. |
| Gold used (200) | Fail: nothing bought on 70% of turns above the reserve (Run 9: 78%). |
| Builders idle under 50% (200) | Fail: 64% (Run 9: 76%); 16% of builder actions refused. |
| No early loyalty loss (199) | Fail: Palenque, founded on turn 101 on an estimate of +2.3 a turn, read -27 the next turn and flipped on turn 105. No other loyalty loss. |
| No city lost | Fail: the capital fell on turn 233 (retaken on turn 243) and Uxmal on turn 276, both to the Mapuche. |
| Score | 921 on turn 330, 4th of 6 (Run 9: 902). |

What the finished game showed (read-only Lua on the turn-330 save):

- **The army was never modernised.** At the end it held 9 Crossbowmen, 4 Cuirassiers and one Field Cannon, with Ballistics, Rifling and Replaceable Parts researched. Production chose Crossbowmen until turn 275. The gold policy logged "no unit can upgrade" 217 times and upgraded only Knights and Coursers. civ6-mcp tests upgrades with `CanStartCommand(unit, UPGRADE, nil, true)`, which was false for every unit, while `CanStartCommand(unit, UPGRADE, true, true)` was true for every Crossbowman (205 gold to a Field Cannon).
- **Gold sat idle under attack.** On turn 275 the bot had 1,226 gold, 6 threatened cities and a reserve of 0, and bought nothing: every unit offered was obsolete (Crossbowman, Courser, Knight, Pike and Shot) and blocked by a unit on the city tile. Uxmal fell the next turn.
- **The capital fell in one turn.** The Mapuche declared war on turn 232 with 5 units next to it, and it fell on turn 233.
- **Palenque's loyalty.** The game's breakdown lists pressure from citizens (capped at 20 a turn) plus religion (-3 for not following our religion), happiness and governors. The estimate counts only pressure, and the site was surrounded by Mapuche cities. On turn 99 a site one tile away scored +5.7 a turn with a margin of 20, 17 site points lower.
- **Repeat district refusals.** The game's own reason was "No suitable location to zone this district." The re-ask-on-change signature covers every tile we own within 3 tiles, so any improvement nearby made the district eligible again.

## Run 9: turns 1 to 330, with Batch B and the placement redesign (PRs 193 to 198)

STD_T1_01 from turn 1 on main, 2026-11-14, in two sessions. Turns 1 to 30 ran in `20261114T061230Z`, which hung on turn 30 after a diplomacy answer (fixed by PR 198). Turns 30 to 330 ran in `20261114T063748Z`, with `--probe placement`, no hands-on help and no stop other than game over. Result: defeat at the turn limit. `PlayerStatus(alive=True, winning_team=1, cities=6)`: Germany again, on score. The final score was 902 on turn 329 (Run 8: 922). $0.00 in model spend. Decision time mean 11.7 s (p90 16.6 s), end-turn wait mean 12.1 s.

| Check | Result |
|---|---|
| Ask again after a diplomacy answer (198) | Pass: fired on turns 233 and 246, and the next turns followed in order. No skipped turn all game. |
| Preflight shows 9 patches; no snapshot Lua error (195) | Pass. |
| No false `blocked` stop (195) | Pass. |
| Production refused under 30% (187) | Fail: 507 of 818 (62%; Run 8: 64%). Causes found and fixed in PR 201 (below). |
| Harbors built (187) | 1 Harbor. 79 Harbor refusals, from the district population cap, not the water filter. |
| `placement_plan` buy-then-place (196) | Fail: all 226 plans were `rules_mismatch`, so no tile was ever bought (PR 201). |
| Probe screenshots (197) | Pass: 12 placement probes, the cap, with sidecars; `scripts/probe_report.py` lists them. |
| Faith idle at most 250 (194) | Fail: up to 4,519; 18 purchases in 247 turns above the reserve (PR 200). |
| Gold used | Fail: nothing bought on 78% of turns above the reserve (PR 200). |
| Builders idle under 50% | Fail: 76% (PR 200 stops buying Builders while any is idle). |
| Great people | Gate fail: 6 waiting at once, 4 wasted. |
| No city lost (174 to 176) | Fail: 3 lost to loyalty. Adab and Chichen Itza on turn 127, Copan on turn 232 (PR 199). |
| 6 cities by turn 150 (180) | Pass: 6 on turn 150, 7 at most, 6 at the end. |
| Score against Run 8 | Level: 153, 277, 452, 623 and 790 on turns 100, 150, 200, 250 and 290 (Run 8: 139, 272, 432, 612 and 796). |

What the finished game showed (read-only Lua on the turn-330 save):

- **Wonder refusals (207):** civ6-mcp's wonder advisor merges the game's `MOUNTAIN_PLOTS` with its valid `PLOTS`, and ranks a bare mountain first. El Mirador's Chichen Itza was offered at (62,19), a mountain, and refused every time.
- **District "no valid tile" (290):** the game's reason was "Requires Population of N". Cities sat at their district cap (El Mirador: 7 of 7 at population 18), and the bot kept choosing new districts.
- **The planner never trusted its rules:** civ6-mcp's district advisor prints at most 10 tiles, so cities with more valid tiles always "missed" some. The rules also allowed luxury and strategic resource tiles.
- **Faith:** Builder purchases refused about 70 times by `STACKING_CONFLICT` (a unit on the city tile), and great people judged "not worth saving for" at 107 faith a turn against a 1,160 cost.
- **Loyalty:** the Chichen Itza site was founded on a margin of 14, which read 82, 16, 92, 96 and 14 on successive turns as a rival city went in and out of the fog. The game then said -24 a turn.

## Run 8: turns 1 to 330, with Batch A (issues 174 to 176 and 180)

STD_T1_01 from turn 1 on main at the merge of PRs 190 and 191, 2026-11-13. It ran turns 1 to 291 in one session, then stopped as `blocked` on turn 291, although the game had already advanced to turn 292 (the screenshot shows turn 292). The run was resumed with no reload and played to the turn limit (330). Game over: `PlayerStatus(alive=True, winning_team=5, cities=7)`, so a rival won at the turn limit. Final score read on turn 291: 796. $0.00 in model spend. Decision time mean 10.1 s (8.3 s after the resume).

| Check | Result |
|---|---|
| No city lost within 10 turns of a war (174) | Pass: no city lost all game (Run 7: 7). One war with Sumeria, turns 168 to about 179. |
| Gold buys or upgrades after a war starts (174) | Pass: the reserve dropped on 97 turns and 32 upgrades were logged, for example Slinger to Archer in a threatened city. |
| Walls in threatened cities (174) | Pass: 3 switches to walls. |
| Two defenders in or beside a threatened city (175) | 89 turns with a `defence` line, 35 lines marked SHORT. A swap ran on turn 10. |
| Refused military moves under 25% (175) | Pass: 2 `STACKING_CONFLICT` among 2,614 military decisions. In all logs, `STACKING_CONFLICT` fell from 135 to 23. |
| No city lost to loyalty (176) | Pass: no loyalty warning all game, so `loyalty_response` never ran. |
| 6 cities by turn 150 (180) | Pass: 6 cities on turn 150 and 7 from turn 175 (founded on turns 2, 47, 83, 104, 127, 132 and 175). |

New in this run:

- **False `blocked` stop on turn 291:** `Turn did not read as advanced` when the game was already on turn 292.
- **A settler stayed on the map from about turn 150 to the end**, while the site survey aged to 139 turns, because no survey ran with the settler idle.
- **Unchanged waste:** 64% of production choices refused (Harbor refused in 7 cities, issue 187); faith idle up to 2,799; gold not spent on 73% of the turns above the reserve; builders idle on 74% of builder-turns (98% after turn 292); 71 starving notices before the resume and 57 after.

## Run 7: turns 1 to 309, after offline batch 2 (issues 148 and 127)

STD_T1_01 from turn 1 on main at 6731933, 2026-11-12 evening, one session with no restart and no hands-on help. It stopped on turn 309 on the game-over screen: `PlayerStatus(alive=True, winning_team=1, team=0, cities=2)`, so a rival won and we were still alive with 2 cities: a Germany science victory (the owner read the end screen; the screenshot shows only the backdrop). Germany also won Runs 3 and 6 on this save. Cities: 7 founded (the 4th on turn 72), 2 lost early, then the capital fell on turn 280, three turns after Sumeria declared war on turn 277, and four more fell by turn 302, after peace on turn 288. 6,797 s of bot time, $0.00 in model spend. Decision time mean 11.5 s (p90 15.7 s), end-turn wait mean 10.5 s.

| Check (PR) | Result |
|---|---|
| `congress:` lines at each session (129) | Pass in the log: 115 lines, votes answered `VOTED|...`. Not compared with the game's own record. |
| Wars read back after a mid-war restart (131) | Not reached: no restart. `runs/STD_T1_01_wars.json` was written (turn 309, no wars). |
| Settle-too-close statement answered (134) | Pass: answered POSITIVE on 2 statements with no turn held. Favor change not read. |
| `stray log line` retries (135) | Pass: 16 retries, 3 handler failures in the whole game (2 blockers, 1 monitors). |
| City strike (136) | Pass: 23 `CITY_RANGE_ATTACK|` results, no `City strike from ... failed`. |
| Production refusals below 30% (143) | Fail: 324 of 549 refused (59%); Wak Kab'nal DISTRICT_HARBOR refused 26 times (HARBOR refused in 5 cities). |
| Claim-great-person blocker (144) | Pass: no `No handler` line; turn 66 held once, cleared and ended. |
| Idle faith under 250 (159) | Fail: up to 1,004 idle above the reserve; 15 purchases in 243 turns. |
| End-of-run "Policy effectiveness" lines (160) | Pass: printed. G1 gate FAILED with 8 warnings. |
| Preflight lines (161) | Pass: four OK. |
| Unit refusals (163) | Mixed: no `refused: UPGRADED`, "can't activate yet" replaces "can't activate at" (84). `STACKING_CONFLICT` appears 135 times, not near zero. |
| Policy cards (164) | 109 `Slot ... takes` lines; the most repeated pair 4 times. |
| Victory-proximity decimals (165) | Pass: no ValueError. |
| Spaceport (166) | Set once, on turn 270 (after turn 200). |
| `production_refused` with tile, `district_completed` (168) | Pass: 324 and 37 events. |
| `UNKNOWN` blocker at congress (169) | Pass: none. |
| Great-person, gold, envoy lines (170) | Pass: 17 activated, 239 `gold_spent`, 309 `envoy_tokens` lines. |
| `build gaps:` lines (172) | Pass: one per turn. Ledger printed "restored from 0 earlier events" at start. |
| No `0_MCP_*` saves | Pass: the newest in the saves folder is from 2026-11-10. |

New in this run:

- **Unhandled `ENDTURN_BLOCKING_RELIGION` (turn 220):** a prophet was activated after the founding step had already run that turn, so the blocker had no handler 6 times. The next pass founded Confucianism. It cost about 35 s, not a stop.
- **civ6-mcp post-turn snapshot:** `Post-turn snapshot failed` with `[string "..."]:65: function expected instead of nil` from `build_units_query`, 30 times. The end-turn events are thinner on those turns. Also seen in Run 6's console.
- **Overview read:** 3 `Overview response has 1 fields` errors caused by the `Omitting feature label: CHOCOLATE` stray line.
- **Policy effectiveness gate:** great people waiting at once 6 (limit 5); faith idle 1,004 (limit 250); 7 cities lost, 5 after a loyalty warning; builders idle on 68% of builder-turns; gold unspent on 75% of turns above the reserve.

## Run 6: turns 1 to 331, with the settle-statement and city-strike fixes (issues 127, 134 to 136)

- **Game:** standard save `STD_T1_01`, two sessions on 2026-11-12 (`20261112T052854Z` for turns 1 to 102, `20261112T134816Z` for turns 102 to 331). No model calls, $0.00.
- **Result:** the game ended on turn 331 and the bot logged "Game over for the local player" with `winning_team=1` (Germany, the same id as the rival-victory warnings) and the local player alive with 2 cities and 12 units. The game was scored, so Germany won on score; the log doesn't name the victory type, so that comes from the owner. 5 cities were lost over the game (turns 87, 135, 244, 247 and 273, from the event log). It doesn't count toward gate G1, because the turn 102 stop needed a fix and a new session.
- **Speed:** decision time (turn time minus the end-turn wait) averaged 10.4 s with a median of 10.4 s, and the end-turn wait averaged 10.4 s (median 6.8 s, longest 59 s), over 331 turns with a repeated line for turn 102. Decision time by period: 8.5 s up to turn 100, 10.6 s for turns 100 to 199, 11.6 s from turn 200. Run 5 was 12.7 s (8.7, 12.4 and 16.3 s). Both are well inside the 60 s development bar.
- **Stops:** one stop that wasn't the game ending, on turn 102 (`blocked`). Khmer's "Your people settle too close!" statement stayed open, with a trade deal pending at the same time. The same message pair had been held and closed on turns 58, 80 and 95. Fixed by PR 134 (answer the statement, clear diplomacy before dismissing popups); the second session held for the other 229 turns.
- **Effectiveness checks (issue 127):** 12 of 31 failing. The worst rows: faith idle above its reserve at 1,574 (one log line shows the cheapest great person costing 2,147,483,647 faith, an unreachable sentinel, issue 146), nothing bought on 84% of the turns gold was above its reserve, 263 "left something unfinished" governor warnings, 10.4 re-sets of one policy slot and policy pair on average, 58% of military moves refused (mostly `STACKING_CONFLICT`), 5 cities lost, 56% of production choices refused (the worst repeat is 22 times, under the limit of 25), 13% of builder actions refused and 8% of turns with a handler failure.
- **What the new fixes did live:** the great-person pile-up fix (PR 126) held: 4 seen, 5 activated, at most 1 waiting at once, 0 "no district" waits and 0 refused activations. Diplomacy: 1 screen closed unanswered (limit 5), 26 of 55 deals accepted, 51 envoys sent. Trade routes: 17 of 17 attempts worked. The wonder reply fix (PR 124) probably brought production refusals down to 56% from 70 to 86%. Unhandled blockers held 21 turns (48 to 66 before): `ENDTURN_BLOCKING_CLAIM_GREAT_PERSON` 18 times and the World Congress results message 3 times (issues 137 and 145).
- **Not live yet:** PR 135 (stray Lua log line in game reads; 5 of the 25 handler-failure turns were in the blockers handler and 14 in the secondary one) and PR 136 (the city strike fix) were merged after this game started. City strikes ran 34 times and 12 failed with the Lua error that PR 136 replaces (first seen on turn 282, from Palenque). A logged successful strike is the live check for PR 136 (the table at the top of this file).
- **Still wrong at the end:** the faith sentinel, governor placement that never finishes, a policy set again every turn, and cities lost (why is issue 147).

The speed figures are computed from the `.jsonl` turn logs as `total_seconds - end_turn_seconds` and `end_turn_seconds`, averaged over the logged turns. Issues 127 and 150 quote 11.1 s and 11.7 s, which this method doesn't reproduce; the method behind those isn't recorded.

## Run 5: turns 1 to 315, the evening of 2026-11-11 (issue 15, before PR 126 and PR 134)

- **Game:** standard save `STD_T1_01`, three sessions: `20261111T220949Z` (turns 1 to 248), `20261111T233601Z` (turns 249 to 264) and `20261112T001949Z` (turns 265 to 315, after PR 126). No model calls.
- **Result:** the game ended on turn 315 with the bot logging "Game over for the local player", `winning_team=4` and the local player alive with 7 cities. Mapuche is player id 4 in the rival-victory warnings; the victory type is not in the logs. 3 cities were lost (turns 133, 296 and 300). It doesn't count toward gate G1, because two stops needed a new session.
- **Speed:** decision time averaged 12.7 s (median 11.8 s): 8.7 s up to turn 100, 12.4 s for turns 100 to 199 and 16.3 s from turn 200. The end-turn wait averaged 10.8 s (median 6.6 s, longest 60 s). No turn took over 3 minutes.
- **Stops:** 2 stops that weren't the game ending, both `blocked`, on turns 248 and 264, with the game already on the next turn (the second time with its "Make Dedication" era-change notification up). PR 126 fixed it by reading the notification turns when the UI doesn't show the new turn.
- **Findings (see `docs/policy-effectiveness.md`):** before PR 126 the great-person advisor read no district for every person, so 21 stacked in one city, 0 of 21 were activated and 583 "no district to activate" waits were logged. After the fix, session 3 activated 23 of 25 but 79 activations were refused (a Great Engineer needs a wonder in progress). The claim-great-person blocker was unhandled in every run (57 held turns in sessions 1 and 2, 12 in session 3; issue 137), faith sat idle (360 before the fix, 1,101 after), and 27 turns had a handler failure.

## Run 4: turns 1 to 325, with the speed and diplomacy fixes (issues 15, 17 and 117)

- **Game:** standard save `STD_T1_01`, played over many sessions and reloads. No model calls, $0.00.
- **Result:** the game ended on turn 325 with "Culture Victory: Sumerian Empire defeats Mayan Empire", as the owner read it from the defeat screen. It doesn't count toward gate G1, because it needed hands-on help (below). Run 3's recorded "Germany won a science victory" is not verified by any log, and the owner has set that conclusion aside.
- **Hangs, each needing a reload or a restart:** turn 105 (after a trade offer the AI accepted with extra terms: Joint War, Open Borders and 30 favor; trade offers are now off in `run_game.py` until the terms are checked); turn 221 (the game held Sumeria's diplomacy screen open with no session behind it, closed by `OnClose` in the view's own Lua state, PR 118); turn 256 (the "Welcome to the World Congress" screen; the watcher logged nothing for eight minutes, cause unknown, issue 117).
- **Speed (decision time, mean):** 40 s on turns 1 to 105; 11.8 s on turns 104 to 221 with the popup scan narrowed and the call waits shortened (PR 116); 14.9 s on turns 262 to 318 with diplomacy answered during the end-turn wait (PR 121). Turns held by a diplomacy screen cost 68 s of end-turn wait against 6.6 s for the rest, before PR 121.
- **War:** Khmer attacked from turn 223 and took Wak Kab'nal on turn 230. Peace was accepted on turn 256. The war tracker lost the declaration twice, because a restart drops its baseline ("start unknown") and because a hang loses the end-turn report that names the declarer. The flag also read "no war" on turns 234 to 248 with no peace deal logged. Explained (issue 88): the game answers a peace offer a little after civ6-mcp checks, so the turn 233 offer was logged as refused, but Khmer accepted it and the war was over at the next read. The game itself reported a new declaration by Khmer on turn 249. The tracker was right and only the "refused" log line was wrong; the tracker now notes a war that ends within two turns of a refused offer.
- **Great people:** many sat unactivated in one city. Not caused by the war alone. PR 120 logs each one with a reason; see the criteria in issue 15.
- **Still wrong at the end:** on turn 318 the turn check read the old turn once and stopped the run as "blocked" (it had advanced); and on the defeat screen the bot looped on a civic-slot hold on turn 325 for several minutes instead of recognising game over.

## Run 3: the fully wired bot from turn 1 (issues 6, 13 and 16)

- **Build:** main at `adef6e6`, with every policy wired in, plus the World Congress fix below. civ6-mcp pinned at `dd20190`. No model calls.
- **Game:** standard save `STD_T1_01`, played from turn 1 to the end of the game, on turn 318, over seven sessions. The owner paused the game on turn 131 and reloaded it on turn 133. Later sessions ran on main as each fix below was merged (PRs 85, 86 and 92 to 95).
- **Result:** recorded at the time as a Germany science victory at the end of turn 318, with the Maya defeated (no log holds a victory event, and the owner has since set that conclusion aside). This is the first game the bot played to the end. It doesn't count toward gate G1, because it needed hands-on help: nudges through World Congress sessions on turns 81, 156 and 306, and the governor title hold on turn 318 (stop 6).
- **Time:** 5.7 hours of bot time for 312 logged turns. The median was 52s a turn: 40s up to turn 131 and 58s after it. Four turns took over 3 minutes (the table below). Turns 200 and 303 were skipped (stops 3 and 4), and turns 131, 132 and 306 have no line because a session stopped on them.

### Stops and fixes

1. **Turn 81, the first World Congress session.** `end_turn` refuses while a session is open unless a voter is registered, even after votes are submitted. The bot voted directly in an open session and never registered one, so every retry was refused. Fixed: voting in an open session now registers the voter first. Confirmed live on turn 81, and on turn 131's session.
2. **Turn 156, an open session the game didn't list.** The voter was registered after the session opened, so it never fired. The game's blocking list stayed empty while the session held the turn, and `end_turn` reported a hang after 12 minutes. Fixed (PR 92): the end-turn watcher (PR 86) also reads whether a session is open, and votes and submits.
3. **Turn 200 was skipped.** The turn still read as 199 while turn 200's blockers showed. The watcher cleared them and asked again, which ended turn 200 before the bot played it. Fixed (PR 93): the watcher acts only on a hold it sees twice with the same turn and the same blockers.
4. **Turn 303 was skipped.** After submitting a session on turn 302, the watcher also asked again to end the turn, which ended turn 303. Fixed (PR 94): after a submit the watcher waits, and doesn't ask again on that check.
5. **Turn 306, a session that stayed open after each submit.** Fixed (PR 95): if the session is still open on a later check of the same turn, the watcher asks once to end the turn.
6. **Turn 318, a governor title nobody could spend.** The only governor left to appoint was Ibrahim. Each appointment was reported as made, but the point stayed (4 points available), and each promotion through civ6-mcp failed with `PROMOTION_FAILED` or `PREREQ_NOT_MET`. `UI.CanEndTurn()` was true, yet the end-turn request never advanced the turn. The bot was stopped, and the turn then ended, with Germany's science victory. How it was released isn't known: the owner may have spent the title by hand, and nobody recorded it. Partly fixed (PR 97): the bot no longer retries an appointment that keeps the point. Why the promotions failed is still open (issue 14).

### Slow turns: one cause

| Turn | Seconds | What held the turn |
|---|---|---|
| 10 | 612 | A unit left with no orders after a refused move, reported as "Command Units" |
| 97 | 693 | A World Congress session reopened after the end-turn request |
| 106 | 616 | No blocker reported, so probably a popup during the AI turn |
| 156 | 716 | An open World Congress session the game didn't list (stop 2) |

The first three, and Run 2's turns 81 and 96, fit one pattern. Something needs attention after the end-turn request, and civ6-mcp only looks after polling for about 9 minutes: then it reports a blocker, or closes popups and asks again. PR 86 added a watcher that checks every 20 seconds while `end_turn` waits, and after it no turn took over 3 minutes except turn 156, which led to PR 92.

### Seen, not fixed

- **Refused moves leave units idle.** A unit's move away from danger is often refused because a friendly unit holds the tile (`STACKING_CONFLICT`), and the unit gets no other order.
- **Great people go unclaimed.** `ENDTURN_BLOCKING_CLAIM_GREAT_PERSON` has no handler under that name (turns 61, 68, 86 and 121). The turn still ends.
- **Units lost near the capital.** Two Builders, a Trader, a Scout, two Slingers and two Warriors died by turn 80, mostly to a Sumerian Archer and barbarians that stayed close for dozens of turns.
- **Traders try routes with no moves left**, and Builders are refused some improvements ("cannot improve").
- **Votes aren't logged**, so how the bot voted can't be checked.
- **The governor point loop (turns 259, 262, 280, 285, 302 and 318).** The bot appoints Ibrahim, the game keeps the point, and the bot gives up for that turn. On earlier turns the turn still ended. On turn 318 it didn't (stop 6).
- **Wars.** Sumeria was at war with the Maya for much of the game, and the city-state Kandy, with Sumeria as its suzerain, joined in. The Mapuche declared war on turn 302 and killed a Heavy Chariot, a Warrior and a Settler, and the score fell from 498 to 448. Peace and war awareness is issue 88.
- **The watcher closes the leader screen** (`LeaderScene`) before an AI deal or demand is answered, so the diplomacy policy never sees the deal.
- **The military handler failed once (turn 287)**, parsing a diplomacy statement: "Overview response has 1 fields".
- **civ6-mcp's victory check raises `ValueError`** on a decimal value ('33.5') in `_check_victory_proximity`. Its report loses that section, and the turn goes on.
- **Resources and gold pile up.** Strategic resources and gold were unspent late in the game. These are issues 89 (resource trades) and 87 (gold spending).
- civ6-mcp's after-turn snapshot sometimes fails with a Lua error. It catches this, and the only effect is a shorter event report.

## Run 2: the wired bot from turn 1 (issues 6 and 12)

- **Build:** the `live-t1-run` branch (main at `61f6759` plus the fixes below), civ6-mcp pinned at `dd20190`.
- **Game:** standard save `STD_T1_01`, identified by seed. Maya, Prince. Played from turn 1 to turn 100 (98 turn lines logged over 13 session files, with no line for turn 40; the first session stopped after one turn, stop 1).
- **Time per turn:** median 25.6s, and 28.2s on average over the 87 turns where `end_turn` took under a minute. Handlers averaged about 15s (monitors 1.2s, settling 5.3s, research 0.5s, production 2.8s, resolver 4.7s) and a normal `end_turn` took 11 to 13s. Settling spikes to 20 to 65s on turns where a settler or scout travels far. Some `end_turn` calls took 79 to 97s, usually around an autosave or a diplomacy retry.
- **Two `end_turn` calls took 583s (turns 81 and 96).** Both returned a normal `Turn N -> N+1` report with no blocker and no handler error, so `end_turn` waited out its full polling time and the AI turn then finished. Cause not found, because nothing could be read while the bot held the one tuner connection. Turn 80 had ended with "Turn paused - AI diplomatic proposal from Sumeria [deal]" and one retry. For turn 96, a natural disaster popup had been dismissed on turn 95. This looks like a `HANG:` that recovered by itself.
- **`0_MCP_*` saves:** 8 left (`0_MCP_0093` to `0_MCP_0100`), 9.9 MB. Older ones were pruned.

### Cities and the first city

- **Cities by turn:** 1 city from turn 1 to 56, 2 at turn 57, 3 at turn 77, still 3 at turn 100 (a fourth settler was on its way). **"4 cities by turn 100" is refuted on this save** with the bot as it stood. The delay came from the problems in stops 3 and 4 below, not from the site choice.
- **First city:** founded on turn 1 at (66,25), the tile the settler started on ("delay over", since `first_city_delay_turns` is 0). `runs/*_settle.jsonl` has the decision. Here score 183. Candidate site scores (before travel cost, none with a path threat): (66,25) 183, (66,24) 175, (65,25) 165, (66,26) 157, (67,26) 153, (67,24) 150, (67,25) 139, (65,26) 139, (65,24) 136, (66,23) 129. `threats` was empty. The result was `FOUNDED|66,25`. Confirmed: the file receives `settle_decision` lines with candidates, rejections, threats and the result.
- **Threats seen:** a barbarian scout 2 tiles from the capital on turn 9. From turn 13 a group of barbarian warriors, slingers and a scout stayed 1 to 3 tiles from the capital until at least turn 77, and never attacked. A Mapuche crossbowman was near the second city on turn 77.
- **Later cities:** second at (63,27) on turn 57 (site score 242, the best on the map), third at (64,22) on turn 77.

### Stops and fixes

1. **Turn 1 stopped `blocked` although the report said "Turn 1 -> 2".** A natural disaster popup (moderate flood) held the game UI on the old turn: the GameCore turn was 2, while the InGame context and `get_game_overview` still read 1. civ6-mcp's `dismiss_popup` (Lua only, no clicks) cleared it. Fix: when the turn doesn't read as advanced, the orchestrator dismisses popups once and reads again before stopping. Test `test_popup_holding_the_turn_is_dismissed_instead_of_stopping`. It worked again live between turns 95 and 96.
2. **The defeat check could not read the player.** `Game.GetWinningTeam()` returns nil before anyone wins, which printed `nil` and failed the integer parse. The Lua now prints -1 for nil. Test `test_status_with_no_winning_team_reads_as_minus_one`. A live read now gives `DEFEAT_STATE|true|-1|0|1|1|hidden` in the InGame context.
3. **Barbarians next to the capital blocked all expansion (turns 9 to 30).** A threatened city scored settlers and builders as zero and defenders and walls high with no limit, so the capital built six defenders and nothing else. Fix: defence only outranks expansion while the empire has fewer defenders than `min(threats + 1, 3)` per city. Test `test_a_long_siege_by_barbarians_does_not_stop_expansion_once_defended`.
4. **The settler found no site for 25 turns.** Nothing explored: the starting warrior is not a scout and the first scout scored 5, below a settler, so the revealed map was about 30 tiles. Fixes: the first scout scores 15 (test `test_the_first_scout_comes_before_a_settler`); a scout waits 2 turns for a camp to leave and then goes, because every route out passed within 2 tiles of the camp (test `test_a_scout_stops_waiting_for_a_camp_that_never_leaves`); and a settler with a combat unit on its tile now ignores "hostile unit near the site", which had rejected every site while the settler stood on the best one with its escort (test `test_an_escorted_settler_founds_though_a_hostile_unit_shadows_it`). It founded the next turn.
5. **`ENDTURN_BLOCKING_GOVERNOR_OPPORTUNITY` (turn 62 on).** The notification reads "New Governor Title Available". The turn still ended, so the game doesn't treat it as a hard block, but the type had no handler and raised `UnresolvedBlockers` every turn (about 3s). It is now handled like an appointment (test `test_a_governor_title_opportunity_is_spent_like_an_appointment`). Not confirmed live that this spends the point: the game still had `points_available=1` and an unassigned governor at turn 80, and no handler error followed on turns 80 to 99.
6. **Seen, not fixed:** the starting warrior and slingers stay in the capital all game; builders are made but never given work (the builder policy was not wired yet; it now is, see below); 625 gold is unspent at turn 100; a trade route sits idle; a unit promotion notification is listed from turn 80 on. A settler escort step onto a tile holding our scout was refused with `STACKING_CONFLICT|Friendly UNIT_SCOUT already on (65,26). Cannot stack same formation class.` The pair held for a turn and went on.

### Checks for the wired bot (PR 74)

| Check | Status |
|---|---|
| `DEFEAT_STATE` Lua parses and prints in both UI and GameCore contexts | Confirmed at turn 25, after the nil fix. Prints in both; the screen field is `unknown` in GameCore. |
| `IsAlive` is true for a living player with units and no city | Not reached. A city existed before any read. |
| `/InGame/EndGameMenu` is found and `IsHidden` flips on the defeat screen | Found and hidden while alive (turn 25). The flip was not reached. |
| A defeat is turned into `GAME_OVER` after `end_turn` returns | Not reached. |
| Settling, research and production policies run with no handler errors | Confirmed on turns 1 to 61 and 80 to 99. Turns 62 to 79 had only the governor error (stop 5). |
| `runs/*_settle.jsonl` receives `settle_decision` lines | Confirmed, turn 1 on. |
| Time per turn with `SharedReads` against the 18s baseline | Not a like for like: 28s on average now. The baseline had no policies, which add about 10s of reads and moves, and the game has more to process. |

### Checks from PR 70 (research and civics)

| Check | Status |
|---|---|
| Real names are `TECHNOLOGY_*` and `CIVIC_*` | Refuted for techs: the policy logs `TECH_MATHEMATICS` and `CIVIC_FOREIGN_TRADE`. The policy works with both. |
| The item in progress appears in the available list | Not read directly. Consistent with it: a new item was started only after a completion, never every turn. |
| `LockedResearch.missing` names match `ResearchOption.name` | Not reached. |
| `turns` is positive for options | Not read directly. No option was refused for it. |
| Switching mid-way keeps progress | Not reached. |
| Research and civic never empty in a full game | Confirmed for turns 1 to 100: the policy set one at the start of every turn that needed it (Mining and Code of Laws on turn 1, then after each completion), with no refusal. Between a completion and the next turn's handler the slot is empty, which doesn't block. Late game not reached. |

### Checks from PR 71 (production)

| Check | Status |
|---|---|
| Item names are `BUILDING_*`, `DISTRICT_*`, `UNIT_*` | Confirmed: `UNIT_SETTLER`, `UNIT_SCOUT`, `UNIT_BUILDER`, `DISTRICT_COMMERCIAL_HUB`, `BUILDING_LIBRARY`. |
| Wonders list as `BUILDING` and are refused with `MISSING_COORDS` | Confirmed at turn 95: `BUILDING_ORACLE` and `BUILDING_APADANA` were refused with `MISSING_COORDS`. |
| `get_wonder_advisor` result shape | Not reached in a way the log shows. |
| District placement | In part: a Commercial Hub was started; `DISTRICT_GOVERNMENT` was refused with `ERR:NO_PLACEMENT|no valid tile` and the policy moved on. |
| `set_city_production` with repair coordinates | Not reached. |
| `District.district_type` and `City.buildings` naming | Not checked directly. |
| Scores need tuning (settler timing, builder count, walls) | Confirmed: the threat response starved expansion (stop 3), the first scout came too late (stop 4), and builders were made but unused. |

### Checks from PR 72 (settling)

| Check | Status |
|---|---|
| Four cities by turn 100 on each standard save | Refuted on `STD_T1_01`: 3 cities at turn 100. |
| `found_city` result strings and failure modes | Success is `FOUNDED|x,y` (turns 1, 57, 77). Failure not reached. |
| Escort and settler stop on the same tile | Confirmed over about 10 steps. The pair moved one tile a turn (`STOPPED_MID_PATH (moves exhausted)`), stayed stacked, and the settler founded on the escort's tile. |
| A combat unit and a settler can stack; `move_unit` onto an unrevealed tile | Stacking confirmed. Unrevealed tiles not reached. A combat unit can't join a tile holding our scout (`STACKING_CONFLICT`). |
| `automate_explore` leaves the scout needing no orders | Not reached: the scout was routed to frontier points and those moves succeeded. |
| Fog rays are approximate | The frontier points were reachable (`MOVING_TO` succeeded). |
| Site-score weights and `min_site_score` 50 | Not tuned. First-city options scored 129 to 183 and later sites 98 to 242. |
| Names `UNIT_SETTLER`, `UNIT_SCOUT`, sea filter | Names confirmed. Sea filter not reached. |

### Checks from PRs 64 and 65 that this run reached

| Check | Status |
|---|---|
| Threat monitor: no errors, radius, stealth units | Ran every turn with no error and raised `hostile_units_near` for barbarians 1 to 3 tiles away. No stealth units seen. |
| Victory monitor with a city | Confirmed: no error on turns 1 to 99, including the empty read before the first city on turn 1. |
| `enabled_victories` empty semantics | Refuted again for the overview: turn 1 `get_game_overview` returned an empty set. |
| `has_met` / `is_at_war` against real declarations | Not reached. Mapuche and Sumeria were met by turn 77 to 80, with no war. |
| Religion majority names, 20 diplomatic points | Not reached. |
| A full game ending every turn with no block | Confirmed for 99 turn ends: all ended or stopped only on stop 1. Two took 583s (see above). |
| World Congress, `STACKED_UNITS`, great person claim, promotion lookup | Not reached. A promotion notification was listed from turn 80 on and never appeared in the blocking query. |
| Auto End Turn must be off | Consistent: turns ended only when the bot ended them. |
| Diplomacy session handling | In part: "Turn paused - AI diplomatic proposal from Sumeria [deal]" on turn 80 was cleared and the retry went through. |
| Unknown blocker types | `ENDTURN_BLOCKING_GOVERNOR_OPPORTUNITY` was logged unresolved on turns 62 to 79 (stop 5). |

## Pending: builders, government, trade, religion and great people (issues 1.6, 1.10 and 1.11)

`BuilderPolicy`, `GovernmentPolicy` and `SecondaryPolicy` are now handlers in `build_orchestrator`, and the resolver uses their blocker policies. A fake-game test runs a full turn through every handler. None of this has run live yet: the checks below, and the builder and secondary behaviour in play, are the live acceptance.

### Government, policies and governors (issue 1.10)

Written against civ6-mcp's source at the pin, not yet run live.

| Check | Status |
|---|---|
| `change_government` reply contains `GOVERNMENT_CHANGED` on success and `GOV_LOCKED` for a locked one | Not run. The policy tries every better government best first, so the first turn costs up to 12 calls, then one pass per completed civic. A read of unlocked governments would replace this. |
| A government change applies only on a later turn, so slots read stale in the same turn | Not run. The policy skips filling slots on the turn it changes government. |
| Policy cards survive or empty on a government change, and a refill is free | Not run. |
| Whether slotting or swapping a card costs gold outside a new civic | Not run. The policy never swaps a filled slot. |
| `set_policies` for one slot returns `POLICIES_SET`; a wildcard slot accepts cards of any type | Not run. |
| `appoint_governor` returns `APPOINTED` or `APPOINT_REQUESTED`, and the point drops by the next read | Not run. A point that doesn't drop stops the loop for the turn. |
| `assign_governor` returns `ASSIGNED`; a governor left unassigned stays so after the idle blocker is dismissed | Not run. |
| Governor promotion list includes ones with unmet prerequisites (`PREREQ_NOT_MET`) | Not run. The policy falls through to the next promotion. |
| Default rank, card and governor priorities are sensible in play | Not tuned. |
| No policy slot empty for more than 5 turns in a full game | Not run. |

## Run 1: blocker resolver and monitors (issues 1.2 and 1.3)

- **Build:** this repo at `3f4a4c8` plus the fix below, civ6-mcp pinned at `dd20190`.
- **Game:** standard save `STD_T1_03`, identified by seed. Byzantium, Prince, no city founded yet, because the bot only passes turns.
- **Turns played:** 35 to 38 (4 turn ends, all normal). Turn 39 never finished.
- **Time per turn:** 20.2, 19.3, 17.5 and 16.4 seconds, about 18 on average. Monitors took 1.3 to 2.4s of that, the resolver a steady 4.0s with nothing to resolve, and `end_turn` 10 to 12s.
- **`0_MCP_*` saves:** 8 left (4 older, 4 from this run), about 8 MB. Older ones are pruned, so they don't pile up.

### Stops

1. **Victory monitor, turn 39.** Before a player has a city, the game reports it as not alive, and `get_victory_progress` then lists no players at all, not even us (all five rivals were unmet). The monitor treated that as an incomplete read and failed every turn. Fixed: with no cities and an empty read it returns nothing. Test: `test_no_city_yet_is_not_a_failed_victory_read`.
2. **Turn 39 "hang" was a defeat.** After `end_turn`, the turn stayed on 39 for over 15 minutes, with only the barbarian player still marked as taking its turn. No blocker, diplomacy session, trade deal or popup was open, and the tuner still answered. At the time this looked like the `HANG:` case from `docs/blockers.md`, and no recovery was attempted. **The owner has since confirmed that the game had actually ended in a defeat.**
   - **Confirmed:** the game was over (owner, by looking at the game). The bot never founded a city. Barbarians captured our warrior on turn 39.
   - **Inference, not confirmed:** the defeat came from losing our last unit. Nothing was read from the game to show this, such as `IsAlive` or the defeat screen, before the run stopped. Which units we still had, and whether a settler was lost earlier, were not recorded.
   - **From the game's own UI scripts (read only):** the defeat screen is shown on the `PlayerDefeat` event and pauses the game, so the turn never advances. That fits a turn that stays put with no blocker open, though it was not observed directly.
   - **Not known:** whether `Players[me]:IsAlive()` is already false for a player who simply has no city yet. The victory monitor's empty read before turn 39 suggests it might be (stop 1), so `civ6_ai.defeat` does not trust "not alive" on its own. It needs the end-game screen up, or no units and no cities left.

### Side findings in civ6-mcp at the pin

- `GameState.check_game_over` can never succeed here. Its InGame query fails with a nil comparison in Lua (while we have no city), and the fallback calls `build_gameover_check_gamecore`, which is defined in `civ_mcp/lua/overview.py` but not exported from `civ_mcp.lua`, so the call raises and the error is swallowed. It returns `None`, so game-over detection during a hang is blind. We don't patch civ6-mcp. `civ6_ai/defeat.py` is our own read-only check (fixed Lua through `execute_lua`, UI context first, then GameCore), used by the turn loop when a turn is `BLOCKED` or `HUNG`, or `end_turn` raises.
- **How long a defeat takes to notice.** `end_turn` (civ6-mcp `end_turn.py`) polls for the turn to advance for 4s, then 550s more in 31 steps, so about 554s (9.2 minutes) plus the read time of each poll. Its mid-wait game-over checks (every poll of 10s or longer) go through the blind `check_game_over`, so they find nothing. Phase 3 then adds a diplomacy probe, a trade-deal check, one popup dismissal that can add up to about 10s, and a final 2s wait. So `end_turn` returns about 9.5 to 10 minutes after a defeat, with a `HANG:` or "End turn blocked" report, and our check turns that into `GAME_OVER` straight away. This is computed from the code, not timed live. The earlier T39 wait of over 15 minutes was the owner's observation of the game, not the time `end_turn` took to return.

### Turn-time costs (run 1 numbers, with an inference)

- The resolver's steady 4.0s with nothing to resolve is five reads in a row (diplomacy sessions, trade deals, World Congress, the end-turn blocking query and notifications), so about 0.8s each. Each call to civ6-mcp waits 0.1s to drain stale output before sending and 0.2s after the sentinel, which is 0.3s of every read that never goes away, and the rest is the game answering through the tuner. Every query ends with its sentinel, so none is waiting out a 2s timeout. This is inferred from the code and the totals, not timed per read. No read can be dropped safely: each is a separate blocker source that `end_turn` itself checks, so it was not trimmed.
- What was trimmed: reads that several handlers repeat in a turn (`get_game_overview`, `get_cities`, `get_empire_resources`, `get_diplomacy`, `get_victory_progress`, `get_threat_scan`) are now answered from memory by `SharedReads` until any other call changes the game, and `end_turn` always clears it. Empty answers are never kept, because civ6-mcp returns a timed-out read as a short result. `read_cities` (used by the settling and production policies) raises `IncompleteRead` when the city count differs from the overview's, so no policy acts on a short city list.

### Live checks needed for the wiring and defeat detection

| Check | Status |
|---|---|
| `DEFEAT_STATE` Lua parses and prints in both UI and GameCore contexts | Not run. No live game this round. |
| `IsAlive` is true for a living player with units and no city | Not run. Decides whether "not alive" could ever be a false alarm. |
| `/InGame/EndGameMenu` is found and `IsHidden` flips on the defeat screen | Not run. |
| A defeat is turned into `GAME_OVER` after `end_turn` returns | Not run. Expected about 9.5 to 10 minutes after the defeat. |
| Settling, research and production policies run in a played turn with no handler errors | Not run. |
| `runs/*_settle.jsonl` receives `settle_decision` lines | Not run. |
| Time per turn with `SharedReads` against the 18s baseline | Not run. |

### Live checks from PR 65 (resolver)

| Check | Status |
|---|---|
| A full game ending every turn with no block | Not reached. Four turns only. |
| `vote_world_congress` plus submit-only Lua clears an open session | Not reached. |
| `queue_wc_votes([])` satisfies the World Congress gate | Not reached. `end_turn` logged "WC fires this turn with 0 resolutions" on turn 39, but that is its own check, not our voter. |
| `move_unit` to adjacent tiles clears `STACKED_UNITS` | Not reached. |
| `NOTIFICATION_CLAIM_GREAT_PERSON` ever blocking | Not reached. |
| `get_unit_promotions` for units without XP | Not reached. A promotion notification stayed listed for turns 36 to 39 but never appeared in the blocking query, so nothing was exercised. |
| Auto End Turn must be off | Consistent: turns ended only when the bot ended them. Not read from the settings. |
| Resolver ran without handler errors | Confirmed for 4 turns, with nothing to resolve. |

### Live checks from PR 64 (monitors)

| Check | Status |
|---|---|
| `get_diplomacy` `has_met` / `is_at_war` match real war declarations | Not reached. No war occurred. `has_met` was false for all five rivals, matching `HasMet` read directly. |
| Religion majority types and founder names in `get_victory_progress` | Not reached. No religions existed. |
| `get_threat_scan` radius and stealth units | Not reached. Barbarian units were reported 1 to 2 tiles from our units, but no stealth units, and with no city no event was raised. |
| `enabled_victories` empty semantics | Refuted for the overview, which returned an empty set while all five victories were enabled. `get_victory_progress`, which the monitor uses, listed all five. |
| 20 diplomatic points to win | Not reached. |
| Victory monitor with no city | Refuted, then fixed (stop 1). |
| Threat, resource and victory monitors ran without errors | Threat and resource: confirmed on turns 35 to 38. Victory: failed on turn 39 (stop 1). |

### Live checks for screenshots

| Check | Status |
|---|---|
| `capture_game_window` returns the game frame | Confirmed on a borderless 2560x1440 game at turn 1: a legible 1280-pixel JPEG of about 130 KB. Not checked in exclusive fullscreen, where `PrintWindow` may return a black frame (reported as a failed capture). |
| A stopped run or a first handler error saves a file and names it in the turn log | Not reached live. Covered by `tests/test_screenshots.py` with a fake capture. |

### Stray log lines in game reads (retry)

- **What was seen live (Run STD_T1_01):** `ValueError: Overview response has 1 fields, expected >=14: Omitting feature label: CHOCOLATE HILLS` and `... Request to BulkHide( true, Diplomacy ), Show on 0 = 1`, raised by `get_game_overview` for the military, secondary and wars handlers. A Lua log line came back in place of data.
- **Fix:** `SharedReads` (src/civ6_ai/bot.py) retries a read up to 2 times, 0.3 s apart, when it raises a `ValueError` that is a field-count error whose reply has no `|` delimiter. This covers the shared reads and every plain `get_`/`list_`/`check_` read. Each retry logs one warning ("stray log line"). Other errors are not retried, and a failed read is never kept. Calls are added only on failure.
- **Not checked live:** whether the noise clears on a second read, and whether other parsers phrase the error differently.


## Run 15 short slices (2026-11-16): 50-turn experiments from STD_T1_01

A game is deterministic from the same save and code, so one 50-turn slice (`run_game.py --load STD_T1_01 --turns 50`, then `scripts/slice_report.py <turn log>`) is a clean A/B test. The baseline repeated exactly (score 61, second city turn 32, 10 attacks) across two runs. Each row changes one thing against the row above it unless noted.

| Change | Score T50 | Cities | Second city | Units lost | Barbarian attacks / kills | Ancient-era score |
|---|---|---|---|---|---|---|
| Baseline | 61 | 2 | T32 | 3 | 10 / 3 | 2 (Dark Age) |
| Attack ratio 1.5 to 1.0 (kept) | 69 | 3 | T35 | 6 | 21 / 7 | 6 |
| Attack ratio 0.7 | 61 | 2 | T32 | 6 | 17 / 7 | 6 |
| First settler without waiting for a survey (PR 234, rejected) | 45 and 53 | 2 | T43-50 | 4 | | 8 |
| Remember camps seen from the capital, camp force ratio 1.2 (reverted) | 61 | 2 | T35 | 3 | 21 / 6 | 6 |
| Same, camp force ratio 1.0 (reverted) | 64 | 2 | T35 | 2 | 11 / 5 | 6 |
| Attack ratio 1.0 and no builder before the first settler (kept) | 69 | 3 | T36 | 2 | | 6 |

Findings:
- Early barbarian pressure decides the first 50 turns. The settler or its escort loses turns on a threatened route (nine turns from finished settler to founded city), and PR 234's earlier settler was killed on turn 17.
- Two barbarian camps stood 4 and 7 tiles from the capital. The bot never attacked one: it only saw camps while a barbarian was near and the camp tile was visible, and a remembered camp (seen turn 27) still was not destroyed with two units. Camps are worth 2-3 era points each in the game's `Moments` table, so this is still open.
- Boosts are 40% of base cost in the game's `Boosts` table (125 of 126 rows; Near Future Governance is 90).
- Every Ancient era ended below the Dark Age threshold of 9 (scores 2 to 8): a Dark Age at turn 41 in all slices. The cheap early era points in `Moments` are camps (2-3), natural wonders (1), pantheon (1), meeting civs (1), small and medium city sizes (1), government tier (2) and great people (1).
- Decision time stayed under 10 s per turn.

### How to read a slice (added after review)

- **Horizon.** A 50-turn slice judges changes whose effect lands by about turn 35-40. A change that acts late (a second city founded at T36 pays off in yields and growth over the next 30+ turns; a builder's tile improvements compound) has not finished showing its effect at T50. `slice_report.py` prints the score and city trajectory every 10 turns and the gain over the last 10; if a gap is still widening at the end, play on from a checkpoint (`--save-at 50`, then load it with `--turns 50`) to T100 before keeping or rejecting. Screen fast at 50, confirm slow at 100.
- **One start is one data point.** Every row above is one civilization, one map and one seed (STD_T1_01). "Kept" means kept as a default for that start, not proven in general. Re-run a kept change on a second start before treating it as more than that.
- **Nothing is a hard rule.** Results are encoded as defaults in the policy document's weights, never as unconditional code. The attack ratio is `military.min_damage_ratio` (postures already move it: DEFEND 1.3, CONQUER 0.8). The builder hold is `production.builder_hold_turns` (0 turns it off) and applies only while a settler is wanted: a site is known, expansion is weighted, and more than one city is the target. A tall start (one city, target_cities 1, or no survey site) trains builders at once. Reverting any kept change is a policy-document edit.


## Opt-in features, first live A/B (2026-11-20 to 21)

The features merged off by default in PRs 326 (cap focus), 327 and 349 (wonder races), and 343 and 351 (boost judgement) were switched on with `run_game.py --enable NAME` (PR 353). Each run was compared with a baseline from the same save on the same code. Runs are deterministic, so each row is one sample.

**Controls.** The STD_T1_04 baselines on main ecbf288 and e5e3668 were identical: score 80 at T50, 92 at T60, cities founded T2 and T24, era score 16. The STD_T1_03 baselines on the same two commits were also identical (56 at T50). Control runs on fd5e04e (adds PR 349) and 8e52823 (adds PR 351) matched ecbf288 turn for turn. So the opt-in merges left default play unchanged.

| Feature | Save, horizon | Fired | Focus metric | Score (feature / baseline) |
|---|---|---|---|---|
| cap_focus | STD_T1_04, 60 turns | 5 focus sets (Rome capped one turn, T41) | build list identical | T50 80 / 80, T60 93 / 92 |
| cap_focus | STD_T1_03, 50 turns | never (no city at a cap) | identical | 56 / 56 |
| cap_focus | STD_T1_04_TURN78, 30 turns | 23 focus sets: Ostia and Aquileia alternate production and gold every two turns | population T107 41 / 41 | T107 368 / 372 |
| wonder_races | STD_T1_04, 60 turns | race lines from T41 | identical through T50 | T60 94 / 92 |
| wonder_races | STD_T1_03, 50 turns | no wonder offered | identical | 56 / 56 |
| wonder_races | STD_T1_04_TURN78, 30 turns | 144 GO lines, all "(not tall and safe)" | wonders started: 0 / 1 (Oracle, T96) | T107 374 / 372 |
| boost_judgement | STD_T1_04, 60 turns | Early Empire delayed 2 turns, then slipped | identical | 80 / 80 |
| boost_judgement | STD_T1_03, 50 turns | Writing delayed T13 to T22 for a meeting that never came | techs T50 9 / 10, civics 6 / 5, third city T48 / T45 | 55 / 56 |
| boost_judgement | STD_T1_04_TURN78, 30 turns | yes | population T98 34 / 35 | T107 373 / 372 |

Verdicts:
- **Cap focus: not switched on.** The openings barely reach a cap, and from turn 78 the focus has no hysteresis, so citizens are reshuffled every other turn. Fix in issue 356.
- **Wonder races: not switched on.** The race estimator works, but `tall_and_safe` is empire-wide (any threatened city, any settler, any camp force), so no wonder is ever started. Fix in issue 357.
- **Boost judgement: not switched on.** A fixed estimate (a meeting "in about 6 turns from exploring") counts down to 1 and stays there until the 8-turn cap, which cost a tech on STD_T1_03. The builder half never ran: `BuilderWeights.use_boost_nudges` is off and was not switchable, so the Masonry quarry nudge logged on T40 to T51 was ignored. Fix in issue 358.

### Re-test after the fixes (2026-11-21)

**Baseline contamination found and fixed (issue 365, PR 366).** A run from `STD_T1_04_TURN78` that followed a run from the `STD_T1_04` start was taken for that game's continuation, because turn 78 comes after the earlier run's last turn. It restored 31 build-ledger events and the scenario-wide war and threat files of an unrelated game. Both turn-78 baselines above were affected, while the feature runs, which followed runs that ended after turn 78, were clean. A bisect on clean runs showed that the gap between the two contaminated baselines (372 and 368 at T107) came from the inherited state, not from code. Now a `--load` run is a new game (a `.fresh` marker the session grouping honours), and war and threat state is kept per game. Live: two turn-78 runs, one right after a start run, restored 0 events and were identical. The turn-1 comparisons above are unaffected, since a run from turn 1 never counts as a continuation.

From STD_T1_04_TURN78, 30 turns, all runs clean, compared with the clean baseline on fab83b7:

| Run | Score T107 | Population T107 | Notes |
|---|---|---|---|
| Baseline | 372 | 41 | starts the Oracle in Rome on T96 |
| cap_focus, no hysteresis | 368 | 41 | 23 focus changes |
| cap_focus with hysteresis (PR 362) | 368 | 41 | 10 focus changes |
| wonder_races, empire-wide gate | 374 | 43 | no wonder started |
| wonder_races, per-city gate (PR 361) | 372 | 41 | starts the same wonders as the baseline |
| boost_judgement | 373 | 41 | |

STD_T1_03 from the start, 50 turns, `--enable boost_judgement,boost_nudges`, compared with a control on 7cdcf4a that matches the earlier baseline:

| Run | Score T30 / T50 | Era score T50 | Techs / civics started by T50 |
|---|---|---|---|
| Control | 31 / 56 | 6 | 10 / 5 |
| Overdue rule (PR 360) | 31 / 57 | 8 | 9 / 6 |
| Plus meeting evidence (PR 364) | 29 / 55 | 5 | 11 / 6 |

Verdicts: all three stay off by default.
- **Cap focus:** hysteresis stopped the reshuffling, but the rule still costs score from turn 78. Its yield choices are production and gold only, never science or culture, which the score counts.
- **Wonder races:** with the per-city gate it now matches the baseline. It needs a save where wonder races are contested before it can show a gain.
- **Boost judgement:** Writing is no longer held for a meeting that never comes, and research gained a tech and a civic by T50. Score moved within 2 points either way, by research order alone (Bronze Working ahead of Sailing for a barbarian-kill boost). It needs more saves.


## Third city on the human's save (2026-11-21)

A head-to-head on STD_T1_04 (Rome), the save the first human game was played on. The run on main dc35850 was compared with the human's observed game. The bot matched the human to turn 20 and founded its 2nd city earlier (turn 23 against 29). After that it fell behind: 3rd city on turn 60 against 49, and score 80 against 130 at turn 50.

The bot log gave two causes, and a third showed up beside them:
- **Settler outbid (issue 369).** From turn 24, camp-force Slingers and Warriors (16.7 to 27) outbid the settler (about 10.4) in Rome, though the camp didn't block expansion.
- **Escort step onto a mountain (issue 368).** From turn 46 to 54 an escorted settler was held: its escort step came from a straight line, and on every turn that step was the mountain at (66,16).
- **Military moves refused (issue 370).** Military fallback moves were refused 13 times on water or mountains.

Each change was run for 60 turns from STD_T1_04 and compared with a baseline rerun on the code under it. Each row is one deterministic sample.

| Run (code) | 3rd city | Score T50 / T60 | Era score T60 | Refused onto water or mountain (military / settler) |
|---|---|---|---|---|
| Baseline (dc35850) | T60 | 80 / 92 | 18 | 13 / 9 |
| Military terrain filter, PR 372 (dc35850 + 372) | T60 | 80 / 93 | 19 | 0 / 9 |
| Settler before camp units, PR 371 (dc35850 + 371) | **T46** | **92 / 116** | 25 | 12 / 2 |
| Baseline (e9ea87e = 371 + 372) | T55 | 82 / 114 | 29 | 0 / 0 |
| Terrain-path escort step, PR 373 (e9ea87e + 373) | **T47** | **92 / 116** | 26 | 0 / 0 |

- **Second save.** PR 371 on STD_T1_03 (50 turns, against a dc35850 baseline) was identical: score 56 / 56. Camp demand came up once there, on a turn with no settler on offer, so the rule never had a chance to fire.
- **No losses.** No unit or city was lost in any run.
- **First try at PR 373.** On dc35850 the escort avoided the mountain, but then it bounced between two tiles beside a city-state border. The game refused that territory, and a refused step was forgotten once the pair moved. Keeping refused tiles for the whole trip fixed it.

Verdicts:
- All three are merged.
- Main 6156fb2 founds a 3rd city on STD_T1_04 on turn 47, against 60 on dc35850. Score at turn 50 is 92 against 80.
- The human's 130 at turn 50 is still far ahead. The largest remaining gap is faith: the human had a Holy Site on turn 19 and a religion on turn 45, and the bot has neither. Population (7 against 14 at turn 60) and Rome's housing cap at 4 come next.
