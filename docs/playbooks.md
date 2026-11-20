# Playbooks

Issue 244. A playbook is a named response to a recurring situation: how to spot it, how urgent it is, what to do at each urgency, when to raise or lower the urgency, and when it is over. This note covers the foundation (slice 1): the format, the runner, the combining rule and the occurrence records. No shipped policy plays differently because of it. The runner is off unless built with `enabled=True`, makes no model calls, and the `playbooks/` directory holds only a marked example.

Code: `src/civ6_ai/strategy/playbook.py` (format, parser, combining), `playbook_runner.py` (handler, records, reader). Tests: `tests/test_playbooks.py`.

## Format

One JSON file per playbook, versioned by `version` (the schema, 1) and `playbook_version` (this playbook's own version, a string). The parser rejects a file whole with every problem listed, like the policy document.

| Field | Meaning |
|---|---|
| `name`, `playbook_version` | Identity. Records carry both. |
| `example` | `true` marks design material. `load_playbooks` skips it unless asked. |
| `trigger` | `{when, hold}`: starts an occurrence when `when` held for `hold` turns in a row. |
| `levels` | Up to 6, each with `name`, rising `urgency`, optional `settings`, `nudges`, `raise_when`, `lower_when`. |
| `raise_hold`, `lower_hold`, `min_dwell` | The hysteresis, below. |
| `end` | `{when, hold}`: closes the occurrence. |
| `outcome_signals` | Readings whose start, max and end values go in the record. |

A **condition** is data, never evaluated text: a comparison `{"signal", "op", "value"}` with `op` one of `>= > <= < == !=`, or `all`, `any` or `not` over conditions (depth and width limited). The signals are a fixed list (`KNOWN_SIGNALS`) of legal reads such as `camps_near_city` and `city_danger_max`. A signal the provider does not supply is missing, and a comparison on a missing signal is false.

## Levels and settings

A level changes the bot two ways, both through the policy document mechanism (`resolve`, then `apply_resolved`), so there is no second way of steering the policies.

- `settings` is a partial policy document (posture, `expansion.target_cities`, weights), validated by the policy document's own parser.
- `nudges` are bounded relative steps on one named setting, the form posture dials (issue 321) take: `{setting: "production.army_per_city", step: 0.4, min: 1.0, max: 3.0}` scales the resolved value by `1.4` and clamps it to 1.0 to 3.0. The step is not cumulative, so a level always means the same thing. Steps are limited to -0.9 to 3.0, and the bounds must be in range.

## Hysteresis

The level must not flip every turn, so:

- a raise rule must hold `raise_hold` turns in a row (default 2) and a lower rule `lower_hold` (default 4), so a level is slower to drop than to rise;
- after any move the level stays `min_dwell` turns (default 2);
- between the raise and lower lines nothing moves, so write the lower threshold well under the raise threshold;
- one move is one level, and raising wins over lowering on the same turn.

A new occurrence starts at the lowest level. The end condition is checked first, at any level. A playbook can open again after it closes.

## Combining

Posture first, then active playbooks; higher urgency wins a clash, ties go to the earlier-started occurrence; explicit weights in the policy document win over all; nudges apply last. The full rule is in [decisions.md](decisions.md#playbooks) and is tested.

## Occurrence records

`runs/<run>_playbooks.jsonl` has one JSON line per occurrence: playbook and version, start turn, the levels reached with the turn of each, peak level, end turn, `status` (`resolved` or `unresolved at game end`) and `outcome.signals`. The file is rewritten whole whenever an occurrence opens, moves or closes, so one still open at the end of the game is already in it as unresolved. `read_occurrences(path)` returns typed records, and `summarise` gives per playbook version: occurrences, resolved, mean turns to resolve and peak levels. `scripts/slice_report.py` prints that summary when the file exists. `effectiveness.py` can adopt the same reader later.

## Promotion

A playbook version is promoted (made the default in full games) only when its occurrence records beat the current version on its focused practice save, with full games as a guardrail. Promotion is a reviewed change to the playbook file. Nothing is learned during a game: a situation comes up too rarely in a mixed game. Automatic promotion stays with Phase 5.

## How later work plugs in

- **Barbarian camps (issue 244, later slice):** the numbers in `policies/military.py` become the `clear` level of a barbarian playbook; the threat map supplies `camps_near_city` and `city_danger_max`. The shipped version must play the same as now.
- **Game-start posture dials (issue 321):** a playbook version whose levels carry nudges on `army_per_city`, `target_cities`, faith weights and so on, moved by signals such as hostiles seen over N turns. Its signals need adding to `KNOWN_SIGNALS` and the provider.
- **Weak-neighbour aggression (issue 303):** a playbook triggered by `weak_neighbour_distance`, whose upper levels set the `conquer` posture.
- **Loyalty:** `CityLoyaltyResponse` can become a playbook later.

## Not in this slice

A live signal provider (the runner takes any async function returning `Readings`), `run_game.py` wiring, and the barbarian playbook. These are live-check items.

## Example

[`playbooks/EXAMPLE_barbarian_pressure.json`](../playbooks/EXAMPLE_barbarian_pressure.json) shows every part of the format. It is marked `example` and is not shipped: its numbers are illustrative, not tuned.
