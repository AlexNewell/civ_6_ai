# Plan: using the map lens data in the bot (issue 245)

This is a plan, not code. `map-lenses.md` (issue 243) says which lens data the bot can reach;
`loyalty-pressure-map.md` (issue 239) found the one call behind the settler's loyalty numbers. This note
decides what to do with them. No read is wired in by this note. Steps that depend on a probe result are
marked **gated on #243**: the live probe run in `map-lenses.md` ("Live check") is not done yet, and each
such step starts with that result.

## The fairness rule, for every read

1. Revealed plots only. The probes already count unrevealed plots; a read that returns any is
   filtered to revealed ones before a policy sees it.
2. A missing plot means unknown, not zero. The loyalty call returned 31 plots, not the whole
   continent, so most plots are simply absent. Code must carry "no value" and never fill in 0.
3. No inference of hidden cities. A pressure value near a plot must never be used to place a city we
   have not seen (a strong negative value with no known city beside it says nothing).
4. Each read gets a flagged entry in `state/fog.py` (`READ_AUDIT`) saying what it can leak, before any
   policy calls it. It is a fixed Lua string, as `city_tiles.py` does. `execute_lua` stays banned.
5. Foreign-city reads (loyalty lens, religion) skip cities of civs we have not met, as the game's own
   lens does.

## 1. Loyalty pressure (settler lens), the first read

The call is `Map.GetContinentPlotsLoyalty()`: plot to loyalty per turn a city founded there would
feel from nearby cities. The first live run had one settler owned and the lens off, and it worked, so
the lens need not be on. Whether it works with no settler is not shown, so read it only while a settler
exists. It looked cheap (below the clock's resolution).

### Where it would improve play

- **Site scoring and rejection (`policies/settling.py`).** Today every site gets `loyalty_rate` and
  `safe_loyalty_rate`, our model of the game's rule, with fixed guesses for growth, fog and new-city
  drag (`loyalty_rival_growth`, `loyalty_fog_distance`, `loyalty_new_city_drag`). The game's own scan
  figure (`get_global_settle_scan` `loyalty_pressure`) is a civ6-mcp formula, not the game's number,
  and is dropped by `fog.py`. For a plot the call returns, the game's number replaces our base rate
  (before the added terms below). Rejection stays as now (`loyalty_reject_rate`, `loyalty_reject_safe`)
  but is judged on the true base. The fog guesses (`_fog_verdict`, phantom cities from borders, scouts
  sent to look) shrink to the plots the call does not cover, where the estimate is still the only source.
- **Which own cities need help.** The bot already reads each own city's real `loyalty_per_turn` through
  `get_cities`, and `policies/loyalty.py` and `growth.py` react to it. The map adds nothing for a city
  that exists and is read. It adds something for a planned or small city: the values on the plots around
  it show which of our cities would sit at a negative rate if a rival city appears or grows beside it.
  Use that as an early trigger for the loyalty response (garrison, governor, more people), not as a
  replacement for the per-city read.
- **Where not to expand.** Plots that read strongly negative over several reads, next to a rival we
  can see, go on a no-go list for settlers and for the frontier scouting list (`_look_points`), instead
  of sending a scout to look at fog the call already priced. All four loyalty losses (`cities-lost.md`)
  were new cities 9 to 12 tiles from the capital beside a rival. This is that rule from data instead of
  from distance.

### What the values leave out, and how the bot adds it

The numbers leave out governors, amenities and policies (by a community report, not yet checked), and
growth, since pressure scales with population and ours and theirs change. The bot adds them as signed
terms on the game's number, each a named setting:

| Left out | Added how |
|---|---|
| Governors | A governor with a loyalty promotion in the new city (or the nearest own city) adds a fixed amount. Read from `get_governors`; zero if none is assigned. |
| Amenities | Happy and ecstatic cities gain, unhappy ones lose. For a new city assume neutral for the first 10 turns, then use the empire's amenity state. |
| Policies and government | Loyalty cards we hold, read from `get_policies`; a fixed term per card. |
| Growth | Keep `loyalty_own_growth` and `loyalty_rival_growth`, applied as the change in pull over the next turns on top of the game's number, not on top of our own full estimate. |

Calibration is part of the plan: record the game's value and our estimate for the same plot, and fit
the added terms against the loyalty a founded city actually shows (`get_cities` `loyalty_per_turn`) in
its first turns.

### How to measure

1. **Game values against our estimates.** On the sites the settle scan already scores, log for each
   candidate the game's per-tile value (or "absent"), our `loyalty_rate` and `safe_loyalty_rate`. Report
   mean and worst error, split by "rival seen near" and "no rival seen". Pass: the game's value is
   present for the sites we actually choose, and the share of sites where the estimate misses by more
   than the reject margin is known. That share decides whether the estimate can stay.
2. **Choices changed.** Replay stored settle logs with the game value substituted offline (needs the
   values logged first, so from the first run with the read on). Count sites that flip between chosen
   and rejected, and how many of those the log's own history proves right or wrong (a city founded on a
   plot the estimate liked and then warned).
3. **Fewer cities flipped.** The check `settling: cities lost`, by cause as in `cities-lost.md`.
   Pass: zero loyalty losses in a run, with the same or higher city count. This is the focus metric;
   score and cities are only a guardrail. A leading measure: the share of new cities that see a
   `city_loyalty_warning` within 20 turns of founding.

### Cost and cadence

One map-wide Lua read, not one per plot. At the connection's cost of about 0.3 s per call
(`speed-profile.md`) that is about 0.3 s. Read it only on turns when a settler is moving or a site is
being chosen, and share it through `SharedReads` like the other single reads. Never every turn. The
settling handler already spends 5 to 7 s on exploration reads on those turns; if the read lets
`_fog_verdict` skip scout work it is net neutral. Against the 60 s dev bar and the 10 s goal the cost
is small, but the plan still measures it with the per-call timer (`handler_calls`) in the first run.

Gated on #243: whether the call works with no settler, on a map with more than one continent, and how
its time reads on a big map.

## 2. The other lenses

Only lenses `map-lenses.md` rates medium or better get a read plan.

| Lens | Rating | Use | Cost and cadence | Gated on #243 |
|---|---|---|---|---|
| Settler hazards (flood, eruption, lowland) | Medium | Reject or mark down sites on flood plains, near volcanoes, or in rising-sea lowlands, only when disasters are on in the game settings. | One call per plot, a few thousand at most; read only for the shortlisted sites (a handful) when a settler is choosing. | Yes: do the three calls work outside the UI context, and does a disasters-off game return nothing |
| Loyalty lens (foreign city identity) | Medium | Foreign cities about to flip (a target for us, or a sign of a rival's weakness) and the list of sources pressing on our own cities. | One call per revealed foreign city, tens at most. Read every few turns, and on a loyalty warning. | Yes: are unmet civs' cities skipped or readable (they must be skipped); are pressure sources listed |

Not worth wiring, with the reason:

- **Appeal** (low to medium). Low value early, and its reveal filtering is unknown. Revisit only with a
  tourism or culture-victory plan, for district placement.
- **Settler water.** The bot knows coast and river; the no-settle list is mostly "too close", which the
  bot checks itself.
- **Religion.** The religion policy works from its own and met-civ counts.
- **Tourism and power.** Own plots or own cities only, and a late-game question.
- **Continent.** The continent id per plot is a read the bot can already make.
- **Government, political, empire, trade routes, great people.** Nothing new; already read.

## 3. Proposed follow-up issues

Not created. One per read, each with its test and measurement, in the order to do them.

1. **Read the settler loyalty map as a flagged read.** A fixed Lua read of
   `Map.GetContinentPlotsLoyalty()`, a `READ_AUDIT` entry (flagged, revealed plots only, missing means
   unknown), a typed view. Test: unit tests on canned replies, including absent plots and unrevealed
   plots dropped. Measure: the per-call timer on a settling turn.
2. **Log the game's loyalty value beside our estimate.** One settle-log line per candidate: game value
   or "absent", `loyalty_rate`, `safe_loyalty_rate`. No decision change. Measure: mean and worst error
   from the next live run. This decides whether the estimate can stay. (After 1.)
3. **Use the game's value as the base of the site loyalty rate.** Use it where present, plus the
   governor, amenity, policy and growth terms; keep the estimate where absent. Test: unit tests that a
   present value is used and an absent one falls back, and a replay of stored settle logs showing which
   choices change. Measure: cities lost to loyalty, and new cities warned within 20 turns of founding.
   (After 2.)
4. **Cut fog guessing where the map prices the site.** Narrow `_fog_verdict`, the border phantom
   cities and the scout look rules to plots the call does not return. Test: unit tests that a priced
   site needs no look. Measure: settler idle turns and scout look count against the previous run, with
   no more loyalty losses. (After 3.)
5. **Mark no-go areas from the loyalty map.** Plots that stay below the reject rate over several
   reads, beside a visible rival, go on the avoid list for settlers and the frontier list. Test: unit
   test on a canned series. Measure: settlers sent to a site later refused (should fall), and
   expansion pace. (After 1.)
6. **Early loyalty response from neighbour values.** For small or new own cities, read the map values
   around the city and start `policies/loyalty.py` before the warning. Test: a unit test that a
   negative neighbour pattern raises the trigger. Measure: turns from first negative rate to response,
   and loyalty losses. Low priority; the per-city read already exists. (After 1.)
7. **Read the settler hazard calls (gated on #243).** Run the `hazards` probe live with disasters on
   and off. If the calls work, read them for shortlisted sites and mark down flood, volcano and
   lowland sites. Test: canned replies, a fog entry. Measure: cities damaged or lost to a disaster per
   run (check whether an event for it exists first) and the number of sites changed. If the probe
   fails, close the issue.
8. **Read foreign city loyalty from the loyalty lens (gated on #243).** Run the `loyalty_lens` probe.
   If unmet civs' cities are dropped cleanly and the source list works, read revealed foreign cities
   every few turns and on a loyalty warning, and give the source list to the loyalty response. Test:
   canned replies including an unmet civ (dropped) and a fogged city. Measure: foreign cities read
   against cities seen, and how often the response used the source list.

No issue for appeal, water, religion, tourism, power, continent or the lenses with no new data.
Items 1 to 6 are gated only on the loyalty call, which already ran live once; they still use the open
questions in section 1 (no settler, several continents) as the first check of issue 1.
