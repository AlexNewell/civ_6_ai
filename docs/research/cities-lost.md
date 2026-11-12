# Why were cities lost?

Research for issue 147. Every run since Run 3 has lost cities (the effectiveness check `settling: cities lost` fails in all of them). This note takes each loss in Runs 4, 5 and 6 (and lists Run 3 from its event log), says what happened, sorts the cause, and proposes fixes with targets. It is read from run logs only: the turn logs, bot logs, event logs and settle logs in `runs/`. No game was open and no model was called.

Sources and limits:

- The event log has `city_lost` and `city_loyalty_warning` only. It names no taker, no cause and no return. Takers and causes below are read from the threat notices in the turn log (`THREAT: <civ> UNIT_...`), the war tracker lines, and where a city's name later appears in production lines.
- Garrison counts come from `military_decision` lines. A unit that got no decision that turn is not counted, so units-in-city is a lower bound.
- The bot never declares war (there is no declare call in `src/`). Every war below was declared by an AI.

## Short answer

Of 15 losses in Runs 3 to 6, 11 were captured in war and 4 were loyalty losses. No loss was shown to come back.

1. **Captured in war: weak defence, in an era gap.** 11 losses. All 11 fell within 10 turns of the war being first seen (0 to 10), which is inside the game's 10-turn window before peace can be offered (`wars.min_war_age`). Defenders were Warriors (CS 20) and Man-at-Arms against Knights, Line Infantry, Artillery and, in Run 5, Modern Armor (CS 95). In every war the gold handler sat above its reserve and bought nothing, though six Warrior upgrades (165 gold each) were on offer for the whole of the Run 6 capital's war.
2. **Loyalty: bad sites.** 4 losses. Each new city sat 10 or more tiles from the capital, near a rival, and the city was lost within 8 to 10 turns of the first loyalty warning. Two fell 8 and 9 turns after being founded. Nothing in the settling policy reads loyalty.
3. **Wars not ended.** Peace cannot be offered for 10 turns, and every captured city fell within it. Ending wars faster is not an available fix. Not being in them, or holding for 10 turns, is.
4. **"Flipped and came back" is not supported.** The Run 6 capital's `city_lost` on turn 135 is real. Its name appears again in production lines from turn 240 because a new city founded that turn was given the freed name (details below). The monitor logs no gain, so a real return would still be invisible; the effectiveness check now handles `city_gained` events when they exist.

## The Run 6 capital (the case in the issue comment)

The issue comment says the capital (id 65536, tile 66,25) has a `city_lost` event on turn 135 but the bot logged production choices for "Wak Kab'nal" until turn 300, so the city may have flipped back.

What the logs show:

- Turn 129: Sumeria's war first seen. Turn 133 to 135: Knights and Coursers (CS 50 and 54) and a Man-at-Arms (CS 62) next to the city. The score drops from 250 to 186 between turns 134 and 135.
- The capital's name is absent from production, gold and builder lines from turn 135 to turn 238, 100 turns. Other cities appear every few turns in the same lines.
- The settle log shows a city founded at (67,36) on turn 238, and the turn-240 notice reads `New city founded: Wak Kab'nal!`. A founded city takes the first free name, so the original capital's name was free. The production lines for "Wak Kab'nal" after turn 240 are that new city's (its Stock Exchange on turn 295 and Neighborhood on turn 300 are plausible for a city 55 to 60 turns old).
- The same pattern shows in Run 6 session 1. Uxmal (64,22) was lost on turn 87, then the settle log founds a city on the same tile (64,22) on turn 101. A city cannot be founded on a tile with a city, so Uxmal was destroyed, not held by the enemy. It came back as "Tikal" (id 196609).

So the logs say the capital was permanently lost, and the later production lines are a name being reused. This does not prove the capital never flipped back; no `get_cities` output is logged with ids to settle it. When the first `city_gained` event lands (issue 156), a rerun of the check settles it directly.

## Per-city table

"War state" is from the war tracker lines and `war_declared` events. "Turns" is war first seen to loss.

| Run | City (tile) | Lost | Taken by | War state | Cause |
|---|---|---|---|---|---|
| 3 | Uxmal (64,22) | 71 | Sumeria (war 68) | 3 turns old | Weak defence |
| 3 | Palenque (70,13) | 192 | loyalty (warned 184) | none | Loyalty, founded turn 183 |
| 3 | Wak Kab'nal, capital (66,25) | 304 | Mapuche | declared 304 | Weak defence |
| 4 | Uxmal (64,22) | 84 | Sumeria (war 77) | 7 turns | Weak defence |
| 4 | Chichen Itza (57,27) | 151 | loyalty (warned 145, -16/turn) | none | Loyalty |
| 4 | Wak Kab'nal, capital (66,25) | 230 | Khmer (attack from 223) | war until 256 | Weak defence |
| 4 | Kaminaljuyu (64,35) | 314 | loyalty (warned 305, -8/turn) | none | Loyalty |
| 5 | Palenque (66,15) | 133 | loyalty (warned 126, -20 then -12/turn) | none | Loyalty, founded turn 125 |
| 5 | Wak Kab'nal, capital (66,25) | 296 | Mapuche (war 290) | 6 turns | Weak defence |
| 5 | Uxmal (64,22) | 300 | Mapuche (same war) | 10 turns; war ended 301 | Weak defence |
| 6 | Uxmal (64,22) | 87 | Sumeria (war 84) | 3 turns | Weak defence; city destroyed |
| 6 | Wak Kab'nal, capital (66,25) | 135 | Sumeria (war 129) | 6 turns | Weak defence |
| 6 | Tikal (64,22) | 244 | Mapuche (war 243) | 0 to 1 turns | Weak defence |
| 6 | Chichen Itza (63,27) | 247 | Mapuche (war 243) | 4 turns | Weak defence |
| 6 | Yaxchilan (63,31) | 273 | Mapuche (war 268) | 5 turns | Weak defence |

Run 3 and the later Run 4 sessions are read from the event logs only; their per-turn military decisions were not read.

## Defence detail, per capture (Runs 4 to 6)

**Run 4 Uxmal, turn 84.** The second city. Our Settler was killed outside the city on turn 76. War on turn 77. A Warrior at the city took 36 then 19 damage and died on turn 79 to Archers (CS 15, ranged 25, a Warrior has CS 20 and no ranged attack). One Warrior was left. The military policy answered with `skip` six turns running: `no attack: deals 23 and takes 25` (the odds rule refuses even trades). Sumerian Archers stayed 1 tile away for the rest of the war.

**Run 6 Uxmal, turn 87.** Same site, same enemy, same shape: Sumerian War Cart and Warrior at 1 tile from turn 79 to 85, 1 unit on the tile, orders `move refused` and `city tile refused, so went to a tile beside it` (a second unit cannot stack on a tile that holds a same-class unit). The city fell on turn 87 and was razed.

**Run 6 capital, turn 135.** One Warrior inside the city (unit at 66,25). Three more Warriors were fortified outside it at (67,25), (67,24) and (66,24), the last two with `garrison` orders refused by `STACKING_CONFLICT` on turns 131 and 135, each followed by "fortified where it stands". The military policy then logged `no attack: unit is hurt` and `would be left with 0 hp on a bad roll` on turns 133 to 135. Enemy: Knight (CS 50), Courser (CS 54), Man-at-Arms (CS 62). The era's defender was a Warrior (CS 20 to 27). Meanwhile the capital's production was `UNIT_SETTLER` (turn 132) then a Hul'che (turn 133). Gold was 176 to 264 against a reserve of 120. The gold handler logged "nothing bought" on every one of the 7 war turns while `UNIT_WARRIOR upgrade costs 165` was listed six times a turn; its budget was gold minus reserve (56 to 144), always under 165. Walls cost 210 and were never built.

**Run 6 Tikal and Chichen Itza, turns 244 and 247.** Mapuche denounced us (diplomacy talk, turn 243) and declared war the same turn. Tikal had two Warriors (both killed on turn 243, the same turn the city fell). Mapuche arrived with 5 units; a Mapuche Line Infantry (CS 65) was already seen 4 tiles from our cities on turns 216 and 217. Chichen Itza fell four turns later. The log shows 241, 281, 291 gold above the reserve on turns 243, 244, 247, "nothing bought". A Hul'che costs 160. Warriors were still being built in Tikal on turns 185 and 189 and in Chichen Itza on turns 244 and 245, in an era where Man-at-Arms and Pikeman were affordable (425 and 480 on the gold lines).

**Run 6 Yaxchilan, turn 273.** Three units in the city on turns 267 and 268, one by turn 269. A Field Cannon (CS 50, ranged 60) came on turn 268. The policy attacked with the odds in its favour (`odds favour the attack`, `planned kill`) and lost the Crossbowman on turn 273. Gold 216 to 307 over the war, `UNIT_WARRIOR upgrade costs 270` available, `nothing bought`.

**Run 5 capital and Uxmal, turns 296 and 300.** Mapuche declared on turn 290: our Builder and a Cavalry unit died in the first turn. By turn 293 Modern Armor (CS 95) and Artillery (CS 60) were 1 tile from the capital. Our defenders were Warriors and Man-at-Arms. None of the units that got a decision on turns 287 to 296 stood on the capital's tile; five stood next to it. `garrison` ended with `city tile refused, so went to a tile beside it` 3 times in a turn. Our Cuirassier and Heavy Chariot were killed near Uxmal on turn 299. Peace was offered on turn 300, the first turn it could be, and refused; the war ended on turn 301.

### What the military policy did, in common

- The odds rule (`no attack: would be left with 0 hp on a bad roll, under the floor`) turned off almost every attack against stronger units, which is right for attacks but leaves the garrison as the only defence.
- `garrison` and `retreat` orders end in `STACKING_CONFLICT` or `city tile refused, so went to a tile beside it`. Units stand outside the city and are killed one by one, and the city keeps its one defender (documented in `policy-effectiveness.md` as 45 to 58% of military decisions refused).
- No policy raises production or spending on a new war. The gold reserve and its budget stay the same through a war. Production kept choosing Settlers and Builders in the capital (turn 132) and Warriors in Tikal.
- `garrison_required` is 1, or 2 when hostile units crowd the city (`max_garrison`). A single Warrior against 5 modern units cannot hold anything at any count; the number is not the problem, the quality is.

## Loyalty losses

Four cities (Run 3 Palenque, Run 4 Chichen Itza and Kaminaljuyu, Run 5 Palenque) fell to loyalty. All four:

- were new (two within 8 and 9 turns of founding) and far from the capital: Palenque (70,13) and (66,15) are 10 to 12 tiles north, next to Khmer; Chichen Itza (57,27) is 9 tiles west; Kaminaljuyu (64,35) is 10 tiles south;
- had a `city_loyalty_warning` 6 to 9 turns before the loss with a falling rate of -8 to -20 per turn, so the loss was predictable and the bot saw it. Nothing responds to the warning (only the monitor reads it);
- are not helped by anything in `settling.py`: it has no loyalty term (`grep loyal src/civ6_ai/policies/settling.py` finds nothing).

Cities that were warned and survived: Yaxchilan in Run 6 (-9.3 per turn on turn 153, held), Palenque in Run 6 (turn 328, game ended), Copan and Calakmul in Run 4. A warning alone is not a loss: the loss needs the rate to stay negative.

## Cause classification

| Cause | Count of 15 | Runs | Notes |
|---|---|---|---|
| Weak defence | 11 | 3, 4, 5, 6 | Obsolete Warriors, 1 defender, units refused entry |
| Loyalty (bad site) | 4 | 3, 4, 5 | Far, new, near a rival, no response to the warning |
| War started by an AI | 11 of 11 captures | all | We never declared. The war cannot end before 10 turns |
| War not ended | not the cause | | All 11 captures came within 10 turns of the first sighting |
| Flip that returned | 0 shown | | Not measurable before issue 156 |

Two sites account for 9 of the 11 captures: Uxmal and its replacement Tikal at (64,22) (5 losses: once in every run, plus once more in Run 6) and the capital (4 losses, one in every run). The (64,22) site is the second city every run and fell before turn 90 in three runs of four, to Sumeria. The capital falls to whichever AI attacks it between turns 135 and 304.

## Effectiveness check

`settling: cities lost` counted every `city_lost` event. It now reports permanent losses: a loss is removed when a later `city_gained` event names the same city id, and the note says how many lost events there were, how many came back and how many followed a loyalty warning (up to 30 turns before). The logs of Runs 3 to 6 have no `city_gained` events, so their figures are unchanged (Run 6: 5 lost, 0 came back, 0 after a loyalty warning; Run 5: 3, 0, 1). The warning match uses the city id in the event, so it is only as good as the monitor's ids. Unit tests cover a flip that came back, a loyalty loss, a stale warning and a gain that predates the loss.

The monitor still has two gaps this check cannot close:

- It never reports a gain (`monitors.py` compares the previous and current city id sets and emits `CityLost` only). Issue 156's `city_gained` fixes this.
- Its baseline is empty after a restart (`self._cities = None`), so a city lost while the bot is down, as in a hang or a reload, is never logged. The count is a lower bound.

## Suggested fixes and targets

These are proposed issues. None has been created.

1. **War-time defence.** When a war is first seen, or hostile units of a rival sit within 3 tiles of a city, (a) lower the gold reserve to zero for that turn, (b) upgrade the weakest defender in the most threatened city first, (c) buy a defender in a city that holds fewer than 2, (d) set production to a defender or walls there. Target: no loss within 10 turns of the first sighting of a war (today 11 of 11 captures), and gold above reserve with nothing bought and a city under threat: 0 turns (today every war turn).
2. **Keep the garrison obsolescence in check.** Stop building Warriors once Man-at-Arms or Pikeman is available, and upgrade Warriors in cities near a rival's border when gold allows (165 to 270 each). Target: at most 20% of defenders in cities hold a unit with CS under half the strongest rival unit seen within 5 tiles in the last 20 turns.
3. **Units enter the city.** Treat a `STACKING_CONFLICT` on a city tile as a signal to swap, or to put the second unit on an adjacent hill or wall tile that covers the city, rather than standing in the open. This is fix 8 in `policy-effectiveness.md`. Target: 2 defenders on the city tile of a threatened city in 90% of turns with hostile units within 2 tiles; refused-move share under 25% (today 45 to 58%).
4. **Loyalty-aware settling and response.** Add a loyalty term to the site score: reject sites more than 8 tiles from the capital that are within 6 tiles of a rival city, and prefer sites that raise our own loyalty. Respond to `city_loyalty_warning` with the available tools (a governor, an amenity or a garrison, or stop expanding there). Target: 0 loyalty losses per run (today 4 in 4 runs); no city warned within 15 turns of founding.
5. **Defend the (64,22) site or skip it.** It fell to Sumeria before turn 90 in three of four runs. Either garrison it with two units and walls before turn 70 (the first Sumerian war falls between turns 68 and 84), or found it later. Target: the (64,22) city is held past turn 100 in 3 of 3 runs (it fell before turn 90 in 3 of 4 runs).
6. **Record what happened to a city.** Extend `CityLost` with the new owner and a cause (captured, destroyed, loyalty, unknown) from the tile owner after the loss, and add `city_gained`. Persist the monitor's city set across a restart. Target: every `city_lost` carries a cause and a taker; a restart does not hide a loss.
7. **Pre-war signals.** A denounce plus rival units inside our borders (Mapuche's Line Infantry stood 4 tiles from Tikal on turns 216 to 243) is the cue for fix 1, ahead of the declaration. Target: defenders raised before the declaration in Mapuche's case, 25 turns earlier.

## Evidence index

- Event logs: `runs/STD_T1_01_<session>_events.jsonl` for Run 4 (`20261110T220138Z`, `from_run4_popups`, `from_run4_fastdiplo`), Run 5 (`20261111T220949Z`, `20261111T233601Z`, `20261112T001949Z`), Run 6 (`20261112T052854Z`, `20261112T134816Z`), Run 3 (`from_fullgame`).
- Per-turn military decisions: the `military_decision` JSON lines of each `_bot.log`.
- The capital's re-foundation: `20261112T134816Z_settle.jsonl` (turn 238) and the turn-240 notice in `20261112T134816Z.jsonl`.
- Peace timing: `wars:` lines of each bot log (`no peace offer to <civ>, war is N turns old, game waits 10`).
