# Structured state representations and derived spatial features in game AI (lessons for a Civ VI bot)

Context: the civ_6_ai bot reads state through the game's scripting API, respects fog, and builds a per-tile threat map (issue 238; `docs/threat-map.md`). Each threat **source** (camp or last-seen hostile unit with strength, first_seen, last_seen) fades linearly with distance (by movement cost from stage 1b), plus layers for our cover, a fog penalty, and per-turn JSONL dumps. The notes below ask what research systems do and what carries over.

## 1. SC2LE/PySC2 feature layers and AlphaStar: why feature layers rather than RGB, and what they found

### Takeaway
DeepMind built PySC2 on fixed-size, top-down grids of categorical and scalar features plus non-spatial scalars. The API did not render RGB, and the grids suit neural networks and human replays. Categorical planes were embedded (one-hot plus 1x1 conv) and large scalars were log-scaled. AlphaStar later moved to three encoders: an entity list (transformer), minimap planes (ResNet) and scalars. In both, the hand-designed spatial planes are simple facts (visibility, height, owner, type), not derived danger fields. The network learns any danger.

### Cited Findings
- The StarCraft II API "does not currently render RGB pixels. Rather, it generates a set of 'feature layers', which abstract away from the RGB images seen during human play, while maintaining the core spatial and graphical concepts". — [Vinyals et al. 2017, SC2LE, arXiv 1708.04782](https://arxiv.org/abs/1708.04782)
- The layers are rendered at N×M (N=M in experiments, 64×64 for replay training). Each represents one thing, "for example: unit type, hit points, owner, or visibility". Some are scalars (hit points, height map) and some categorical (visibility, unit type, owner). The **minimap** is "a coarse representation of the state of the entire world" and the **screen** is a detailed view of the camera. Owner and visibility exist on both; unit type and hit points are screen only. The viewer's top row shows "terrain height, fog-of-war, creep, camera location, and player identity". — [SC2LE](https://arxiv.org/abs/1708.04782)
- Non-spatial observations (minerals, gas, available actions, selected-unit details, build queues) come separately, and the list of legal actions comes as an observation. Agents mask unavailable actions and renormalise. — [SC2LE](https://arxiv.org/abs/1708.04782)
- Layers use a top-down orthographic camera "so each pixel ... corresponds to precisely the same amount of world real estate", which the authors say does not quite match human perception. — [SC2LE](https://arxiv.org/abs/1708.04782)
- Preprocessing: "We embed all feature layers containing categorical values into a continuous space, which is equivalent to using a one-hot encoding in the channel dimension followed by a 1×1 convolution. We also re-scale numerical features with a logarithmic transformation as some of them such as hit-points or minerals might attain substantially high values." — [SC2LE](https://arxiv.org/abs/1708.04782)
- Rationale and future plan: the authors meant to expose RGB later "to study the effects of learning from raw pixels versus learning from feature layers". They played with feature layers themselves "to verify that agents are not severely handicapped" and found 64×64 enough to control small units. They saw fixed-size feature layers as "natural for neural network based agents". — [SC2LE](https://arxiv.org/abs/1708.04782)
- The raw API (unit list with type, owner, coordinates, health; fog still applies; no camera) is "a simpler and more precise representation". DeepMind called it "cheating" for human comparison because it shows all visible units at once. — [SC2LE](https://arxiv.org/abs/1708.04782)
- Results: baseline agents reached novice level on mini-games but "cannot learn to win against the easiest built-in AI on the full game". Fully convolutional agents did best among the baselines. — [SC2LE](https://arxiv.org/abs/1708.04782)
- AlphaStar uses three encoders, then an LSTM core and autoregressive heads:
  - an entity encoder: a transformer over a list of visible units/buildings, reportedly capped at 512 entries;
  - a spatial encoder: a ResNet over minimap planes;
  - a scalar encoder (resources, available actions, the statistic z).
  
  "Scatter connections" project entity embeddings back onto the spatial map. Secondary sources disagree on layer counts (2 vs 3 transformer layers). — [xlnwel blog](https://xlnwel.github.io/blog/reinforcement%20learning/AlphaStar); [mini-AlphaStar, arXiv 2104.06890](https://arxiv.org/abs/2104.06890); [Yekun's notes](https://cyk1337.github.io/notes/2019/07/21/RL/DRL/Decipher-AlphaStar-on-StarCraft-II)

### Inferences
- The SC2 design separates three kinds of data: **entities** (units, cities, camps as records with fields), **planes** (per-tile facts) and **scalars** (treasury, era, legal actions). The threat map already has this shape: sources are entities, `heat`/`cover`/`fog` are planes, and the rest of the bot holds the scalars. AlphaStar's scatter connection, painting entities onto the grid, is the same operation as `threat_at` summing faded source strengths.
- Visibility is a first-class categorical plane in PySC2, and CivRealm uses the same three states (section 3). The bot can treat "never seen / seen but fogged / visible now" plus "turns since last seen" as a core per-tile layer. That layer also gives the planned `oldest_knowledge` for free.
- Log-scaling large magnitudes and masking illegal actions are cheap deterministic habits worth copying: compress strength values before they reach an LLM, and list legal options rather than letting the model propose illegal ones.
- None of these systems hand-engineered "danger" planes, because they had billions of frames to learn them. A bot without training must compute danger explicitly. Freeciv's AI (section 3) is the better model for that.

### Gaps
- The AlphaStar Nature paper and supplement were not read directly. Entity-cap and layer details come from secondary sources.
- DeepMind never published the promised pixel-vs-feature-layer ablation as far as found.

## 2. OpenAI Five observation design; API vs pixels

### Takeaway
OpenAI Five used the game's bot API, not pixels. It took about 16,000 values per step (entity arrays of floats and categoricals) limited to what the team could see, plus a few per-hero derived features such as "distance to me". The stated reasons were to focus on strategy rather than vision and to avoid a many-fold compute cost.

### Cited Findings
- "OpenAI Five observes ∼16,000 total values" per step, "mostly floats and categorical values with hundreds of possibilities", structured as "a set of data arrays" rather than a flat vector. — [Berner et al. 2019, arXiv 1912.06680](https://arxiv.org/abs/1912.06680)
- Why not pixels: the goal was strategic planning, not visual processing, and rendering pixels for all training games would multiply compute "many-fold". — [arXiv 1912.06680](https://arxiv.org/abs/1912.06680)
- Fog: team-visible information is shared, so observations are "nearly identical for each hero". The model sees all available information at once where a human must click around. The authors say everything the model sees is available to a human, but admit the approximation is imperfect. — [arXiv 1912.06680](https://arxiv.org/abs/1912.06680)
- Derived features: a "very small number" depend on the controlled hero, for example "distance to me" for each unit. — [arXiv 1912.06680](https://arxiv.org/abs/1912.06680)
- PySC2's raw unit-list API is the SC2 equivalent and is "simpler and more precise" than its screen layers. — [SC2LE](https://arxiv.org/abs/1708.04782)

### Inferences
- The project's "only legal reads" rule matches the standard OpenAI Five set: observe what a human could see, through an API. The fairness argument is about which information is exposed, not whether it arrives as pixels.
- "Distance to me" per entity is the kind of derived feature that pays off cheaply. For the LLM, each threat source can carry `city_distance` (already in the dump), turns-to-reach and strength ratio against local cover, so the model does not have to do geometry.

### Gaps
- Appendix E of the OpenAI Five paper (the full feature list, including any spatial grid around heroes) was not read. I could not confirm whether a minimap-style grid was used.

## 3. 4X/Civ environments: CivRealm, CivBench (Civ VI), CivAgent/Unciv, Freeciv AI danger and want

### Takeaway
CivRealm (Freeciv) gives tensor agents stacked per-tile planes with a three-state fog layer, plus unit and city tables. Its LLM agents get local tile windows (5×5 per unit, or 15×15 compressed into 9 blocks for the stronger Mastaba agent) plus a national summary. CivBench (Civ VI via MCP) uses deterministic text narration that marks fog explicitly and lists threats as coordinate+strength+distance lines; its games cost $31–229. Freeciv's classic AI computes **per-city danger** from enemy units weighted by turns-to-reach, with urgency and grave-danger counts, and feeds it into a numeric "want" for builds. That is the closest existing design to the threat map.

### Cited Findings
**CivRealm (ICLR 2024)**
- Tensor observation: stacked M×N layers. They cover terrain type, tile owner, infrastructure (binary), resource outputs (6 binary types), unit owner, city owner, and unit distribution (52 layers, one per unit type). — [CivRealm, arXiv 2401.10568](https://arxiv.org/html/2401.10568v2)
- Fog: a per-tile status layer, where "0 means a tile has never been explored", "1 means a tile has been explored before while being covered by war fog currently", and 2 means currently in vision. — [CivRealm](https://arxiv.org/html/2401.10568v2)
- Units and cities come as attribute tables (position, owner, HP, type, attack/defence, moves left), with own-only fields set to −1 for other players' units. — [CivRealm](https://arxiv.org/html/2401.10568v2)
- BaseLang: "a 5x5 tile-based observation ... centered on each unit's location", one LLM per unit. Mastaba condenses "a 15×15 tile region into 9 blocks, each spanning 5×5 tiles" and gives an advisor a nationwide summary (units, cities, threats), which helps with "managing prompt loads effectively". The LLM used was GPT-3.5-turbo. — [CivRealm](https://arxiv.org/html/2401.10568v2)
- Mastaba "significantly outpaces" BaseLang (population, military, score; explored/settled area by turn 113 that BaseLang matched only around turn 210). But it failed to defend against pirate invasions, which its higher score triggered earlier. — [CivRealm](https://arxiv.org/html/2401.10568v2)
- Both RL and LLM agents "struggle to make substantial progress in the full game". — [CivRealm abstract](https://arxiv.org/abs/2401.10568)

**CivBench (Civ VI, arXiv 2609.02459, Sept 2026)**
- The setup has 76 MCP tools and 300+ turn episodes. A narration layer, "implemented entirely through deterministic code, without an additional language model", returns "structured text rather than raw tensors or pixels". — [CivBench](https://arxiv.org/html/2609.02459)
- "Fog-of-war tiles are marked [fog] rather than omitted" so the agent can tell unknown from empty. Example threat line: "Nearby threats (1): Barbarian (1 unit): WARRIOR at (13,24) -- CS:20 HP:100/100 (3 tiles away)". Threats are bold-marked. Exploration is summarised as "Explored: 34% of land (412/1212 tiles)". The map is about 4,000 hex tiles. — [CivBench](https://arxiv.org/html/2609.02459)
- Cost: each run "costs approximately $31–229 in API fees and takes 2–8 hours". The playbook warns "You only know what you explicitly query". Agents monitored victory state less often than asked and often failed to carry out their own short-term plans (RAG@10 of 48.2–65.8%). — [CivBench](https://arxiv.org/html/2609.02459); [search summary](https://huggingface.co/papers/2609.02459)
- The authors say representation may shape behaviour but this "has not been isolated through a controlled ablation". — [CivBench](https://arxiv.org/html/2609.02459)

**CivAgent / Digital Player (Unciv)**
- An LLM "digital player" for Unciv (arXiv 2502.20807, code at fuxiAIlab/CivAgent). A secondary summary describes a loop that starts by extracting a structured observation from the serialized game state, then uses RAG memory and tool-based rollouts. — [Digital Player](https://arxiv.org/html/2502.20807v1); [Emergent Mind summary](https://www.emergentmind.com/topics/civagent)

**Freeciv classic AI (`ai/default/daimilitary.c`, `doc/README.AI`)**
- `assess_danger()` is computed per city from enemy units that could reach it:
  - The lookahead is 3 turns by default (2 under the `H_ASSESS_DANGER_LIMITED` handicap, 6 for "CPU hog").
  - Reach is computed with a **reverse pathfinding map** from the city (`pf_reverse_map_unit_position`), plus transport and paratrooper cases.
  - Harmless units and units that cannot reach the city are skipped.
  
  — [daimilitary.c](https://github.com/freeciv/freeciv/blob/main/ai/default/daimilitary.c)
- Per-unit danger works as follows:
  - Start from attack rating times an attack bonus, divided by the city's defence bonus.
  - Squaring adds "positive feedback".
  - **Divide by move_time** when it is greater than 1, then sum into `total_danger`.
  - `urgency` is "the number of hostile units that can attack us in three turns", plus 10 × grave_danger.
  - `grave_danger` is the "number of units that can attack us next turn".
  - A separate `diplomat_threat` flag exists.
  
  — [daimilitary.c](https://github.com/freeciv/freeciv/blob/main/ai/default/daimilitary.c)
- Danger drives builds:
  - danger² is compared with the city's defence to get a want percentage, capped at 200 plus urgency.
  - Defenders are chosen while `num_defenders <= urgency`, and a city with no defender gets one at once.
  - Walls get want from `wallvalue` (90 if any threat; the code notes "FIXME: this value assumes that walls give x3 defense").
  
  — [daimilitary.c](https://github.com/freeciv/freeciv/blob/main/ai/default/daimilitary.c)
- The want system works in bands (normal, `DAI_WANT_MILITARY_EMERGENCY` "used to requisition emergency needs", domestic max). Military operation want = profit × amortization, where amortize(benefit, delay) = benefit·((MORT−1)/MORT)^delay with MORT=24. — [README.AI](https://github.com/freeciv/freeciv/blob/main/doc/README.AI)
- Known weaknesses the developers list:
  - "Assess_danger should acknowledge positive feedback between multiple attackers".
  - "Cities don't realize units are on their way to defend it".
  - "AI builds cities without regard to danger at that location".
  
  — [README.AI](https://github.com/freeciv/freeciv/blob/main/doc/README.AI)
- A later patch ("improve ai danger") changed the assessment to the worst attack action a unit could take and to count units able to capture cities. — [Freeciv patch 46046](https://dotsrc.dl.osdn.net/osdn/ticket/g/f/fr/freeciv/46046/10783/improve-ai-danger.patch)

### Inferences
- Freeciv confirms the threat-map direction and suggests three refinements for later stages:
  - **Weight by turns-to-reach, not tiles.** Freeciv divides danger by move_time from a reverse path map. Stage 1b's movement-based spread is the same idea. Reach could be stated directly in turns ("can attack next turn" / "within 3").
  - **Keep counts beside the summed number.** "Units able to hit this city next turn" (grave danger) and "within 3 turns" (urgency) are more readable and less noisy than one summed heat value. They suit `DefenceState` and LLM summaries.
  - **Its known gaps are this project's planned features.** Cover from our units on their way (the `cover` layer), danger at prospective city sites (the opportunity layer / settling), and multi-attacker synergy are all missing from Freeciv's AI.
- Freeciv's amortized want, (23/24)^delay per turn, is a ready-made, documented discount for `net_value` (opportunity − threat − cost), including "cost of doing nothing = threat per turn left standing".
- CivRealm's three-state fog plane matches the project's camp rule ("drop only when a visible tile shows no camp"). CivBench's "[fog] rather than omitted" is the text version of the same principle.
- CivRealm's evidence (Mastaba beats per-unit 5×5 windows) supports giving the LLM a coarse, aggregated regional view plus a national summary, not raw per-tile windows. Mastaba's failure against invasions suggests the threat summary needs to be first-class, not buried.
- CivBench's $31–229 per game, with the LLM pulling all state through tools, is 30–200× the project's ~$1 target. The project's split (deterministic code does the spatial work, the LLM sees a few pre-digested lines) is the right way to close that gap.

### Gaps
- CivAgent's exact map/fog encoding was not found (paper appendix and repo not read).
- Unciv's own automation threat code (a `ThreatManager` class was expected) could not be fetched (404 at the guessed path). Not verified.
- No primary source found for a Freeciv-bot (freeciv-web bot) map encoding beyond CivRealm.
- CivBench gives no per-turn token counts or latency.

## 4. How LLM game agents put spatial state into text, and evidence on format and token cost

### Takeaway
Most successful LLM game agents avoid raw maps. They fill templates with state variables (PokéLLMon), call high-level APIs (Voyager/Mineflayer), summarise frames (TextStarCraft II's Chain of Summarization), or narrate (CivBench). Benchmarks find vision input usually hurts (BALROG). Controlled maze studies find graph or adjacency text far beats visual or ASCII grids for some models, and accuracy falls as grids grow. No study gives a clean head-to-head of ASCII vs coordinate list vs image for strategy games.

### Cited Findings
- BALROG (ICLR 2025): "we observe severe deficiencies in vision-based decision-making", as "several models perform worse when visual representations of the environments are provided". NetHack remains far from solved. — [BALROG, arXiv 2411.13543](https://arxiv.org/abs/2411.13543)
- In a grid-world maze study, Gemini-2.5-Flash scored 80–86% on 5×5–7×7 mazes with tokenized adjacency representations versus 16–34% with visual grid formats. The authors call this "representation-dependent reasoning", and GPT-5-mini was less format-sensitive. — [arXiv 2604.10690](https://arxiv.org/abs/2604.10690); GPT-5-mini note from [summary](https://www.themoonlight.io/review/do-llms-build-spatial-world-models-evidence-from-grid-world-maze-tasks)
- A TU Delft maze thesis found adjacency-list JSON performed best, and absolute coordinates in answers beat egocentric frames. — [TU Delft repository](https://repository.tudelft.nl/record/uuid:e14eeb6d-0d7a-4d27-82d0-186aafe5c8ff) (via search summary; not read in full)
- An ASCII-grid probing study ("Stuck in the Matrix") reports accuracy falling as grid size grows, blamed on weak coordinate tracking. — [summary](https://www.themoonlight.io/review/stuck-in-the-matrix-probing-spatial-reasoning-in-large-language-models) (secondary)
- An OOD visual-planning study notes ASCII grids "require fewer tokens" than other text formats. Performance drops sharply on larger maps or longer start–goal distances. — [arXiv 2602.15460](https://arxiv.org/html/2602.15460) (search summary)
- PokéLLMon: because the state and action spaces are discrete, the state converts to text losslessly. The agent adds external knowledge (type advantages, move effects) to the state text. — [PokéLLMon, arXiv 2402.01118](https://arxiv.org/html/2402.01118v2)
- TextStarCraft II has an "Observation-to-Text" adaptor and Chain of Summarization (single-frame distillation, then multi-frame summarization) to cut what the LLM must read. — [arXiv 2312.11865](https://arxiv.org/html/2312.11865v3)
- Voyager reads state through Mineflayer's high-level APIs (positions, blocks, inventory) rather than pixels. SmartPlay is classed as a Text/2D-grid benchmark and Cradle as video/VLM-based. — [Orak, arXiv 2506.03610](https://arxiv.org/html/2506.03610v2); [LLM game agent survey, arXiv 2404.02039](https://arxiv.org/html/2404.02039v1)
- CivBench (Civ VI) uses coordinate-plus-distance threat lines and [fog] markers, produced by deterministic code. — [CivBench](https://arxiv.org/html/2609.02459)

### Inferences
- For the cheap LLM, the best-supported format is a **short list of pre-computed facts**, one line per relevant source, not a grid. Each line gives what, where, strength, turns to reach our nearest city, our cover there and its age. CivBench already uses that format. The evidence (BALROG, maze studies) argues against images and large ASCII grids for decisions.
- If a spatial picture is needed for a rare frontier-LLM call, keep it small and local. A Mastaba-style coarse block summary (for example 3×3 regions around a city with summed threat/cover/fog) is cheaper and more robust than a per-hex grid. Hex offsets in ASCII are an added source of coordinate-tracking error. That last point is inference; no hex-specific study was found.
- Adjacency or graph text beats grids in mazes, so path questions ("is the route from camp to city clear?") should be answered by deterministic code (`path_clear`), with only the result passed to the LLM.
- Token arithmetic (inference, not measured): a 10-line threat summary is about 150–300 tokens, while a 15×15 hex grid with several layers is several thousand. At the ~$1/game target, only the former can be sent every turn.

### Gaps
- No found study measures token cost against decision quality for strategy-game map formats, or tests hex grids specifically.
- Voyager, Cradle and SmartPlay papers were not read directly; their descriptions come from survey/benchmark summaries.
- Several maze/ASCII findings are from secondary summaries; exact numbers for the ASCII condition were not verified.

## 5. Hex-grid ML: hexagonal convolutions, axial tensors, GNNs

### Takeaway
Hex maps can be stored as rectangular tensors with offset columns and processed with hexagonal kernels (HexagDLy). For a non-learning bot the useful lesson is storage and neighbourhood ops (offset/axial arrays, six-neighbour stencils, distance transforms), not the convolution networks.

### Cited Findings
- HexagDLy (SoftwareX 2019, PyTorch) maps hex data to a square array by shifting every second column by half a cell. It builds hexagonal kernels from several rectangular sub-kernels to keep hexagonal symmetry, with strides along the three symmetry axes. The authors say it aims "for flexibility than for performance". — [HexagDLy, arXiv 1903.01814](https://arxiv.org/abs/1903.01814); [GitHub](https://github.com/ai4iacts/hexagdly)
- CivRealm stores the Freeciv map as M×N planes, a plain rectangular tensor. — [CivRealm](https://arxiv.org/html/2401.10568v2)
- AlphaStar connects entity lists and spatial planes with scatter connections rather than a full graph network. — [xlnwel blog](https://xlnwel.github.io/blog/reinforcement%20learning/AlphaStar)

### Inferences
- Civ VI's map is offset-coordinate hex (the project's `[x, y, value]` dump already uses it). Keeping layers as dense numpy arrays in offset form lets fades, sums and "max within k hexes" run as vectorised six-neighbour stencils or a multi-source Dijkstra over movement cost. That should stay far inside the 10 s/turn budget on maps of about 4,000 tiles (CivBench's figure), though this is not benchmarked here.
- A GNN or hex-CNN would only matter if the project later learns a value function from its JSONL dumps. The dumps (sources + planes) are already in a shape such a model could consume.

### Gaps
- Searches did not find game-specific hex-CNN or GNN work (for example hex wargames or Civ-like RL with graph encoders). Only HexagDLy (from astroparticle physics) was verified.
- No benchmark of hex-convolution speed against plain offset-array stencils was found.
