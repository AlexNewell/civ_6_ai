# The turn-100 score gap

Research for issue 181. At turn 100 every full run scored 114 to 179 against CivBench's 239 (frontier model) and 254 (built-in AI). This page reads the turn, event, settle and bot logs of finished runs to see where the first 100 turns went. It uses logs only: no game and no model calls. All runs are one start (STD_T1_01, Maya, same map and seed), so every number is one start's evidence.

Run names follow `docs/score-history.csv`. "Slices" are the 50-turn experiments of 16 and 17 November (`docs/live-checks.md`, "Run 15 short slices"). They ran on newer code that is not yet on main. The turn 52 and turn 71 slice sessions resume from checkpoints of earlier slices.

## Short answer

1. **Half the gap exists by turn 50, and it is not the city count.** Our score at turn 50 was 44 to 57 in Runs 7 to 14 against CivBench's 98 (LLM) and 86 (AI). Cities were 1 to 2 at turn 50 and 3 to 5 at turn 100 (Runs 11 to 13) against CivBench's 2.0 and 4.0 (LLM) and 2.4 and 4.6 (AI). The count is about par. What lags is what each city does.
2. **The first settler dies to barbarians on turn 20.** A barbarian camp stands 2 to 3 tiles north of the capital on this map. In Runs 8, 9, 10 and 11 the first settler (finished turn 15) was killed on turn 20, with its escort Slinger killed on turn 19. The second city then came on turn 38 to 57 instead of turn 21.
3. **Science buildings come late.** The first campus district (Observatory) finished on turn 79 in Run 12 and not at all by turn 100 in Runs 8 to 11 (Run 7 had one on turn 32, and Run 13 one on turn 27 in an early session that was then stopped, then the next on turn 77). No library before turn 83 in any run. We completed 9 to 13 techs by turn 100 against CivBench's 26 (LLM) and 21 (AI). Our science was about 5 a turn at turn 50 and 10 at turns 70 to 85, against 28 (LLM) and 48 (AI) at turn 100.
4. **A third of early production went to soldiers.** Military was 29 to 44% of city-turns before turn 100 in Runs 7, 10, 11 and 12 (25% in Run 8). Districts and buildings together were 11 to 25%. Many soldiers died: 1 to 3 Slingers were killed before turn 50 in nearly every run.
5. **Newer code already closes most of the gap.** Slices that founded the second city on turn 21 had 71 to 79 points on turn 50 and 3 cities by turn 42 to 49. Resumed from a turn 52 checkpoint, three slices reached 203, 208 and 213 on turn 100 with 4 cities and 20 to 21 citizens, above the 180 to 186 mean of the rivals met. That is a stitched path (the first 50 and the last 50 turns were different sessions), so only a full run can confirm it.

## Score and cities by turn

Source: turn logs (`Score:` line, `score` and `cities` fields) and `city_gained` events.

| Run | T50 | T75 | T100 | Cities founded (turn) | Second city |
|---|---:|---:|---:|---|---:|
| Run 7 | 56 | 109 | 172 | 2, 30, 56, 72, 91 (a city lost turn 60) | 30 |
| Run 8 | 49 | 105 | 139 | 2, 47, 83 | 47 |
| Run 9 | 51 | 102 | 153 | 2, 38, 58, 84, 100 | 38 |
| Run 10 | 51 | 90 | 127 | 2, 38, 72, 79 | 38 |
| Run 11 | 49 | 96 | 132 | 2, 44, 56, 72, 87 (a city lost turn 76) | 44 |
| Run 12 | 45 | 79 | 114 | 2, 57, 75, 84 (a city lost turn 88) | 57 |
| Run 13 | 55 | 97 | 147 | 2, 49, 67 | 49 |
| Run 14 | 44 | 69 | 121 | 2, 76, 89 | 76 |
| Slices from 16 Nov 21:00 (UTC) | 71 to 79 | | | 2, 21, then 42 or 49 | 21 |
| Slices resumed at turn 71 | | 112 | 203 to 213 | 4 cities at turn 100 | |
| CivBench LLM | 98 | | 239 | 2.0 at T50, 4.0 at T100 | |
| CivBench AI | 86 | | 254 | 2.4 at T50, 4.6 at T100 | |

A city "founded turn 2" is the capital (the log records it the turn after founding). Runs 4 to 6 scored 145, 179 and 158 on turn 100 (`docs/research/strength-run6.md`). The median of Runs 4 to 14 is 145.

The rivals we met averaged 205 to 280 on turn 100 in Runs 11 to 14, close to CivBench's AI mean, so the standard is fair. Our score ran at 40 to 49% of the leading met rival (`docs/score-history.csv`, `share_100`).

## What the first 100 turns spent

### Settling (settle logs)

- The first settler is finished on turn 15 in every run. That part works.
- Settler turns spent waiting (a `hold`, `flee`, `no_site` or `escort_gather` line) before turn 100: Run 8 17, Run 9 17, Run 10 8, Run 11 3, Run 12 22, Run 13 25, Run 14 26. The newer slice that founded on turn 21 spent 0 and walked 3 turns.
- Typical waits: "route is threatened, so the escort joins first" with `STACKING_CONFLICT` (the escort cannot stack on the settler's tile) for 3 to 4 turns; repeated flee-and-retreat cycles on turns 30 to 40 in Run 12; and `no_site` for about 10 turns around turns 65 to 75 in Run 14 (settler standing still while sites were refused).
- Settlers killed before turn 100: Run 8 three (turns 20, 53, 82), Runs 9, 10 and 11 one each (turn 20), Run 12 one (turn 40), Run 14 one (turn 26). Each loss costs the settler's production plus the delay.

### Production split (production events, turns 1 to 100)

Share of city-turns by what the city was building, from the interval between `production_set` events. A rough measure (it ignores overflow and purchases).

| Run | Military | Settlers | Builders | Districts | Buildings |
|---|---:|---:|---:|---:|---:|
| Run 7 | 44% | 18% | 13% | 5% | 13% |
| Run 8 | 25% | 42% | 12% | 6% | 11% |
| Run 10 | 29% | 26% | 9% | 18% | 13% |
| Run 11 | 37% | 26% | 16% | 11% | 5% |
| Run 12 | 35% | 29% | 13% | 9% | 8% |
| Run 13 (turns 29 to 100) | 18% | 13% | 16% | 17% | 29% |
| Slice, found turn 21 (turns 1 to 50) | 34% | 22% | 8% | 8% | 23% |
| Slice resumed at turn 71 (turns 71 to 100) | 23% | 5% | 14% | 30% | 27% |

The opening is Slinger, Scout, Settler (turns 1 to 15), then Warriors and Slingers while the settler is held. Run 11's capital built five military units between its first and second settler and a Builder only on turn 51.

### Research and civics (bot logs, "Started" lines)

- Tech order in Runs 11 and 12: Mining, Pottery, Writing, Animal Husbandry or Masonry, Archery, Bronze Working, Currency (turn 41 to 84). A tech took 6 to 7 turns on average.
- Civic order: Craftsmanship (turn 11), Foreign Trade (21), Early Empire (31 to 32), State Workforce (41 to 46), Political Philosophy (51 to 56), then Mysticism and Drama and Poetry. The first change of government, to Classical Republic, came on turn 63 to 64.
- Completed by turn 100 (`Research complete` and `Civic complete` events): techs 9 to 13 and civics 5 to 9 in Runs 7 to 13, against CivBench techs of 26 (LLM) and 21 (AI). CivBench's count may include techs the game gives for free, so read the direction, not the exact gap.
- The slice that founded on turn 21 had 9 techs and 6 civics by turn 50 against 2 to 6 techs and 2 to 4 civics for Runs 8 to 12.
- Science per turn (`SCOREBOARD` notices): 4.6 at turn 49 (Run 11), 5.1 at turn 49 (Run 10), 10.3 at turn 69 (Run 9), 9.8 at turn 84 (Run 10), against 16.8 at turn 79 in a newer slice (rank 2 of 4, leader 41.5).

### Buildings and the first districts

First finished turn by turn 100, from `building_completed` and `district_completed` events:

| Run | Observatory (campus) | Library | Commercial hub | Market |
|---|---:|---:|---:|---:|
| Run 7 | 32 | none | 93 | |
| Run 8 | none | none | 88 | |
| Run 10 | none | none | 86 | 94 |
| Run 11 | none | none | 72 | 92 |
| Run 12 | 79 | none | | |
| Run 13 | 27 (next 77) | 83 | 96 | 89 |
| Slice, found turn 21 | 29 | 50 | | |

Runs 8 to 12 built walls (turn 40 to 89), most of them before any science building. Run 7 had its campus early.

### Refusals and idle time

- Refused production orders by turn 100: 0 to 3 per run (Runs 7 to 11). Not a lever.
- "Finished building X. Now: nothing" appears 12 to 26 times by turn 100, but the log does not show how long each city stayed idle; the production policy normally sets the next item the same turn. Not measured, so not ranked.
- Decision time was 8.5 to 8.7 s a turn up to turn 100 (`docs/live-checks.md`), so speed is not what limits the early game.

## Where the points come from

The score formula is not in this repo. A fit of score against population, cities, techs, civics, districts and buildings over Runs 11 and 12 and two slices (300 turns) gave unstable weights, so no component split is claimed. Two direct readings:

- Single-turn jumps of 19 to 21 points before turn 100 coincide with the Oracle wonder (Run 7 turn 87: +19; a newer slice turn 94 and 95: +11 and +21). Run 8 had +20 on turn 66 with no event in the log. One wonder is worth about as much as a city's growth over 20 turns.
- Era score is logged only from 16 November. In the newer slices it stood at 29 in the Classical Era on turn 75 and 35 to 38 on turn 100, against a Golden Age threshold of 56 and a Dark Age threshold of 43. In every slice the Ancient era ended below 9, a Dark Age from turn 41 (`docs/live-checks.md`).

## Ranked fixes

The order is by expected effect on turn-100 score. Each target is meant to be read off the next full run's logs.

| # | Change | Why (source) | Target |
|---|---|---|---|
| 1 | **Keep the first settler alive and founding by turn 21**: deal with the camp north of the capital before the settler leaves, and let the settler go when the escort cannot stack | Runs 8 to 11: settler killed turn 20, second city turn 38 to 57. Newer slices: second city turn 21, score 71 to 79 against 44 to 55 at turn 50 | Second city by turn 25, third by 45, no settler killed before turn 100, score at turn 50 of 75 or more |
| 2 | **Merge the newer early-game code and confirm it in one full run** (attack ratio 1.0, no builder before the first settler, camp memory and clearing, threat-map settling) | The 203 to 213 at turn 100 comes from stitched slices, not one game | Score at turn 100 of 200 or more; stretch 239 (the CivBench LLM median); at or above the met rivals' mean |
| 3 | **Science buildings by turn 35 and 55**: Observatory first, then Library, ahead of walls and the Commercial Hub | No campus by turn 100 in Runs 8 to 11 and turn 79 in Run 12; no library before turn 83; science 5 to 10 a turn against 28 to 48 | Campus by turn 35, library by turn 55, 15 or more techs by turn 100 (now 9 to 13), science 20 a turn or more on turn 100 |
| 4 | **Cap settler waiting**: after 3 turns of `hold`, `flee` or `no_site`, go on a safe route alone or found at the best acceptable tile | Runs 12 to 14: 22 to 26 waiting turns | At most 5 waiting turns before turn 100, 4 cities by turn 75 (Runs 11 to 13 had 3 to 4) |
| 5 | **Trim military to a mission or a known threat**: lower confidence, since the early slice that scored well also spent 34% | 29 to 44% of city-turns in Runs 7, 10, 11, 12; 1 to 3 Slingers lost before turn 50 | At most 25% of city-turns before turn 100, at most 2 military units lost before turn 50 |
| 6 | **Earn era points on purpose**: camps (2 to 3 each), natural wonders, pantheon, meeting civs, first government | Dark Age from turn 41 in every slice; era score 35 to 38 against 56 for a Golden Age on turn 100 | Ancient era score of 9 or more, Classical score above the Golden Age threshold |
| 7 | **Earlier government**: reach Political Philosophy by turn 40 | Classical Republic on turn 63 to 64, with Early Empire and State Workforce researched first | New government by turn 50. Unquantified; the slot gain is small before turn 100 |

Items 1 to 3 are most of what separates the 114 to 179 runs from the 203 to 213 slices. Items 4 to 7 are smaller and not yet separated from them.

## What this does not show

- One start only. The camp north of the capital belongs to this map; another start may not have it. Issue E.5 (first-city timing) covers more starts.
- Each slice is one deterministic game (the baseline repeated exactly, `docs/live-checks.md`), not a repeat run, and the newer code changed several things at once. Which change moved the second city from turn 32 to 35 to turn 21 is not isolated.
- The turn log holds no per-city yield, per-component score or wonder-completion record, so each fix's share of the points is an estimate, not a measurement.
- The CivBench scenarios differ from ours (Standard map, 8 players for Ground Control), so its numbers are reference levels.

## Follow-ups worth an issue

- Settler escort: when the escort cannot stack on the settler's tile, move the settler first (`STACKING_CONFLICT` held it 3 to 4 turns in Runs 11 and 12).
- Science-first opening: Writing and Observatory in the capital ahead of walls and spare military.
- A 100-turn slice from the start, not a stitched one, as the gate for fixes 1 to 3 (`scripts/slice_report.py`).
- Log per-city yields, wonders held and era score at turns 25, 50, 75 and 100.
