"""Work through a queue of games, with a stall watchdog and a JSON record of each one.

`--status` only reads the queue file. Playing needs `--live`, which connects to the open game
through run_game.py's turn loop, so it must never be passed by accident. The queue file is JSON
(see config/queue.example.json) and stays out of git. The default is runs/queue.json.

Each item's save is loaded first with no one at the keyboard (`runner.loader`: the game's
automation auto-start skips the "Continue Game" button). An item may name a checkpoint to load
(`"load": "CKPT_STD_T1_01_T100"`) and checkpoints to write (`"options": {"save_at": [150]}`), so
a queue can play short slices of a game. Pass `--no-load` to expect the save to be open already.
Crash restart is not built: a crashed game fails after the watchdog gives up, and the next item
then fails the same way until the game is restarted by hand.
"""

import argparse
import asyncio
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
    """A launch step that does nothing and expects the item's save to be open already."""
    logging.getLogger(__name__).info("Expecting %s to be open already", item.save)


async def load_item_save(item: QueueItem) -> None:
    """The launch step: load the item's save (or its `load` checkpoint) with no one at the
    keyboard, from whatever game is open, the defeat screen included. See runner.loader."""
    from civ6_ai.runner.loader import load_save

    loaded = await load_save(item.load or item.save)
    logging.getLogger(__name__).info(
        "Loaded %s at turn %d in %.0fs", loaded.name, loaded.turn, loaded.seconds
    )


def live_play_step() -> PlayStep:
    """The play step that drives the real game. Imported late, so tests never reach it."""
    import run_game

    async def play(item: QueueItem, progress: Progress) -> PlayOutcome:
        def on_turn(record: TurnRecord) -> None:
            progress.beat(record.turn, record)

        played = await run_game.play_game(
            item.turns,
            expect_save=item.save,
            on_turn=on_turn,
            save_at=[int(t) for t in item.options.get("save_at", [])],
        )
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

    Run 4's end-turn watcher went silent on turns 222 and 256 with the game stuck. Each task's stack
    shows what it awaits while the loop is alive but nothing moves. (A faulthandler thread dump
    was tried and crashed the bot, exit 139, so it is not used.)
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
    try:
        return await _run(queue_path, max_items, launch, play)
    finally:
        dumper.cancel()


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
    parser.add_argument(
        "--no-load", action="store_true", help="do not load saves: expect each one to be open"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        if args.status:
            print("\n".join(status_lines(RunQueue.load(args.queue))))
            return 0
        if not args.live:
            print("Nothing done. Use --status to read the queue, or --live to play it.")
            return 2
        launch = attach_to_open_game if args.no_load else load_item_save
        return asyncio.run(run(args.queue, args.max_items, launch, live_play_step()))
    except QueueError as error:
        print(f"Queue problem: {error}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
