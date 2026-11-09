# civ_6_ai

Better opponents for human players of Civilization VI, built from LLMs.

The goal is not to replace human players. It is to make the game more fun for them, by giving them opponents that play like capable people instead of like scripts.

## Why

Most people play grand strategy games against the built-in AI. That AI mostly follows a very long list of hand-written if-then rules. It is predictable, and advanced players find it too weak to be a real challenge.

Developers know this, so higher difficulty levels don't make the AI play better. They give it advantages instead: extra yields, extra starting units, combat bonuses. In Civilization VI, for example, [every level above Prince hands the AI bonuses](https://civilization.fandom.com/wiki/Difficulty_level_(Civ6)). The result can feel less like facing a skilled rival and more like facing someone who is cheating.

This project explores a different route: opponents that win or lose on decisions, under the same rules as the human. A language model can adapt to what the human is actually doing, change plans, and behave less predictably than a rule list. The hard part is cost, so the design keeps most decisions in ordinary code and calls a model only when judgment is needed.

## Approach

- Deterministic code handles routine decisions, a cheap model tier handles routine judgment calls, and a frontier Claude model handles rare strategic ones.
- A hard budget of $1 of external model calls per player per game, enforced in code.
- No handicap bonuses. Results are measured at Prince, the level where the built-in AI gets no advantages.
- Early work pits one LLM player against the built-in AI, to measure whether it plays better. The long-term goal is a set of LLM opponents for a human player.

**Status:** early. The budget tooling, baseline data and test saves are in place. Nothing plays a full game yet. This is a personal experiment, and anything here may change.

- [Roadmap](roadmap.md)
- [Decisions and public-ready rules](docs/decisions.md)
- [Research notes](docs/research/reducing-frontier-model-cost.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)

## Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- [GitHub CLI](https://cli.github.com/), to create issues
- For game work: Civilization VI on Steam with Gathering Storm, plus the Civ VI SDK

## Development

```bash
uv sync
uv run pre-commit install
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python scripts/check_public_ready.py --history
uv run python scripts/seed_github.py --dry-run
```

If this folder is synced by a cloud drive, set `UV_PROJECT_ENVIRONMENT` to a path outside it so the virtual environment doesn't sync.

Put API keys in an untracked `.env`, using [`.env.example`](.env.example) as the template. Personal strings to block from commits go in `.local/public-ready-patterns.txt`, one regular expression per line.

## Licence

All rights reserved. The code is public to read and follow, not to reuse. See [`LICENSE`](LICENSE). This may change as the project matures. Third-party material keeps its own terms, listed in [Third-party notices](THIRD_PARTY_NOTICES.md).

## GitHub setup

[`scripts/seed_github.py`](scripts/seed_github.py) creates the labels, milestones and issues from [`.github/roadmap-issues.yaml`](.github/roadmap-issues.yaml), and links them from the roadmap. It is safe to run again. Use `--dry-run` to preview.

Civilization VI is a product of Firaxis Games and 2K. This project isn't affiliated with or endorsed by them.
