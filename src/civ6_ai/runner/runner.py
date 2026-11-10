"""Work through the run queue, one game at a time.

Three steps touch the game, and all three are passed in:

- `launch(item)`: get the item's save loaded and past the loading screen. Not built yet. Issue 17
  found that a Lua save load stops at the loading screen until someone clicks, so a later live
  slice has to supply this. The CLI's default only checks nothing and expects the save to be open.
- `play(item, progress)`: play the turns and return a `PlayOutcome`. It calls `progress.beat` after
  each finished turn, which is what the watchdog watches.
- `recover()`: the watchdog's recovery hook. Optional.

Not covered here: restarting a crashed game, which needs `launch`.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from civ6_ai.runner.clock import Clock, SystemClock
from civ6_ai.runner.queue import QueueItem, RunQueue
from civ6_ai.runner.record import PlayOutcome, Result, RunRecord, classify, timing_summary
from civ6_ai.runner.watchdog import Progress, RecoveryHook, StallThresholds, Watchdog

log = logging.getLogger(__name__)

LaunchStep = Callable[[QueueItem], Awaitable[None]]
PlayStep = Callable[[QueueItem, Progress], Awaitable[PlayOutcome]]


@dataclass
class RunnerDeps:
    launch: LaunchStep
    play: PlayStep
    runs_dir: Path
    clock: Clock = SystemClock()
    thresholds: StallThresholds = StallThresholds()
    recover: RecoveryHook | None = None


def record_path(item: QueueItem) -> str:
    return f"records/{item.id}_attempt{item.attempts}.json"


async def run_item(queue: RunQueue, item: QueueItem, deps: RunnerDeps) -> RunRecord:
    """Run one item and write its record. Never raises for a failed game."""
    started_at = deps.clock.timestamp()
    queue.mark_running(item, started_at)
    progress = Progress(deps.clock)
    outcome: PlayOutcome | None = None
    try:
        await deps.launch(item)
        progress = Progress(deps.clock)  # The stall clock starts once the game is ready.
        game = asyncio.create_task(deps.play(item, progress))
        watchdog = Watchdog(progress, deps.thresholds, deps.recover)
        if await watchdog.supervise(game):
            result = Result("failed", reason=watchdog.reason)
        else:
            outcome = game.result()
            result = classify(outcome)
    except Exception as error:
        log.exception("Item %s failed", item.id)
        result = Result("failed", reason=f"error: {error!r}"[:300])

    records = list(outcome.turn_records) if outcome else list(progress.records)
    turns = [r.turn for r in records]
    run_record = RunRecord(
        item_id=item.id,
        save=item.save,
        attempt=item.attempts,
        started_at=started_at,
        ended_at=deps.clock.timestamp(),
        result=result,
        turns_played=len(records) if records else progress.turns,
        first_turn=min(turns) if turns else None,
        last_turn=max(turns) if turns else progress.last_turn,
        timing=timing_summary(records),
        log_paths=list(outcome.log_paths) if outcome else list(progress.log_paths),
    )
    path = record_path(item)
    run_record.write(deps.runs_dir / path)
    queue.mark_finished(
        item,
        run_record.ended_at,
        ok=run_record.ok,
        result=result.kind,
        record=path,
        note=result.reason,
    )
    return run_record


async def run_queue(
    queue: RunQueue, deps: RunnerDeps, *, max_items: int | None = None
) -> list[RunRecord]:
    """Recover from any earlier interruption, then run pending items in order."""
    for item in queue.recover_interrupted():
        log.warning("Item %s: %s", item.id, item.note)
    records: list[RunRecord] = []
    while max_items is None or len(records) < max_items:
        item = queue.next_pending()
        if item is None:
            break
        log.info("Starting %s (%s, %d turns)", item.id, item.save, item.turns)
        records.append(await run_item(queue, item, deps))
    return records
