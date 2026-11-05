"""Turn the fetched CivBench tables into the baseline summary in docs/civbench-baselines.md.

Run scripts/fetch_civbench.py first. Only aggregates reach the summary, so no dataset rows get
committed. Only admissible games are used, which is the same 23-game cut as the CivBench paper.
"""

import argparse
import json
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from fetch_civbench import DEFAULT_ROOT, REVISION

OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "civbench-baselines.md"
MILESTONE_TURNS = (50, 100, 150, 200, 250)
MILESTONE_METRICS = ("score", "cities", "science", "culture", "techs_completed")
PLAYER_COLUMNS = ("runId", "pid", "civ", "is_agent", "turn", *MILESTONE_METRICS)
CALL_COLUMNS = ("runId", "type", "tool", "category", "success")
TOP_TOOLS = 15
PAPER_COST_LOW, PAPER_COST_HIGH = 31, 229
MODEL_NAMES = {
    "claude-opus-4-6": "Claude Opus 4.6",
    "gemini-3.1-pro-preview": "Gemini 3.1 Pro",
    "gpt-5.4": "GPT-5.4",
    "Kimi-K2.5": "Kimi K2.5",
}
SCENARIO_NAMES = {"ground_control": "Ground Control", "snowflake": "Snowflake"}


@dataclass(frozen=True)
class GameResult:
    run_id: str
    model: str
    scenario: str
    won: bool
    victory_type: str
    final_turn: int
    players: int
    rank: int
    normalised_score: float


def normalise_civ(name: str) -> str:
    """Match outcome names such as CIVILIZATION_JAPAN to player rows such as Japan."""
    return name.removeprefix("CIVILIZATION_").replace("_", " ").casefold()


def latest_rows(rows: Iterable[dict]) -> list[dict]:
    """Keep one row per run, turn and player. The tables repeat some, and the last one wins."""
    return list({(row["runId"], row["turn"], row["pid"]): row for row in rows}.values())


def last_agent_turn(rows: list[dict]) -> int:
    return max(row["turn"] for row in rows if row["is_agent"])


def final_standings(rows: list[dict], winner_civ: str) -> tuple[int, int, float]:
    """Return the agent's rank, the player count and its score relative to the winner.

    The winner ranks first even when another player has more points, because science and culture
    wins can come from behind. Everyone else ranks by score, and ties share the better rank.
    Players missing from the final turn were eliminated, so they rank below those still in.

    The final turn is the agent's last one with data. Some games have stray rows after it.
    """
    final_turn = last_agent_turn(rows)
    final = [row for row in rows if row["turn"] == final_turn]
    winner_key = normalise_civ(winner_civ)
    winners = [row for row in final if normalise_civ(row["civ"]) == winner_key]
    if len(winners) != 1:
        raise ValueError(f"Winner {winner_civ!r} isn't one player on turn {final_turn}")
    winner = winners[0]
    agent = next(row for row in final if row["is_agent"])

    if agent["pid"] == winner["pid"]:
        rank = 1
    else:
        others = [row for row in final if row["pid"] != winner["pid"]]
        rank = 2 + sum(row["score"] > agent["score"] for row in others)
    players = len({row["pid"] for row in rows})
    return rank, players, agent["score"] / winner["score"]


def game_result(game: dict, rows: list[dict]) -> GameResult:
    outcome = json.loads(game["outcome"])
    rank, players, normalised = final_standings(rows, outcome["winnerCiv"])
    won = outcome["result"] == "victory"
    return GameResult(
        run_id=game["runId"],
        model=game["agentModel"],
        scenario=game["scenarioId"],
        won=won,
        victory_type=outcome["victoryType"],
        final_turn=last_agent_turn(rows),
        players=players,
        rank=rank,
        normalised_score=1.0 if won else normalised,
    )


def milestones(rows: list[dict], turns: Iterable[int] = MILESTONE_TURNS) -> list[dict]:
    """Median stats at each turn for agents and built-in AIs, per run then across runs.

    Only runs with data for a turn count towards it, so later turns favour longer games.
    """
    by_key: defaultdict[tuple[int, bool, str], list[dict]] = defaultdict(list)
    wanted = set(turns)
    for row in rows:
        if row["turn"] in wanted:
            by_key[(row["turn"], bool(row["is_agent"]), row["runId"])].append(row)

    summary = []
    for turn in sorted(wanted):
        for is_agent in (True, False):
            groups = [v for (t, a, _), v in by_key.items() if t == turn and a == is_agent]
            if not groups:
                continue
            medians = {
                metric: statistics.median(
                    statistics.mean(row[metric] or 0 for row in group) for group in groups
                )
                for metric in MILESTONE_METRICS
            }
            summary.append({"turn": turn, "is_agent": is_agent, "runs": len(groups), **medians})
    return summary


def call_stats(calls: list[dict], turns_by_run: dict[str, int]) -> dict:
    """Summarise agent tool calls. Rows typed error or game_over aren't calls."""
    tool_calls = [call for call in calls if call["type"] == "tool_call"]
    per_game = Counter(call["runId"] for call in tool_calls)
    per_turn = [per_game[run] / turns for run, turns in turns_by_run.items()]
    return {
        "total": len(tool_calls),
        "per_game": statistics.median(per_game[run] for run in turns_by_run),
        "mean_per_game": statistics.mean(per_game[run] for run in turns_by_run),
        "per_turn": statistics.median(per_turn),
        "failed_share": sum(not call["success"] for call in tool_calls) / len(tool_calls),
        "categories": Counter(call["category"] for call in tool_calls),
        "tools": Counter(call["tool"] for call in tool_calls),
    }


def scenario_label(scenario: str) -> str:
    return SCENARIO_NAMES.get(scenario, scenario)


def model_label(model: str) -> str:
    return MODEL_NAMES.get(model, model)


def outcome_table(results: list[GameResult]) -> list[str]:
    lines = [
        "| Model | Scenario | Games | Wins | Median final turn | Median rank | "
        "Median score vs winner |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    groups: defaultdict[tuple[str, str], list[GameResult]] = defaultdict(list)
    for result in results:
        groups[(result.scenario, result.model)].append(result)
    for (scenario, model), group in sorted(groups.items()):
        lines.append(
            f"| {model_label(model)} | {scenario_label(scenario)} | {len(group)} "
            f"| {sum(g.won for g in group)} "
            f"| {statistics.median(g.final_turn for g in group):.0f} "
            f"| {statistics.median(g.rank for g in group):g} of {group[0].players} "
            f"| {statistics.median(g.normalised_score for g in group):.2f} |"
        )
    return lines


def milestone_table(rows: list[dict]) -> list[str]:
    lines = [
        "| Turn | Player | Games | Score | Cities | Science | Culture | Techs |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        who = "LLM" if row["is_agent"] else "Built-in AI"
        lines.append(
            f"| {row['turn']} | {who} | {row['runs']} | {row['score']:.0f} | {row['cities']:.1f} "
            f"| {row['science']:.0f} | {row['culture']:.0f} | {row['techs_completed']:.0f} |"
        )
    return lines


def render(results: list[GameResult], by_scenario: dict[str, list[dict]], calls: dict) -> str:
    wins = sum(result.won for result in results)
    victories = Counter(result.victory_type for result in results)
    win_types = Counter(result.victory_type for result in results if result.won)
    low = PAPER_COST_LOW / calls["mean_per_game"]
    high = PAPER_COST_HIGH / calls["mean_per_game"]
    lines = [
        "# CivBench baselines",
        "",
        "`scripts/summarise_civbench.py` generates this page, so change the script, not the "
        "page. It sets a frontier-model baseline for the bot to beat, without paying for "
        "frontier games.",
        "",
        "Source: [CivBench](https://huggingface.co/datasets/civbench/civbench-v1), revision "
        f"`{REVISION[:12]}`, by Andrews, Wilkinson, Heagerty, Coppock, Foerster and Ponte Costa "
        "([arXiv 2609.02459](https://arxiv.org/abs/2609.02459)), used under "
        "[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). This page holds aggregates "
        "derived from that data, and the ranks are this project's own calculation.",
        "",
        "## How far this compares",
        "",
        "- Our standard game has 6 players on a Small map at Prince. Ground Control has "
        "8 players on a Standard Pangaea map at Prince. Snowflake has 6 players on a Small map "
        "at King, with play restricted to military victory.",
        "- Neither scenario matches ours, so treat these as a reference level, not a pass mark. "
        "M1 is still measured on our own standard games.",
        "- The sample is small. The paper calls it a pilot and says it can't support a stable "
        "model ranking, so the model table below isn't a leaderboard.",
        "",
        "## Outcomes",
        "",
        f"The LLM won {wins} of the {len(results)} admissible games ("
        + ", ".join(f"{count} {kind}" for kind, count in win_types.most_common())
        + "). Victory types across all games: "
        + ", ".join(f"{kind} {count}" for kind, count in victories.most_common())
        + ". The dataset calls a science victory Technology.",
        "",
        *outcome_table(results),
        "",
        "Rank puts the winner first and orders everyone else by final score. CivBench doesn't "
        "publish a rank, and M1 needs one. Score vs winner follows the paper: the agent's score "
        "divided by the winner's, and 1.0 for a win. Final turn is the agent's last turn with "
        "data.",
        "",
        "The tables don't always agree with each other. In some games the outcome's turn "
        "differs from the last turn of player data, one game has stray rows long after it "
        "ended, and the score column in the games table is stale for some games. These figures "
        "therefore come from the player rows, and only the winner and victory type come from "
        "the outcome.",
        "",
        "## Progress by turn",
        "",
        "Medians across games, using the mean of the built-in AIs within each game. Games count "
        "only for turns they have data for. Some have gaps, and later turns lean towards longer "
        "games.",
        "",
    ]
    for scenario, rows in sorted(by_scenario.items()):
        lines += [f"### {scenario_label(scenario)}", "", *milestone_table(rows), ""]
    lines += [
        "## Decisions",
        "",
        f"Agents made {calls['total']:,} tool calls: a median of {calls['per_game']:,.0f} a game "
        f"and {calls['per_turn']:.1f} a turn. {calls['failed_share']:.1%} of calls failed.",
        "",
        "| Kind | Calls | Share |",
        "|---|---:|---:|",
    ]
    for kind, count in calls["categories"].most_common():
        lines.append(f"| {kind} | {count:,} | {count / calls['total']:.1%} |")
    lines += ["", "| Tool | Calls | Share |", "|---|---:|---:|"]
    for tool, count in calls["tools"].most_common(TOP_TOOLS):
        lines.append(f"| `{tool}` | {count:,} | {count / calls['total']:.1%} |")
    lines += [
        "",
        "## Cost",
        "",
        f"The paper reports ${PAPER_COST_LOW}-{PAPER_COST_HIGH} in API fees per full game, with "
        f"no split by model. Across a mean of {calls['mean_per_game']:,.0f} tool calls a game, "
        f"that's very roughly ${low:.3f}-{high:.3f} per call, so at that rate $1 buys about "
        f"{1 / high:.0f}-{1 / low:.0f} calls.",
        "",
        "This page gives no tokens per game. Only 49 of the 238 Inspect logs carry usage "
        "figures, most don't name their run, and runs overlapped in time, so matching logs to "
        "games by time gives wrong answers. The logs do show single sessions reaching the "
        "1 billion token limit, and a game can span several sessions.",
        "",
    ]
    return "\n".join(lines)


def load_table(path: Path, columns: Iterable[str] | None = None) -> list[dict]:
    import pyarrow.parquet as parquet  # Dev-only dependency, needed just for this script

    return parquet.read_table(path, columns=list(columns) if columns else None).to_pylist()


def build(tables: Path) -> str:
    games = [game for game in load_table(tables / "games.parquet") if game["admissible"]]
    runs = {game["runId"] for game in games}
    rows = latest_rows(
        row
        for row in load_table(tables / "player_rows.parquet", PLAYER_COLUMNS)
        if row["runId"] in runs
    )
    calls = [
        call
        for call in load_table(tables / "tool_calls.parquet", CALL_COLUMNS)
        if call["runId"] in runs
    ]

    rows_by_run: defaultdict[str, list[dict]] = defaultdict(list)
    for row in rows:
        rows_by_run[row["runId"]].append(row)
    results = [game_result(game, rows_by_run[game["runId"]]) for game in games]

    scenario_rows: defaultdict[str, list[dict]] = defaultdict(list)
    for game in games:
        scenario_rows[game["scenarioId"]] += rows_by_run[game["runId"]]
    by_scenario = {scenario: milestones(group) for scenario, group in scenario_rows.items()}
    turns_by_run = {result.run_id: result.final_turn for result in results}
    return render(results, by_scenario, call_stats(calls, turns_by_run))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="Download folder")
    args = parser.parse_args()
    page = build(args.root / REVISION[:12] / "tables")
    OUTPUT.write_text(page, encoding="utf-8", newline="\n")
    print(f"Wrote {OUTPUT}")


if __name__ == "__main__":
    main()
