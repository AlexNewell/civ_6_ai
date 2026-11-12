# Triaging a stopped run

For a run that ended with a stop reason other than `turn_limit`, or whose turn log shows
`handler_errors`. Work from the cheapest evidence to the dearest, and stop once the cause is clear.

## Run the summary

    python scripts/triage_run.py [runs/<save>_<stamp>.jsonl]

It reads the newest turn log unless one is named. It prints the last turn and stop reason, the
`detail` text, handler errors, the end of the bot log, and the screenshots saved for that turn. It
makes no model or game calls, so running it again gives the same answer.

## Files in `runs/`

Files for one session share a stem `<save>_<UTC stamp>`.

| File | Holds |
|---|---|
| `<stem>.jsonl` | The turn log: one line per turn with `turn`, `stop_reason`, `detail`, `handler_errors` and timings. `detail` ends with `[screenshot: <file>]` when one was taken. |
| `<stem>_bot.log` | The bot's INFO log, including handler tracebacks. |
| `<stem>_events.jsonl` | Monitor events (cities lost, wars declared and so on). |
| `<stem>_settle.jsonl` | Settling decisions. |
| `screenshots/<game id>_T<turn>_<reason>.jpg` | Screenshots of the game window. |

A game can span several sessions, so read the previous session's tail when the stop is on a
session's first turn.

## When a screenshot is worth reading

The summary's `Screenshot:` line applies fixed rules:

- **Not needed** when the run stopped with `game_over` or `run_aborted`, because the end-turn report
  says why. Also when a `blocked` or `hung` stop names a known blocker type (see
  [blockers.md](blockers.md)) in the last 3 turns' reports or the bot log tail. Also when only
  handler errors occurred, because the traceback is in the bot log.
- **Worth reading** when the run stopped `blocked` or `hung` and no known blocker is named. That is
  a state the bot can't read, such as a popup or a screen it doesn't recognise.

The rules are deliberately simple, so they will sometimes point at an image that adds nothing. Skip
the image when the question is about numbers (gold, yields), which the logs and game reads give
exactly.

## Screenshots

- Taken automatically, as a 1280-pixel JPEG of the game window, when a turn stops the run or a
  handler fails for the first time. At most 20 per game, one per turn, and the last 3 are kept for
  stops. The design is in [decisions.md](decisions.md#screenshots-for-triage).
- Taken by hand while the game is still showing the stuck state:

      python scripts/screenshot.py <short-reason> [--log runs/<stem>.jsonl] [--stop]

  It names the game, turn and log it used. It picks the newest turn log by default, which may
  belong to another run, so pass `--log` if it isn't the game that is open. It shares the cap. If it
  reports nothing taken, the cap is used or the window can't be captured.
- They are for looking at, not for sharing. They can show player names, so don't paste them into
  issues, PRs or commits.

## Recording the cause

Write what you find into [blockers.md](blockers.md), [live-checks.md](live-checks.md) or an issue,
as for any other stop.
