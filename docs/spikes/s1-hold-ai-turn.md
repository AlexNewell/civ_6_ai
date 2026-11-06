# S.1 spike: can an AI player's turn be held for external control?

Issue 22. Verdict: **held**, with limits. A hold that delays the start of the AI phase, is visible
over the tuner and can be released from the tuner was demonstrated live. Two other tried methods do
not hold the AI turn usefully. Nothing was installed.

## Method

- The hooks are plain Lua sent over the tuner (`mods/s1_hold_turn/s1_hooks.lua`). They are
  registered in both the `GameCore_Tuner` and `InGame` Lua states and keep their log and hold
  state in `ExposedMembers.Civ6AiS1`. They live only in the running game's memory. No mod was
  installed and nothing was written to Documents, OneDrive or the game folder. The driver is
  `scripts/spike_hold_turn.py`; its pure helpers are unit tested.
- The driver ends the local turn with `UI.RequestAction(ACTION_ENDTURN)`, logs every hooked event
  with a timestamp, and polls the hold state over the tuner every 0.25 s. The tuner is both
  reader and releaser. It does not use civ6-mcp's `end_turn`, and it makes no saves.
- Game: standard save STD_T1_03 (matched by seed), played from turn 32 to 35 in the three live runs
  below. Player 1 is the first AI to move after the local player (player 0). A per-player digest
  (unit count, moves left, position hash, gold, researching tech, city count) was recorded at
  events, to tell whether the built-in AI had acted yet.
- Raw evidence is in `runs/` (git-ignored, since it can hold local data).

## Event order (measured, same in every run)

Ending the local turn fires, within about 1 ms: `Events.LocalPlayerTurnEnd` (both states),
`Events.PlayerTurnDeactivated` for player 0, then `Events.RemotePlayerTurnBegin(1)` (both states).
In GameCore only, 30 to 55 ms later: `GameEvents.PlayerTurnStarted(1)`, then
`GameEvents.PlayerTurnStartComplete(1)`. About 20 to 80 ms after that,
`Events.PlayerTurnActivated(1, true)` fires in both states, followed within a millisecond by the
AI's unit events (`UnitMoved`, `UnitMoveComplete`, `UnitOperationStarted`), then
`RemotePlayerTurnEnd` and `PlayerTurnDeactivated`. This repeats for each AI about 70 ms apart; the
whole AI phase took about 1.3 to 1.5 s. Then `LocalPlayerTurnBegin`, `PlayerTurnStarted(0)` and
`PlayerTurnActivated(0)` fire.

- `GameEvents.*` could not be registered in InGame (the hook attempt failed there); they exist in
  GameCore only. `Events.*` work in both.
- The digest changed between `PlayerTurnStartComplete` and `PlayerTurnActivated` (moves left,
  position hash, gold), so the built-in AI makes its moves inside that window, after
  `PlayerTurnStartComplete`, not before it.
- `Events.RemotePlayerTurnBegin` fires in InGame before `GameEvents.PlayerTurnStarted`, so it is a
  point where the AI has done nothing yet.

## Hold methods tried (live, turns 32 to 35)

| Method | Hooked at | What happened | Held? |
| --- | --- | --- | --- |
| Busy loop (`spin`) | GameCore, after `PlayerTurnStartComplete(1)` | The game blocked for the loop's own 3 s limit. The tuner could not answer during it (one poll took 2.3 s), so it cannot be released from outside. The AI had not moved at hold start and moved straight after. | Held, but not releasable and freezes the game. Unusable. |
| `SetWantsPause` | InGame, `RemotePlayerTurnBegin(1)` | `IsPaused()` stayed false. Every AI took its turn normally within about 1.3 s. `LocalPlayerTurnBegin` was delayed until the release, 5.8 s after the end turn. | Not for the AI phase. It only delays the human's next turn after all AIs have moved. |
| `UI.ReferenceCurrentEvent` / `UI.ReleaseEventID` | InGame, `RemotePlayerTurnBegin(1)` | `PlayerTurnStarted(1)` did not fire until the release. The AI digest was unchanged during the hold and no AI unit events were logged. Polls from both states answered in about 0.33 s and reported `held` the whole time. | Held and releasable from the tuner. |

The `ui_event` run in numbers: the hold began at the end-turn event; 14 polls over about 6.5 s all
reported `hold=held` and `local_active=false`; the release was sent from the tuner at about 6.6 s;
`PlayerTurnStarted(1)` fired at 6.56 s after hold start, i.e. as soon as the release arrived (to
within the polling interval), and all AIs then ran normally. The local turn came back as turn 33
with the tuner responsive. No timeouts occurred apart from the spin's intended one.

## What this does and does not show

- Measured: the start of an AI player's turn can be paused from the tuner and resumed from the
  tuner, with the built-in AI not yet having acted, using an event reference taken in an InGame
  `Events.RemotePlayerTurnBegin` handler.
- Not measured: holding before a later AI (only the hold before player 1 was run, though the
  handler can match any player or all players), issuing a puppet's own actions during a hold (S.3),
  and suppressing the built-in AI after release (S.2). After release the built-in AI plays its
  normal turn, so a hold is not suppression.
- Inference: the reference holds the event queue rather than the AI, so it probably behaves the
  same for other AI players, but this is untested. UI popups or other event holders might interact
  with it.
- Inference: the InGame (UI) state needs a fully loaded game; the tuner already reaches it with no
  install, so this spike did not need an installed mod.

## Game state and restart

The owner closed the game themselves for a few hours and reopened it, so the process ID change was
not a crash and no test caused it. The earlier hooks did not survive. A read-only check before
anything else showed no `Civ6AiS1` object in the InGame state, so no hold could be active, and the
game was not paused. The game was STD_T1_03 at turn 32, which is a standard save by seed.

Six earlier runs from the previous game process left report files, but their command lines were
not recorded, so they are not relied on; they showed the same event order. The three runs above
were redone on the current game.

After the runs: turn 35 of STD_T1_03, local player active, not paused, hooks removed. No game
files, saves or Documents folders were created or edited.

## Feeds into later spikes

- S.2 and S.4 can use the event-reference hold as the point to run puppet actions before the
  built-in AI takes its turn.
- S.3 must still show that a puppet can act while the hold is active.
- S.5 only needs this for choosing where reads are taken.

## Note on the release context

The scheduled release first ran in the last context of `--poll`. The live `ui_event` and `pause`
runs used `--poll GameCore InGame`, so the release did run in InGame, the same context that took
the hold. The recorded results therefore stand. The driver now always releases in `--hold-context`,
so a default poll list can no longer send the release to the wrong state. Not re-run live after
this change.
