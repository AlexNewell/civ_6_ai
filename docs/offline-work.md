# Offline work beside a live game

A full game takes hours and runs from one checkout. Code, tests and docs can still move forward during it, as long as nothing touches that checkout or the game. This is the protocol for agents and people doing that work.

## Rules

1. **One sibling git worktree per task.** Create it by hand from the main repository, next to it, on a new branch from `origin/main`:

   ```
   git fetch origin
   git worktree add ../<repo>-<task> -b <task-branch> origin/main
   ```

   Some agent tools' own worktree option can fail on Windows when the drive letter's case differs between paths (`C:` vs `c:`), so make the worktree by hand and work in it with absolute paths. Never switch branches, write files or run anything that writes in the live checkout, and don't use another task's worktree.
2. **Each worktree has its own environment.** Run `uv sync` inside the worktree, then run tests with that worktree's interpreter (for example `.venv/Scripts/python -m pytest -q` on Windows, `.venv/bin/python -m pytest -q` elsewhere). Tests then import that worktree's code, never the live run's.
3. **Nothing that touches the game or its tooling.** No Civ, no tuner port, no `run_game.py`, `verify_game.py`, `screenshot.py` or queue runner, no model or API calls, and no killing of processes. A live check is written down as outstanding; it is not run from here.
4. **Logs are read-only, and the live run's are off limits.** Read finished runs from the shared `runs/` folder, but exclude the live run's own stem (its newest session files), because they are still being written. Copy what you need to a scratch folder and work on the copy.
5. **Keep personal data out.** Docs, comments and PR text use no personal names or machine paths; give generic instructions. The repo may become public.

## Pull requests

- Say `Refs #N`, not `Closes #N`, when a live check is still outstanding. Use `Closes #N` only when nothing is left to verify in a game.
- List a `Live check needed` section: what to look for in the next run's logs, and the line or number that would show the change worked.
- After the PR merges, comment on the issue with what changed and the PR link, and say what is left.
- CI must be green and review threads resolved before merging, as for any PR.

## After the live run stops

Only when the run has ended (not paused), in the live checkout:

1. `git pull` main.
2. Re-run `uv sync`, because dependencies or pinned patches may have changed.
3. Run the tests, then start the next run and work through the `Live check needed` lists from the merged PRs.

Worktrees can be removed with `git worktree remove <path>` once their PR has merged.
