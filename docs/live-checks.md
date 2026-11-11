# Live checks

What was verified against a running Civ VI game, and what wasn't reached. Newest run first. Run 2 updates the status of earlier checks it reached.

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

## Run 4: turns 1 to 325, with the speed and diplomacy fixes (issues 15, 17 and 117)

- **Game:** standard save `STD_T1_01`, played over many sessions and reloads. No model calls, $0.00.
- **Result:** the game ended on turn 325 with "Culture Victory: Sumerian Empire defeats Mayan Empire", as the owner read it from the defeat screen. It doesn't count toward gate G1, because it needed hands-on help (below). Run 3's recorded "Germany won a science victory" is not verified by any log, and the owner has set that conclusion aside.
- **Hangs, each needing a reload or a restart:** turn 105 (after a trade offer the AI accepted with extra terms: Joint War, Open Borders and 30 favor; trade offers are now off in `run_game.py` until the terms are checked); turn 221 (the game held Sumeria's diplomacy screen open with no session behind it, closed by `OnClose` in the view's own Lua state, PR 118); turn 256 (the "Welcome to the World Congress" screen; the watcher logged nothing for eight minutes, cause unknown, issue 117).
- **Speed (decision time, mean):** 40 s on turns 1 to 105; 11.8 s on turns 104 to 221 with the popup scan narrowed and the call waits shortened (PR 116); 14.9 s on turns 262 to 318 with diplomacy answered during the end-turn wait (PR 121). Turns held by a diplomacy screen cost 68 s of end-turn wait against 6.6 s for the rest, before PR 121.
- **War:** Khmer attacked from turn 223 and took Wak Kab'nal on turn 230. Peace was accepted on turn 256. The war tracker lost the declaration twice, because a restart drops its baseline ("start unknown") and because a hang loses the end-turn report that names the declarer. The flag also read "no war" on turns 234 to 248 with no peace deal logged, which is unexplained.
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
