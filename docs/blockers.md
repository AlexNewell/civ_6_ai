# Turn blockers

Everything that can stop a turn from ending, how it's found and what clears it. [`blockers.py`](../src/civ6_ai/blockers.py) holds the handlers. This list comes from reading civ6-mcp's `end_turn` at the pinned commit, so a type the game has but civ6-mcp never names may be missing. See [Unknown blockers](#unknown-blockers).

## How it works

- `BlockerResolver` is a turn handler and must run last, because it skips idle units. It reads every source, acts on what's open, and repeats up to 3 times, because clearing one blocker can reveal another. Once a handler has settled a type, the resolver leaves it alone until the next call.
- `ResolvingGame` wraps the game and is what `Orchestrator` gets. Some blockers only appear after the turn is ended, and the turn loop stops on any turn that doesn't advance. So when `end_turn` reports a blocker, the wrapper clears it and calls `end_turn` again, up to 8 times.
- The wrapper retries on these report prefixes:

| Report | Pass before the retry |
|---|---|
| `Turn paused`, `WAR DECLARED`, `Cannot end turn: diplomacy`, `Cannot end turn: incoming trade deal` | Diplomacy only, because the AI may still be working and other queries can stall it |
| `Cannot end turn`, `World Congress fires`, `End turn blocked` | Full |

- Any other report goes straight to the turn loop, which judges it. That covers normal reports and `GAME OVER`, `HANG:`, `RUN ABORTED` and `CRITICAL:`.

## Catalogue

"Left to `end_turn`" means civ6-mcp clears the type itself just before ending the turn, so the resolver does nothing unless a policy takes it over. Policies run first, which lets one make the choice before `end_turn` applies its default.

### Found by their own queries

| Type | How detected | How resolved |
|---|---|---|
| `DIPLOMACY_SESSION` | `get_diplomacy_sessions` | War declarations are closed with `EXIT`, as `end_turn` does, because they can't be declined. Sessions with a deal are refused. Anything else gets `POSITIVE` for up to 4 rounds, then `EXIT` as a last resort. |
| `TRADE_DEAL` | `get_pending_deals` | Refused, because the bot can't value a deal yet. |
| `WORLD_CONGRESS_VOTE` | `get_world_congress`, using `end_turn`'s own gate: it fires this turn with resolutions, or is in session | `queue_wc_votes([])` queues civ6-mcp's default voter, which spreads favor evenly on option A and target 0 during `end_turn`. The session opens and closes inside `end_turn`, so this must be done beforehand. It's dropped when the game also reports `ENDTURN_BLOCKING_WORLD_CONGRESS_SESSION`, because the handler only fires on a later session and the open one is voted directly. |
| `GREAT_PERSON_CLAIM` | `NOTIFICATION_CLAIM_GREAT_PERSON` in `get_notifications` | Recruits everyone with `can_recruit`. civ6-mcp lists no blocking type for this, so it may never block a turn. It's handled anyway because the issue names it and recruiting has no downside. |

### The game's end-turn blocking types

All of these come from civ6-mcp's blocking query, run through `execute_lua` in the InGame context.

| Type | How resolved |
|---|---|
| `ENDTURN_BLOCKING_RESEARCH` | Starts the tech with the fewest turns. If a tech is already set, the notification is stale and is left to `end_turn`. |
| `ENDTURN_BLOCKING_CIVIC` | Same, for civics. |
| `ENDTURN_BLOCKING_PRODUCTION` | In each idle city, starts the cheapest non-unit item, then units, skipping districts and repairs because they need a tile. Tries every option until one is accepted, because wonders are listed as buildings but refused without a tile, so no cap on tries can be safe. Corrupted queues are left to `end_turn`. |
| `ENDTURN_BLOCKING_FILL_CIVIC_SLOT` | Puts an unused policy that fits into each empty slot. A wildcard slot takes any type. |
| `ENDTURN_BLOCKING_GOVERNOR_APPOINTMENT` | Appoints the first available governor that's accepted, or promotes the first one with a promotion on offer. |
| `ENDTURN_BLOCKING_GOVERNOR_OPPORTUNITY` | Same as an appointment. Seen live on turn 62, when a governor title became available: the turn still ended, but the type had no handler and raised `UnresolvedBlockers` every turn. |
| `ENDTURN_BLOCKING_GOVERNOR_IDLE` | Left to `end_turn`, which dismisses it. The governor stays unassigned until a policy places it. |
| `ENDTURN_BLOCKING_UNIT_PROMOTION` | Asks each combat unit for its options and takes the first. Stale notifications are left to `end_turn`. |
| `ENDTURN_BLOCKING_PANTHEON` | Chooses the first belief offered. |
| `ENDTURN_BLOCKING_COMMEMORATION_AVAILABLE` | Chooses the first dedications, up to the number allowed. This is the era choice. |
| `ENDTURN_BLOCKING_GIVE_INFLUENCE_TOKEN` | Sends each envoy to the first city-state that accepts it. With no envoys left it's stale, and `end_turn` clears it. |
| `ENDTURN_BLOCKING_WORLD_CONGRESS_SESSION` | If a session is open, casts one free vote on option A, target 0, for each open resolution, then submits. Otherwise queues the default voter. civ6-mcp's `submit_congress` isn't used, because it also ends the turn and a second end-turn request can skip turns. |
| `ENDTURN_BLOCKING_WORLD_CONGRESS_LOOK` | Left to `end_turn`, which marks the results as seen. |
| `ENDTURN_BLOCKING_WORLD_CONGRESS_SPECIAL_SESSION` | Left to `end_turn`, which passes on it, as any other World Congress type. |
| `ENDTURN_BLOCKING_CONSIDER_GOVERNMENT_CHANGE` | Left to `end_turn`, which keeps the current government. |
| `ENDTURN_BLOCKING_CONSIDER_RAZE_CITY` | Left to `end_turn`, which keeps the city. |
| `ENDTURN_BLOCKING_CONSIDER_DISLOYAL_CITY` | Left to `end_turn`, which keeps the city. |
| `ENDTURN_BLOCKING_SPY_CHOOSE_ESCAPE_ROUTE` | Left to `end_turn`, which picks the fastest route. |
| `ENDTURN_BLOCKING_STACKED_UNITS` | Moves extra units to any of the eight neighbouring tiles that accepts them. Combat units and others count as separate groups. |
| `ENDTURN_BLOCKING_UNITS` | `skip_remaining_units`, which fortifies combat units and skips the rest. Runs after every other type. |

### Handled by `end_turn` alone

These have no entry in `BlockerType`, because the bot never sees them.

| Blocker | How `end_turn` handles it |
|---|---|
| Popups: wonders, disasters, era screens, tech and civic completion | `dismiss_popup` before each end of turn |
| AI turn hang | Returned as a `HANG:` report. The turn loop stops, as `GAME_OVER` instead if `civ6_ai.defeat` finds the player defeated, which looks the same from outside. Recovery is out of scope here, because civ6-mcp's recovery restarts the game. |

## Things that look like blockers

- **Choosing a religion:** civ6-mcp has hints for `NOTIFICATION_CHOOSE_RELIGION` and `NOTIFICATION_CHOOSE_BELIEF` but no blocking type. They follow a Great Prophet's activation, which no default does, so they're left to the religion policy. [`SecondaryPolicy`](../src/civ6_ai/policies/secondary.py) founds the religion on that notification or the turn after it activates a prophet, and can take over `PANTHEON` and `GREAT_PERSON_CLAIM` through `blocker_policies()`.
- **Choosing a first government:** no type names this. `CONSIDER_GOVERNMENT_CHANGE` only keeps what's there, so changing government is left to a policy.

## Unknown blockers

A blocking type with no handler is logged, and `run` raises `UnresolvedBlockers`, which the turn log records under `handler_errors`. The turn is still tried, because `end_turn` asks the game whether it can end anyway. If it can't, retries run out and the turn loop stops with `blocked`, so a block can't go unnoticed. The fix is to add the type to `BlockerType`, this page and `DEFAULT_RESOLVERS`.

## Taking over a type

Pass a function to `BlockerResolver`:

```python
async def choose_research(game: BlockerGame, blocker: Blocker) -> bool: ...


resolver = BlockerResolver({BlockerType.RESEARCH: choose_research})
```

The function returns True once it's done. If it returns False or raises, the default runs instead, so a bug in a policy can't stall a game. A policy can also take a type left to `end_turn`, such as `ENDTURN_BLOCKING_CONSIDER_RAZE_CITY`, or a type that has no default.

## Known limits

- These defaults only keep a game moving. They aren't good play. Roadmap items 1.5 to 1.12 replace them.
- If the game's Auto End Turn option is on, skipping the last unit can end the turn before the loop does, and the handlers wouldn't run on the next turn. civ6-mcp notes this can happen, so standard games need the option off.
