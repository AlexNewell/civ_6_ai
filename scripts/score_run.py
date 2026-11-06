"""Score one run as JSON: one of our turn logs, or a game from the fetched CivBench tables.

The metric definitions are in src/civ6_ai/metrics. Metrics a run can't support are listed under
`absent` with the reason, never given as zero. Nothing here connects to the game or the network.
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

from civ6_ai.metrics import RunData, Snapshot, Standing, add_spend, load_turn_log, score
from fetch_civbench import DEFAULT_ROOT, REVISION
from summarise_civbench import (
    CALL_COLUMNS,
    PLAYER_COLUMNS,
    GameResult,
    call_stats,
    game_result,
    last_agent_turn,
    latest_rows,
    load_table,
)

SOURCE = "civbench"
PLAYER_RUN_COLUMNS = (*PLAYER_COLUMNS, "gold_per_turn", "faith_per_turn", "game_speed")
CITY_COLUMNS = ("pid", "city_id", "turn", "food", "production")
NO_CALLS = "the tool_calls table has no calls for this run"
NO_CITY_ROWS = "the city_rows table has no row for the agent on its final turn"
NO_PER_RUN_COST = "CivBench reports only a cost range across all games, not a cost per run"
NO_COMBAT_COUNTS = (
    "CivBench has no kill or loss columns, and the tool text can't replace them: 'was killed' "
    "also fires for any unit that disappears, such as a settler founding a city"
)
GAPS = {
    "cost_usd": NO_PER_RUN_COST,
    "cost_unsettled_usd": NO_PER_RUN_COST,
    "kills": NO_COMBAT_COUNTS,
    "losses": NO_COMBAT_COUNTS,
    "handler_errors": "CivBench agents have no handlers, so failed calls are its error measure",
    "stop_reason": "CivBench records an outcome, not why the run stopped",
    "total_seconds": "CivBench doesn't time whole turns",
    "food_per_turn": NO_CITY_ROWS,
    "production_per_turn": NO_CITY_ROWS,
}


def city_yields(cities: list[dict], pid: int) -> dict[int, tuple[float, float]]:
    """Sum food and production across the player's cities, per turn. Repeats keep the last row."""
    latest = {(c["turn"], c["city_id"]): c for c in cities if c["pid"] == pid}
    totals: defaultdict[int, list[float]] = defaultdict(lambda: [0.0, 0.0])
    for city in latest.values():
        totals[city["turn"]][0] += city["food"]
        totals[city["turn"]][1] += city["production"]
    return {turn: (food, production) for turn, (food, production) in totals.items()}


def agent_snapshots(rows: list[dict], cities: list[dict]) -> tuple[Snapshot, ...]:
    """Build the agent's turns in order, so stray rows from other players can't be its final one."""
    agent_rows = sorted((row for row in rows if row["is_agent"]), key=lambda row: row["turn"])
    yields = city_yields(cities, agent_rows[0]["pid"]) if agent_rows else {}
    return tuple(
        Snapshot(
            turn=row["turn"],
            score=row["score"],
            cities=row["cities"],
            science=row["science"],
            culture=row["culture"],
            gold_per_turn=row["gold_per_turn"],
            faith_per_turn=row["faith_per_turn"],
            food_per_turn=yields.get(row["turn"], (None, None))[0],
            production_per_turn=yields.get(row["turn"], (None, None))[1],
            techs_completed=row["techs_completed"],
        )
        for row in agent_rows
    )


def result_of(game: dict, rows: list[dict]) -> tuple[GameResult | None, str]:
    """Return the game's result, or why it has none. Excluded games can lack an outcome."""
    if not game["outcome"]:
        return None, "the game has no recorded outcome"
    try:
        return game_result(game, rows), ""
    except ValueError as error:
        return None, f"the outcome doesn't fit the player rows: {error}"


def civbench_run(
    game: dict, rows: list[dict], calls: list[dict], cities: list[dict] | None = None
) -> RunData:
    """Convert one game's rows. The rows must already be for this run only."""
    rows = latest_rows(rows)
    result, why_no_result = result_of(game, rows)
    turns = last_agent_turn(rows) if any(row["is_agent"] for row in rows) else None
    has_calls = any(call["type"] == "tool_call" for call in calls)
    stats = call_stats(calls, {game["runId"]: turns or 1}) if has_calls else None
    speeds = {row["game_speed"] for row in rows if row.get("game_speed")}

    gaps = dict(GAPS)
    if result is None:
        gaps |= dict.fromkeys(
            ("won", "victory_type", "rank", "players", "score_vs_winner"), why_no_result
        )
    if stats is None:
        gaps |= {"decisions": NO_CALLS, "failed_decisions": NO_CALLS}
    info: dict[str, str | bool] = {
        "model": game["agentModel"],
        "scenario": game["scenarioId"],
        "admissible": bool(game["admissible"]),
    }
    if len(speeds) == 1:
        info["game_speed"] = speeds.pop()
    return RunData(
        run_id=game["runId"],
        source=SOURCE,
        turns_played=turns,
        won=result.won if result else None,
        victory_type=result.victory_type if result else None,
        standing=Standing(result.rank, result.players, result.normalised_score) if result else None,
        snapshots=agent_snapshots(rows, cities or []),
        decisions=stats["total"] if stats else None,
        failed_decisions=round(stats["failed_share"] * stats["total"]) if stats else None,
        info=info,
        missing=gaps,
    )


def run_rows(path: Path, run_id: str, columns: tuple[str, ...]) -> list[dict]:
    """Read one run's rows. `load_table` would read whole tables just to discard most of them."""
    import pyarrow.parquet as parquet  # Dev-only dependency, needed just for CivBench runs

    table = parquet.read_table(path, columns=list(columns), filters=[("runId", "=", run_id)])
    return table.to_pylist()


def load_civbench(tables: Path, run_id: str) -> RunData:
    if not tables.is_dir():
        raise ValueError(f"No CivBench tables at {tables}: run scripts/fetch_civbench.py first")
    games = [game for game in load_table(tables / "games.parquet") if game["runId"] == run_id]
    if not games:
        raise ValueError(f"CivBench has no run named {run_id}")
    return civbench_run(
        games[0],
        run_rows(tables / "player_rows.parquet", run_id, PLAYER_RUN_COLUMNS),
        run_rows(tables / "tool_calls.parquet", run_id, CALL_COLUMNS),
        run_rows(tables / "city_rows.parquet", run_id, CITY_COLUMNS),
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, nargs="?", help="our turn log, from runs/")
    parser.add_argument("--civbench", metavar="RUN_ID", help="score this CivBench run instead")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="CivBench download folder")
    parser.add_argument("--ledger", type=Path, help="spend ledger, to add a turn log's cost")
    parser.add_argument("--game-id", help="the run's game id in the spend ledger")
    parser.add_argument("--out", type=Path, help="write the JSON here instead of printing it")
    args = parser.parse_args(argv)
    if (args.log is None) == (args.civbench is None):
        parser.error("give either a turn log or --civbench RUN_ID")
    if args.civbench and (args.ledger or args.game_id):
        parser.error("--ledger and --game-id only apply to our own turn logs")
    if bool(args.ledger) != bool(args.game_id):
        parser.error("--ledger and --game-id go together")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.civbench:
            run = load_civbench(args.root / REVISION[:12] / "tables", args.civbench)
        else:
            run = load_turn_log(args.log)
            if args.ledger:
                run = add_spend(run, args.ledger, args.game_id)
    except (ValueError, OSError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1

    report = json.dumps(score(run), indent=2)
    if args.out:
        args.out.write_text(report + "\n", encoding="utf-8")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
