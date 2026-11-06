# civ_6_ai

Experiments in building Civilization VI opponents powered by LLMs, for no more than $1 of external model calls per player per game.

Most decisions are handled by deterministic code, routine judgment calls by a cheap model tier, and rare strategic ones by a frontier Claude model. Early work pits one LLM player against the built-in AI. The long-term goal is a set of LLM opponents for a human player.

**Status:** Phase 1, the deterministic bot. The turn loop and policies for settling, research, government, production, builders, trade and religion, defence and diplomacy are built and tested offline. Live acceptance is still in progress.

- [Roadmap](roadmap.md)
- [Decisions and public-ready rules](docs/decisions.md)
- [Original research report](docs/research/reducing-frontier-model-cost.md)
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

## First push

Run these once from the repo root:

```bash
gh repo create civ_6_ai --private --source . --push
uv run python scripts/seed_github.py
git add roadmap.md
git commit -m "Link roadmap items to issues"
git push
```

The seed script creates the labels, milestones and issues, then adds issue links to the roadmap. It is safe to run again.
