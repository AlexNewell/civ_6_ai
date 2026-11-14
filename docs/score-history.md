# Score history across runs

`docs/score-history.csv` has one row per live run of STD_T1_01, so we can tell whether runs are
getting better, worse or staying flat. Winning is the real measure, but most runs end in a rival's
victory on different turns, so each row holds figures that compare fairly between a short run and a
long one: our score at turns 50, 100, 150, 200, 250 and 300 (blank when the run did not reach it),
our share of the leading met rival's score and our rank at each of those turns, the mean score per
turn played, the mean share of the leader, and the outcome. The reasoning, and the noise band used
to call a change real, is in the docstring of `src/civ6_ai/metrics/history.py`.

Rows marked `source=manual` were copied from `docs/live-checks.md` (checkpoint scores for Runs 8
and 9, outcomes only for older runs). They have no rival scores, so no share or rank. Rows marked
`log` come from the turn logs.

## What the turn log records

Each line of `runs/<scenario>_<stamp>.jsonl` also has `score`, `cities` and `population` (ours, at
the start of the turn) and `rival_scores` (player id to score, for every rival we have met). They
come from the game overview that the turn loop already reads every turn, so they cost no game call.
If the overview cannot be read, a warning is logged once and the fields stay `null`; the run goes
on. Logs from before this change have no scores, so a row from them has outcome and turns only.

## After a game: add its row

```
.venv/Scripts/python scripts/score_history.py add "Run 10" runs/<first session>.jsonl runs/<later session>.jsonl --date 2026-11-15
.venv/Scripts/python scripts/score_history.py report
```

Give every session log of the run, oldest first (a turn in two logs takes the later one). Adding
the same name again replaces its row. Fill in `winner` or `victory_type` in the CSV by hand if the
game report in the log did not name them. Commit the CSV with the run's entry in `live-checks.md`.
`report` prints the table and, per checkpoint, whether each run was better, worse or flat against
the previous run and against the best earlier run.
