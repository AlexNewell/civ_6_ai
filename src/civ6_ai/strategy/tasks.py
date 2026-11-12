"""A persistent task database: stated intentions turned into tracked work.

A task is something the strategist wants true by some turn, such as "6 cities by turn 90". It has
an optional measurable target, a deadline, a priority and dependencies. Software checks the tasks
every turn against figures read from the game overview, so no model call is spent on watching
them. A task whose target is met is done. A task still open after its deadline raises one
`TaskOverdue` event, which the strategist can act on, and stays open and tracked.

The database is one JSON file, rewritten atomically after every change, so a restart picks up
where the process stopped. A file that can't be read is moved aside, not deleted, and the
database starts empty.

Dependencies must already exist when a task is added and can't be edited afterwards, so they can't
form a cycle. A task can't be done while anything it depends on is open, even if its own target is
met. A task with open dependents can't be cancelled until they are, so no task is left waiting on
something that will never finish. A deadline counts from the turn it's set, whatever the
dependencies, because a late prerequisite is exactly what the strategist should hear about.

If the game turn goes backwards (a save was loaded), tasks keep their state. Re-plan by cancelling
and adding tasks.
"""

import json
import logging
import math
import operator
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from civ6_ai.monitors import Event, EventBus, register_event

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MIN_PRIORITY = 0
MAX_PRIORITY = 9
DEFAULT_PRIORITY = 5
MAX_TITLE_LENGTH = 200
MAX_TASKS = 500  # Far more than a strategist should keep open, so a runaway writer is stopped.

# The figures a target can name, and the `GameOverview` field each is read from.
METRICS: dict[str, str] = {
    "turn": "turn",
    "gold": "gold",
    "gold_per_turn": "gold_per_turn",
    "science": "science_yield",
    "culture": "culture_yield",
    "faith": "faith",
    "cities": "num_cities",
    "units": "num_units",
    "score": "score",
    "population": "total_population",
}
_COMPARE: dict[str, Callable[[float, float], bool]] = {">=": operator.ge, "<=": operator.le}


class Status(StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class TaskError(ValueError):
    """A task or the database file is invalid, or an action isn't allowed."""


@dataclass(frozen=True)
class TaskOverdue(Event):
    """An open task is past its deadline. Raised once per task."""

    kind = "task_overdue"

    task_id: int
    title: str
    deadline: int
    priority: int


register_event(TaskOverdue)


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    created_turn: int
    priority: int = DEFAULT_PRIORITY
    deadline: int | None = None  # The last turn it can be done on without being overdue.
    metric: str | None = None  # With `op` and `value`, the target. None means done by hand.
    op: str = ">="
    value: float = 0.0
    depends_on: tuple[int, ...] = ()
    note: str = ""
    status: Status = Status.OPEN
    completed_turn: int | None = None
    overdue_turn: int | None = None  # The turn the overdue event was raised, if it was.

    @property
    def is_open(self) -> bool:
        return self.status is Status.OPEN

    @property
    def done_late(self) -> bool:
        return (
            self.status is Status.DONE
            and self.deadline is not None
            and self.completed_turn is not None
            and self.completed_turn > self.deadline
        )

    def target_met(self, metrics: Mapping[str, float]) -> bool:
        """True if the target is met. A task with no target is never met by figures."""
        if self.metric is None or self.metric not in metrics:
            return False
        return _COMPARE[self.op](metrics[self.metric], self.value)


def metrics_from_overview(overview: Any) -> dict[str, float]:
    """The figures targets can name, from civ6-mcp's `GameOverview`. Our own empire only."""
    return {name: getattr(overview, field) for name, field in METRICS.items()}


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_whole(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _task_from_dict(raw: object) -> Task:
    """Validate one stored task. Raises `TaskError` naming the problem."""
    if not isinstance(raw, Mapping):
        raise TaskError("task must be an object")
    known = Task.__dataclass_fields__.keys()
    if extra := sorted(str(key) for key in raw if key not in known):
        raise TaskError(f"unknown task fields {extra}")
    try:
        task = Task(**{**raw, "status": Status(raw.get("status", "open"))})
    except (TypeError, ValueError) as error:
        raise TaskError(f"bad task: {error}") from error
    task = replace(task, depends_on=_ids(task.depends_on))
    _validate(task)
    return task


def _ids(value: object) -> tuple[int, ...]:
    if not isinstance(value, list | tuple) or not all(_is_whole(item) for item in value):
        raise TaskError("depends_on must be a list of task ids")
    return tuple(value)


def _validate(task: Task) -> None:
    if not _is_whole(task.id) or task.id < 1:
        raise TaskError(f"id {task.id!r} must be a whole number from 1")
    if not isinstance(task.title, str) or not task.title.strip():
        raise TaskError(f"task {task.id}: title is required")
    if len(task.title) > MAX_TITLE_LENGTH:
        raise TaskError(f"task {task.id}: title is over {MAX_TITLE_LENGTH} characters")
    if not isinstance(task.note, str):
        raise TaskError(f"task {task.id}: note must be text")
    if not _is_whole(task.priority) or not MIN_PRIORITY <= task.priority <= MAX_PRIORITY:
        raise TaskError(f"task {task.id}: priority must be {MIN_PRIORITY} to {MAX_PRIORITY}")
    if not _is_whole(task.created_turn) or task.created_turn < 0:
        raise TaskError(f"task {task.id}: created_turn must be a whole turn number")
    for name in ("deadline", "completed_turn", "overdue_turn"):
        value = getattr(task, name)
        if value is not None and (not _is_whole(value) or value < 0):
            raise TaskError(f"task {task.id}: {name} must be a whole turn number")
    if task.deadline is not None and task.deadline < task.created_turn:
        raise TaskError(f"task {task.id}: deadline is before the turn it was created")
    if task.metric is not None:
        if not isinstance(task.metric, str) or task.metric not in METRICS:
            raise TaskError(
                f"task {task.id}: unknown metric {task.metric!r}, use {sorted(METRICS)}"
            )
        if not isinstance(task.op, str) or task.op not in _COMPARE:
            raise TaskError(f"task {task.id}: op must be one of {sorted(_COMPARE)}")
        if not _is_number(task.value):
            raise TaskError(f"task {task.id}: value must be a finite number")
    if task.id in task.depends_on or len(set(task.depends_on)) != len(task.depends_on):
        raise TaskError(f"task {task.id}: bad dependencies {list(task.depends_on)}")


class TaskDB:
    """The tasks, kept in memory and mirrored to `path` after every change.

    With no path it is memory only, for tests and dry runs.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._tasks: dict[int, Task] = {}
        self._next_id = 1
        self._stamp: tuple[int, int] | None = (
            None  # Modified time and size of the file as we left it.
        )
        if path is not None and path.exists():
            self._load(path)

    def refresh(self) -> bool:
        """Re-read the file if something else changed it, so another writer's tasks aren't lost.

        True if it was reloaded. Our own writes are recognised and not re-read. If the changed file
        is unreadable it is moved aside as at start, and what is in memory is written back.
        """
        if self.path is None or self._file_stamp() in (self._stamp, None):
            return False
        self._load(self.path)
        if not self.path.exists():
            self._save()  # The file was bad: put what we have back.
        return True

    def _file_stamp(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()  # type: ignore[union-attr]
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    # Reading

    def get(self, task_id: int) -> Task:
        try:
            return self._tasks[task_id]
        except KeyError:
            raise TaskError(f"no task {task_id}") from None

    def tasks(self, status: Status | None = None) -> list[Task]:
        """Every task in id order, or those with `status`."""
        return [t for t in self._tasks.values() if status is None or t.status is status]

    def open_tasks(self) -> list[Task]:
        """Open tasks, most urgent first: priority, then earliest deadline, then oldest."""
        far = float("inf")
        return sorted(
            self.tasks(Status.OPEN),
            key=lambda t: (-t.priority, far if t.deadline is None else t.deadline, t.id),
        )

    def waiting_on(self, task: Task) -> list[int]:
        """Ids of the open tasks that `task` still depends on."""
        return [d for d in task.depends_on if self._tasks[d].is_open]

    def ready_tasks(self) -> list[Task]:
        """Open tasks with nothing left to wait for, most urgent first."""
        return [t for t in self.open_tasks() if not self.waiting_on(t)]

    # Changing

    def add(
        self,
        title: str,
        *,
        turn: int,
        priority: int = DEFAULT_PRIORITY,
        deadline: int | None = None,
        metric: str | None = None,
        op: str = ">=",
        value: float = 0.0,
        depends_on: tuple[int, ...] = (),
        note: str = "",
    ) -> Task:
        """Add a task created on `turn`. Raises `TaskError` and changes nothing if it's invalid."""
        if len(self.tasks(Status.OPEN)) >= MAX_TASKS:
            raise TaskError(f"already {MAX_TASKS} open tasks")
        task = Task(
            id=self._next_id,
            title=title,
            created_turn=turn,
            priority=priority,
            deadline=deadline,
            metric=metric,
            op=op,
            value=value,
            depends_on=_ids(depends_on),
            note=note,
        )
        _validate(task)
        for dep in task.depends_on:
            if dep not in self._tasks:
                raise TaskError(f"task {task.id}: depends on unknown task {dep}")
            if self._tasks[dep].status is Status.CANCELLED:
                raise TaskError(f"task {task.id}: depends on cancelled task {dep}")
        self._tasks[task.id] = task
        self._next_id += 1
        self._save()
        return task

    def complete(self, task_id: int, turn: int) -> Task:
        """Mark a task done by hand, for one with no figure to measure."""
        task = self.get(task_id)
        if not task.is_open:
            raise TaskError(f"task {task_id} is {task.status.value}")
        if waiting := self.waiting_on(task):
            raise TaskError(f"task {task_id} still waits on {waiting}")
        return self._finish(task, turn)

    def cancel(self, task_id: int) -> Task:
        task = self.get(task_id)
        if not task.is_open:
            raise TaskError(f"task {task_id} is {task.status.value}")
        dependents = [t.id for t in self.tasks(Status.OPEN) if task_id in t.depends_on]
        if dependents:
            raise TaskError(f"task {task_id} is waited on by {dependents}; cancel those first")
        self._tasks[task_id] = cancelled = replace(task, status=Status.CANCELLED)
        self._save()
        return cancelled

    def check(self, turn: int, metrics: Mapping[str, float]) -> list[TaskOverdue]:
        """Finish tasks whose targets are met, then flag the overdue. Call once per turn.

        Returns the events for tasks that became overdue on this call, each task only once.
        """
        changed = False
        progress = True
        while progress:  # A finished task can free a dependent, so go until nothing changes.
            progress = False
            for task in self.tasks(Status.OPEN):
                if task.target_met(metrics) and not self.waiting_on(task):
                    self._finish(task, turn, save=False)
                    progress = changed = True
        events = []
        for task in self.tasks(Status.OPEN):
            if task.deadline is not None and turn > task.deadline and task.overdue_turn is None:
                self._tasks[task.id] = replace(task, overdue_turn=turn)
                changed = True
                events.append(
                    TaskOverdue(
                        turn=turn,
                        task_id=task.id,
                        title=task.title,
                        deadline=task.deadline,
                        priority=task.priority,
                    )
                )
        if changed:
            self._save()
        return events

    def _finish(self, task: Task, turn: int, save: bool = True) -> Task:
        self._tasks[task.id] = done = replace(task, status=Status.DONE, completed_turn=turn)
        if save:
            self._save()
        return done

    # Persisting

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "next_id": self._next_id,
            "tasks": [asdict(task) for task in self._tasks.values()],
        }

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + ".tmp")
        temp.write_text(json.dumps(self.to_dict(), indent=1, default=str), encoding="utf-8")
        os.replace(temp, self.path)  # Atomic, so a crash leaves the old file or the new one.
        self._stamp = self._file_stamp()

    def _load(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            tasks, next_id = self._parse(data)
        except (OSError, ValueError, TypeError, TaskError) as error:
            aside = path.with_name(path.name + ".corrupt")
            log.warning("Task file unreadable (%s); moved to %s and starting empty", error, aside)
            try:
                os.replace(path, aside)
            except OSError:
                log.exception("Could not move the task file aside")
            return
        self._tasks = {task.id: task for task in tasks}
        self._next_id = max(next_id, self._next_id)  # Ids given out are never reused.
        self._stamp = self._file_stamp()

    @staticmethod
    def _parse(data: object) -> tuple[list[Task], int]:
        if not isinstance(data, Mapping) or data.get("version") != SCHEMA_VERSION:
            raise TaskError("not a version 1 task file")
        raw_tasks = data.get("tasks")
        next_id = data.get("next_id")
        if not isinstance(raw_tasks, list) or not _is_whole(next_id):
            raise TaskError("tasks and next_id are required")
        tasks = [_task_from_dict(raw) for raw in raw_tasks]
        ids = [task.id for task in tasks]
        if len(set(ids)) != len(ids):
            raise TaskError("duplicate task ids")
        known = set(ids)
        for task in tasks:
            if missing := [d for d in task.depends_on if d not in known or d >= task.id]:
                raise TaskError(f"task {task.id} depends on {missing}")
        return tasks, max(next_id, max(ids, default=0) + 1)


class TaskHandler:
    """The turn-loop step: check the tasks against this turn's figures and publish overdue ones."""

    name = "tasks"

    def __init__(self, db: TaskDB, bus: EventBus) -> None:
        self.db = db
        self._bus = bus

    async def run(self, game: Any) -> None:
        self.db.refresh()
        overview = await game.get_game_overview()
        for event in self.db.check(overview.turn, metrics_from_overview(overview)):
            self._bus.publish(event)
