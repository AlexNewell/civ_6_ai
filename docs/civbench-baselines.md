# CivBench baselines

`scripts/summarise_civbench.py` generates this page, so change the script, not the page. It sets a frontier-model baseline for the bot to beat, without paying for frontier games.

Source: [CivBench](https://huggingface.co/datasets/civbench/civbench-v1), revision `2d9937d1e273`, by Andrews, Wilkinson, Heagerty, Coppock, Foerster and Ponte Costa ([arXiv 2609.02459](https://arxiv.org/abs/2609.02459)), used under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). This page holds aggregates derived from that data, and the ranks are this project's own calculation.

## How far this compares

- Our standard game has 6 players on a Small map at Prince. Ground Control has 8 players on a Standard Pangaea map at Prince. Snowflake has 6 players on a Small map at King, with play restricted to military victory.
- Neither scenario matches ours, so treat these as a reference level, not a pass mark. M1 is still measured on our own standard games.
- The sample is small. The paper calls it a pilot and says it can't support a stable model ranking, so the model table below isn't a leaderboard.

## Outcomes

The LLM won 3 of the 23 admissible games (3 Technology). Victory types across all games: Technology 12, Culture 5, Score 4, Religious 1, Domination 1. The dataset calls a science victory Technology.

| Model | Scenario | Games | Wins | Median final turn | Median rank | Median score vs winner |
|---|---|---:|---:|---:|---:|---:|
| Kimi K2.5 | Ground Control | 1 | 0 | 219 | 8 of 8 | 0.48 |
| Claude Opus 4.6 | Ground Control | 6 | 2 | 303 | 6.5 of 8 | 0.64 |
| Gemini 3.1 Pro | Ground Control | 6 | 1 | 300 | 6 of 8 | 0.65 |
| GPT-5.4 | Ground Control | 6 | 0 | 298 | 7.5 of 8 | 0.40 |
| Claude Opus 4.6 | Snowflake | 2 | 0 | 330 | 6 of 6 | 0.18 |
| GPT-5.4 | Snowflake | 2 | 0 | 321 | 5.5 of 6 | 0.26 |

Rank puts the winner first and orders everyone else by final score. CivBench doesn't publish a rank, and M1 needs one. Score vs winner follows the paper: the agent's score divided by the winner's, and 1.0 for a win. Final turn is the agent's last turn with data.

The tables don't always agree with each other. In some games the outcome's turn differs from the last turn of player data, one game has stray rows long after it ended, and the score column in the games table is stale for some games. These figures therefore come from the player rows, and only the winner and victory type come from the outcome.

## Progress by turn

Medians across games, using the mean of the built-in AIs within each game. Games count only for turns they have data for. Some have gaps, and later turns lean towards longer games.

### Ground Control

| Turn | Player | Games | Score | Cities | Science | Culture | Techs |
|---:|---|---:|---:|---:|---:|---:|---:|
| 50 | LLM | 17 | 98 | 2.0 | 10 | 8 | 14 |
| 50 | Built-in AI | 17 | 86 | 2.4 | 12 | 10 | 8 |
| 100 | LLM | 17 | 239 | 4.0 | 28 | 23 | 26 |
| 100 | Built-in AI | 17 | 254 | 4.6 | 48 | 35 | 21 |
| 150 | LLM | 19 | 369 | 6.0 | 46 | 39 | 37 |
| 150 | Built-in AI | 19 | 417 | 6.1 | 80 | 70 | 32 |
| 200 | LLM | 19 | 492 | 5.0 | 68 | 51 | 45 |
| 200 | Built-in AI | 19 | 585 | 7.6 | 119 | 121 | 43 |
| 250 | LLM | 15 | 659 | 8.0 | 107 | 68 | 57 |
| 250 | Built-in AI | 15 | 770 | 9.1 | 198 | 211 | 54 |

### Snowflake

| Turn | Player | Games | Score | Cities | Science | Culture | Techs |
|---:|---|---:|---:|---:|---:|---:|---:|
| 50 | LLM | 4 | 63 | 2.0 | 6 | 6 | 8 |
| 50 | Built-in AI | 4 | 84 | 1.6 | 8 | 12 | 8 |
| 100 | LLM | 4 | 152 | 3.5 | 22 | 14 | 18 |
| 100 | Built-in AI | 4 | 228 | 3.2 | 41 | 31 | 21 |
| 150 | LLM | 4 | 238 | 4.0 | 36 | 20 | 26 |
| 150 | Built-in AI | 4 | 398 | 4.7 | 76 | 60 | 31 |
| 200 | LLM | 4 | 296 | 4.5 | 58 | 22 | 32 |
| 200 | Built-in AI | 4 | 572 | 6.1 | 107 | 113 | 41 |
| 250 | LLM | 4 | 301 | 3.5 | 35 | 16 | 40 |
| 250 | Built-in AI | 4 | 782 | 7.0 | 165 | 178 | 51 |

## Decisions

Agents made 103,205 tool calls: a median of 4,146 a game and 14.3 a turn. 8.6% of calls failed.

| Kind | Calls | Share |
|---|---:|---:|
| action | 52,257 | 50.6% |
| query | 40,710 | 39.4% |
| turn | 10,238 | 9.9% |

| Tool | Calls | Share |
|---|---:|---:|
| `unit_action` | 23,644 | 22.9% |
| `end_turn` | 10,238 | 9.9% |
| `set_city_production` | 8,841 | 8.6% |
| `get_units` | 6,222 | 6.0% |
| `skip_remaining_units` | 5,327 | 5.2% |
| `get_city_production` | 4,523 | 4.4% |
| `get_cities` | 4,438 | 4.3% |
| `get_notifications` | 3,736 | 3.6% |
| `get_game_overview` | 3,580 | 3.5% |
| `get_map_area` | 3,282 | 3.2% |
| `get_district_advisor` | 2,413 | 2.3% |
| `set_research` | 2,016 | 2.0% |
| `respond_to_diplomacy` | 1,696 | 1.6% |
| `city_attack` | 1,645 | 1.6% |
| `get_pending_diplomacy` | 1,545 | 1.5% |

## Cost

The paper reports $31-229 in API fees per full game, with no split by model. Across a mean of 4,487 tool calls a game, that's very roughly $0.007-0.051 per call, so at that rate $1 buys about 20-145 calls.

This page gives no tokens per game. Only 49 of the 238 Inspect logs carry usage figures, most don't name their run, and runs overlapped in time, so matching logs to games by time gives wrong answers. The logs do show single sessions reaching the 1 billion token limit, and a game can span several sessions.
