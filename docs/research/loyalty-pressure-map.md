# Can the bot see the settler's loyalty pressure map? (issue 239)

With a settler selected, the game draws a per-tile number (a flag icon and a value, about -22 to -2,
a few small positive ones) showing the loyalty pressure a city founded there would sit under.

## What the bot has today

| Source | What it gives | Real game number? |
|---|---|---|
| `get_global_settle_scan` (civ6-mcp `map.py`) | `loyalty_pressure` per candidate site | No. A civ6-mcp formula: for each city within 9 tiles, `population * (10 - distance) / 10` (capital x1.5), friendly minus enemy, then `10 * diff / (min + 0.5)` clamped to +/-20. |
| `get_city_info` / `get_cities` | `loyalty`, `loyalty_per_turn`, `turns_to_loyalty_flip` for cities we own | Yes (`CulturalIdentity`), but only for a city that exists. |
| `policies/settling.py` | `loyalty_rate`, `safe_loyalty_rate`: our own model of the game's rule, with growth, fog and new-city drag | No. An estimate, tuned on earlier runs. |

So the bot cannot see the map's numbers. It sees an estimate of them.

## The fairness problem

`state/fog.py` already drops `loyalty_pressure` and `score` from the settle scan, because the scan
reads every player's cities, fogged ones included. Any exact per-tile number the bot read would have
to pass the same test: it may use only cities the player has seen. The in-game map does this by
drawing only where we know of foreign cities, which is the behaviour to match.

## Where the game's number lives

The first version of this note said no Lua call exists for the per-tile numbers. That was wrong: the
search looked for "loyalty" and "identity", and the file that draws the numbers matches on neither.

- The shipped UI script `DLC/Expansion2/UI/Additions/SettlerInfluenceIconManager.lua`
  (`RealizeInfluenceTiles`, lines 95-126) gets every number it draws from one call,
  `Map.GetContinentPlotsLoyalty()`, which returns plot index to loyalty per turn. It writes each value
  to the tile's label and to the tooltip `LOC_SETTLER_LOYALTY_WARNING_TOOLTIP`. It draws only while the
  settler lens layer is on; whether the call itself needs the layer or a settler is not known.
- A community API reference lists it as UI context only, with no arguments (reported by a PR comment,
  not checked here). The bot already sends InGame Lua (`city_tiles.py`).
- The value is the loyalty per turn nearby cities would apply to a new city there. By the same
  report it leaves out governors, amenities and policies, so those still need modelling.
- `execute_lua` is banned for the bot (`state/fog.py`). A bot read would be a fixed Lua string, like
  `city_tiles.py`, registered as a flagged read.

## Answers so far

1. **Visible while a settler is selected?** Yes, as one UI call. Nothing in the repo reads it yet.
2. **Reachable with no settler selected?** Unknown. This is the probe's question.
3. **Shape of the data:** one signed number per plot, the net pull of nearby cities.

## The probe (built, not yet run)

`src/civ6_ai/loyalty_probe.py` and `scripts/probe_loyalty_map.py` run the call from the InGame context
and report, without changing anything in the game:

- whether the call exists and works from our connection;
- plots returned, which continents they sit on (a multi-continent map answers what "continent" covers),
  and how many are not revealed to us;
- the value range (no cap is assumed; the screenshot behind issue 239 shows -22);
- settlers owned and whether the settler lens is on, so runs with and without a settler compare;
- call time.

It has been tested only against a fake game. A person runs it by hand on a game they own: with a
settler, with the lens on, with no settler, and on a map with more than one continent. A missing plot
must be read as unknown, not zero.

## First live run

One run, on a game with one settler owned and the settler lens off:

- The call exists and works from our connection (`function`, no error).
- It returned 31 plots on one continent (id 42), all revealed to us, with values from -20 to -4. So it
  works with the lens off, and it did not return every plot on the map: it covers a small set around
  the foreign cities, not the whole continent.
- Values ran from -20 to -4, with many at -20. The screenshot behind issue 239 shows -22, so do not
  assume a cap. The range of the numbers is not important to the decision; the call itself is.
- The call time read as 0.0000 s, which is below the clock's resolution. Treat it as cheap, not free.

The settler was owned but the lens was off, so the data is readable without the lens. A settler
being owned is not the point of the test. Still useful, not required: a map with more than one
continent, and a run near our own cities to see positive values.

## If it works: fairness

Keep revealed plots only, read only while we own or plan a settler, register the read as flagged in
`state/fog.py`, and never work back from the values to locate cities we have not seen. Compare the
values with `loyalty_rate` before using them. If the call does not work, keep the estimate and
calibrate it against screenshots of the map.
