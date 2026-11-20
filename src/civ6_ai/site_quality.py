"""City-site quality measurement (issue 324): enrich a surveyed site from a map read, and report.

Pure functions only. The survey (`SettleSite`) carries totals; the per-tile facts the owner's
factors need (what lies under the centre, ring yields) come from a map-area read of radius 3.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path

from civ6_ai.state.models import SettleSite, Tile
from civ6_ai.state.reader import STANDARD_MAP_WIDTH, hex_distance

_CLASS_PREFIX = {"strategic": "S", "luxury": "L", "bonus": "B"}


def enrich_site(
    site: SettleSite, tiles: Iterable[Tile], map_width: int | None = STANDARD_MAP_WIDTH
) -> SettleSite:
    """Fill the centre resource and ring yields from the tiles read around the site.

    Fogged tiles show no yields, so they count 0: a ring read from a partly hidden area is low.
    """
    centre = None
    inner = outer = 0
    for tile in tiles:
        d = hex_distance(site.x, site.y, tile.x, tile.y, map_width)
        if d == 0:
            if tile.resource and tile.resource_class:
                prefix = _CLASS_PREFIX.get(tile.resource_class, "B")
                centre = f"{prefix}:{tile.resource}"
        elif tile.yields and d <= 3:
            total = tile.yields[0] + tile.yields[1]
            if d == 1:
                inner += total
            else:
                outer += total
    return replace(site, centre_resource=centre, inner_yield=inner, outer_yield=outer)


def format_breakdown(breakdown: dict[str, Sequence[float]]) -> str:
    """One line per component, with the weighted total at the end."""
    lines = [
        f"  {name:18} raw {raw:7.1f}  weighted {weighted:7.1f}"
        for name, (raw, weighted) in breakdown.items()
    ]
    total = sum(weighted for _, weighted in breakdown.values())
    lines.append(f"  {'total':18} {'':>11}  weighted {total:7.1f}")
    return "\n".join(lines)


def founded_site_scores(decisions: Iterable[dict]) -> list[float]:
    """The site score of each city the bot founded, from the settle log's decision lines."""
    scores = []
    for d in decisions:
        if d.get("action") != "found":
            continue
        where = tuple(d["target"]) if d.get("target") else (d["x"], d["y"])
        # `quality_score` is the site's own value; `site_score` has the loyalty and far-city
        # penalties taken off. Logs written before the field existed fall back to `site_score`.
        match = next(
            (
                c.get("quality_score", c["site_score"])
                for c in d.get("candidates", [])
                if (c["x"], c["y"]) == where
            ),
            None,
        )
        if match is None:
            match = d.get("here_score")
        if match is not None:
            scores.append(match)
    return scores


def mean_site_score(settle_log: Path) -> tuple[int, float] | None:
    """(cities founded, mean site score) from a `_settle.jsonl`, or None with no founded city."""
    if not settle_log.exists():
        return None
    lines = settle_log.read_text(encoding="utf-8").splitlines()
    scores = founded_site_scores(json.loads(x) for x in lines if x.strip())
    return (len(scores), sum(scores) / len(scores)) if scores else None
