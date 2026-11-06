# Cost tracking

`scripts/cost_report.py` prints tokens and dollars per game, and per turn played. It is a plain program: no model reads the logs, and it makes no network calls. Phase 1 makes no model calls, so today every game shows $0.00. That is the evidence gate G1 needs, three full games with zero LLM calls.

```text
uv run python scripts/cost_report.py                      # table, totals, trend
uv run python scripts/cost_report.py --json               # the same, for other programs
uv run python scripts/cost_report.py --history runs/cost_history.csv
uv run python scripts/cost_report.py --full-game-turns 300 --fail-over-budget --strict
```

## Where the numbers come from

- **Spend**: `runs/spend_ledger.jsonl`, written by `Ledger` when a call goes through `Governor`. It holds the model, player, turn and billed tokens of every call. The report prices those tokens itself from `src/civ6_ai/budget/pricing.toml`, so a cost never comes from a model. If the ledger's own figure differs from the table, the call is counted under `cost_mismatches`.
- **Turns**: the lines of the turn logs `run_game.py` writes, `runs/<save>_<UTC stamp>.jsonl`. The `_events`, `_settle` and `_bot` files beside them are not read.

## For anyone writing an LLM client

Every model call must go through `Governor.call`, with a `BudgetedCall` that names the game, player, model and turn. That is the only place usage is recorded. A call made any other way is missing from every figure here, and from the cap.

Use the game id that `run_game.py` prints on its `Game:` line. In code, `civ6_ai.metrics.costs.game_id_for_new_session` returns it for a session that is about to start. The cap is per `(game_id, player_id)`, so a client that used a new id each session would reset its cap. The report also accepts a session's own name as the id and puts the spend on its game, but the cap would not follow.

## Which sessions are one game

A long game is played over many sessions, each with its own turn log. Sessions of the same save, in time order, are one game while each session starts on a turn later than the last turn the game reached. A session that starts at or before that turn was loaded from an earlier point, so it begins a new game. The game's id is the name of its first session. Gaps are fine, because turns played by hand leave no log. The one thing this can't see is a second game that starts, from a later save, after the turn the first one reached.

Turns are the valid lines across the game's sessions, so a game resumed from a mid-game save counts only the turns played. A game with no turn log, or none valid, has unknown turns and shows `unknown` for every per-turn figure, never 0.

## Reading the output

- **Cost** includes calls that never closed or may have been billed, at their worst case, shown as `unsettled`. These are real commitments on the cap.
- **UNPRICED** means calls on a model that has no entry in `pricing.toml`. Their tokens are counted, their dollars are not, so the cost shown is a floor. To add a model, add a `[models."<id>"]` table with `input`, `cache_write_5m`, `cache_write_1h`, `cache_read` and `output` in USD per million tokens, and update `checked` and `source`.
- **$/turn** and **Tokens/turn** divide a game's total by its turns played, so the same spend over 100 turns is a tenth of the cost per turn of 10 turns. The player and model rows divide by the game's turns too.
- **Projected** is $/turn times `--full-game-turns`. The budget is per game, so this is the figure to compare with it. The length is an assumption, not a fact about the game.
- `Spend ledger ... NOT FOUND` means $0.00 is the absence of records. It proves zero calls only if every call goes through the Governor.
- **PROBLEMS** lists unreadable or torn lines and ledger events that don't fit. They are not counted, and the rest of the file is still read. `--strict` makes them, and unpriced models, an exit status of 1.

## Budget check

If `.local/cost-budget.json` exists (git-ignored), the report adds an over or under line per player per game. The file is `{"per_player_per_game_usd": "<number>"}`. The figure is kept out of the repo. With no file the lines are left out, and a file that can't be read is an error. `--budget-file` points elsewhere, and `--fail-over-budget` exits 1 if any player is over. An `unknown` status means some spend is unpriced, so being under isn't proven.

## History

`--history` adds one row per game to a CSV, replacing a game's row when it is seen again and keeping rows of games whose logs were deleted. Running it again on the same logs leaves the file unchanged. Totals and the trend come from the logs in `runs/` at the time, not from the history file.
