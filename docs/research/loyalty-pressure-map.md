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

## Where the game's number lives (not yet confirmed live)

- The shipped UI Lua (`DLC/Expansion2/UI/Additions/LoyaltySupport.lua`, `CityBannerManager.lua`)
  reads loyalty only per city (`GetCulturalIdentity()`, `GetLoyaltyPerTurn()`, `GetLoyaltyLevel()`).
  It holds no per-tile pressure call.
- The per-tile numbers are drawn by the engine's lens layer, so no script call for them was found in
  the game's Lua. This is from searching the install, not from a live probe.
- `execute_lua` is banned for the bot (`state/fog.py`), so a live probe would be a probe-time tool, not
  a bot read.

## Answers

1. **Visible while a settler is selected?** Only to the human, as the engine's lens. No bot read
   exists. Selecting a settler would not help the bot unless the numbers are reachable from Lua.
2. **Reachable with no settler selected?** Not found. The loyalty lens, not the settler, is the likelier
   route, and it is also engine-drawn.
3. **Shape of the data:** one signed number per tile, net of friendly and foreign pull.

## Next step

A probe run, off by default, in the style of `probes.py`: with a settler selected and then not, save a
screenshot of the map and, from a Lua session, try the candidate calls on a plot
(`Players[i]:GetCulturalIdentity()`, `CityManager`, `Game.GetLoyalty*`) and log what exists. If one
returns the per-tile value, add it to `state/reader.py` behind the fog rule above and compare it with
`loyalty_rate`. If none does, the answer is to keep the estimate and calibrate it against screenshots
of the map.
