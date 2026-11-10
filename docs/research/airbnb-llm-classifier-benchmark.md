# Fine-Tuned Small Models vs Frontier APIs: Airbnb's Classifier Benchmark

Source: Clay Gibson and Adel Elmalaha, [Frontier quality at 1% of the cost: a production benchmark of LLM classifiers](https://airbnb.tech/ai-ml/frontier-quality-at-1-of-the-cost-a-production-benchmark-of-llm-classifiers/), Airbnb Tech Blog, November 2026. Read on 2026-11-10.

This page is a paraphrased summary followed by our review. Figures are as the article states them. Values that appear only in the article's charts weren't readable and are left out.

## Summary

### The task

Airbnb benchmarked one production classifier, which screens listings for breaches of its pricing and payment transparency policy. It needs:

- high precision, because a false positive blocks a legitimate listing
- a score within 200 ms, because it gates publication in real time
- thousands of calls a minute at a manageable cost
- an adjustable trade-off between precision and recall

### Models

- **Gemma4-E2B, fine-tuned:** about 2B effective parameters, trained on hundreds of thousands of in-domain examples with Airbnb's internal tooling.
- **Off-the-shelf open-weight models,** self-hosted with vLLM on one NVIDIA RTX Pro 6000. Models up to 12B ran in bfloat16 and larger ones from FP8 or NVFP4 checkpoints. Gemma4-31B is the one named in the text.
- **Jev,** a decision model that returns a probability.
- **Proprietary generative models:** GPT-6.1 Sol, Claude Sonnet 5.5 and GPT-6 Luna.

### Method

- A held-out set of 10,000 examples, weighted to correct for how it was sampled from production traffic. A subset of models was also run on 184,000 examples, and their scores moved by under 0.01.
- Every model was tried with a concise prompt of about 400 words and a detailed one of about 2,700 words, which listed each behaviour, worked examples and common false positives.
- **Quality:** maximum F1. Models that return a score were swept across thresholds. Generative APIs give one verdict, so they have a single point.
- **Latency:** server-side handling time.
- **Cost:** observed tokens at October 2026 list prices, with an assumed 71% cache-hit rate (higher than observed, which favours the APIs), against public rental rates for the GPU.

### Results

| Model | F1 | Median latency | Notes |
|---|---|---|---|
| Gemma4-E2B, base | 0.563 | | |
| Gemma4-E2B, fine-tuned | 0.902 | 35 ms (p99 about 85 ms) | The only fast model near frontier quality |
| GPT-6.1 Sol, concise prompt | single point: 0.82 precision, 0.96 recall | about 900 ms, p99 several seconds | Changing the trade-off means rewriting the prompt |
| Claude Sonnet 5.5, concise prompt | similar to GPT-6.1 Sol | about 900 ms, p99 several seconds | |
| Jev | not given in the text | under 100 ms | Best off-the-shelf model under 100 ms |

- The detailed prompt added 1 to 3 F1 points to the frontier models, bringing them level with the fine-tuned model, and raised their cost by a further 20 to 40%. Fast models gained only a little and stayed well behind.
- Extra thinking didn't meaningfully improve quality, so low-thinking results are reported.
- Off-the-shelf open models were fast but levelled off well below the frontier and fine-tuned models. Latency didn't follow parameter count alone.
- Jev's cost quadrupled with the detailed prompt, because its API gives no discount on cached input.
- **Cost:** at thousands of requests a minute, frontier APIs cost about 100 times one self-hosted GPU, and the cheaper APIs about 5 times. One GPU is cheaper above about 30 requests a minute. Below about 15 to 30, Sonnet 5.5 and GPT-6.1 Sol cost less than a dedicated GPU.

### The authors' conclusions

- Fine-tuning can improve quality, latency and cost together, instead of trading one against another.
- Better prompts help frontier models a little but don't close the gap for models fast enough to deploy.
- Self-hosting only pays at enough traffic.
- Fine-tune when volume is high, latency is tight, you need a score you can set a threshold on, and you have labelled data. For low-volume tasks, or ones that change week to week, a frontier model with a good prompt is often the right choice.

### Caveats

- It covers one binary task, though the authors say the same trade-offs held on other models they fine-tuned.
- The authors are benchmarking their own tooling.
- Prices are list prices, not negotiated ones.
- The generative APIs get one fixed F1 point, which isn't quite comparable with a maximum over thresholds.
- The article doesn't cover distillation, cascades or routing between models.

## Review: what it means for this project

### It supports the two-tier plan

- **Strategist (Phase 2):** about 24 calls a game, planned in the background off the turn's critical path. This is the article's low-volume case, where a frontier model with a good prompt is the right choice. It matches the plan to use Sonnet 5.5.
- **Cheap tier (Phase 3):** about 1,250 calls a game per player, multiplied by five opponents and by every player if released. This is the article's high-volume case, and it meets most of their conditions for fine-tuning:
  - **Latency:** the long-term goal is 10 s per opponent turn, and Run 3 already took a mean 34.5 s with no model calls. Frontier calls at about 900 ms median, with multi-second outliers, only fit in background planning. A small local model at tens of milliseconds could run inside the turn.
  - **Cost:** under our [cost rules](../decisions.md#cost), local inference counts as $0. The article's break-even assumes a rented GPU, which doesn't apply here. Our constraint is memory shared with Civ VI on a consumer PC, already noted in the decisions table. E2B is built to run on devices, so it's a sensible size to test.
  - **Scores:** a score lets the code layer keep its own default when the model is unsure, which is what the 3.2 router's confidence thresholds need. As far as we know, Anthropic's API doesn't return token probabilities. Jev and local models do.
  - **Labelled data:** this is the gap. See below.

### Where labels could come from

- The deterministic bot's own choices, which every game already produces. This only teaches the model to copy the bot, so it's useful mainly for speed.
- A stronger model labelling a sample, which is distillation. The article doesn't test this. At the planned cheap-tier size (1,500 input and 100 output tokens, $0.0002 per Haiku call), 100,000 labels cost about $20. That's most of the development budget, so it needs a cheaper label source or a separate budget.
- Game outcomes. These are the true measure but are noisy and slow to collect.

### Where the analogy is weak

- Airbnb's task is a crisp yes-or-no decision with clear correct answers. Many Civ VI decisions choose among several options, and whether a choice was right often only shows in the game result. Offline agreement scores need confirming in full games, as Phase 5 already requires.
- Airbnb fine-tuned with hundreds of thousands of examples and in-house training tools. We'd need a GPU and time for training, which neither the per-game cap nor the development budget covers.

### Changes to Phase 3

Recorded as comments on the issues:

- **3.1, replay corpus ([#34](https://github.com/AlexNewell/civ_6_ai/issues/34)):** store each decision's inputs, legal options, the choice made and the later outcome, so the corpus can also be a training set. Weight the evaluation set by how often each situation occurs in real games.
- **3.2, router ([#35](https://github.com/AlexNewell/civ_6_ai/issues/35)):** let each tier return a confidence score as well as a choice. Providers without scores (Anthropic) need a different confidence signal, or a fixed routing rule.
- **3.3, Haiku 5.5 ([#36](https://github.com/AlexNewell/civ_6_ai/issues/36)):** compare a short and a long prompt, and test without extended thinking first. Batch API runs don't show live latency, so time a small sample of live calls too and report p90 and p99 as well as the median.
- **3.4, local models ([#37](https://github.com/AlexNewell/civ_6_ai/issues/37)):** add a small fine-tuned model, such as Gemma4-E2B, beside the off-the-shelf models. The article's main finding is that untuned small models fall well short and tuned ones don't. Training needs a budget decision first.

None of this changes work before G1.
