"""Work through a queue of games, with a stall watchdog and a JSON record of each one.

`--status` only reads the queue file. Playing needs `--live`, which connects to the open game
through run_game.py's turn loop, so it must never be passed by accident. The queue file is JSON
(see config/queue.example.json) and stays out of git. The default is runs/queue.json.

Launching and loading a save is not built yet (issue 17: a Lua load stops at the loading screen
until someone clicks). For now each item's save must already be open, and the game fails if a
different one is. Crash restart is not built either: a crashed game fails after the watchdog
gives up, and the next item then fails the same way until the game is restarted by hand.
"""

import argparse
import asyncio
import faulthandler
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from civ6_ai.orchestrator import TurnRecord
from civ6_ai.runner.queue import QueueError, QueueItem, RunQueue
from civ6_ai.runner.record import PlayOutcome
from civ6_ai.runner.runner import LaunchStep, PlayStep, RunnerDeps, run_queue
from civ6_ai.runner.watchdog import Progress

ROOT = Path(__file__).parent.parent
RUNS = ROOT / "runs"


async def attach_to_open_game(item: QueueItem) -> None:
    """The launch step for now: do nothing, and expect the item's save to be open already."""
    logging.getLogger(__name__).info("Expecting %s to be open already", item.save)


def live_play_step() -> PlayStep:
    """The play step that drives the real game. Imported late, so tests never reach it."""
    import run_game

    async def play(item: QueueItem, progress: Progress) -> PlayOutcome:
        def on_turn(record: TurnRecord) -> None:
            progress.beat(record.turn, record)

        played = await run_game.play_game(item.turns, expect_save=item.save, on_turn=on_turn)
        if played is None:
            return PlayOutcome("failed", detail=f"the open game is not {item.save}")
        paths = [
            played.turn_log.path,
            played.decisions.path,
            played.turn_log.path.with_name(played.turn_log.path.stem + "_events.jsonl"),
            played.turn_log.path.with_name(played.turn_log.path.stem + "_bot.log"),
        ]
        last = played.turn_log.records[-1].detail if played.turn_log.records else ""
        return PlayOutcome(
            played.reason,
            played.turn_log.records,
            [path.relative_to(ROOT).as_posix() for path in paths],
            last,
        )

    return play


def status_lines(queue: RunQueue) -> list[str]:
    return [
        f"{i.id}: {i.status} ({i.save}, {i.turns} turns, attempts {i.attempts}/{i.max_attempts})"
        + (f", {i.result}" if i.result else "")
        + (f" - {i.note}" if i.note else "")
        for i in queue.items
    ]


STACK_DUMP_EVERY_SECONDS = 150


async def _dump_stacks(path: Path) -> None:
    """Every few minutes, write where each task is waiting, to find what stalls a held turn.

    Run 4's end-turn watcher went silent on turns 222 and 256 with the game stuck. A thread dump
    (faulthandler, below) shows a blocked event loop. This shows what each task awaits when the
    loop is alive but nothing is moving.
    """
    while True:
        await asyncio.sleep(STACK_DUMP_EVERY_SECONDS)
        with path.open("a", encoding="utf-8") as out:
            out.write(f"=== {datetime.now(UTC).isoformat()} tasks ===\n")
            for task in asyncio.all_tasks():
                out.write(f"-- {task.get_name()} done={task.done()}\n")
                task.print_stack(file=out)


async def run(queue_path: Path, max_items: int | None, launch: LaunchStep, play: PlayStep) -> int:
    dump_path = queue_path.parent / "stack_dumps.txt"
    dumper = asyncio.ensure_future(_dump_stacks(dump_path))
    dump_file = dump_path.open("a", encoding="utf-8")
    faulthandler.dump_traceback_later(STACK_DUMP_EVERY_SECONDS, repeat=True, file=dump_file)
    try:
        return await _run(queue_path, max_items, launch, play)
    finally:
        faulthandler.cancel_dump_traceback_later()
        dumper.cancel()
        dump_file.close()


async def _run(queue_path: Path, max_items: int | None, launch: LaunchStep, play: PlayStep) -> int:
    queue = RunQueue.load(queue_path)
    deps = RunnerDeps(launch=launch, play=play, runs_dir=queue_path.parent)
    records = await run_queue(queue, deps, max_items=max_items)
    for record in records:
        print(f"{record.item_id}: {record.result.kind} {record.result.reason}".strip())
    return 0 if all(record.ok for record in records) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, default=RUNS / "queue.json")
    parser.add_argument("--status", action="store_true", help="show the queue and exit")
    parser.add_argument("--live", action="store_true", help="play the queue in the open game")
    parser.add_argument("--max-items", type=int, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        if args.status:
            print("\n".join(status_lines(RunQueue.load(args.queue))))
            return 0
        if not args.live:
            print("Nothing done. Use --status to read the queue, or --live to play it.")
            return 2
        return asyncio.run(run(args.queue, args.max_items, attach_to_open_game, live_play_step()))
    except QueueError as error:
        print(f"Queue problem: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
