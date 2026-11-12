# Strength review: where we trailed Germany on score in Run 6

Research for issue 153. Run 6 reached the end of the game (turn 331 of about 330) with the Maya alive on 2 cities, and Germany won on score. This page works out how far behind we were, where we fell behind, and what would have closed it. It reads logs only: no game calls and no model calls. The cause of the city losses is issue 147 and is not repeated here; this page only prices them.

## Short answer

1. **We can't say how far behind Germany we were, because no log holds a rival's score.** The turn log has our score every turn and nothing for any rival. Our final score was 371 (turn 330). The only Germany figures in the logs are diplomatic points (10 of 20 on turn 282 in Run 6; 10 on turn 259 and 16 on turn 310 in Run 5) and its suzerainties.
2. **Run 6 had the worst trajectory of the four full games, and the cause is cities.** Our score tracked Runs 4 and 5 until about turn 125 (216 against 202 and 248), then flattened: 199 on turn 150 against 267 and 321. Five cities were lost across the game, among them the capital on turn 135.
3. **Run 5 scored about twice as much at the same turns** (644 against 334 on turn 250) with 7 cities at the end against 2. Run 5 still did not win; the game ended with the Mapuche winning.
4. **Against CivBench we were below the built-in AI mean from turn 150 on in Run 6**, but Run 5 came within 2% of the frontier-model median at turn 250. Run 6 sat at about 40 to 45% of the built-in AI mean at turn 250.
5. **Per-component data is mostly missing.** Cities and population are read every turn and thrown away; rival scores are read every 5 turns and thrown away. Both reads already exist (see "Missing data").

## Our score over time, by run

Our score from the `Score: N` line of each turn log (the end-turn report). The last logged turn at or before each column. Run 3 is `from_fullgame`, Run 4 is the first session plus `from_run4_*`, Run 5 is the three sessions of 20261111 to 20261112, Run 6 is the two sessions starting 20261112T05.

| Turn | Run 3 | Run 4 | Run 5 | Run 6 |
|---:|---:|---:|---:|---:|
| 50 | 56 | 57 | 57 | 57 |
| 100 | 139 | 145 | 179 | 158 |
| 125 | 187 | 202 | 248 | 216 |
| 150 | 221 | 267 | 321 | 199 |
| 200 | 323 | 401 | 482 | 256 |
| 250 | 423 | 465 | 644 | 334 |
| 300 | 498 | 518 | 680 | 350 |
| last logged | 317 | 324: 596 | 314: 772 | 330: 371 |

Score gained per 50 turns:

| Window | Run 4 | Run 5 | Run 6 |
|---|---:|---:|---:|
| 50 to 100 | 88 | 122 | 101 |
| 100 to 150 | 122 | 142 | 41 |
| 150 to 200 | 134 | 161 | 57 |
| 200 to 250 | 64 | 162 | 78 |
| 250 to 300 | 53 | 36 | 16 |

Run 6 matched the others through turn 100, then lost about 100 points per window for 150 turns.

## Our score against each rival

**Not answerable from the logs.** What exists:

- The turn log's `Score:` is ours only.
- `SCOREBOARD` notices (science per turn and our rank) appear on about 15 turns across all runs, and the report line is truncated on some. In Run 6 on turn 124 our science was 23.8 a turn, rank 4 of 4, with the Mapuche leading at 82. Earlier runs: 4 of 4 behind Sumeria (64 a turn) on turn 100 in Run 4; 3 of 4 behind Sumeria (104 a turn) on turn 175 and 4 of 5 behind Sumeria (222 a turn, ours 84) on turn 254 in Run 5. So we were at or near the bottom of science in every run, at 25 to 40% of the leader.
- `rival_victory_warning` events give diplomatic points, not score. Run 6: Khmer 13 and Mapuche 10 on turn 232, Khmer 17 on 257, Germany and Sumeria 10 on 282, Mapuche 16 and Sumeria 15 on 307.
- Germany held the suzerainty of Kandy and Caguana only briefly (turns 259 to 293, flipping with Khmer and the Mapuche). Nothing says it led on score at any named turn.

The result that Germany won on score at the turn limit is taken from the owner's reading of the run (and `winning_team=1` in the defeat line), not from a score table.

## What the score is made of, and what the logs hold

| Component | Run 6 evidence | Run 5 | Run 4 | Gap |
|---|---|---|---|---|
| Cities | Founded on turns 1, 29, 101, 122, 152, 238; lost on 87, 135, 244, 247, 273. Final 2 per the defeat line; the events imply 1, so one change is unlogged. | Founded 1, 29, 61, 78, 97, 125, 145; lost 133 (loyalty), 296, 300. 7 at the end. | Lost the capital on turn 230. | Run 6 peaked near 4 and fell to 2, against 7. The largest cause. |
| Population | Not logged. `Empire.population` is read each turn. | Not logged | Not logged | Unknown; follows cities. |
| Techs | 38 distinct `Started TECH_` lines | 54 | 48 | 16 fewer than Run 5. "Started" is not "finished". |
| Civics | 32 distinct `Started CIVIC_` lines | 50 | 41 | 18 fewer than Run 5. |
| Wonders | 5 started (Apadana, Chichen Itza, Etemenanki, Mont St Michel, Taj Mahal). None logged as completed. | 8 started (also Oracle, Cristo Redentor, St Basil) | 0 | Completion is not logged; a city lost with a wonder in it loses the wonder too. |
| Great people | 4 `great_people` lines, 1 person (Chaucer, turns 128 to 131) | 188 + 47 lines, up to 21 stacked and unactivated before the fix (issue 126) | No `great_people` lines | Run 6 had almost none seen. Activations are not counted anywhere. |
| Era score | `Empire.era_score` is read, not logged | Not logged | Not logged | Unknown. |

Notes on reading this:

- The game's score rules (a few points for each citizen, each tech and civic, each wonder, each great person, and so on) are not written down in this repo, so a conversion from components to points would be a guess. The gap is too large for techs and civics alone: 16 techs and 18 civics are a small share of the 310 point gap with Run 5 at turn 250. Cities and everything inside them (population, districts, buildings, wonders, the great people they produce) are the likelier bulk.
- The drops confirm what a city is worth: the capital falling on turn 135 cost 64 points that turn (250 to 186). Turns 243 to 246 (two cities) cost 43 and 48. Run 4's capital loss on turn 230 cost 48. The growth forgone after each loss is larger than the immediate drop (see the window table).
- "Started" counts are the cheapest proxy and are lower bounds: a research can be set and not finished, and `builds BUILDING_X` is a production choice, not a completion.

## Where we fell behind

1. **Turns 75 to 100, no loss yet.** Score 134 on turn 75 against 120 and 116 in Runs 4 and 5, so early expansion was not the problem. Uxmal, the second city, was lost on turn 87 (Sumeria declared war on turn 84; session 1 refounded it on turn 101) and session 1 stopped on turn 102 (stuck trade screen, `blocked`).
2. **Turns 102 to 150, the decisive window.** Sumeria declared war again on turn 129. Capital lost on turn 135. The capital is the biggest city and holds the early wonders, so this one loss is the largest single cause of the gap (+41 in 50 turns against +142). Run 5 faced the same war and kept all but one city.
3. **Turns 150 to 240, rebuilding.** Three cities were refounded or founded (turns 122, 152, 238) and the score rose about 1.1 a turn, against 3.2 for Run 5.
4. **Turns 243 to 273, the Mapuche war.** War on turn 243 and again on 268. Three more cities lost (244, 247, 273). Score ended at 366 on turn 325. Run 5 had the same Mapuche war from turn 290 and lost two cities, so this is a recurring pattern, not bad luck (issue 147 is looking at why).
5. **Science.** Rank 4 of 4 (turn 124) with 29% of the leader's output.

## Comparison with Runs 4 and 5 and CivBench

Run 4 (final 596 on turn 325) lost the capital to Khmer on turn 230 and ended with a Sumerian culture victory. Run 5 (772 on turn 314) lost one city (Palenque, to loyalty, turn 133) and no other until turn 296, and ended with a winning team of 4 (the Mapuche) while we held 7 cities. Run 6 is the only one that lost the capital before turn 200.

CivBench medians (Ground Control, 8 players, Prince; from `docs/civbench-baselines.md`) against our runs:

| Turn | LLM median | Built-in AI mean | Run 4 | Run 5 | Run 6 |
|---:|---:|---:|---:|---:|---:|
| 100 | 239 | 254 | 145 | 179 | 158 |
| 150 | 369 | 417 | 267 | 321 | 199 |
| 200 | 492 | 585 | 401 | 482 | 256 |
| 250 | 659 | 770 | 465 | 644 | 334 |

- Run 5 at turn 250 (644) was 84% of the built-in AI mean and 98% of the frontier-LLM median. Run 6 was 43% and 51%.
- We are below both baselines at turn 100 in every run (145 to 179 against 239 to 254). Early game is a gap even before any war.
- The scenario differs (8 players on Standard, ours is 6 on Small), so these are reference levels, not pass marks. At the default winner ratio of 0.64 for a frontier model, scoring about 0.6 of the winner would be par; we cannot compute our ratio for Run 6 without Germany's score.

## Levers, ranked by likely score gain

The estimate for each is the gap to Run 5's trajectory, which is the best of our own runs, not a promise.

1. **Keep the capital and the second city through turn 200.** Target: no city lost before turn 200 and score at least 480 on turn 200 (Run 5 had 482; Run 6 had 256). Likely gain 150 to 250 points by turn 200 and more by the end. This is issue 147's subject; this page only sets the measurable target. A candidate step: scale the defender count to the rival's military strength, which `get_diplomacy` already returns.
2. **More cities earlier.** Run 5 founded 7 cities by turn 145; Run 6 reached 4 at its best. Target: 6 cities alive by turn 150, 8 by 200. Likely gain 100 to 200 points by turn 250 (each city is worth its own population plus buildings). Constraint: this competes with lever 1 for defenders.
3. **Log rival scores and per-component data (prerequisite).** Without it every other estimate here stays a guess. Target: a rival score, city count and population in the turn log every turn, and a `score_vs_leader` line in the end-of-run summary. See "Missing data". Zero game calls.
4. **Raise science from last place.** We rank 3rd to 4th at 25 to 40% of the leader's output in every run. Target: science per turn at least half the leader's by turn 150 and a rank no worse than median by turn 200. Gain is mostly techs and the era score; modest by itself (see the techs row), so it ranks below cities.
5. **Use the great people we make.** Run 5 had 21 stacked, with the fix landing late (issue 126); Run 6 had one person seen. Target: at least 60% of great people seen are activated (the standing check already says 50%), and at least 4 activated by turn 250. Gain small in points per person but compounding through yields.
6. **Finish wonders.** 5 started in Run 6 and none logged as finished. Target: log completions; at least 3 completed by turn 250. A wonder is worth a flat few points and is lost with its city, so it follows lever 1.
7. **Spend idle faith and gold.** 360 to 1,101 faith idle across runs (`docs/policy-effectiveness.md`); gold unspent on 76 to 85% of turns over its reserve. Target: idle faith under 250 and gold left unspent on under half the eligible turns. Small direct score, some indirect.

## Missing data and the existing read that would log it

No new game call is needed for any of these.

| Wanted | Existing read | Where |
|---|---|---|
| Every rival's score, techs, science, culture, military strength, tourism | `get_victory_progress` (`progress.players`: `score`, `techs_researched`, `science_yield`, `culture_yield`, `military_strength`, `tourism`), already called by the victory monitor every 5 turns (period `VICTORY_PERIOD`) and the diplomacy policy; `Rival.score` etc. are already filled by `_with_rankings` in `state/reader.py` | log one compact line per read in the turn record |
| Our cities and population per turn | `get_game_overview` (every turn: `Empire.num_cities`, `Empire.population`) | add both to the turn record's header |
| Our techs, era score, tourism | the same overview (`era_score`, `tourism`); tech count from `get_victory_progress` for us | same line |
| City-level yields and wonders held | `get_cities`, called every turn by `monitors` (875 calls in the Run 6 session 2 log) | log city count and the wonders each city holds only when they change |

With these, `scripts/score_run.py` and `scripts/compare_runs.py` could print the score against each rival per turn and break the gap into parts.

## Suggested follow-up issues

(Not created.)

1. **Log rival scores and our cities and population in the turn log.** Per-turn `rivals: {id: score, techs, science, culture, military}` from the existing victory-progress read, plus `cities` and `population` from the overview. Target: every turn log has them.
2. **Add a score-vs-leader report to `compare_runs.py` and the end-of-run summary.** Target: print our score, the leader's, and the ratio at turns 100, 150, 200, 250 and the end, against the CivBench medians.
3. **Keep the capital through turn 200 (links issue 147).** Target: zero cities lost before turn 200 and score at least 480 on turn 200 in the standard game.
4. **Expansion target: 6 cities alive by turn 150.** Tune the settling pace against defender count; measure with the new city count.
5. **Low early score against CivBench (145 to 179 against 239 to 254 on turn 100).** Find what the frontier-model agents did in the first 100 turns that we don't, before any war.
6. **Science rank: stay at least at the median.** A research and campus pass; measure with the `SCOREBOARD` rank and the rival science figures.
7. **Count great-person activations and wonder completions in the bot log.** The policy-effectiveness check is missing both.
8. **Resolve Run 6's city count.** The founded and lost events imply 1 city at the end where the game reported 2; log capture or a missing event.
