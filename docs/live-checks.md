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
2. **AI turn hang, turn 39.** After `end_turn`, the turn stayed on 39 for over 15 minutes, with only the barbarian player still marked as taking its turn. No blocker, diplomacy session, trade deal or popup was open, and the tuner still answered. This is the `HANG:` case from `docs/blockers.md`, whose recovery restarts the game, so it was not attempted. The bot's own code had nothing in flight. It is not known whether the barbarians' capture of our warrior on this turn is related.

### Side findings in civ6-mcp at the pin

- `GameState.check_game_over` can never succeed here. Its InGame query fails with a nil comparison in Lua (while we have no city), and the fallback calls `build_gameover_check_gamecore`, which doesn't exist at the pin. It returns `None`, so game-over detection during a hang is blind.

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
