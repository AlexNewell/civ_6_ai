"""Print the headline figures of a run log: the standard readout for 50-turn slice experiments."""

import json
import sys
from pathlib import Path

from civ6_ai.effectiveness import load_run, run_checks

path = Path(sys.argv[1])
rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
second = next((r["turn"] for r in rows if (r.get("cities") or 0) >= 2), None)
third = next((r["turn"] for r in rows if (r.get("cities") or 0) >= 3), None)
last = rows[-1]
lost = sum(
    1
    for r in rows
    for ln in (r.get("detail") or "").splitlines()
    if "Your" in ln and "was killed" in ln
)
settlers_lost = sum(
    1
    for r in rows
    for ln in (r.get("detail") or "").splitlines()
    if "Your Settler" in ln and "was killed" in ln
)
print(
    f"turns {rows[0]['turn']}-{last['turn']} score {last.get('score')} cities {last.get('cities')} "
    f"pop {last.get('population')} second_city_T {second} third_city_T {third} "
    f"units_lost {lost} settlers_lost {settlers_lost}"
)
for c in run_checks(load_run([path])):
    if c.policy in ("offense", "boosts", "era score"):
        print(f"  {c.status:7} {c.name}: {c.figures()} {c.note[:110]}")
