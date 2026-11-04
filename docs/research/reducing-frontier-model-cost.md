# Reducing Frontier-Model Cost in Civilization VI Agents: Local Models, Jev, and LLM-Generated Deterministic AI

## Executive assessment

The strongest architecture for a Civilization VI agent is **not** “replace the frontier model with one cheaper model.” It is a hierarchy in which deterministic software handles everything that can be expressed as a rule or optimization problem, a small local model handles routine semantic choices, a specialized decision model such as Jev handles bounded classifications or rankings, and a frontier model is invoked only for genuinely open-ended strategic reasoning and for improving the lower layers. This conclusion follows naturally from CivBench: a normal Civ VI game lasts 300+ turns, a typical turn involves roughly 5–15 MCP calls, and a complete run therefore produces thousands of model-mediated interactions. citeturn440679view2turn309354view2

The distinction that matters is between **decision frequency** and **decision difficulty**. Most Civilization actions are frequent but not especially difficult: choosing between a handful of production options, moving a scout toward unexplored territory, improving an unimproved luxury, healing a damaged unit, or selecting the highest-yield trade route. A much smaller number of decisions are both consequential and genuinely strategic: whether to pivot victory conditions, declare war, abandon an expansion plan, undertake a timing attack, choose between long-term economic and military trajectories, or respond to another civilization approaching victory. CivBench's MCP already exposes 76 structured tools and 29 narration functions, so these layers can operate on structured state rather than pixels. citeturn309354view1turn440679view0

My recommended target therefore looks like this:

```text
                     Frontier strategist
                       invoked rarely
                             │
              strategy / policy / code changes
                             │
          ┌──────────────────┴──────────────────┐
          │                                     │
    Local small LLM                         Jev
  ambiguous tactical              bounded semantic decisions
      reasoning                    ranking / classification
          │                                     │
          └──────────────────┬──────────────────┘
                             │
                    Deterministic policy layer
                  rules / scoring / planners
                             │
                        civ6-mcp
                             │
                     Civilization VI
```

Over time, I would expect the deterministic layer to grow and the real-time LLM layer to shrink. In the mature version, the frontier model becomes less the player and more the **designer, critic, and strategic commander of the player**. That design has close precedents in Voyager, Code as Policies, and DeepMind's AlphaEvolve: all use LLMs to create executable programs whose repeated execution is cheaper and more consistent than asking a language model to reason afresh about every low-level action. citeturn888394academia5turn888394academia6turn888394search0

## Why Civ VI is unusually suitable for this decomposition

The current `civ6-mcp` implementation already removes most of the reason one would need a large general-purpose model at every interaction. Its server communicates with Civilization VI through FireTuner, generates Lua inside the game's GameCore and InGame environments, and exposes structured functions for units, cities, research, trade, diplomacy, governors, religion, World Congress, victory monitoring, and game lifecycle management. The game itself remains responsible for enforcing game rules. citeturn309354view1

More importantly, some MCP functions already embody the beginning of a non-LLM tactical intelligence layer. `get_settle_advisor` scores settlement locations using yields, water, defense and resources; `get_global_settle_advisor` searches revealed territory for promising settlement positions; and `get_builder_tasks` produces a prioritized work board for builders, with pillaged improvements and strategic resources receiving higher priority than ordinary improvements. citeturn915120search0 These are precisely the sort of functions that should proliferate.

CivBench also provides unusually good evidence about where LLM architectures fail. Even frontier agents under-query important state: strategic-monitoring calls represented only about 0.96–2.13% of non-infrastructure calls, despite explicit instructions to monitor such information. In 7 of 20 defeats where an opponent's impending victory could have been detected at least twenty turns in advance, the agent did not query victory progress during that warning window. citeturn440679view2 Separately, the benchmark found that only 48.2–65.8% of near-term commitments recorded in agents' planning reflections were actually executed within ten turns. citeturn309354view2

Those failures are important because neither really requires a better language model. A scheduler can execute:

```python
if turn % 15 == 0:
    check_victory_progress()

if rival_science_victory_eta < 30:
    escalate_to_strategist()

if city.production_empty:
    choose_city_production(city)

if builder.is_idle:
    execute_best_builder_task(builder)
```

A deterministic implementation cannot “forget” to run these checks. That suggests an architectural principle:

> **LLMs should decide things whose answers cannot cheaply be specified in advance. Software should remember, enumerate, calculate, schedule, verify and execute everything else.**

That would directly attack both problems CivBench exposed: under-monitoring and failure to carry out previously stated intentions. citeturn309354view2turn440679view2

## Local models are a strong fit for the tactical middle layer

A locally hosted model is technically straightforward because the problem is already text-and-tool based. `civ6-mcp` speaks MCP/JSON-RPC, while common local inference servers expose OpenAI-compatible APIs and support structured tool calls. `llama.cpp`, for example, supports quantized CPU/GPU inference, an OpenAI-compatible server, schema-constrained JSON, and function calling. vLLM similarly supports named and required function calling and can enforce JSON schemas through constrained decoding. citeturn311297search2turn311297search0

That means you do not need the local model to be “an MCP client” in any special sense. Your orchestrator can translate between the two interfaces:

```text
civ6-mcp
    ↓ state
orchestrator
    ↓ compact tactical state
localhost: OpenAI-compatible model endpoint
    ↓ structured decision
orchestrator
    ↓ MCP action
civ6-mcp
```

There are now open models small enough to make this plausible on consumer hardware while still being trained for tool use. Qwen's 4B models explicitly emphasize instruction following, reasoning, coding and tool usage; Qwen documents MCP integration through Qwen-Agent. Qwen3-8B likewise supports tool calling and can switch between a slower reasoning mode and a faster non-thinking mode. citeturn210112search0turn210112search2turn210112search5 Salesforce's xLAM family is even more specialized: its 1B, 3B and 8B variants are specifically fine-tuned for function calling and multi-turn agent interaction, and GGUF releases exist for local deployment. citeturn210112search9turn210112search10

I would benchmark at least three local-model classes rather than choosing one immediately:

| Role | Candidate scale | Suitable Civ tasks |
|---|---:|---|
| Ultra-cheap dispatcher | 1–3B function-calling model | classify event, choose handler, select one of several legal routines |
| Tactical reasoner | ~4B | city builds, builder choices, scouting, trade-route choices |
| Difficult local specialist | ~8B | military positioning, policy-card choices, multi-step tactical plans |

Parameter count alone does not determine memory use, but low-bit quantization makes these sizes dramatically more practical than their full-precision equivalents. `llama.cpp` is explicitly designed to run quantized models across CPU and GPU, so even systems without a large accelerator can participate, albeit at lower throughput. citeturn311297search2

For Civ, **throughput matters more than latency**. You are not running a realtime shooter. A local model taking several seconds to resolve twenty tactical decisions is acceptable if those decisions occur while a Civ turn is processing. You can also batch work by domain: generate all city recommendations together, all worker recommendations together, and all unit recommendations together. vLLM supports parallel serving and structured tool output, making it attractive if your machine has sufficient GPU memory; `llama.cpp` is probably the simpler first experiment on mixed or modest hardware. citeturn311297search0turn311297search2

The more important optimization, however, is **not sending the entire Civ state into the local model**. Give a city model something like:

```json
{
  "city": {
    "population": 7,
    "production": 18,
    "food_surplus": 3,
    "districts": ["Campus", "Commercial Hub"]
  },
  "empire_policy": {
    "victory": "science",
    "war_posture": "defensive",
    "expansion": "moderate"
  },
  "legal_builds": [
    {"id": "University", "turns": 7},
    {"id": "Settler", "turns": 8},
    {"id": "Crossbowman", "turns": 5}
  ]
}
```

instead of an entire 30,000-token game transcript. Qwen's models can support very long contexts, but cheap inference does not make unnecessary context free. citeturn210112search1turn210112search5

A particularly useful architecture would let the frontier strategist periodically produce a compact **policy document**:

```yaml
victory_target: science
strategic_horizon: 30
expansion_priority: medium
military_posture: deterrence
gold_reserve_minimum: 500
research_priorities:
  - universities
  - industrialization
  - rocketry
city_policy:
  science_city: maximize_science
  frontier_city: defense_then_growth
war_escalation:
  require_frontier_model: true
```

The local models would then optimize within those constraints. The strategic model would not need to think about every archer or granary.

## Jev is unusually well matched to some Civ decisions — but not to all of them

Jev is worth testing because it occupies a different point in the design space from both frontier and local generative LLMs. Rather than generating prose, it takes a state plus typed questions and returns bounded answers and probability distributions. Its supported primitives are essentially **choice**, **score**, and probabilistic yes/no; one request can contain up to twenty questions. citeturn387742search0turn387742search1

That maps remarkably well to many Civilization decisions:

```text
CHOICE
Which legal city build best supports the current strategy?
→ University / Settler / Crossbow / Market / ...

SCORE
How strategically important is defending this city?
→ negligible / low / medium / high / existential

YES/NO
Should this scout retreat instead of continuing exploration?
→ P(retreat)

CHOICE
Which governor should receive the next promotion?
→ Pingala / Magnus / Liang / ...

SCORE
How dangerous is Civilization B over the next 20 turns?
→ minimal / manageable / serious / critical

YES/NO
Does this event justify escalating to the frontier strategist?
→ probability
```

Its current public API describes roughly 0.2-second median upstream latency, up to twenty questions in one call, a 32K-token context, and typed probability-bearing responses rather than generated prose. citeturn387742search0turn387742search1 Independent research suggests that specialized decision models can indeed be dramatically cheaper and faster on suitable classification tasks, although the exact advantage varies strongly by comparison model and workload. One 2026 comparison of Jev against LLM rubric judges reported LLM costs 29–325 times higher and latency 30–220 times higher, while also finding important correlated error modes. citeturn421330academia6 Another independent study found Jev competitive with both a commercial LLM and an open model on several political-science classification tasks, but found no cost advantage against a cheap batch-priced commercial model; it concluded that Jev's clearest benefit there was speed and convenient probability output. citeturn421330academia7

So I would treat “Jev is 100× cheaper” as workload-dependent rather than universal.

The article you linked, published September 25, 2026, reports an empirical comparison across 3,080 classification tasks and frames Jev as a decision layer rather than a replacement for generative LLM reasoning. citeturn421330search0turn421330search1 That is exactly how I would deploy it here.

There is also a strikingly relevant artifact already in existence: the public `jev-civ6` repository is explicitly described as a **“decisions-only LLM harness over civ6-mcp”**, and it retains per-run decision logs and screenshot timelines. citeturn309354view0 This is still a very small experimental project rather than a mature ecosystem—the repository showed no stars or forks at the time of retrieval—but it confirms that somebody has independently arrived at essentially the same integration concept. citeturn309354view0

Where I would **not** use Jev is equally important. An independent benchmark across thirteen knowledge and reasoning benchmarks found Jev competitive on many knowledge and commonsense problems but substantially weaker on mathematical word problems, trailing the frontier median by 17.7 points on MathQA. citeturn421330academia8 Exact arithmetic, pathfinding and numerical optimization should therefore remain in code. Open-ended plans such as “derive a military strategy for exploiting Rome's geography while preventing Korea's science victory” belong with a generative reasoning model.

A useful routing principle would therefore be:

```text
Can the answer be computed exactly?
    → CODE

Is the answer one of a known finite set,
but requires semantic/contextual judgment?
    → JEV

Does it require a little open-ended reasoning,
but is local and low-risk?
    → LOCAL LLM

Does it require genuinely new strategy,
cross-domain synthesis or creation of a new policy?
    → FRONTIER LLM
```

This is significantly more attractive than simply replacing GPT/Claude/Gemini calls one-for-one with Jev.

## The highest-leverage idea is letting the LLM write the AI instead of being the AI

Your third idea is the one I would invest in most heavily.

There is substantial precedent for using an LLM to generate **executable policies** and then reusing those policies without involving the model on every action. Code as Policies demonstrated that language models can synthesize programmatic control policies consisting of ordinary logic, loops, API calls and calculations. citeturn888394academia6 Voyager went further in Minecraft: GPT-4 generated executable skills, tested them against the environment, incorporated execution errors and feedback, and stored successful skills in a growing reusable library. The authors deliberately chose code rather than low-level actions because programs naturally encode temporally extended and compositional behavior. citeturn888394academia5turn888394search1

Civ VI is arguably even better suited to this approach than Minecraft because its state is discrete, turn-based and already available through structured APIs.

A frontier model could create routines such as:

```python
def choose_builder_task(state, strategy):
    tasks = state.builder_tasks

    # strategic resources first
    for task in tasks:
        if task.is_unimproved_strategic_resource:
            return task

    # repair pillaged economic tiles
    for task in tasks:
        if task.is_pillaged and task.expected_yield_recovery >= 3:
            return task

    # improve luxuries if amenities are constrained
    if state.empire.amenities_margin <= 1:
        luxuries = [t for t in tasks if t.resource_type == "luxury"]
        if luxuries:
            return max(luxuries, key=lambda t: t.expected_value)

    return max(tasks, key=score_builder_task)
```

That routine could resolve hundreds of worker decisions for zero inference cost.

The frontier model should then be asked a *different* question after a game:

> Here are all builder decisions, resulting yields, missed resources, city-growth trajectories and the final outcome. Propose a modification to `choose_builder_task()` that would likely improve performance. Do not change unrelated behavior.

That converts your Civilization environment into an **automatic policy-development laboratory**.

DeepMind's AlphaEvolve provides a particularly relevant design template. It uses a fast model to generate many candidate code modifications, a stronger model to contribute deeper ideas, automated evaluators to score resulting programs, and an evolutionary database that preserves promising variants. citeturn888394search0 The critical feature is that the LLM does not itself decide whether its idea worked: **the evaluator does**. AlphaEvolve has subsequently been used on optimization problems where candidate algorithms can be objectively scored. citeturn888394search0turn888394search2

Civ VI provides exactly this kind of evaluator.

You could score candidate policies using metrics already represented in CivBench:

```text
overall outcome
relative score
science / turn
culture / turn
gold
cities founded
exploration %
military strength
unit kill/loss ratio
technology pace
settlement quality
tool errors
victory progress
```

CivBench itself defines evaluation dimensions including economic management, military competence, scientific progress, diplomatic skill, spatial reasoning, tool-use fluency and long-horizon coherence. citeturn440679view2 The public CivBench dataset additionally exposes complete per-turn state, tool calls and structured reflections, making it usable as an initial corpus for developing offline evaluators before you have accumulated many games of your own. citeturn755127search8

The development loop I would build is:

```text
                    ┌───────────────┐
                    │ Current AI v7 │
                    └───────┬───────┘
                            │
                   run many Civ games
                            │
                            ▼
                       telemetry
                            │
              ┌─────────────┴─────────────┐
              │                           │
       failure analysis              evaluation
     frontier / local LLM        deterministic metrics
              │                           │
              └─────────────┬─────────────┘
                            ▼
                 candidate code changes
                      A / B / C / D
                            │
                    regression tests
                            │
                   tournament games
                            │
                    statistical score
                            │
                       best survives
                            │
                          AI v8
```

Crucially, I would **not allow the live game agent to rewrite production code and immediately use it**. That makes attribution nearly impossible and risks catastrophic regressions halfway through a game. Instead, have it propose candidate modules into a sandbox, run unit tests and scenario tests, then promote code only after evaluation.

There is room for limited within-game learning, but I would constrain it to data rather than source-code mutation. For example, the strategic model can change:

```yaml
weights:
  science: 1.20
  production: 0.90
  military: 1.35
```

during a game. It should not rewrite the combat planner at turn 172 unless you are explicitly running an experimental self-modifying mode.

## A practical architecture I would build

The cleanest system would split the Civ AI into **three timescales** rather than merely three model tiers.

At the **action timescale**, which may execute dozens of times per turn, use deterministic algorithms whenever possible. Movement should use pathfinding. Worker decisions should use scored candidate tasks. Target selection should use expected combat outcomes. City production should first eliminate dominated choices and apply hard strategic constraints. Research should use prerequisites and target-value calculations. Monitoring should occur on fixed schedules and event triggers. The existing MCP advisors already demonstrate this pattern. citeturn915120search0

At the **tactical timescale**, invoke Jev or a local small model only when several reasonable actions remain. I would prefer Jev where the remaining problem can be represented as a fixed choice or scored rubric, and a local 4B–8B model where the decision benefits from a short explanation or a multi-step local plan. Both should receive a small domain-specific state representation rather than generic MCP narration. Jev's API is explicitly designed around state plus bounded typed questions, while Qwen and xLAM provide local tool-capable alternatives for generative reasoning. citeturn387742search0turn210112search2turn210112search9

At the **strategic timescale**, invoke the frontier model perhaps every 10–20 turns and on exceptional events:

```text
WAR_DECLARED
CITY_CAPTURED
CITY_LOST
RIVAL_VICTORY_WARNING
NEW_ERA
NEW_CONTINENT_DISCOVERED
STRATEGIC_RESOURCE_SHORTAGE
VICTORY_PATH_INVALIDATED
MAJOR_DIPLOMATIC_REVERSAL
```

The result should not be thirty immediate unit orders. It should update the policy state that governs all lower layers.

A fourth, **offline evolutionary timescale**, operates between games. A strong coding model analyzes game traces and proposes changes to deterministic policies. Candidate policies run against fixed scenarios and seeds, with the incumbent retained unless the challenger demonstrates improvement. This mirrors the evaluator-driven principle used by AlphaEvolve rather than trusting a generated patch merely because its reasoning sounds plausible. citeturn888394search0

That gives you an eventual agent something like:

```text
                         CIV VI
                            │
                         civ6-mcp
                            │
                        State Bus
                            │
       ┌────────────────────┼────────────────────┐
       │                    │                    │
   Monitoring            Tactical           Strategic
   + Rules Engine          Router             Router
       │                    │                    │
       │              ┌─────┴─────┐              │
       │              │           │              │
       │            Jev       Local LLM      Frontier LLM
       │              │           │              │
       └──────────────┴─────┬─────┴──────────────┘
                            │
                      Policy / Task DB
                            │
                            ▼
                       Action Queue
                            │
                         civ6-mcp


              OFFLINE / BETWEEN GAMES

        telemetry → evaluator → coding LLM
                           ↓
                 candidate policies
                           ↓
                  simulation / A-B
                           ↓
                      promotion
```

The **Policy / Task DB** is particularly important. CivBench shows that frontier models often formulate reasonable intentions but fail to carry them out later. A plan such as “settle the northern iron within eight turns” should therefore become a machine-readable task:

```json
{
  "goal": "settle",
  "target": [31, 17],
  "deadline": 126,
  "priority": 0.88,
  "dependencies": [
    "produce_settler",
    "escort_available"
  ],
  "status": "active"
}
```

Software—not the LLM—should notice each turn whether the goal is still pending. That directly addresses CivBench's measured gap between stated plans and subsequent execution. citeturn309354view2

## Recommended experimental path

I would treat the system as an incremental research program rather than trying to build the final architecture immediately.

First, run a handful of games with the existing frontier-only `civ6-mcp` setup and preserve every tool call and turn-state record. This establishes your own baseline and provides examples for later specialists. CivBench already demonstrates the usefulness of complete trace logging and provides reproducible metrics and scenarios. citeturn440679view2turn755127search8

Second, replace the easiest model-mediated loops with deterministic handlers: required monitoring, builder management, idle-unit handling, obvious healing/fortifying, trade-route scoring, production sanity checks and scheduled victory monitoring. Every replacement should have a fallback path to the frontier model when its confidence or rule coverage is insufficient.

Third, stand up a local OpenAI-compatible endpoint using `llama.cpp` and benchmark a 3B/4B and an 8B tool-oriented model against recorded decisions from your frontier baseline. Qwen3 and xLAM are natural starting points because both explicitly support tool-oriented use, while `llama.cpp` and vLLM provide local structured interfaces. citeturn311297search2turn311297search0turn210112search2turn210112search9 Do not initially let the local model play live; replay archived game states and ask whether its decisions match or improve on the baseline.

Fourth, run the same replay corpus through Jev. Convert tactical situations into Choice, Score and Noul questions and measure both outcome agreement and confidence calibration. Jev's probabilities make it particularly suitable for a **selective automation** strategy: accept a decision when confidence exceeds a validated threshold and escalate uncertain cases. Independent Jev studies support this cascading idea but also warn that correlated errors can limit the benefit, so thresholds should be measured rather than assumed. citeturn421330academia6turn387742search4

Fifth, build your first LLM-generated deterministic subsystem. I would choose **city production** or **builder policy**, because both provide frequent observations and relatively clear objective measures. Give the frontier coding model the current policy, several hundred historical decisions and resulting outcomes, and ask for one narrowly scoped improvement. Evaluate old and new versions across identical saved states or fixed-seed scenarios.

Only after that would I build the full AlphaEvolve-like loop in which candidate code is generated automatically, evaluated over batches of games, preserved in a population, and selectively promoted. DeepMind's results suggest evaluator-driven code evolution is particularly effective when candidate quality can be measured objectively; Civilization supplies an unusually rich set of numerical metrics for doing so. citeturn888394search0turn440679view2

The long-term target I would aim for is roughly **90–99% of individual game interactions requiring no frontier-model call at all**. That percentage is a design target rather than an empirical claim: whether it is achievable will depend on how much tactical autonomy you are willing to encode. But the structure of Civ VI strongly favors it. The frontier model should be spending its tokens on questions like *“Are we still pursuing the right theory of how to win this game?”* and *“How should our military doctrine change after encountering this opponent?”*, not *“Which farm should this builder improve?”*

The most interesting consequence is that cost reduction stops being the primary goal. You potentially get a **stronger agent**. Deterministic monitors do not forget victory checks; task queues do not lose five-turn intentions; pathfinders calculate paths better than language models; local specialists can make hundreds of routine decisions without marginal API cost; Jev can cheaply adjudicate bounded forks; and a frontier model gets to concentrate on the part of Civilization where its capabilities are genuinely valuable. CivBench's observed monitoring and execution failures suggest that this division of labor may improve reliability even before considering cost. citeturn309354view2turn440679view2