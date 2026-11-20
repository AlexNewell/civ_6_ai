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
# The trajectory shows whether a change is still paying off at the end of the slice. A gap that
# is still widening at the last turn has not finished showing its effect: extend the run
# (`--save-at` a checkpoint, then play on) before keeping or rejecting it.
by_turn = {r["turn"]: r for r in rows}
marks = [t for t in range(10, last["turn"] + 1, 10) if t in by_turn]
print("  score by turn: " + " ".join(f"T{t}={by_turn[t].get('score')}" for t in marks))
print("  cities by turn: " + " ".join(f"T{t}={by_turn[t].get('cities')}" for t in marks))
tail = [r.get("score") for r in rows[-11:] if r.get("score") is not None]
if len(tail) >= 2:
    print(f"  score gain over the last {len(tail) - 1} turns: {tail[-1] - tail[0]:+}")
# The threat map's dump (issue 238). "Camp-turns near a city" counts turns a known camp stood
# within `near_city` tiles of one of our cities: a camp left standing is the failure it measures.
dump = path.with_name(f"{path.stem}_threatmap.jsonl")
if dump.exists():
    maps = [json.loads(x) for x in dump.read_text(encoding="utf-8").splitlines() if x.strip()]
    camps = {
        (s["x"], s["y"]): m["turn"]
        for m in reversed(maps)
        for s in m["sources"]
        if s["kind"] == "camp"
    }
    near_turns = sum(m["camps_near_city"] for m in maps)  # One per camp per turn.
    print(
        f"  threat map: {len(maps)} turns dumped, camps ever known {len(camps)}, "
        f"camp-turns near a city {near_turns}, camps known at the end "
        f"{len([s for s in maps[-1]['sources'] if s['kind'] == 'camp'])}"
    )
    seconds = [r.get("handler_seconds", {}).get("threat_map", 0.0) for r in rows]
    print(f"  threat map handler: {sum(seconds) / len(seconds):.2f} s per turn on average")
# Did the military use the map? Counted from its offense lines (issue 238). A run that scores the
# same as before has to be read with this: it may simply never have used the map.
bot_log = path.with_name(f"{path.stem}_bot.log")
if bot_log.exists():
    offense = []
    for line in bot_log.read_text(encoding="utf-8", errors="replace").splitlines():
        if "military_offense " in line:
            offense.append(json.loads(line.split("military_offense ", 1)[1]))
    if offense:
        uses = [o.get("map_use", {}) for o in offense]
        print(
            f"  map use: map {'ON' if offense[-1].get('map_on') else 'off'}; "
            f"turns with a camp known only through the map "
            f"{sum(1 for u in uses if u.get('camps_only_in_map'))}, "
            f"turns the target came from the map "
            f"{sum(u.get('target_from_map', 0) for u in uses)}, "
            f"muster moves {sum(u.get('muster_moves', 0) for u in uses)}, "
            f"waits {sum(u.get('muster_waits', 0) for u in uses)}, "
            f"missions released {sum(u.get('missions_released', 0) for u in uses)}, "
            f"camps gone {sum(o.get('camps_gone', 0) for o in offense)}, "
            f"barbarian kills {sum(o.get('kills_barb', 0) for o in offense)}"
        )
for c in run_checks(load_run([path])):
    if c.policy in ("offense", "boosts", "era score"):
        print(f"  {c.status:7} {c.name}: {c.figures()} {c.note[:110]}")
# Issue 324: how good the sites of the cities the bot founded were, from the settle log.
settle_log = path.with_name(f"{path.stem}_settle.jsonl")
if settle_log.exists():
    from civ6_ai.site_quality import founded_site_scores

    decisions = [
        json.loads(x) for x in settle_log.read_text(encoding="utf-8").splitlines() if x.strip()
    ]
    site_scores = founded_site_scores(decisions)
    if site_scores:
        print(
            f"  city sites: {len(site_scores)} founded, mean site score "
            f"{sum(site_scores) / len(site_scores):.1f} "
            f"(min {min(site_scores):.1f}, max {max(site_scores):.1f})"
        )
