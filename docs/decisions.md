# Decisions

Project decisions and why they were made. New ones are added as the work goes on.

## What shaped the plan

| Finding | Consequence |
|---|---|
| Frontier-only CivBench games cost about $31-229 each and take 2-8 hours. | A $1 cap needs a 97-99.5% reduction, so most turns must make no LLM call and a deterministic bot comes first. |
| FireTuner is hard-disabled in all multiplayer modes, including hot-seat. | Opponents must "puppeteer" AI slots in single-player. |
| Puppeteering is an unimplemented upstream proposal with open questions. | The long-term goal is unproven, so Workstream "S" tests it early. |
| The tuner allows one connection, and upstream recommends one orchestrator process for multi-agent play. | The game AI runs in-process against civ6-mcp's `GameState`, with no per-model MCP tool loop. |
| GameCore can read every player's state. | Fog-of-war filtering must be enforced, or opponents can cheat by accident. |
| Claude Haiku 5.5 costs $0.10 input and $0.50 output per million tokens, for prompts up to 100,000 tokens. Above that, the whole request costs five times as much. | It is a viable cloud option for routine decisions, provided prompts stay small. |
| Consumer PCs share limited memory between the game and a local model. | Local models are benchmarked offline first and may not be viable during live play. |

## Cost

Lowering cost per game is a main goal, because players are unlikely to pay much for an opponent. The $1 per player per game cap is a working target and a safeguard, and the aim is to go lower.

- The cap counts every external model call in a game, per player. Local inference counts as $0.
- A code-enforced governor blocks any call whose worst-case cost would exceed the cap.
- At the cap, or if an API is down, the player continues deterministically. A game never stalls.
- Every call is logged using actual API usage.
- A failed call that may still have been billed, such as a timeout, stays charged at its worst case. Only a definite API rejection is refunded.
- Spend is written to disk before each request, so a crash or restart can't reset a player's budget. If the ledger can't be read or written, calls are blocked.
- The cap relies on callers declaring a true input limit, measured with token counting, and making one request with SDK retries and refusal fallbacks turned off. These rules are in [`governor.py`](../src/civ6_ai/budget/governor.py).

Estimated split, to be replaced with measurements in issue 2.5:

| Bucket | Allocation | Assumption | Buys |
|---|---|---|---|
| Strategist | $0.65 | 6k input, 1.5k output, $0.027 per Sonnet call | About 24 Sonnet calls (every 20 turns plus about 8 events), or 12 Opus |
| Cheap tier | $0.25 | Haiku, 1.5k input, 100 output, $0.0002 per call | About 1,250 calls, under 4 per turn |
| Crisis reserve | $0.10 | Late-game escalations | About 3 Sonnet calls |

A serialised city gets at most 400 estimated tokens, about a quarter of the Cheap tier's 1,500-token call, which leaves room for instructions, an empire summary and the choices on offer. Tokens are estimated offline as UTF-8 bytes divided by 3, and a test enforces the limit ([`serialise.py`](../src/civ6_ai/state/serialise.py)). A typical city is about 80.

Prompt caching is left out of these figures. Strategist calls will often be more than 5 minutes apart, so cache hits can't be assumed. Prices live in [`pricing.toml`](../src/civ6_ai/budget/pricing.toml), last checked against Anthropic's pricing page on 2026-11-05. Recheck them before use.

## Models

- The strategist defaults to Sonnet 5.5, with Opus 5.5 as a measured comparison. Fable-class models are out of scope.
- Model IDs and prices live in config.
- Haiku 5.5, local models and Jev compete for the routine-decision tier on measured quality per dollar.
- The baseline comes from the public CivBench dataset, so no expensive frontier-only games are needed. See [`civbench-baselines.md`](civbench-baselines.md), which `scripts/fetch_civbench.py` and `scripts/summarise_civbench.py` rebuild.
- Baselines use `civbench/civbench-v1`, the release the paper links. The earlier `lmwilkin/civbench` repo has no parquet tables or Inspect logs.
- CivBench scenarios don't match our standard game, so baselines are a reference level and M1 is still measured on our own games.

## Speed

A strong, cheap opponent is no use if the human waits too long for it. Speed is the third thing to watch, beside strength and cost.

- **Measure:** an opponent's *decision time*, from the start of its turn to its end-turn request: state reads, policies and model calls. The game's own end-turn processing is logged separately, because a human waits for it in any Civ game. [`speed.py`](../src/civ6_ai/metrics/speed.py) reports both at the end of each `run_game.py` session.
- **Development bar:** a mean of 60 seconds per opponent turn. It is monitored while the bot is built, not an acceptance criterion.
- **Long-term goal:** 10 seconds per opponent turn, so five opponents take under a minute together. At 60 seconds each, five opponents would take 5 minutes a turn, and about 25 hours of waiting over a 300-turn game.
- Opponents act one after another, so their decision times add up. Each one can plan in the background while others and the human play, then on its own turn only react to what it can see changed since it started planning. Model calls belong in that background planning, not on the turn's critical path.
- **Baseline (Run 3, 313 turns, no model calls):** decision time mean 34.5 s, median 32.5 s, p90 50 s, rising from about 29 s before turn 100 to about 40 s after turn 200. The end-turn wait had a mean of 31.7 s and a median of 13.3 s, with stalls up to 11 minutes. The bot meets the development bar, but the goal needs decisions about 3.5 times faster before any model call is added. Most of the time goes to blockers, government, settling, production and builders, each 3 to 7 s a turn.

## Game

- Standard setup: 6 players, Small map, Quick speed (about 330 turns), Prince, Gathering Storm, fixed T1 saves.
- Game build 15296837 is pinned as far as Steam allows, and each result records its build.

## Opponents

- Every LLM player uses identical code, prompts and config.
- Strict fairness: own fog of war only, no cheat APIs, and no resource bonuses added by this project.
- Speed targets are in [Speed](#speed).
- Play isn't fully deterministic, because Claude responses can't be reproduced on request. Deterministic layers are seeded and model replies are stored, so any game can be replayed exactly.

## Policy document

Issue 2.1. `src/civ6_ai/strategy/policy_document.py` defines how strategy steers the deterministic policies. It is offline: no model or game call.

- A JSON document with `version` (1), `victory` (`any`, `science`, `culture`, `domination`, `religious`, `diplomatic`), `posture` (`expand`, `balanced`, `defend`, `conquer`), `expansion.target_cities` and `weights` per domain (`production`, `research`, `secondary`, `military`, `gold`). Every part is optional.
- `resolve` builds each policy's weights from its defaults, then scales them by the victory and posture profiles, then applies the explicit `weights`, so an explicit value always wins. The default document resolves to exactly the policies' own defaults.
- Only plain numeric weights are settable. The named priority tables (research priorities, government ranks) stay with their policies. Numbers must be finite and between 0 and 1000, and health shares at most 1.
- An invalid document is rejected whole, with every problem listed, and the weights in force stay. Partial application was ruled out because a half-applied document is hard to reason about.
- `PolicyDocumentHandler` (`strategy/apply.py`) is a turn-loop handler that runs right after `Monitors`. It re-reads one file when its modified time or size changes. `build_orchestrator(policy_document=path)` adds it. With no path nothing changes. The blocker resolver's fallback policies keep their defaults.

## Task database

Issue 2.2. `src/civ6_ai/strategy/tasks.py` turns the strategist's intentions into tracked work, with no model call.

- A task has a title, a priority (0 to 9, higher first), an optional deadline turn, optional dependencies and an optional target: a figure from our own overview (`cities`, `gold`, `science`, `score` and so on), `>=` or `<=`, and a value. A task with no target is finished by hand.
- `TaskHandler` runs each turn after `Monitors` (and the policy document handler). It finishes tasks whose target is met and whose dependencies are done, and publishes one `TaskOverdue` event, once, for each open task past its deadline. An overdue task stays open and can still finish, which is then recorded as late.
- Dependencies must exist when a task is added and can't be edited, so there are no cycles. A task with open dependents can't be cancelled, so nothing waits on work that will never finish. The deadline counts regardless of dependencies, so a late prerequisite is reported.
- State is one JSON file, written atomically (temp file then replace) after every change, and read at start. An unreadable file is moved to `<name>.corrupt` and the database starts empty, so nothing is lost silently. The overdue flag is saved before the event is published, so an event is delivered at most once, and a crash between the two loses it.
- Loading an earlier save leaves tasks as they are, because their deadlines are turn numbers in the run. Re-plan by cancelling and adding.
- `build_orchestrator(task_db=path)` adds the handler. With no path there are no tasks.

## Dependencies

- [civ6-mcp](https://github.com/lmwilki/civ6-mcp) (MIT) is pinned to commit `dd2019056371b92ea4854e879ddf05a8cad95e8a` (v1.1.11), then vendored under `third_party/` in Phase 6, because a GitHub fork of a public repo can't be private.
- Only civ6-mcp's `GameState` code is used. Its base dependencies serve its server, so `pyproject.toml` overrides them away, and `pywin32` is added directly because the crash-dialog handling needs it.

### Rules for using civ6-mcp

A review of the pinned commit found nothing malicious. These rules still apply:

- Never run `civ_mcp.server`. It opens an unauthenticated dashboard on all network interfaces, moves the mouse and can close the game.
- `load_game_save` can restart the game and click through its menus if Lua can't find the save.
- `end_turn` deletes older `0_MCP_*` saves, so project saves must not use that prefix.
- civ6-mcp assumes the default Documents folder, so call `use_real_save_dirs` first, or `0_MCP_*` saves are never pruned. After that, never call its `load_game_save`, because its fallback would then find the save and restart the game.
- `end_turn` writes a `0_MCP_NNNN` save every turn, which duplicates Civ's own autosave (issue #17). `disable_mcp_saves` in `saves.py` turns that off at startup. Crash recovery then uses the newest `AutoSave_*` in the `auto` folder, which civ6-mcp's `get_latest_autosave` falls back to.
- Loading a save over Lua stops on Civ's loading screen until someone clicks, and the tuner stays closed until then. Unattended runs need a way past this. See issue #17.
- Strings sent to Lua aren't escaped, so model-chosen names must be validated first. Never expose its raw Lua method to a model, because that would bypass fog of war.
- [`archi-max/jev-civ6`](https://github.com/archi-max/jev-civ6) (MIT) is a usable reference.
- [`Roarpeng/jev-civ6`](https://github.com/Roarpeng/jev-civ6) has no licence, so it is read for ideas only and no code is copied.

### Screenshots for triage

- When a turn stops the run (blocked, hung, game over) or a handler fails for the first time, `src/civ6_ai/screenshots.py` saves a 1280-pixel JPEG of the game window, with at most 20 per game. The last 3 slots are kept for stops, and there is at most one per turn.
- The count is the number of files already saved for the game id, so it carries across sessions. It uses the same id as the budget ledger.
- They go under the git-ignored `runs/screenshots/`, because they can show player names. The turn log's `detail` names the file.
- It uses `pywin32` and `Pillow` (win32 only) directly, not civ6-mcp's private launcher helpers, which can change with the pin. It captures with `PrintWindow`, so the game must be windowed or borderless. A blank frame counts as a failed capture.

## Public-ready rules

A public repo exposes all of its history and issues, so these rules apply from the first commit.

**Never in git or issues:**

- Email addresses, or game and service account names. The owner's name is fine, because the GitHub handle already shows it.
- Absolute local paths or usernames. Code uses env vars and relative paths; docs use placeholders.
- API keys and tokens. They live only in an untracked `.env`.
- Personal schedules or spending details beyond the project's per-game cap.
- Game saves, logs, telemetry and stored model replies, which can contain player names and local paths.
- Civ VI assets or binaries, and downloaded datasets.

**Enforcement:**

- Commits use a GitHub noreply address.
- Merges done on GitHub can use the account's email, so CI scans a PR's own commits and history stays noreply only if the account keeps its email private.
- `gitleaks` runs in CI and as a pre-commit hook.
- [`scripts/check_public_ready.py`](../scripts/check_public_ready.py) checks files and commit metadata. Personal strings go in the untracked `.local/public-ready-patterns.txt`.
- Test fixtures must be synthetic or scrubbed.

The repo is public under an all-rights-reserved [`LICENSE`](../LICENSE), the most protective choice that still suits a public repo. It allows reading and GitHub forks only. As sole copyright holder the owner can relicense later, for example to MIT, but only while no outside code has been merged. Until the licence is changed, don't merge contributions from others without their agreement to relicense. Issue 7.4 revisits the choice.

## Sources

- [civ6-mcp repository](https://github.com/lmwilki/civ6-mcp), including `docs/agent-vs-agent.md`, `docs/observability.md` and `docs/paper/scenario-spec.md`
- [CivBench paper, arXiv 2609.02459](https://arxiv.org/abs/2609.02459)
- [CivBench dataset](https://huggingface.co/datasets/civbench/civbench-v1)
- [Original research report](research/reducing-frontier-model-cost.md)
- [Airbnb classifier benchmark and our review](research/airbnb-llm-classifier-benchmark.md): supports a frontier strategist and suggests a fine-tuned small model for the cheap tier
