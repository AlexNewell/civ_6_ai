# Speed profile

Where the bot's decision time goes, and what to change. This is part of [issue #52](https://github.com/AlexNewell/civ_6_ai/issues/52). The targets are in [decisions.md](decisions.md#speed): a mean of 60 s per opponent turn while we build, and 10 s as the long-term goal.

The data is the Run 3 logs: 312 turns of one game (turns 1 to 317), played in several sessions, with 10 handlers. The turn log has seconds per handler, but not per game call. Call counts below are therefore **estimates from the code**, checked against the seconds. A per-call timer is added with this change, so the next live run measures them.

## Main finding: time is the number of game calls

Every `GameState` call is one round trip to the game. The connection code waits 0.1 s for stray messages before it sends and 0.2 s after the reply, so each call costs at least 0.3 s whatever it reads.

The Run 3 numbers agree. The research handler makes one call on a quiet turn (`get_tech_civics`), and it took 0.36 s on 235 of 312 turns. Government makes about 9 reads on a quiet turn and took 3.4 s at its median early on. At about 0.35 s per call, the 32 s of handler time is roughly 90 calls a turn. The 10 s goal is about 28 calls, unless calls get cheaper.

So there are two levers: make fewer calls, and make each call cheaper. The second is worth more than anything else here.

## What the logs hold

| File | Holds | Used here |
| --- | --- | --- |
| Turn log (`*.jsonl`) | Per turn: seconds per handler, handler errors, end-turn seconds, total seconds, end-turn text | Yes, all timing |
| Bot log (`*_bot.log`) | Policy log lines | Counts of repeated actions |
| Events log, settle log | Rival warnings, settling decisions | No |

Turns that appear in more than one session were counted once. A turn's decision time is total minus end-turn seconds, as in `speed.py`. It is 34.5 s on average, and the handlers add up to 32.2 s of that. The other 2.3 s is the turn read before the handlers and the checks after the end-turn request, about 6 calls.

## Per handler

Seconds per turn, all 312 turns. Share is of the handler total.

| Handler | Mean | p50 | p90 | Max | Share |
| --- | ---: | ---: | ---: | ---: | ---: |
| blockers | 6.54 | 4.02 | 14.47 | 23.10 | 20.3% |
| government | 5.64 | 5.37 | 9.38 | 12.26 | 17.5% |
| settling | 5.09 | 4.74 | 7.07 | 25.40 | 15.8% |
| city_production | 3.57 | 4.06 | 6.26 | 10.44 | 11.1% |
| builder | 3.24 | 2.08 | 5.41 | 23.74 | 10.1% |
| secondary | 2.94 | 2.41 | 4.40 | 8.05 | 9.1% |
| military | 2.41 | 2.36 | 3.71 | 24.71 | 7.5% |
| monitors | 1.15 | 1.01 | 1.70 | 1.73 | 3.6% |
| diplomacy | 1.01 | 1.00 | 1.01 | 3.20 | 3.2% |
| research | 0.55 | 0.36 | 1.04 | 2.04 | 1.7% |

## Over the game

Mean seconds per turn in each 50-turn band. The logs have no city or unit counts per turn, so the turn number stands in for them.

| Handler | 1-49 | 50-99 | 100-149 | 150-199 | 200-249 | 250-317 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| blockers | 3.3 | 4.4 | 4.2 | 5.7 | 8.6 | 11.4 |
| government | 3.9 | 3.4 | 3.8 | 5.7 | 6.8 | 9.0 |
| settling | 5.3 | 6.7 | 2.9 | 5.4 | 5.1 | 5.0 |
| city_production | 2.5 | 3.7 | 4.2 | 3.1 | 3.9 | 3.9 |
| builder | 1.2 | 2.2 | 6.1 | 5.6 | 2.0 | 2.6 |
| secondary | 2.2 | 2.6 | 2.9 | 2.9 | 3.0 | 3.7 |
| military | 2.7 | 4.2 | 2.6 | 2.1 | 1.5 | 1.6 |
| monitors, diplomacy, research | 2.7 | 2.6 | 2.8 | 2.7 | 2.7 | 2.7 |

By period the decision mean is 28.8 s before turn 100, 34.3 s from 100 to 199, and 39.5 s from turn 200.

What grows, and why:

- **Government and blockers** account for the whole late rise (together +8 s). The bot log shows why. In turns 216 to 305 the line "Appointed GOVERNOR_IBRAHIM ... kept the point" appears 191 times in 89 turns. Both the government handler and the blocker resolver ran the same appointment loop every turn, about 10 calls a turn, and the loop never finished. The fix (`unappointable` in `GovernmentPolicy`) was merged after Run 3. So the late numbers overstate what the current code costs. We expect government to fall back toward its early 3 to 4 s. This needs a live run to confirm.
- **City production** is flat at about 4 s from turn 100. Its log shows 719 "can't build ... no valid tile / MISSING_COORDS" lines in 89 turns (about 8 refused calls a turn, about 3 s). The same wonders and districts are tried again every turn.
- **Builder** peaks at turns 100 to 199, when 16 turns took over 10 s, and is 2 to 3 s once the work runs out.
- **Settling** is flat. 83 turns took under 1 s (no settler or scout), 113 turns took 5 to 7 s (the exploration reads, `get_strategic_map` and `get_global_settle_scan`, plus one `get_map_area` per visible rival city), and 24 turns took over 10 s. The long turns are spread over turns 11 to 180.
- **Military** falls with time because there are fewer fights than in the early expansion.
- **Monitors, diplomacy, research** are flat and small.

The turn log shows no growth by handler that can be tied to city or unit count rather than to the above. The per-call timer will show it directly.

## Estimated calls on a quiet turn

Real calls, counting the `SharedReads` cache as it was before this change. One call is about 0.35 s.

| Handler | Calls | What they are |
| --- | ---: | --- |
| monitors | 4 | overview, diplomacy, cities, threat scan (6 on turns that also check victory and resources) |
| diplomacy | 2 to 3 | sessions, pending deals |
| settling | 1 | `get_units`, then stops if no settler or scout |
| settling, with one | 5 to 15 | adds the exploration reads and one map-area read per visible rival city |
| military | 2 | `get_units`, and `get_notifications` (new since Run 3) |
| research | 1 | `get_tech_civics` |
| government | 9 | policies and governors three times each, tech and civics once |
| city_production, idle city | 10 and up | empire read (5 calls), units, then one list per idle city and each try |
| builder | 1 | `get_units`, then stops if no free builder |
| secondary | 6 | pantheon, religion, great people, units, trade routes |
| blockers | 12 | 5 reads, a unit skip, 5 reads again |

These match the seconds above: research 0.36 s for 1 call, secondary 2.4 s for about 7, government 3.4 s for 9 to 10, blockers 4.0 s for about 12.

## What the new handlers add

These were merged after Run 3 and are not in the data. The estimate is calls per turn, from the code.

| Handler | Quiet turn | Busy turn |
| --- | --- | --- |
| gold | 0 to 5. It read the full empire (5 calls) before checking whether it had anything to spend. | 10 to 25: empire read, then one production list and one tile read per city, up to 3 upgrades, 3 purchases and 2 tile buys. Run 3 banked over 1,300 gold, so spendable turns will be common. |
| wars | 1 (`get_city_states`) | Up to 4 peace offers, each opening a session. |
| trade_offers | 0 if no surplus | About 5 to 7: sessions read, then up to 2 offers, each with a trade call and a session check. Run 3 showed resources at their caps, so a surplus is likely. |
| faith | 1 (`get_units`) | Up to 13: pantheon, great people, religion, up to 4 city production lists, up to 6 purchase tries. |
| military changes | 1 (`get_notifications`) | Up to 10 `get_unit_promotions` reads when a promotion is waiting, plus city strikes within the 12-order cap. |

Together that is about 2 to 3 calls (1 s) more on a quiet turn and 10 to 30 calls (4 to 10 s) more on a busy one, before the changes below. The decision mean would probably move from 34.5 s to about 38 to 42 s.

## Changes, ranked

Savings are estimates in seconds off a mean decision time of about 35 s. Risk is the risk of a wrong game decision or a stall, not of a coding error.

| # | Change | Saves | Risk | Needs live game |
| --- | --- | ---: | --- | --- |
| 1 | Cut the per-call wait in the connection: drain for 0.1 s before and 0.2 s after becomes a few milliseconds, or reads until the end marker only. | 15 to 20 s | Medium. A stray message could be read as the answer to the next call. | Yes |
| 2 | Batch reads into one Lua call (units, cities and overview together). | 5 to 10 s, more with #1 off | Medium. New Lua in the vendored code. | Yes |
| 3 | Settling: keep the rival-city visibility and the exploration scan for a few turns, and read them only when a settler or scout will move. | 2 to 4 s late | Medium. Settling would act on slightly old scans. | Yes |
| 4 | Blocker resolver: after acting, read again only the sources that could have changed, and skip the read of sources the diplomacy handler just cleared. | 1 to 2 s | Medium. A missed blocker stalls the turn. | Yes |
| 5 | Production: remember which items a city could not build, and do not try them again for some turns. | 2 to 3 s late | Low to medium. A city may miss a tile that opens up. | No, but a live run should compare |
| 6 | Share more reads in `SharedReads` (done here). | 3 to 4 s | Low | No |
| 7 | Gold reads the overview first and stops if there is nothing to spend (done here). | 0 to 2 s | Low | No |
| 8 | Run low-value steps every few turns: the trade-route check, research check when nothing finished, monitors. | 1 to 2 s | Low to medium | No |
| 9 | Gold, faith and trade offers: one production list per city per turn shared between production, gold and faith; skip tile reads when the cheapest known tile costs more than the budget. | 2 to 5 s on busy turns | Low to medium | No |

Change 1 alone cuts the 90 calls from about 0.35 s to about 0.1 s each, which would be 9 to 10 s of reads. Together with the call reductions above, the 10 s goal looks reachable only if #1 works. Without it, the call count would have to fall to about 28, which needs batching (#2) on top of everything else.

Not counted above: about 2 s a turn for the turn read and the checks after the end-turn request. They count toward decision time in `speed.py` although they follow the end-turn request.

## Done in this change

1. **More shared reads.** `SharedReads` in `bot.py` now also remembers `get_units`, `get_tech_civics`, `get_policies`, `get_governors`, `get_trade_routes`, `get_pantheon_status` and `get_city_states` until a change or the end of the turn. These are single read-only queries with no side effects. Any call that is not a plain read still clears the memory, so a handler that acts reads fresh data afterwards. The diplomacy, notification and congress reads are left out on purpose, because the end-turn watcher polls them while the game moves. Estimated saving: 8 to 12 calls (3 to 4 s) on a quiet turn, mostly in government (reads it three times) and the repeated `get_units`. No decision changes, because the second read of an unchanged game gives the same answer.
2. **Gold stops early.** `GoldPolicy` reads the overview, which holds gold, income and city count, and returns if there is nothing above the reserve. It reads the full empire only when it can spend. The decision is the same; the five extra calls are skipped.
3. **Per-call timing in the turn log.** Each turn log line now has `handler_calls`: for each handler, for each `GameState` method, `[count, seconds]`. Only real calls to the game are counted, not reads answered from the cache. Calls outside a handler (the turn read, the end-turn request) are not counted. The overhead is two clock reads and a dict update per call. The next live run will show the true cost of each call and which handler makes the most.

## To check in the next live run

- Sum `handler_calls` by method: which methods are most of the time, and whether 0.35 s per call holds for all of them or only for cheap ones.
- Government and blockers in turns 200 and later, now that the governor loop is fixed.
- Gold, faith, trade_offers and wars: their calls on busy turns against the estimates above.
- Decision mean and p90 against Run 3 (34.5 s and 50 s).
