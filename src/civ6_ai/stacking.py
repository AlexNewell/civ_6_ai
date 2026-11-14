"""Remember which unit blocked a purchase, and try again only once it has gone.

The game refuses to buy a unit while another of the same formation class stands on the city tile
(`STACKING_CONFLICT ... (unit_id=N) is on the city tile`). Run 9 logged about 70 such refusals
from the faith policy and 26 from gold, the same one turn after turn. A timer would only guess
when the unit leaves. `StackBlocks` records the blocking unit per city and item, and the block
lifts when the units read shows that unit is no longer on the city tile (moved, used or dead).
Nothing here forces a purchase: a lifted block only makes the item eligible again.
"""

import re
from collections.abc import Iterable
from typing import Any

_BLOCKER = re.compile(r"STACKING_CONFLICT.*?\(unit_id=(\d+)\)")


def blocker_of(result: Any) -> int | None:
    """The id of the unit a `STACKING_CONFLICT` refusal names, else None."""
    match = _BLOCKER.search(result) if isinstance(result, str) else None
    return int(match.group(1)) if match else None


class StackBlocks:
    """(city, item) pairs refused because a unit stands on the city tile, with that unit's id."""

    def __init__(self) -> None:
        self._blocked: dict[tuple[int, str], int] = {}

    def __len__(self) -> int:
        return len(self._blocked)

    def record(self, city_id: int, item: str, result: Any) -> bool:
        """Note a refusal. True when it was a stacking conflict that names its blocker."""
        unit_id = blocker_of(result)
        if unit_id is None:
            return False
        self._blocked[(city_id, item)] = unit_id
        return True

    def blocker(self, city_id: int, item: str) -> int | None:
        return self._blocked.get((city_id, item))

    def refresh(self, cities: Iterable[Any], units: Iterable[Any]) -> None:
        """Lift every block whose unit is no longer on its city's tile."""
        if not self._blocked:
            return
        tiles = {c.city_id: (c.x, c.y) for c in cities}
        where = {u.unit_id: (u.x, u.y) for u in units}
        for key, unit_id in list(self._blocked.items()):
            if where.get(unit_id) != tiles.get(key[0]):
                del self._blocked[key]

    def is_blocked(self, city_id: int, item: str) -> bool:
        return (city_id, item) in self._blocked
