# The turn-100 score gap

Research for issue 181. At turn 100 every full run scored 114 to 179 against CivBench's 239 (frontier model) and 254 (built-in AI). This page reads the turn, event, settle and bot logs of finished runs to see where the first 100 turns went. It uses logs only: no game and no model calls. All runs are one start (STD_T1_01, Maya, same map and seed), so every number is one start's evidence.

Run names follow `docs/score-history.csv`. "Slices" are the 50-turn experiments of 16 and 17 November (`docs/live-checks.md`, "Run 15 short slices"). They ran on newer code that is not yet on main. The turn 52 and turn 71 slice sessions resume from checkpoints of earlier slices. Tables list runs oldest first, as in `docs/score-history.csv`; the recency table below says which to trust.

## How to read the runs (recency)

The bot changed a lot between Run 8 and Run 14, so an old run says less about where it stands now. Weight the evidence like this:

| Tier | Runs | Code state | Use |
|---|---|---|---|
| Current | Run 14 (15 Nov), Runs 12 and 13 (14 to 15 Nov), and the slices of 16 and 17 Nov | Run 12 and later have the settler escort rules (PRs 211, 216), the safe-site gate (209) and the survey as an escorted settler (220). Run 14 also has the loyalty fixes of PRs 221, 222 and 228. The slices run newer code not yet on main | Where the bot stands now |
| Older | Runs 10 and 11 (14 Nov) | Before the escort and safe-site changes (PRs 209 to 211) | Trend only |
| Historical | Runs 7 to 9 (12 to 14 Nov) | Before loyalty-aware settling, defence and the faith and gold outlets (PRs 190, 191, 200) | Context; do not rank fixes on them alone |

Each finding below names the runs it rests on and whether the current tier still shows it.

## Short answer

1. **The gap is there by turn 50 and the full runs have not closed it.** Turn-50 score was 45, 55 and 44 in Runs 12, 13 and 14 (49 to 56 in Runs 7 to 11) against CivBench's 98 (LLM) and 86 (AI). Cities at turn 100 were 4 (Run 12) and 3 (Runs 13 and 14) against 4.0 and 4.6. Still shown by the newest full run (Run 14). The slices reached 71 to 79 on turn 50, so the newer code does move it.
2. **The second city is late, but the cause changed.** Runs 8 to 11 lost the first settler to a camp north of the capital on turn 20 and founded on turn 38 to 57 (historical and older tier). In Runs 12 to 14 the second city came on turn 57, 49 and 76 with the settler waiting 22 to 26 turns (hold, flee, no_site, escort_gather). Two settlers were still killed before turn 100 (Run 12 turn 40, Run 14 turn 26), but none on turn 20: that kill is not shown by any run after Run 11. Waiting and refused sites are the current cause. The slices founded on turn 21.
3. **Science buildings are late, though less than first read.** Observatory on turn 53 and Library on turn 68 in Run 14; Observatory on turn 79 in Run 12; Library on turn 83 in Run 13 (current tier). Runs 8 to 11 had no campus by turn 100. We completed 9 to 13 techs by turn 100 (Runs 7 to 13) against 26 (LLM) and 21 (AI); the slices had 9 techs by turn 50. Still shown, in a milder form than the older runs suggested.
4. **Military takes a large share of production, mostly in older runs.** 29 to 44% of city-turns in Runs 7, 10, 11 and 12; 18% in Run 13 (turns 29 to 100); 34% in the 50-turn slice that scored well. The high figures come from the older tier, so it is the weakest lever.
5. **Newer code closes most of the gap, but only in slices.** Slices resumed from the turn 52 and 71 checkpoints reached 203, 208 and 213 on turn 100 with 4 cities, above the 180 to 186 mean of the rivals met. That is a stitched path (two sessions), so only a full run can confirm it.

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
| Run 14 | 53 | 68 | | |
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

Ordered by expected effect on turn-100 score, resting first on the current tier. Targets are meant to be read off the next full run's logs.

| # | Change | Rests on | Still shown by newer runs? | Target |
|---|---|---|---|---|
| 1 | **Merge the newer early-game code and confirm it in one full 100-turn run** (attack ratio 1.0, no builder before the first settler, camp memory and clearing, threat-map settling) | Run 14 (T50 44, T100 121, second city 76) against slices (T50 71 to 79, second city 21, T100 203 to 213 stitched) | Yes: the newest full run is still at the old level | Score at turn 100 of 200 or more; stretch 239 (CivBench LLM); at or above the met rivals' mean |
| 2 | **Cap settler waiting**: after 3 turns of `hold`, `flee` or `no_site`, go on a safe route alone or found at the best acceptable tile; let the settler go when the escort cannot stack | Runs 12 to 14: 22 to 26 waiting turns, second city on turn 57, 49 and 76; Run 14 had a long `no_site` stretch around turns 65 to 75 | Yes, in every run since Run 12. Replaces the old fix 1 (settler killed turn 20, Runs 8 to 11 only) | Second city by turn 25, third by 45, at most 5 waiting turns before turn 100, 4 cities by turn 75 |
| 3 | **Science buildings by turn 35 and 55**: Observatory first, then Library, ahead of walls and the Commercial Hub | Run 14 (Observatory 53, Library 68), Run 12 (Observatory 79), Run 13 (Library 83); Runs 8 to 11 had none | Yes, but milder than first read: Run 14's Library came before turn 83. The slices had the Observatory on turn 29 and the Library on turn 50, so it is reachable | Campus by turn 35, library by turn 55, 15 or more techs by turn 100 (now 9 to 13), science 20 a turn or more on turn 100 |
| 4 | **Earn era points on purpose**: camps (2 to 3 each), natural wonders, pantheon, meeting civs, first government | Slices only (era score is logged since 16 Nov): Dark Age from turn 41, era score 35 to 38 against 56 for a Golden Age on turn 100 | Not measured in full runs | Ancient era score of 9 or more, Classical score above the Golden Age threshold |
| 5 | **Trim military to a mission or a known threat** (low confidence: the slice that scored well also spent 34%) | Runs 7, 10, 11, 12 (29 to 44%) | Partly: Run 13 was at 18%; the high figures are from the older tier | At most 25% of city-turns before turn 100, at most 2 military units lost before turn 50 |
| 6 | **Keep the first settler alive** (clear the camp north of the capital before the settler leaves) | Runs 8 to 11: killed turn 20, escort Slinger on turn 19 | No kill on turn 20 since Run 11; later kills (Run 12 turn 40, Run 14 turn 26) need their own look. PRs 211 and 216 and the slices' camp clearing address it | No settler killed before turn 100 |
| 7 | **Earlier government**: Political Philosophy by turn 40 | Runs 11 and 12: Classical Republic on turn 63 to 64 | Not checked in Runs 13 and 14. Unquantified; small before turn 100 | New government by turn 50 |

What changed from the first ranking: the turn-20 settler kill (old fix 1) drops to 6 because only Runs 8 to 11 show it; settler waiting (old 4) rises to 2 because Runs 12 to 14 all show it; merge-and-confirm (old 2) is first because the newest full run still sits at the old level; science buildings stay at 3 but the "no library before turn 83" claim is corrected by Run 14.

Merged PRs that changed early-game behaviour between runs (not exhaustive): 209 to 211 (safe-site gate, escorts; after Run 11), 216 and 220 (escort and survey; Runs 12 to 13), 221, 222 and 228 (loyalty tests; Runs 13 to 14), 229 and 230 (stall and unseen-rival fixes; after Run 14).

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
