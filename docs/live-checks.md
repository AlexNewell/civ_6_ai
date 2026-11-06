# Live checks

What was verified against a running Civ VI game, and what wasn't reached. Newest run first.

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
- What was trimmed: reads that several handlers repeat in a turn (`get_game_overview`, `get_cities`, `get_empire_resources`, `get_diplomacy`, `get_victory_progress`, `get_threat_scan`) are now answered from memory by `SharedReads` until any other call changes the game, and `end_turn` always clears it.

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
