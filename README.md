# civ_6_ai

Better opponents for human players of Civilization VI, built from LLMs.

The aim is to make the game more fun for human players with opponents that play like capable people. It is not meant to replace human players.

## Why

Most people play grand strategy games against the built-in AI. That AI mostly follows a very long list of hand-written if-then rules. Advanced players find it predictable and too weak to be a real challenge.

Higher difficulty levels usually give the AI advantages instead of better play: extra yields, extra starting units, combat bonuses. In Civilization VI, [every level above Prince gives the AI bonuses](https://civilization.fandom.com/wiki/Difficulty_level_(Civ6)). That can feel like playing against someone who is cheating.

This project tries opponents that follow the same rules as the human and win or lose on their decisions. A language model can adapt to what the human is doing, change plans, and be harder to predict than a rule list.

## Cost

Calling an LLM costs money, so a main goal of this project is to make the cost per game as low as possible. A strategy game can take many hours or days, so a player might be willing to pay a very small amount for an opponent that makes the game worthwhile. Tens or hundreds of dollars per game is too much.

[CivBench](https://arxiv.org/abs/2609.02459), the one public benchmark of LLMs playing Civilization VI, reports roughly $31 to $229 in API fees per full game. In the sample this project analysed, the LLM won only 3 of 23 games. The paper calls its data a pilot, so this is a rough signal.

To keep costs down, ordinary code makes most decisions, a cheap model handles routine judgment calls, and a frontier model is called only for rare strategic choices. The project measures cost per game and works to lower it. 

Furthermore, wherever possible, this project will explore locally hosting miniature LLM models to avoid API costs. However, locally hosting an LLM may not be reasonable for all potential players, given the hardware requirements.

## Approach

- Deterministic code handles routine decisions, a cheap model tier handles routine judgment calls, and a frontier Claude model handles rare strategic ones.
- Cost is measured on every call. There is a working target of $1 per opponent player per game. It is currently unclear how realistic that goal is.
- No handicap bonuses. Results are measured at Prince, where the built-in AI gets no advantages.
- Early work pits one LLM player against the built-in AI to see whether it plays better. The long-term goal is a set of LLM opponents for a human player.

**Status:** Phase 1, the deterministic bot. The turn loop and policies for settling, research, government, production, builders, trade and religion, defence and diplomacy are built and tested offline. Live acceptance is still in progress. This is a personal experiment and anything here may change.

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
