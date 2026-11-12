# Policy effectiveness: does each policy achieve its purpose?

Research for issue 127. A handler that "runs without error" can still do nothing useful, and the logs then look healthy. The great-person pile-up in Run 5 (21 stacked in one city, 583 "no district to activate" waits) sat in plain sight for three runs for that reason.

This note lists the flows each policy is meant to carry out, where the two ends of each flow are recorded, and what a measure of each says about Runs 3, 4 and 5. The measures are code: [`src/civ6_ai/effectiveness.py`](../src/civ6_ai/effectiveness.py), run with [`scripts/policy_effectiveness.py`](../scripts/policy_effectiveness.py). It reads the turn log, bot log, event log and settle log that a run already writes, and makes no game or model calls (no spend).

```
policy_effectiveness.py [--label NAME] [--json] LOG [LOG ...]      # LOG = the session turn logs of one run
```

It exits 1 when any check fails, so it can gate a run later. A check is "no data" when a run's logs can't support it, never zero.

## How the runs were grouped

| Label | Sessions | Notes |
|---|---|---|
| Run 3 | the `from_fullgame` sessions, turns 1-317 | Turn logs have no per-call counts, so call-based checks are "no data". Sessions overlap by a few turns where a game was resumed (324 records for 312 turns). |
| Run 4 | the first session plus the `from_run4_popups` and `from_run4_fastdiplo` sessions, turns 1-324 | Has per-call counts. No `great_people` lines yet. |
| Run 5 sessions 1-2 | turns 1-264 | Before the great-person fix (#126). |
| Run 5 session 3 | turns 265-315 | After the fix, to game over. |

The live run in progress when this was written is not included.

## Flow inventory and data sources

"Created" is what the policy produces or is offered, "used" is what it turned into the outcome it exists for. "Source" is where each end is recorded today.

| Policy | Created | Used | Source today | Gap |
|---|---|---|---|---|
| Great people | people on the map (`great_people turn N: K on the map` lines) | `activate_great_person` calls minus `can't activate at` refusals | bot log; turn log `handler_calls` | No activation success line, and no "wasted" count (killed or idle for N turns). Run 4 and older logs have no `great_people` lines. |
| Blockers | turns the game held | holds with a handler | bot log (`No handler for blocker ...`) | None: names the blocker. |
| Faith | turns above the reserve | faith purchases (`Faith bought` lines, `patronize_great_person` calls) | bot log; calls | Only idle turns log the balance; spent faith has no total. |
| Gold | turns the handler got past its reserve gate (from its calls) | turns with a purchase or upgrade call | turn log `handler_calls` | No gold-spent total, and no balance per turn. |
| Production | choices tried | choices the game accepted (`builds` vs `can't build` lines) | bot log | Items finished are read from the `finished building` notices in the turn detail. |
| Builders | improve jobs tried | jobs the game accepted (`builder_decision`) | bot log | Tiles wanted vs improved is not recorded. |
| Military | combat units built (completion notices) | `attack` and `city_strike` decisions not logged as refused | turn detail; bot log | Units that fought is only attacks by decision; no per-unit record. Units lost come from "was killed" notices. |
| Settling | settlers built | `found_city` calls; `settle_decision` outcomes; `city_lost` events | turn detail; calls; settle log; event log | None needed. |
| Research and civics | tech and civic completions | completions that arrive with an empty queue | turn detail | Boosts available vs taken is not recorded. |
| Government | slot assignments logged | distinct slot and policy pairs | bot log | A policy set again each turn shows as churn. Governor titles spent is only visible as warnings. |
| Diplomacy | deals offered | deals accepted (`Deal from player N accepted/refused`) | bot log | Sessions the game opened vs answered is not counted. Unanswered sessions are only logged when the bot gives up. |
| Trade | `make_trade_route` calls | calls not followed by a `Trade route to X refused` line | bot log; calls | Routes that earned anything are not measured. |
| Envoys | `send_envoy` calls | not measurable | calls | Envoy tokens available are not read, so "sent vs unspent" has no second number. |
| Religion | pantheon and religion founding status polls | founding events | bot log | No religion was founded in these runs; there is no check yet. |
| Handlers | turns | turns with no handler error | turn log | None. |
| Run | stops | stops that were not game over | turn log | None. |

## Backtest on Runs 3, 4 and 5

Each cell is the measure's figure; F means the check failed. For ratio checks the figure is used/created. For share checks it is bad/total.

| Check | Run 3 | Run 4 | Run 5 s1-2 | Run 5 s3 |
|---|---|---|---|---|
| Great people seen vs activated | no data | no data | 0 of 21 F | 23 of 25 |
| Great people "no district" waits | 0 | 0 | 583 F | 0 |
| Most great people waiting at once | no data | no data | 21 F | 21 F |
| Activations refused by the game | 0 | 0 | 0 | 79 F |
| Turns held by an unhandled blocker | 66 F | 48 F | 57 F | 12 F |
| Faith: turns spent vs idle | no data | 5 of 258 F | 5 of 195 F | 2 of 51 F |
| Faith: most idle above reserve | no data | 919 F | 360 F | 1101 F |
| Gold: turns above reserve, nothing bought | no data | 194 of 252 F | 138 of 181 F | 39 of 46 F |
| Upgrade logged as refused when it succeeded | 0 | 21 F | 11 F | 7 F |
| Production choices refused | 971 of 1127 F | 491 of 693 F | 629 of 799 F | 260 of 305 F |
| Most repeats of one refused build | 59 F | 50 F | 37 F | 17 |
| Builder actions the game refused | 59 of 305 F | 122 of 740 F | 108 of 579 F | 23 of 94 F |
| Builder-turns idle | 1042 of 1288 F | 498 of 1116 | 450 of 921 | 164 of 235 F |
| Military moves refused | 73 of 140 F | 138 of 328 F | 115 of 232 F | 30 of 92 F |
| Attacks landed per combat unit built (units from notices) | 28 attacks, 13 units | 69 attacks, 12 units | 23 attacks, 13 units | 18 attacks, 0 units |
| Settling decisions with no site | 147 of 202 F | 21 of 126 | 11 of 57 | 0 of 10 |
| Cities lost | 3 F | 4 F | 1 F | 2 F |
| Slot/policy pair set again (times each) | 35.8 F | 36.6 F | 18.1 F | 34.8 F |
| Governor "left something unfinished" warnings | 297 F | 220 F | 123 F | 45 F |
| Governor actions that kept the point | 291 F | 4 | 2 | 1 |
| Deals accepted of offered | 18 of 33 | 18 of 51 | 15 of 36 | 11 of 15 |
| Route calls that worked | no data | 19 of 23 | 8 of 8 | 5 of 5 |
| Turns with a handler error | 16 of 312 F | 32 of 322 F | 21 of 264 F | 6 of 50 F |
| Starving notices | 111 F | 32 F | 24 | 61 F |

Did each measure flag the failures found by hand?

| Past failure | Measure | Verdict |
|---|---|---|
| Great-person pile-up (21 stacked, engineers sent where they can't be used) | seen vs activated (0 of 21), "no district" waits (583), most waiting (21) | Flagged in Run 5. Missed in Runs 3 and 4 because those logs had no `great_people` lines; the line exists now. |
| Great-person claim screens never answered | unhandled blocker `ENDTURN_BLOCKING_CLAIM_GREAT_PERSON` | Flagged in all three runs (66, 48, 57 holds). This was the earliest signal, and it was visible from Run 3. Nobody read it. |
| 210 unspent faith | faith idle above reserve, and spent vs idle turns | Flagged: 360 (Run 5), 919 (Run 4), 1101 (Run 5 session 3). Faith was bought 5 times in 258 turns. |
| Units idle after refused moves | military moves refused (42-55% of unit decisions) | Flagged in every run. The usual result is `STACKING_CONFLICT`. |
| False "blocked" stops | stops that were not game over | Counted, not judged. The count says how often, not whether the stop was false; that needs the screenshot rule in [triage.md](triage.md). |
| Unanswered diplomacy screens | screens closed unanswered | Missed. The check only sees the sessions the bot itself gave up on (0-1 per run). A held screen shows only in the blocker holds, which don't separate diplomacy from the rest. |
| Trade-offer hang | route calls that worked | Missed. A hang is a stopped run, and it shows only as the `hung` stop. |

### Problems nobody had flagged

- **Production tries the same impossible build every turn.** 70-86% of production choices were refused. One city and item were refused 37-59 times in a run (a harbor with `NO_PLACEMENT` every turn). Each refusal is a game call, about 0.1 s, over 1,100 refusals in Run 5.
- **A policy card is set again every turn.** The same slot and policy pair was logged 18-37 times per pair: for example, `Slot 1 takes POLICY_GOD_KING` on turn after turn. The game either doesn't keep it or the bot doesn't see that it stuck. Run 5 made 572 `set_policies` calls over 315 turns.
- **Governor placement never finishes.** 220-297 `Left something unfinished in governor placement` warnings in Runs 3 and 4, and 291 "kept the point" warnings in Run 3. End-turn holds for the governor-opportunity blocker also appear (34 across the runs).
- **Upgrades are logged as refused when they succeeded.** `Upgrade of ... refused: UPGRADED|...` 21 times in Run 4. A measure that counted refusals would be wrong by that much.
- **Builders keep trying forest and jungle tiles.** 21 failures logged as `tile has FEATURE_FOREST` or `FEATURE_JUNGLE` ("use remove_feature first") in Run 5's first session. The game refused 16-24% of builder actions (moves, improves, repairs), and one tile and improvement repeated 15-23 times.
- **Activations are now refused rather than skipped.** After #126 the great people are activated (23 of 25), but the game refused 79 other activations (`CANNOT_ACTIVATE`, mostly engineers). The pile-up moved from "waiting" to "retrying".
- **Cities were lost every run** (3, 4, 1, 2) and starvation notices ran at 24-111 per run.
- **Combat units built are barely visible.** The end-of-turn notices show 0-13 combat units built per run against 18-69 attacks, so the units-vs-attacks ratio passes and says little until units built are logged properly. Settlers built vs cities founded passed wherever a settle log exists (9 cities for 7 settlers noted in Run 4, 7 for 6 in Run 5 sessions 1-2).

## Weakest policies, ranked

Ranked by how much of the policy's purpose was lost, then by how early the signal was available.

1. **Great people.** The biggest loss. Zero activations in Run 5 before #126, and the claim-screen blocker was unhandled in every run. Since the fix, 79 activations were still refused. Failing: seen vs activated (before the fix), stacking, claim blocker, refused activations.
2. **Production.** 70-86% of choices refused; the same impossible build is retried every turn. This is wasted time and a likely reason the empire builds less than it could.
3. **Government and governors.** The same policy is set again every turn (18-37 times per pair), and governor placement is left unfinished 220-297 times. Policy cards and governor titles may not be doing what the logs claim.
4. **Faith.** 360 to 1,101 faith idle above the reserve, with 5 purchases over 258 turns. The idle note names only the great-person price, and the few buys were great people.
5. **Builders.** 16-24% of builder actions are refused, with forest and jungle tiles retried; 45-81% of builder-turns are idle with no job on offer.
6. **Military.** 42-55% of unit decisions end in a refused move (stacking). Units built are too poorly recorded to say how many fought.
7. **Settling and city health.** Cities were lost every run and starving notices stayed high. Run 3 had no site on 125 of 202 decisions; this is fixed in Runs 4 and 5.
8. **Blockers and handlers.** 5-14% of turns had a handler error, and one blocker type has no handler.
9. **Gold.** Nothing was bought on 76-85% of the turns the handler got past its reserve (138 of 181 in Run 5 sessions 1-2). Buys are counted from the log's success lines, and older logs have none for item purchases, so this is an upper bound. Often nothing was worth buying, and the log's own reason is a short list of costs, so this is the one to read before changing anything.
10. **Research, diplomacy, trade.** These look healthy: routes work 83-100% of the time, and 35-73% of deals offered were accepted. Envoys are only counted.

## Suggested fixes

| # | Fix | Why | Live check |
|---|---|---|---|
| 1 | Handle `ENDTURN_BLOCKING_CLAIM_GREAT_PERSON` (claim the person, then let the policy activate it). | Unhandled in every run, the first signal of the pile-up. | Run to the first great person and confirm no `No handler for blocker` line. |
| 2 | Skip a build the game refused with `NO_PLACEMENT` or `CANNOT_START` for a number of turns, keyed by city and item. | 1,100 refusals a run; 37-59 repeats of one. | Refused/accepted below 30% and no pair above 10 repeats. |
| 3 | Read back a set policy before setting it again, or skip when the slot already holds it. | 18-37 sets of the same pair; each set is a game call. | Each slot/policy pair set at most 3 times on average. |
| 4 | Finish governor placement, or stop warning once the points are spent. | 220-297 unfinished warnings. | Warnings under 20 a run. |
| 5 | Remove the feature before improving a forest or jungle tile, or choose a tile without one. The builder now has a `remove_feature` action; check it covers these tiles. | The improve call says so in its result. | Refused builder actions under 10%. |
| 6 | Log `UPGRADED` as a success. | The refusal count is wrong by up to 21. | No "refused: UPGRADED" line. |
| 7 | Spend idle faith on something besides great people (a missionary, a building) once the cheapest great person is beyond reach. | 360-1,101 faith idle. | Idle faith under 250. |
| 8 | Stop moving a unit onto a tile that holds a same-class unit (check the destination first). | 42-55% of military decisions end in `STACKING_CONFLICT`. | Refused moves under 25%. |
| 9 | Look into why activations are refused (`CANNOT_ACTIVATE`) now that the advisor works. | 79 refusals in 51 turns. | Refused activations under 20. |

## Standing check

**Where it lives.** `scripts/policy_effectiveness.py` is a standalone script on the same pattern as `triage_run.py`. It is cheap enough to call from the end of `run_game.py` next, printing the failing checks beside the existing speed summary. `score_run.py` takes one number per metric for comparing runs, so these ratios don't belong there. Per turn it's not worth running: the checks need counts over many turns, so end of run is the right place. A small per-turn exception is the great-person line (it is already logged).

**Thresholds.** The constants at the top of `effectiveness.py` are a first guess from these three runs. They are set where the runs' failures cross them and are meant to be tightened as the fixes land.

**Gate G1?** Yes for a short list, warn for the rest. A "clean run" should fail on checks where the bot did nothing for a feature or left a screen unhandled:

- an unhandled blocker (any),
- great people seen vs activated below 50%, or more than 5 waiting at once,
- faith idle above 250,
- turns with a handler error above 2%,
- a stop that was not game over.

The others (production refusals, builder idle, policy re-sets, military refusals) should warn only. They are real losses, but a game can still be won with them, and gating on them would block G1 on fixes that haven't been written.

**Cost.** Nothing in the script touches the game. All the data is in the logs the bot already writes. Two measures need new logging, with no new game calls:

- an activation success line next to the refusal line (great-person flow),
- an envoy tokens line from the `get_city_states` read that `wars` already makes.

## Follow-up feature issues proposed

1. Handle the claim-great-person blocker (fix 1) and add the live check.
2. Add a refusal memory to production (fix 2).
3. Stop re-setting policy cards each turn and finish governor placement (fixes 3 and 4).
4. Wire `policy_effectiveness` into the end-of-run summary of `run_game.py`, with the G1 gate list above.
5. Log activation success, wasted great people and envoy tokens (the logging gaps).
6. Builder, faith and military fixes (5, 7 and 8) and the `CANNOT_ACTIVATE` investigation (9).

## Limits of this research

- Units built, settlers built and own units lost come from the end-of-turn notices, which may be truncated, so they are lower bounds.
- Run 3's turn logs have no per-call counts, and Run 4's bot logs have no great-person lines, so those checks are "no data" there rather than a pass.
- A check that passes only says the ratio is within its threshold. It does not say the policy is good (gold spent half the time says nothing about what was bought).
- The thresholds are a first pass and are untested against a clean run, because no run has been clean yet.
