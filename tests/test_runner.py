import asyncio
import functools
import json
import logging
from pathlib import Path

import pytest

import run_queue as cli
from civ6_ai.orchestrator import StopReason, TurnRecord
from civ6_ai.runner.clock import FakeClock
from civ6_ai.runner.queue import QueueError, QueueItem, RunQueue, Status
from civ6_ai.runner.record import PlayOutcome, classify
from civ6_ai.runner.runner import RunnerDeps, run_item, run_queue
from civ6_ai.runner.watchdog import Progress, StallThresholds, Step, Watchdog

ROOT = Path(__file__).parent.parent


def sync(test):
    """Run an async test with asyncio.run, since there is no async plugin."""

    @functools.wraps(test)
    def wrapper(*args, **kwargs):
        return asyncio.run(test(*args, **kwargs))

    return wrapper


def turn(n: int, total: float = 30.0, end: float = 20.0) -> TurnRecord:
    return TurnRecord(n, {}, {}, end, total)


def make_queue(tmp_path: Path, *items: dict) -> RunQueue:
    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"version": 1, "items": list(items)}), encoding="utf-8")
    return RunQueue.load(path)


def item(id: str = "a", **extra) -> dict:
    return {"id": id, "save": "STD_T1_01", "turns": 5, **extra}


class Fakes:
    """A launch step and play step with scripted behaviour, and a record of the calls."""

    def __init__(self, clock: FakeClock, plays: list) -> None:
        self.clock = clock
        self.plays = list(plays)
        self.launched: list[str] = []
        self.recovered = 0

    async def launch(self, queue_item: QueueItem) -> None:
        self.launched.append(queue_item.id)

    async def play(self, queue_item: QueueItem, progress: Progress) -> PlayOutcome:
        step = self.plays.pop(0)
        return await step(progress, self.clock)

    async def recover(self) -> None:
        self.recovered += 1

    def deps(self, tmp_path: Path, **kw) -> RunnerDeps:
        return RunnerDeps(
            self.launch,
            self.play,
            tmp_path,
            clock=self.clock,
            thresholds=StallThresholds(100, 150, 300, poll_every=10),
            recover=self.recover,
            **kw,
        )


def plays_turns(count: int, reason: str = StopReason.TURN_LIMIT, detail: str = ""):
    async def play(progress: Progress, clock: FakeClock) -> PlayOutcome:
        records = []
        for n in range(1, count + 1):
            await clock.sleep(30)
            records.append(turn(n))
            progress.beat(n, records[-1])
        return PlayOutcome(reason, records, ["runs/x.jsonl"], detail)

    return play


async def never_progresses(progress: Progress, clock: FakeClock) -> PlayOutcome:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


async def raises(progress: Progress, clock: FakeClock) -> PlayOutcome:
    raise RuntimeError("boom")


# The queue


def test_queue_round_trips_and_rejects_bad_input(tmp_path):
    queue = make_queue(tmp_path, item("a"), item("b", options={"x": 1}))
    queue.save()
    assert [i.id for i in RunQueue.load(queue.path).items] == ["a", "b"]
    assert not list(tmp_path.glob("*.tmp"))
    bad = [
        item("a/../b"),
        {**item(), "save": "x y"},
        {**item(), "turns": 0},
        {**item(), "turns": "5"},
        {**item(), "extra": 1},
        {**item(), "status": "weird"},
        {"id": "a", "save": "S"},
    ]
    for entry in bad:
        with pytest.raises(QueueError):
            make_queue(tmp_path, entry)
    with pytest.raises(QueueError, match="unique"):
        make_queue(tmp_path, item("a"), item("a"))


def test_example_queue_loads_and_uses_standard_saves():
    queue = RunQueue.load(ROOT / "config" / "queue.example.json")
    names = {
        s["name"] for s in json.loads((ROOT / "config/standard_saves.json").read_text())["saves"]
    }
    assert queue.items and all(i.save in names for i in queue.items)


def test_interrupted_running_item_is_retried_then_skipped(tmp_path):
    queue = make_queue(
        tmp_path,
        item("retry", status="running", attempts=1, pid=111),
        item("spent", status="running", attempts=2, pid=111),
        item("untouched"),
    )
    changed = queue.recover_interrupted(alive=lambda pid: False)
    assert [i.id for i in changed] == ["retry", "spent"]
    reloaded = RunQueue.load(queue.path)
    assert [i.status for i in reloaded.items] == [
        Status.PENDING,
        Status.INTERRUPTED,
        Status.PENDING,
    ]
    assert "will retry" in reloaded.items[0].note
    assert reloaded.next_pending().id == "retry"


def test_running_item_with_live_process_is_not_taken_over(tmp_path):
    queue = make_queue(tmp_path, item("a", status="running", attempts=1, pid=111))
    with pytest.raises(QueueError, match="live process"):
        queue.recover_interrupted(alive=lambda pid: True)


# The watchdog


def test_watchdog_escalates_once_each_and_resets_on_progress():
    clock = FakeClock()
    progress = Progress(clock)
    dog = Watchdog(progress, StallThresholds(100, 150, 300))
    assert dog.next_step() is Step.NONE
    clock.now = 100
    assert dog.next_step() is Step.WARN
    assert dog.next_step() is Step.NONE
    clock.now = 160
    assert dog.next_step() is Step.RECOVER
    progress.beat(7)
    clock.now = 270
    assert dog.next_step() is Step.WARN  # A new stall starts from the top.
    clock.now = 600
    assert dog.next_step() is Step.FAIL


def test_thresholds_must_be_ordered():
    with pytest.raises(ValueError):
        StallThresholds(100, 50, 300)


def test_default_threshold_is_above_run_3_slow_turns():
    assert StallThresholds().warn_after >= 2 * 10 * 60


@sync
async def test_stall_runs_hook_then_fails_game(tmp_path, caplog):
    clock = FakeClock()
    fakes = Fakes(clock, [never_progresses])
    queue = make_queue(tmp_path, item("a"))
    with caplog.at_level(logging.WARNING):
        record = await run_item(queue, queue.items[0], fakes.deps(tmp_path))
    assert fakes.recovered == 1
    assert record.result.kind == "failed" and "stalled" in record.result.reason
    assert queue.items[0].status is Status.FAILED
    assert any("No turn finished" in m for m in caplog.messages)


@sync
async def test_slow_but_progressing_game_is_left_alone(tmp_path):
    clock = FakeClock()

    async def slow(progress: Progress, clk: FakeClock) -> PlayOutcome:
        for n in range(1, 4):
            await clk.sleep(90)  # Under the 100s warn threshold each time.
            progress.beat(n, turn(n, 90))
        return PlayOutcome(StopReason.TURN_LIMIT, [turn(n, 90) for n in range(1, 4)])

    fakes = Fakes(clock, [slow])
    queue = make_queue(tmp_path, item("a"))
    record = await run_item(queue, queue.items[0], fakes.deps(tmp_path))
    assert record.result.kind == "turn_limit" and fakes.recovered == 0


@sync
async def test_failing_recovery_hook_does_not_stop_escalation(tmp_path):
    clock = FakeClock()
    fakes = Fakes(clock, [never_progresses])

    async def bad() -> None:
        raise RuntimeError("no")

    fakes.recover = bad
    queue = make_queue(tmp_path, item("a"))
    record = await run_item(queue, queue.items[0], fakes.deps(tmp_path))
    assert record.result.kind == "failed"


# Records and the runner


def test_classify():
    def out(reason, detail=""):
        return PlayOutcome(reason, detail=detail)

    assert classify(out(StopReason.TURN_LIMIT)).kind == "turn_limit"
    win = classify(out(StopReason.GAME_OVER, "GAME OVER — VICTORY! You won a science victory!"))
    assert (win.kind, win.victory_type) == ("victory", "science")
    loss = classify(
        out(StopReason.GAME_OVER, "GAME OVER — DEFEAT. X of Y won a domination victory.")
    )
    assert (loss.kind, loss.victory_type) == ("defeat", "domination")
    assert classify(out(StopReason.GAME_OVER, "")).kind == "game_over"
    hung = classify(out(StopReason.HUNG, "HANG: stuck"))
    assert hung.kind == "failed" and "hung" in hung.reason


@sync
async def test_queue_runs_items_in_order_and_writes_records(tmp_path):
    clock = FakeClock()
    fakes = Fakes(
        clock,
        [plays_turns(3), plays_turns(2, StopReason.BLOCKED, "stuck"), raises, plays_turns(1)],
    )
    queue = make_queue(tmp_path, item("a"), item("b"), item("c"), item("d"))
    records = await run_queue(queue, fakes.deps(tmp_path))
    assert fakes.launched == ["a", "b", "c", "d"]
    assert [i.status for i in queue.items] == [
        Status.DONE,
        Status.FAILED,
        Status.FAILED,
        Status.DONE,
    ]
    first = json.loads((tmp_path / "records" / "a_attempt1.json").read_text(encoding="utf-8"))
    assert first["turns_played"] == 3 and first["result"]["kind"] == "turn_limit"
    assert first["first_turn"] == 1 and first["last_turn"] == 3
    assert first["started_at"] < first["ended_at"] and first["log_paths"] == ["runs/x.jsonl"]
    assert first["timing"]["decision"]["mean"] == 10.0
    assert first["timing"]["turns"] == 3
    assert "boom" in records[2].result.reason
    assert not list(tmp_path.rglob("*.tmp"))


@sync
async def test_launch_failure_fails_the_item_and_skips_play(tmp_path):
    clock = FakeClock()
    fakes = Fakes(clock, [plays_turns(1)])

    async def launch(queue_item: QueueItem) -> None:
        raise RuntimeError("loading screen")

    deps = fakes.deps(tmp_path)
    deps.launch = launch
    queue = make_queue(tmp_path, item("a"))
    record = await run_item(queue, queue.items[0], deps)
    assert record.result.kind == "failed" and "loading screen" in record.result.reason
    assert len(fakes.plays) == 1


@sync
async def test_restart_mid_queue_resumes(tmp_path):
    clock = FakeClock()
    queue = make_queue(
        tmp_path,
        item("done", status="done", attempts=1),
        item("mid", status="running", attempts=1, pid=999_999),
        item("next"),
    )
    fakes = Fakes(clock, [plays_turns(1), plays_turns(1)])
    restarted = RunQueue.load(queue.path)
    restarted_records = await run_queue(restarted, fakes.deps(tmp_path))
    assert fakes.launched == ["mid", "next"]
    assert restarted.items[1].attempts == 2
    assert [r.attempt for r in restarted_records] == [2, 1]


@sync
async def test_max_items_limits_the_run(tmp_path):
    fakes = Fakes(FakeClock(), [plays_turns(1)])
    queue = make_queue(tmp_path, item("a"), item("b"))
    await run_queue(queue, fakes.deps(tmp_path), max_items=1)
    assert queue.items[1].status is Status.PENDING


# The CLI


def test_cli_never_plays_without_live(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "live_play_step", lambda: pytest.fail("reached the game"))
    queue = make_queue(tmp_path, item("a"))
    assert cli.main(["--queue", str(queue.path)]) == 2
    assert cli.main(["--queue", str(queue.path), "--status"]) == 0
    assert "a: pending" in capsys.readouterr().out


@sync
async def test_cli_run_with_fakes_uses_injected_steps(tmp_path):
    fakes = Fakes(FakeClock(), [plays_turns(2)])
    queue = make_queue(tmp_path, item("a"))
    code = await cli.run(queue.path, None, fakes.launch, fakes.play)
    assert code == 0 and fakes.launched == ["a"]
    assert (tmp_path / "records" / "a_attempt1.json").exists()


def test_cli_reports_a_bad_queue(tmp_path, capsys):
    path = tmp_path / "queue.json"
    path.write_text("{", encoding="utf-8")
    assert cli.main(["--queue", str(path), "--status"]) == 2
