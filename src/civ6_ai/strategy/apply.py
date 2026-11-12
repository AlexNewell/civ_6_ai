"""Hand the policy document to the domain policies.

`PolicyDocumentHandler` is a turn-loop handler that runs first in a turn. It watches one JSON file,
and when the file changes it parses it and sets the weights each policy reads. A file that is
invalid is logged and ignored, so the weights in force stay as they were. Until a valid file has
been seen no policy is touched, so a run with no document plays on each policy's own defaults.

The policies already read their weights fresh every turn, so replacing them between turns is all
that is needed. The handler reads the file only, never the game.
"""

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from civ6_ai.strategy.policy_document import (
    DEFAULT_DOCUMENT,
    PolicyDocument,
    PolicyDocumentError,
    ResolvedPolicies,
    parse_policy_json,
    resolve,
)

log = logging.getLogger(__name__)


def apply_resolved(
    resolved: ResolvedPolicies,
    *,
    production: Any = None,
    research: Any = None,
    secondary: Any = None,
    military: Any = None,
    gold: Any = None,
) -> None:
    """Set the weights on whichever policies are given."""
    if production is not None:
        production.weights = resolved.production
    if research is not None:
        research.weights = resolved.research
    if secondary is not None:
        secondary.weights = resolved.secondary
    if military is not None:
        military.settings = replace(military.settings, weights=resolved.military)
    if gold is not None:
        gold.weights = resolved.gold


class PolicyDocumentHandler:
    name = "policy_document"

    def __init__(self, path: Path, **policies: Any) -> None:
        """`policies` are the policies to steer, by the keyword names of `apply_resolved`."""
        self.path = path
        self._policies = policies
        self.document: PolicyDocument = DEFAULT_DOCUMENT
        self.problems: list[str] = []  # Why the file last seen was refused, else empty.
        self._seen: tuple[int, int] | None = None  # Modified time and size of the file last read.

    async def run(self, game: Any) -> None:
        self.refresh()

    def refresh(self) -> bool:
        """Read the file if it changed. True if a new document was applied."""
        try:
            stat = self.path.stat()
        except OSError:
            return False  # No document (yet): keep whatever is in force.
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp == self._seen:
            return False
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError:
            return False  # Mid-write or locked: look again next turn, without marking it seen.
        self._seen = stamp
        try:
            document = parse_policy_json(text)
        except PolicyDocumentError as error:
            self.problems = error.problems
            log.warning("Policy document refused, keeping the last one: %s", error)
            return False
        self.problems = []
        self.document = document
        apply_resolved(resolve(document), **self._policies)
        log.info("Policy document applied: %s", document.to_json())
        return True
