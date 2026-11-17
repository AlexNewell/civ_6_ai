# Vision (pixels) vs API (structured state) for a Civilization VI bot

Scope: when pixel-based perception is worth it for a game bot that already has structured API access, and which vision or visualization techniques fit this project (Python Civ VI bot, scripting-API state reads, per-turn screenshots for humans, its own rendered hex threat map in `scripts/threat_map_view.py`, about $1 per game and about 10 s per bot turn).

## Q1. What does the evidence say about pixel vs structured observations for game agents, and how well do multimodal LLMs read grids, counts and spatial relations from images?

### Takeaway
Every source found that compares the two points the same way: for LLM agents, text or structured state matches or beats images. Images often hurt mid-tier models, and frontier models at best break even. No published Civilization LLM work uses screenshots as the main observation. The newest Civ VI benchmark (CivBench, 2026) deliberately turns the game into structured text through tools.

### Cited Findings
- **BALROG (ICLR 2025): average progress, language-only vs vision-language.** claude-3.5-sonnet 32.64 vs 35.48; gpt-4o 32.34 vs 22.56; llama-3.2-90B 27.29 vs 20.99; gemini-1.5-pro 21.00 vs 25.76; gpt-4o-mini 17.36 vs 15.36; llama-3.2-11B 16.82 vs 8.43; gemini-1.5-flash 14.63 vs 14.94. The comparison covers BabyAI, Crafter, Baba Is AI, MiniHack and NetHack; TextWorld has no images. — [BALROG arXiv HTML v2](https://arxiv.org/html/2411.13543v2)
- BALROG authors: the GPT-4o and Llama 3.2 drops are "likely due to confusion arising from the added visual input". Current multimodal Transformers are "still better equipped at handling textual information than visual input". Grid observations also differ from the natural image-text pairs that VLMs are trained on. — [BALROG arXiv HTML v2](https://arxiv.org/html/2411.13543v2)
- **LMAct (ICML 2025, DeepMind).** Tic-tac-toe: "The o1 models reach expert performance with ASCII observations". Other models, "as does o1 with RGB observations", struggle to beat a random baseline. Chess: "PGN observations enable the best results", and RGB was not the strongest format. Gridworld: results vary by model and format, with o1 "near-perfect on ASCII and coordinates". — [LMAct arXiv v3](https://arxiv.org/html/2412.01441v3)
- **VideoGameBench (2025), pure-vision play of 1990s games.** The best model (Gemini 2.5 Pro) completed 0.48% in real time. Pausing the game during inference raised the best score to only 1.6%. Models also did poorly on simple practice tasks such as 2D grid navigation and dragging. — [VideoGameBench arXiv](https://arxiv.org/pdf/2505.18134); [EmergentMind summary](https://www.emergentmind.com/papers/2505.18134)
- **"Vision Language Models Are Blind" (ACCV 2024).** GPT-4o, Gemini-1.5 Pro and Claude 3/3.5 Sonnet fail to count rows and columns in simple grids. A secondary review gives about 34% average accuracy on empty grids and about 60% with text in the cells. — [arXiv 2407.06581](https://arxiv.org/html/2407.06581v4); [Moonlight review](https://www.themoonlight.io/review/vision-language-models-are-blind) (secondary figures; other sources frame them differently, e.g. [TechSpot](https://www.techspot.com/news/103780-study-shows-best-visual-learning-models-fail-basic.html))
- **SpatialEval (NeurIPS 2024),** text-only vs vision-only vs vision+text on spatial-map and grid tasks. Text plays "a critical role", some competitive models do worse than random, and adding the image to a full text description sometimes made results worse. — [arXiv 2406.14852](https://arxiv.org/html/2406.14852v2); [Moonlight review](https://www.themoonlight.io/review/is-a-picture-worth-a-thousand-words-delving-into-spatial-reasoning-for-vision-language-models)
- **Anthropic's own documentation** lists limits: "Spatial reasoning: Claude's coordinate and localization outputs are approximate". "Counting: Claude can give approximate counts ... might not always be precisely accurate, especially with large numbers of small objects". It also says Claude may make mistakes on small images (under 200 px). — [Claude vision docs](https://platform.claude.com/docs/en/build-with-claude/vision)
- **PySC2 / SC2LE (DeepMind 2017).** The main observations are feature layers, not RGB. The docs say the feature layers "represent roughly the same information as RGB pixels, except the information is decomposed and structured" (about 25 layers across screen and minimap). RGB was added later. No head-to-head score comparison of feature layers vs RGB was found. — [SC2LE paper](https://arxiv.org/pdf/1708.04782); [PySC2 environment docs](https://github.imc.re/hejingpeng/pysc2/blob/master/docs/environment.md)
- **CivBench (Civ VI, arXiv 2609.02459, NeurIPS 2026 poster).** It exposes 76 MCP tools and "a deterministic narration layer that retrieves and formats game state as structured text". Agents must call tools to see the map or rivals. Episodes run over 300 turns with thousands of tool calls, across 23 runs in four model families. Its main failures were under-monitoring (victory progress checked every 30-75 turns despite guidance to check every 20) and commitment follow-through (RAG@10 48.2-65.8%). These are planning and attention failures, not perception failures. — [CivBench arXiv](https://arxiv.org/abs/2609.02459)
- Other Civ LLM environments are also API-based: CivRealm uses FreeCiv with a Gymnasium-style API, and a Civ V CivBench builds on Vox Deorum. — [CivBench search result / arXiv 2604.07733](https://arxiv.org/html/2604.07733v1)
- **Claude Plays Pokemon (2025).** The harness gave the model RAM-derived state text and a navigation tool. Anthropic's write-up says explicit location data mattered because the model otherwise sometimes "imagined" successful zone transitions. The model reached the third gym's area in roughly 35,000 actions. — [Latent Space write-up](https://latent.space/reading/claude-plays-pokemon/); [DEV post](https://dev.to/qingze_hu_c4c251c1b353ede/i-replicated-the-vision-only-pokemon-run-anthropic-showcased-on-the-fable-5-launch-page-then-i-35ah). A later claim that a newer model beat FireRed with a "vision-only harness" is unverified: the blogger who reported it notes that no protocol or code was published.
- **Cradle (ICML 2025)** plays Red Dead Redemption 2 from screen pixels only, with GPT-4V and six modules (information gathering, self-reflection, task inference, skill curation, action planning, memory). The authors name multimodal observation and accurate keyboard and mouse control as core obstacles. It is the reference design for games with no API, not for games that have one. — [Cradle arXiv](https://arxiv.org/abs/2403.03186v3)
- **Cost of the screen route vs the API route (Reflex benchmark, 2026, not a game).** The same admin-panel task took 53 ± 13 steps, 1,003 s ± 254 s and about 551k input tokens for a Sonnet vision agent. The Sonnet API agent took 8 calls, 19.7 s and about 12.2k tokens. A Haiku API agent took 7.7 s and about 9.5k tokens. The vision agent succeeded only after its prompt was rewritten as a 14-step walkthrough. With the original prompt it accepted 1 of 4 reviews because it never paginated. — [Reflex blog](https://reflex.dev/blog/computer-use-is-45x-more-expensive-than-structured-apis/); [The Register coverage](https://www.theregister.com/ai-and-ml/2026/05/07/ai-vision-agents-use-45x-more-tokens-than-apis-in-benchmark/5231346)
- **Image token cost on Claude.** Cost is ceil(w/28) x ceil(h/28) visual tokens. Standard tier: 1,568 px long edge, max 1,568 tokens. High-resolution tier (Claude 4.7 and later): 2,576 px long edge, max 4,784 tokens. Examples: 1000x1000 = 1,296 tokens; 1920x1080 = 1,560 tokens (standard, downsized to 1456x819) or 2,691 tokens (high-res). The docs' worked prices are about $1.30 per 1,000 1-MP images on Haiku 4.5 ($1/M input), about $5.18 per 1,000 on Opus 5.5 ($4/M), and about $19.14 per 1,000 4K images on Opus 5.5. — [Claude vision docs](https://platform.claude.com/docs/en/build-with-claude/vision)

### Inferences
- The user's suspicion is supported. Screen-reading adds a lossy, slower, costlier layer. For an LLM tier, structured text of the same facts is the safer input, especially for exact counts, coordinates, hex adjacency and distances.
- Images are cheap in dollars for this budget. One 1-MP image per turn on a Haiku-class model is about $0.0013, so 330 turns is about $0.43, and less if images go only to the rare frontier call. The real costs are accuracy and latency (more input tokens and slower first token), not money.
- The evidence suggests frontier models can tolerate images (Claude 3.5 Sonnet held steady in BALROG). Cheap models are the ones most hurt by them (gpt-4o-mini, small Llama). This project routes routine judgment to the cheap tier, which is the tier where images are riskiest.
- CivBench's failures (forgetting to monitor, not following through) match this project's own threat-map motivation: the bot "forgot" a camp. That is a memory and state-keeping problem. Pixels would not fix it.

### Gaps
- No study found compares the same Civ (or hex-grid 4X) agent on screenshots vs API state. No hex-specific VLM benchmark was found; all grid studies use square grids or ASCII.
- No head-to-head PySC2 feature-layer vs RGB agent scores were found.
- 2026-era frontier models were not re-tested on BALROG or "VLMs are blind" style probes in the sources found. Recent models may close some of the gap, but that is unmeasured here.
- No measured latency cost per image (time to first token) was found for Claude models.

## Q2. Screen-parsing tools (OmniParser, UI-TARS, Set-of-Mark, OCR, template matching, YOLO): what they do and how fast they run on CPU

### Takeaway
Classic CV (OpenCV template matching, Tesseract on a cropped region) runs in milliseconds to a fraction of a second on CPU. That is enough for narrow checks such as "is popup X on screen" or "read this one number". Learned screen parsers (OmniParser) need a GPU to stay under about 1 s per frame, and they solve a general-UI problem this bot does not have.

### Cited Findings
- **OmniParser V2 (Microsoft):** 0.6 s per frame on an A100, 0.8 s on an RTX 4090, 60% faster than V1. It uses about 4 GB of GPU memory. CPU-only latency is unmeasured; third-party guides estimate 10-30x slower and call it impractical for interactive loops. — [OmniParser v2 on Replicate](https://replicate.com/microsoft/omniparser-v2); [Codersera Windows guide](https://codersera.com/blog/run-microsoft-omniparser-v2-on-windows-step-by-step-guide/); [Mixpeek model page](https://www.mixpeek.com/model/microsoft/OmniParser-v2.0)
- **Set-of-Mark prompting** segments an image (SAM/SEEM), overlays numbered marks, and has the VLM answer with mark IDs instead of coordinates. Zero-shot GPT-4V+SoM beat a fully fine-tuned model on RefCOCOg referring-expression comprehension and matched GLIPv2 and Grounding DINO on Flickr30K phrase grounding. — [SoM arXiv 2310.11441](https://arxiv.org/abs/2310.11441)
- **OCR on CPU.** Tesseract took 0.1-0.3 s per image vs 1-5 s for EasyOCR and PaddleOCR on an Apple Silicon machine without a GPU ([DEV Community](https://dev.to/felipe_anyformat/best-python-ocr-libraries-179)). A March 2026 single-invoice test measured Tesseract 5.5 at 0.162 s, EasyOCR at 0.656 s and PaddleOCR at 4.85 s, with PaddleOCR the most accurate ([imagetotable.ai](https://imagetotable.ai/blog/ocr-speed-vs-accuracy-tradeoff)). Tesseract fell to 0.000 at 15-degree rotation while PaddleOCR stayed at 1.000 ([DEV Community](https://dev.to/felipe_anyformat/best-python-ocr-libraries-179)). These are not controlled benchmarks, and the sources disagree on absolute numbers.
- **OpenCV `matchTemplate` (TM_CCOEFF_NORMED)** takes about 10-30 ms on a modern CPU in one loosely specified example. Downscaling and pyramid (coarse-to-fine) matching speed it up without hurting accuracy. Developers report that screen capture is often the bottleneck, and MSS is a fast Python capture library. — [BrainDrip template matching](https://braindrip-web.vercel.app/courses/computer-vision-concepts/feature-extraction-and-classical-vision/template-matching); [Sikuli/stb-tester thread](https://groups.google.com/g/stb-tester/c/_Z6i99yqV4Q); [Hackernoon game bot](https://sia.hackernoon.com/how-to-make-a-gaming-bot-that-beats-human-using-python-and-opencv)
- **Claude coordinates.** Claude returns pixel coordinates relative to the resized image, and they are "approximate". The docs say to pre-resize or rescale, and to verify before relying on them. — [Claude vision docs](https://platform.claude.com/docs/en/build-with-claude/vision)

### Inferences
- For Civ VI's fixed-skin UI, template matching on a known region (popup frames, the "Next Turn" or "Unit needs orders" button state, diplomacy screen banners) is the right-sized tool: deterministic, CPU-only, a few ms per check, no model download.
- Tesseract on a tightly cropped, upscaled, thresholded region (e.g. a turn counter or gold yield) is fine for spot checks. Fully parsing Civ screens with OCR is not, given the stylized fonts and overlays.
- OmniParser, UI-TARS and YOLO-style detectors are aimed at unknown UIs or games with no API. For this bot they add GPU dependence and about 1 s per frame for information the API already has. Not recommended except perhaps for offline labeling.
- Set-of-Mark is the useful idea to borrow if images are ever sent to an LLM. The bot already knows every hex's coordinates, so it can draw its own marks (tile IDs, unit IDs) on its own rendered map. The model then answers in IDs, not pixel positions, which avoids the approximate-coordinate problem.

### Gaps
- No found benchmark of UI-TARS latency or cost; it was not researched in depth.
- No measured CPU latency for OmniParser V2, and no Civ VI-specific OCR or detector accuracy data.
- No YOLO-for-Civ datasets or results were found.

## Q3. Where vision still helps a bot that has an API

### Takeaway
Vision earns its place on the edges the API does not cover: detecting UI state the API does not expose (stuck modal dialogs, blocking popups, "game is waiting on input"), cross-checking API reads against what the player sees, and human-facing diagnosis. Rendering the bot's own derived maps for a VLM is plausible for the rare frontier "strategic overview" call. The evidence says to send the same facts as text alongside the image, and never to rely on the image alone for numbers.

### Cited Findings
- Practitioner harnesses combine machine-readable state with screenshots. In Pokemon harnesses, RAM gives position and HP but not surroundings, so guides tell the agent to check the screenshot. Anthropic found explicit location text prevented hallucinated transitions. — [Latent Space](https://latent.space/reading/claude-plays-pokemon/); [hermes-agent pokemon-player skill](https://claudeskills.info/skills/nousresearch/hermes-agent/pokemon-player/)
- CivBench's narration layer "converts" game state to structured text, a deliberate choice to give the LLM text rather than pixels in Civ VI. — [CivBench arXiv](https://arxiv.org/abs/2609.02459)
- SpatialEval: image plus a full text description sometimes performed worse than text alone. Redundant images are not free. — [Moonlight review of SpatialEval](https://www.themoonlight.io/review/is-a-picture-worth-a-thousand-words-delving-into-spatial-reasoning-for-vision-language-models)
- BALROG: Claude 3.5 Sonnet did slightly better with vision (35.48 vs 32.64), while GPT-4o and the small models dropped. — [BALROG](https://arxiv.org/html/2411.13543v2)
- VideoGameBench measured progress by matching checkpoint screenshots against walkthrough frames with perceptual hashing. This shows image hashing is a cheap, workable way to check "is the screen in state X". — [VideoGameBench summary](https://www.emergentmind.com/papers/2505.18134)

### Inferences (project-specific suggestions, ranked)
1. **Popup and blocked-state detector (high value, low cost).** The bot already takes a screenshot each turn. Add OpenCV template or perceptual-hash checks for known blocking dialogs: era change, Great Person popups, diplomacy, world congress, "please wait" overlays. Fire only when the API says "my turn, nothing pending" but the turn does not advance. This fits the "find the cause, re-check on change" rule: the detector names the cause instead of a timer. It costs milliseconds on CPU.
2. **API-vs-screen consistency checks (medium value, offline).** On sampled turns, check a few API values against the screen: turn number, gold, a city's banner position or name, using cropped Tesseract or template matching. Use this as a test and diagnostic tool, not every turn. It catches bad reads such as fog/revealed confusion, which the threat-map doc already treats as subtle.
3. **Render-then-ask for rare frontier calls (experimental).** `threat_map_view.py` already draws the threat, fog and cover map. For a rare strategic call, send the rendered image (about 1,300-1,600 tokens at 1 MP, a fraction of a cent) plus the same sources as compact text, with Set-of-Mark-style labels (tile or source IDs) drawn on the image. Measure on held-out decisions whether the image changes outcomes. The evidence predicts small or negative gains for cheap models, so keep it off the cheap tier.
4. **Do not use** pixel reading to replace any API read the bot already has (units, tiles, yields, camps), or OmniParser/YOLO-class detectors in the turn loop. The cost is GPU dependence and about 1 s per frame against the 10 s turn budget, with no information gain.

### Gaps
- Which Civ VI UI states the scripting API does not expose was not researched here. The project's own run logs of stuck turns would answer it better than the web.
- No study measured whether a rendered derived map helps an LLM's strategy in a 4X game.

## Q4. Visualization and data-science tools for diagnosing a bot across runs

### Takeaway
Few external sources address this directly. Most value comes from standard techniques applied to the bot's existing per-turn JSONL dumps (threat-map lines, slice metrics): heatmaps aggregated across turns, small multiples across runs, animated replays from the renderer, and simple anomaly flags on per-turn metrics. These are recommendations, not sourced findings.

### Cited Findings
- VideoGameBench used perceptual hashing of screenshots against reference frames to measure progress. The same technique can line up and compare screenshots across runs. — [EmergentMind summary of VideoGameBench](https://www.emergentmind.com/papers/2505.18134)
- CivBench's diagnostic metrics (how often victory progress was checked, RAG@10 commitment follow-through) show that per-turn behavioural logs, not pixels, gave its most useful findings about long Civ VI games. — [CivBench arXiv](https://arxiv.org/abs/2609.02459)

### Inferences
- **Heatmap across turns.** Sum or max the `heat` layer from `runs/<game>_threatmap.jsonl` over a run, drawn with the existing hex renderer. This shows chronic danger zones, such as the camp four tiles from a city for thirty turns.
- **Small multiples.** One small threat-map frame every N turns in a grid, or the same turn range across runs 9-12 side by side. This is cheap with matplotlib on the existing renderer.
- **Animated replay.** Render each turn to PNG and stitch into GIF or MP4 (imageio or ffmpeg). Pair it with the per-turn human screenshots for a side-by-side "what the bot believed vs what the screen showed". This is a manual form of the consistency check in Q3.
- **Visual regression of runs.** Perceptual-hash or SSIM diff of rendered threat maps (not game screenshots, which vary with camera) from a fixed practice save between code versions. This flags behaviour changes in slice tests.
- **Anomaly detection on per-turn logs.** Rolling z-scores or IsolationForest (scikit-learn) on seconds per turn, number of sources, camp-turns near a city, gold and production. Flag turns for review. This also covers the 10 s per turn speed target.
- **Dimensionality reduction** (PCA or UMAP of per-turn state vectors) is lower priority. It can cluster runs or phases, but it is harder to act on than the views above.

### Gaps
- No external evidence was gathered on which visualization methods best help debug game bots. This section is practitioner judgment, not sourced findings.
- No sources were checked for UMAP or IsolationForest use on game logs specifically.
