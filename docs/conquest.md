# Conquest of weak city-states (issue 303)

Goal: take a weak city-state that blocks expansion, with the smallest force that gets the job done at a chosen confidence. It is a recurring situation, so it should end up as a playbook of the kind described in #244 (trigger, urgency levels, end condition, per-occurrence records), and it is one of the dials of the game-start playbook (#321).

## Lessons from the first human game

- Anshan, an unwalled city-state, fell around turn 60 to 2 Legions and a Battering Ram. The ram was probably not needed: it only opens walls.
- Bandar Brunei had walls. It took 4 Legions, a ram and a warrior, and one warrior was lost.
- The owner did not know how much force was enough. The bot should compute it, and check for walls before building any siege unit.

## Slice 1 (this change): pure estimates

- `strategy/siege.py`: `estimate_force(city, current=..., confidence=0.8)` returns the smallest force (melee, siege, ranged counts) that takes the city at that confidence, the win probability of the current force, the turns to capture, expected losses and a list of reasons. Seeded Monte Carlo, so one input gives one answer. Walls are modelled: melee does little to them, siege units do full damage, so an unwalled city never gets a ram.
- `strategy/conquest_targets.py`: `rank_targets(candidates)` ranks city-state candidates with a reason per score term. Allies and far cities are dropped.
- The damage model and wall factors are approximate (about `30 * exp(0.04 * strength difference)` with a 0.8 to 1.2 factor). They must be checked against the game's own combat estimate before any decision relies on them (follow-up 1).
- The bot still never declares war. Nothing calls these modules yet.

## Follow-up steps

1. **Calibrate the estimator against the game's combat estimate.** Read the estimate for real attacker and defender pairs, including a walled city, and fit the constants. Read the city's real strength, HP, wall HP and garrison with legal reads.
2. **Target survey and trigger.** Feed `rank_targets` from legal reads (envoys, walls, garrison, distance, resources, and whether the city blocks settling sites, from the loyalty and settle survey). Define the playbook trigger and the watch level.
3. **Muster and siege plan.** Build the force `estimate_force` asks for and gather it away from the target, reusing the camp machinery (#295: committed target, no flip-flopping, give-up rules). The prepare level of the playbook.
4. **Declare war and capture.** Declare war only when the force is in place and the estimate holds, attack in the order siege, ranged, melee, and capture. Handle the diplomatic cost (grievances, other city-states) and abort when the odds fall.
5. **Keep or raze.** Decide by the city's value, loyalty and the settle survey. Cities we keep need a garrison.
6. **Loyalty after capture.** A captured city-state city has foreign-loyalty pressure and unhappiness; connect it to the loyalty model (#314) and the loyalty response.
7. **Peace and end condition.** Make peace once the city is taken or the attack is abandoned; close the occurrence record with turns, units lost and the outcome, and feed `effectiveness.py`.

Each step sits behind a setting, default off, until its practice run shows it works.
