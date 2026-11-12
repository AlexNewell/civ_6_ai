import asyncio
import dataclasses
import json
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from civ6_ai import bot
from civ6_ai.monitors import EventBus, EventLog, event_from_json
from civ6_ai.orchestrator import TurnLog
from civ6_ai.strategy.tasks import (
    MAX_TASKS,
    METRICS,
    Status,
    TaskDB,
    TaskError,
    TaskHandler,
    TaskOverdue,
    metrics_from_overview,
)
from fixtures import civ_state as fx


def run(coro):
    return asyncio.run(coro)


def figures(**changes):
    return {name: 0 for name in METRICS} | changes


# Adding and reading


def test_add_assigns_increasing_ids_and_defaults():
    db = TaskDB()
    first = db.add("Settle", turn=1)
    second = db.add("Expand", turn=1, priority=8, deadline=50, metric="cities", value=6)
    assert (first.id, second.id) == (1, 2)
    assert first.priority == 5 and first.deadline is None and first.metric is None
    assert db.get(2).deadline == 50


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"priority": 10},
        {"priority": -1},
        {"priority": 2.5},
        {"priority": True},
        {"deadline": 5},  # Before the turn it was created.
        {"deadline": -1},
        {"metric": "mana", "value": 1},
        {"metric": "cities", "op": "=="},
        {"metric": "cities", "value": float("nan")},
        {"metric": "cities", "value": float("inf")},
        {"metric": "cities", "value": True},
        {"depends_on": (99,)},
        {"depends_on": (1, 1)},
        {"depends_on": "1"},
        {"depends_on": (True,)},
        {"note": 5},
    ],
)
def test_invalid_tasks_are_refused_and_change_nothing(kwargs):
    db = TaskDB()
    db.add("Base", turn=10)
    args = {"title": "Task", "turn": 10, **kwargs}
    with pytest.raises(TaskError):
        db.add(**args)
    assert [t.id for t in db.tasks()] == [1]
    assert db.add("Next", turn=10).id == 2  # The refused task used no id.


def test_cannot_depend_on_a_cancelled_task():
    db = TaskDB()
    task = db.add("A", turn=1)
    db.cancel(task.id)
    with pytest.raises(TaskError):
        db.add("B", turn=1, depends_on=(task.id,))


def test_task_limit():
    db = TaskDB()
    for _ in range(MAX_TASKS):
        db.add("t", turn=1)
    with pytest.raises(TaskError):
        db.add("one more", turn=1)


def test_unknown_task_lookup():
    with pytest.raises(TaskError):
        TaskDB().get(7)


def test_open_tasks_order_by_priority_then_deadline_then_age():
    db = TaskDB()
    db.add("low", turn=1, priority=1)
    db.add("late high", turn=1, priority=9, deadline=90)
    db.add("early high", turn=1, priority=9, deadline=40)
    db.add("no deadline high", turn=1, priority=9)
    db.add("also low", turn=1, priority=1)
    assert [t.title for t in db.open_tasks()] == [
        "early high",
        "late high",
        "no deadline high",
        "low",
        "also low",
    ]


# Completing


def test_task_is_done_when_its_target_is_met():
    db = TaskDB()
    db.add("Six cities", turn=1, metric="cities", value=6)
    db.check(10, figures(cities=5))
    assert db.get(1).is_open
    db.check(11, figures(cities=6))
    task = db.get(1)
    assert task.status is Status.DONE and task.completed_turn == 11 and not task.done_late


def test_less_than_or_equal_targets():
    db = TaskDB()
    db.add("Keep unit count down", turn=1, metric="units", op="<=", value=10)
    db.check(2, figures(units=12))
    assert db.get(1).is_open
    db.check(3, figures(units=10))
    assert db.get(1).status is Status.DONE


def test_task_without_a_target_is_only_done_by_hand():
    db = TaskDB()
    db.add("Make peace", turn=1)
    db.check(5, figures(cities=99))
    assert db.get(1).is_open
    db.complete(1, 6)
    assert db.get(1).completed_turn == 6


def test_missing_metric_figure_does_not_complete():
    db = TaskDB()
    db.add("Gold", turn=1, metric="gold", value=0)
    db.check(2, {})
    assert db.get(1).is_open


def test_dependents_wait_and_then_finish_in_the_same_check():
    db = TaskDB()
    db.add("Cities", turn=1, metric="cities", value=4)
    db.add("Gold", turn=1, metric="gold", value=100, depends_on=(1,))
    db.add("Score", turn=1, metric="score", value=10, depends_on=(2,))
    db.check(5, figures(gold=500, score=50, cities=1))
    assert [t.is_open for t in db.tasks()] == [True, True, True]
    assert db.waiting_on(db.get(2)) == [1]
    assert [t.id for t in db.ready_tasks()] == [1]
    db.check(6, figures(gold=500, score=50, cities=4))
    assert [t.status for t in db.tasks()] == [Status.DONE] * 3


def test_complete_by_hand_respects_dependencies_and_state():
    db = TaskDB()
    db.add("A", turn=1)
    db.add("B", turn=1, depends_on=(1,))
    with pytest.raises(TaskError):
        db.complete(2, 3)
    db.complete(1, 3)
    db.complete(2, 4)
    with pytest.raises(TaskError):
        db.complete(2, 5)


def test_cancel_rules():
    db = TaskDB()
    db.add("A", turn=1)
    db.add("B", turn=1, depends_on=(1,))
    with pytest.raises(TaskError):
        db.cancel(1)  # B waits on it.
    db.cancel(2)
    db.cancel(1)
    assert [t.status for t in db.tasks()] == [Status.CANCELLED] * 2
    with pytest.raises(TaskError):
        db.cancel(1)
    db.check(50, figures())  # Cancelled tasks are never checked or flagged.


# Deadlines


def test_overdue_raises_one_event_after_the_deadline():
    db = TaskDB()
    db.add("Six cities", turn=1, priority=7, deadline=20, metric="cities", value=6)
    assert db.check(20, figures(cities=3)) == []  # The deadline turn itself is still on time.
    events = db.check(21, figures(cities=3))
    assert events == [TaskOverdue(turn=21, task_id=1, title="Six cities", deadline=20, priority=7)]
    assert db.check(22, figures(cities=3)) == []  # Once only.
    assert db.get(1).is_open and db.get(1).overdue_turn == 21


def test_overdue_task_can_still_finish_late():
    db = TaskDB()
    db.add("Six cities", turn=1, deadline=20, metric="cities", value=6)
    db.check(21, figures(cities=3))
    db.check(30, figures(cities=6))
    task = db.get(1)
    assert task.status is Status.DONE and task.done_late


def test_meeting_the_target_on_the_first_late_turn_is_not_overdue():
    db = TaskDB()
    db.add("Six cities", turn=1, deadline=20, metric="cities", value=6)
    assert db.check(21, figures(cities=6)) == []
    assert db.get(1).done_late


def test_tasks_without_deadline_are_never_overdue():
    db = TaskDB()
    db.add("Someday", turn=1)
    assert db.check(10_000, figures()) == []


def test_several_overdue_at_once_and_blocked_tasks_still_go_overdue():
    db = TaskDB()
    db.add("A", turn=1, deadline=5)
    db.add("B", turn=1, deadline=6, depends_on=(1,))
    db.add("C", turn=1, deadline=50)
    events = db.check(7, figures())
    assert [e.task_id for e in events] == [1, 2]


def test_turn_going_backwards_keeps_task_state():
    db = TaskDB()
    db.add("A", turn=1, deadline=5)
    db.check(9, figures())
    assert db.check(3, figures()) == []
    assert db.get(1).overdue_turn == 9


def test_overdue_event_round_trips_through_the_event_log(tmp_path):
    event = TaskOverdue(turn=21, task_id=1, title="Six cities", deadline=20, priority=7)
    EventLog(tmp_path / "events.jsonl").write(event)
    line = (tmp_path / "events.jsonl").read_text(encoding="utf-8").strip()
    assert json.loads(line)["kind"] == "task_overdue"
    assert event_from_json(line) == event


# Persistence


def populated(path):
    db = TaskDB(path)
    db.add("Six cities", turn=1, priority=8, deadline=40, metric="cities", value=6, note="n")
    db.add("Gold", turn=1, metric="gold", op=">=", value=250.5, depends_on=(1,))
    db.add("Peace", turn=2)
    db.check(41, figures(cities=2))  # Task 1 goes overdue.
    db.cancel(3)
    return db


def test_state_survives_a_restart(tmp_path):
    path = tmp_path / "state" / "tasks.json"
    before = populated(path)
    after = TaskDB(path)
    assert after.tasks() == before.tasks()
    assert after.get(1).overdue_turn == 41
    assert after.get(3).status is Status.CANCELLED
    assert after.add("Next", turn=50).id == 4  # Ids carry on, none reused.
    assert after.check(60, figures(cities=2)) == []  # The overdue event isn't raised twice.


def test_state_survives_a_real_process_restart(tmp_path):
    path = tmp_path / "tasks.json"
    script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from civ6_ai.strategy.tasks import TaskDB
        db = TaskDB(Path(sys.argv[1]))
        if sys.argv[2] == "write":
            db.add("Six cities", turn=1, deadline=10, metric="cities", value=6)
            db.check(11, {"cities": 1})
        else:
            task = db.get(1)
            print(task.title, task.overdue_turn, len(db.check(12, {"cities": 1})))
        """
    )
    for mode in ("write", "read"):
        result = subprocess.run(
            [sys.executable, "-c", script, str(path), mode],
            capture_output=True,
            text=True,
            check=True,
        )
    assert result.stdout.split() == ["Six", "cities", "11", "0"]


def test_progress_is_saved_after_every_change(tmp_path):
    path = tmp_path / "tasks.json"
    db = TaskDB(path)
    db.add("A", turn=1, metric="cities", value=2)
    assert len(json.loads(path.read_text(encoding="utf-8"))["tasks"]) == 1
    db.check(2, figures(cities=2))
    assert json.loads(path.read_text(encoding="utf-8"))["tasks"][0]["status"] == "done"
    assert not list(tmp_path.glob("*.tmp"))


def test_missing_file_starts_empty(tmp_path):
    db = TaskDB(tmp_path / "none.json")
    assert db.tasks() == []
    assert not (tmp_path / "none.json").exists()  # Nothing is written until something changes.


def good_file():
    return {
        "version": 1,
        "next_id": 3,
        "tasks": [
            {"id": 1, "title": "A", "created_turn": 1},
            {"id": 2, "title": "B", "created_turn": 1, "depends_on": [1]},
        ],
    }


def test_hand_written_file_loads(tmp_path):
    path = tmp_path / "tasks.json"
    path.write_text(json.dumps(good_file()), encoding="utf-8")
    db = TaskDB(path)
    assert [t.id for t in db.tasks()] == [1, 2]
    assert db.get(2).depends_on == (1,)
    assert db.add("C", turn=3).id == 3


def test_stale_next_id_is_raised_above_stored_ids(tmp_path):
    path = tmp_path / "tasks.json"
    data = good_file() | {"next_id": 1}
    path.write_text(json.dumps(data), encoding="utf-8")
    assert TaskDB(path).add("C", turn=3).id == 3


def break_it(data, how):
    how(data)
    return data


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        json.dumps({"version": 2, "next_id": 1, "tasks": []}),
        json.dumps({"version": 1, "tasks": []}),
        json.dumps({"version": 1, "next_id": 1, "tasks": {}}),
        json.dumps(
            break_it(
                good_file(), lambda d: d["tasks"].append({"id": 1, "title": "d", "created_turn": 1})
            )
        ),
        json.dumps(break_it(good_file(), lambda d: d["tasks"][0].update(depends_on=[2]))),
        json.dumps(break_it(good_file(), lambda d: d["tasks"][0].update(depends_on=[9]))),
        json.dumps(break_it(good_file(), lambda d: d["tasks"][0].update(status="paused"))),
        json.dumps(break_it(good_file(), lambda d: d["tasks"][0].update(priority=50))),
        json.dumps(break_it(good_file(), lambda d: d["tasks"][0].update(colour="red"))),
        json.dumps(break_it(good_file(), lambda d: d["tasks"][0].update(metric="mana"))),
        json.dumps(break_it(good_file(), lambda d: d["tasks"].append("task"))),
        json.dumps(break_it(good_file(), lambda d: d["tasks"][0].update(metric=[]))),
        json.dumps(break_it(good_file(), lambda d: d["tasks"][0].update(metric="cities", op=[]))),
        json.dumps(
            break_it(good_file(), lambda d: d["tasks"][0].update(created_turn=None, deadline=5))
        ),
        json.dumps(break_it(good_file(), lambda d: d["tasks"][0].update(deadline="soon"))),
        json.dumps(break_it(good_file(), lambda d: d["tasks"][0].update(title=["x"]))),
        json.dumps(break_it(good_file(), lambda d: d["tasks"][0].update(depends_on=5))),
    ],
)
def test_unreadable_file_is_kept_aside_and_the_db_starts_empty(tmp_path, text):
    path = tmp_path / "tasks.json"
    path.write_text(text, encoding="utf-8")
    db = TaskDB(path)
    assert db.tasks() == []
    assert (tmp_path / "tasks.json.corrupt").read_text(encoding="utf-8") == text
    db.add("Fresh", turn=1)
    assert json.loads(path.read_text(encoding="utf-8"))["tasks"][0]["title"] == "Fresh"


# The turn loop


def test_metrics_use_the_overview_field_names():
    ov = SimpleNamespace(**{field: i for i, field in enumerate(METRICS.values())})
    values = metrics_from_overview(ov)
    assert values == {name: i for i, name in enumerate(METRICS)}


def test_every_metric_is_a_real_overview_field():
    from civ_mcp.lua.models import GameOverview

    names = {f.name for f in dataclasses.fields(GameOverview)}
    assert set(METRICS.values()) <= names


class OverviewGame:
    def __init__(self, **values):
        self.values = values

    async def get_game_overview(self):
        return SimpleNamespace(**self.values)


def full_overview(**changes):
    values = {field: 0 for field in METRICS.values()}
    values.update(changes)
    return values


def test_handler_publishes_overdue_events_and_completes_tasks(tmp_path):
    db = TaskDB(tmp_path / "tasks.json")
    db.add("Six cities", turn=1, deadline=10, metric="cities", value=6)
    db.add("Gold", turn=1, deadline=50, metric="gold", value=100)
    bus = EventBus()
    seen = []
    bus.subscribe(seen.append, TaskOverdue)
    handler = TaskHandler(db, bus)
    game = OverviewGame(**full_overview(turn=11, num_cities=3, gold=150))
    run(handler.run(game))
    assert [e.task_id for e in seen] == [1]
    assert db.get(2).status is Status.DONE
    run(handler.run(game))
    assert len(seen) == 1


def test_bot_wires_the_task_handler_after_the_policy_document(tmp_path):
    orchestrator = bot.build_orchestrator(
        fx.FakeGame(),
        EventBus(),
        TurnLog(tmp_path / "g.jsonl"),
        policy_document=tmp_path / "policy.json",
        task_db=tmp_path / "tasks.json",
    )
    names = [handler.name for handler in orchestrator._handlers]
    assert names[:3] == ["monitors", "policy_document", "tasks"]


def test_bot_without_a_task_file_has_no_task_handler(tmp_path):
    orchestrator = bot.build_orchestrator(fx.FakeGame(), EventBus(), TurnLog(tmp_path / "g.jsonl"))
    assert "tasks" not in [handler.name for handler in orchestrator._handlers]


def test_refresh_picks_up_tasks_another_writer_added(tmp_path):
    path = tmp_path / "tasks.json"
    ours = TaskDB(path)
    ours.add("Ours", turn=1, deadline=5)
    theirs = TaskDB(path)
    theirs.add("Theirs", turn=1)
    assert [t.id for t in ours.tasks()] == [1]
    assert ours.refresh()
    assert [t.title for t in ours.tasks()] == ["Ours", "Theirs"]
    assert not ours.refresh()  # Nothing new.
    ours.check(9, figures())  # Goes overdue and saves; Theirs must survive.
    assert [t.title for t in TaskDB(path).tasks()] == ["Ours", "Theirs"]
    assert ours.add("More", turn=9).id == 3


def test_refresh_ignores_our_own_writes(tmp_path):
    db = TaskDB(tmp_path / "tasks.json")
    db.add("A", turn=1)
    assert not db.refresh()


def test_refresh_with_a_bad_file_keeps_memory_and_restores_the_file(tmp_path):
    path = tmp_path / "tasks.json"
    db = TaskDB(path)
    db.add("A", turn=1)
    path.write_text("{oops", encoding="utf-8")
    assert db.refresh()
    assert [t.title for t in db.tasks()] == ["A"]
    assert (tmp_path / "tasks.json.corrupt").exists()
    assert [t.title for t in TaskDB(path).tasks()] == ["A"]


def test_handler_refreshes_before_checking(tmp_path):
    path = tmp_path / "tasks.json"
    db = TaskDB(path)
    handler = TaskHandler(db, EventBus())
    TaskDB(path).add("Added elsewhere", turn=1, metric="gold", value=10)
    run(handler.run(OverviewGame(**full_overview(turn=3, gold=50))))
    assert db.get(1).status is Status.DONE


def test_bot_accepts_a_shared_task_db(tmp_path):
    db = TaskDB(tmp_path / "tasks.json")
    orchestrator = bot.build_orchestrator(
        fx.FakeGame(), EventBus(), TurnLog(tmp_path / "g.jsonl"), task_db=db
    )
    handler = next(h for h in orchestrator._handlers if h.name == "tasks")
    assert handler.db is db
