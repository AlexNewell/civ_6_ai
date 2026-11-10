"""The run queue: a JSON file listing games to play, with each game's state.

The file holds both what to run and how it went, so a restart can pick up where it stopped. It is
rewritten atomically after every change. The real queue lives outside git (default
`runs/queue.json`); `config/queue.example.json` shows the format.

Restart rule: an item left `running` by a runner that has since died is marked `interrupted`.
If it has attempts left (`max_attempts`, default 2) it goes back to `pending` and is retried.
Otherwise it stays `interrupted` and is skipped, for a person to look at. Edit its status back to
`pending` to run it again.
"""

import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

VERSION = 1
DEFAULT_MAX_ATTEMPTS = 2
MAX_TURNS = 2000
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class Status(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class QueueError(ValueError):
    pass


@dataclass
class QueueItem:
    id: str
    save: str  # A name in config/standard_saves.json.
    turns: int
    options: dict = field(default_factory=dict)
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    status: Status = Status.PENDING
    attempts: int = 0
    pid: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    result: str | None = None
    record: str | None = None  # Path of the run record, relative to the runs folder.
    note: str = ""

    def validate(self) -> None:
        # The names reach file paths and the launch step, so only plain characters are allowed.
        for label, value in (("id", self.id), ("save", self.save)):
            if not isinstance(value, str) or not _NAME.fullmatch(value):
                raise QueueError(f"item {label} {value!r} must match {_NAME.pattern}")
        if isinstance(self.turns, bool) or not isinstance(self.turns, int):
            raise QueueError(f"item {self.id}: turns must be an integer")
        if not 1 <= self.turns <= MAX_TURNS:
            raise QueueError(f"item {self.id}: turns must be 1 to {MAX_TURNS}")
        if isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int):
            raise QueueError(f"item {self.id}: max_attempts must be an integer")
        if self.max_attempts < 1:
            raise QueueError(f"item {self.id}: max_attempts must be at least 1")
        if not isinstance(self.options, dict):
            raise QueueError(f"item {self.id}: options must be an object")


def _item_from_dict(raw: object) -> QueueItem:
    if not isinstance(raw, dict):
        raise QueueError("each item must be an object")
    known = set(QueueItem.__dataclass_fields__)
    if unknown := raw.keys() - known:
        raise QueueError(f"unknown item fields: {sorted(unknown)}")
    if not {"id", "save", "turns"} <= raw.keys():
        raise QueueError("each item needs id, save and turns")
    try:
        item = QueueItem(**{**raw, "status": Status(raw.get("status", "pending"))})
    except ValueError as error:
        raise QueueError(str(error)) from error
    item.validate()
    return item


def pid_alive(pid: int) -> bool:
    """True if a process with this id exists. Never signals it."""
    if pid == os.getpid():
        return False  # A fresh runner never owns an item it didn't start this session.
    if sys.platform == "win32":
        import ctypes

        query_limited = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(query_limited, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class RunQueue:
    def __init__(self, path: Path, items: list[QueueItem]) -> None:
        self.path = path
        self.items = items

    @classmethod
    def load(cls, path: Path) -> "RunQueue":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise QueueError(f"{path} is not valid JSON: {error}") from error
        if not isinstance(data, dict) or data.get("version") != VERSION:
            raise QueueError(f"{path} must be an object with version {VERSION}")
        raw_items = data.get("items")
        if not isinstance(raw_items, list):
            raise QueueError(f"{path} needs an items list")
        items = [_item_from_dict(raw) for raw in raw_items]
        ids = [item.id for item in items]
        if len(set(ids)) != len(ids):
            raise QueueError("item ids must be unique")
        return cls(path, items)

    def save(self) -> None:
        data = {"version": VERSION, "items": [_plain(item) for item in self.items]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def recover_interrupted(self, alive: Callable[[int], bool] = pid_alive) -> list[QueueItem]:
        """Deal with items left `running` by a dead runner. Returns the ones changed."""
        changed = []
        for item in self.items:
            if item.status is not Status.RUNNING:
                continue
            if item.pid is not None and alive(item.pid):
                raise QueueError(f"item {item.id} is running in live process {item.pid}")
            retry = item.attempts < item.max_attempts
            item.status = Status.PENDING if retry else Status.INTERRUPTED
            item.pid = None
            item.note = f"interrupted on attempt {item.attempts}; " + (
                "will retry" if retry else "out of attempts, skipped"
            )
            changed.append(item)
        if changed:
            self.save()
        return changed

    def next_pending(self) -> QueueItem | None:
        return next((i for i in self.items if i.status is Status.PENDING), None)

    def mark_running(self, item: QueueItem, now: str) -> None:
        item.status = Status.RUNNING
        item.attempts += 1
        item.pid = os.getpid()
        item.started_at = now
        item.finished_at = None
        item.result = None
        item.note = ""
        self.save()

    def mark_finished(
        self, item: QueueItem, now: str, *, ok: bool, result: str, record: str, note: str = ""
    ) -> None:
        item.status = Status.DONE if ok else Status.FAILED
        item.pid = None
        item.finished_at = now
        item.result = result
        item.record = record
        item.note = note
        self.save()


def _plain(item: QueueItem) -> dict:
    data = asdict(item)
    data["status"] = item.status.value
    return data
