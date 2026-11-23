# Benchmarks

Use two kinds of evidence when evaluating Memory Cloud: **whether an assistant answers correctly**, and **whether retrieval finds the right memories**. They measure different things.

## Public answer-quality report

Read the [English report](https://www.kagura-ai.com/benchmarks/) or [Japanese report, including LoCoMo](https://www.kagura-ai.com/ja/benchmarks/) for results, charts, and methodology. The snapshot below was checked on **November 16, 2026**; the Japanese page includes a newer LoCoMo section than the English page.

| Evaluation | Tested KMC version | KMC answer accuracy | Scope |
|---|---|---:|---|
| FC: fact retention and updates | v0.135.0 | 81.5% | 69 questions |
| LongMemEval (LME): conversation history | v0.135.0 | ≈88.6% | 66 questions; derived from rounded report values |
| LoCoMo: long conversations, preregistered run | v0.152.0 | 84.2% strict / 90.8% lenient | November 10; 3 conversations, 472 questions |
| LoCoMo: subsequent rerun | v0.156.0 | 83.7% strict / 91.3% lenient | November 12; same questions, not preregistered |

These are developer-run evaluations on selected questions, not official full-dataset scores. LoCoMo excludes adversarial questions. Its rerun follows tuning on those same conversations, so it is not an independent held-out evaluation. The server's current release version is not the version tested in these rows.

### Read savings alongside accuracy

For the preregistered LoCoMo run, v0.152.0 used **80% fewer memory tokens** and **66% less answer-time model cost** than full conversation input. Strict accuracy was **84.2% versus 89.0%**. Memory-token savings exclude the no-memory input baseline; cost savings exclude memory construction and service fees.

Do not combine those savings with the rerun's 91.3% lenient score. For comparisons with Company M and file memory, including confidence intervals and cache effects, use the report's matched runs. Results depend on ingestion, questions, answering model, and grading; cross-report leaderboard comparisons are not valid.

## Agent task benchmark: DolphinBench

[DolphinBench](https://github.com/mem0ai/dolphinbench) measures whether an agent acts correctly on what it should remember: 13,539 history messages for three people, then 600 tasks in simulated apps. With the benchmark's Hermes setup and GPT 5.6 Luna, Kagura Memory Cloud v0.156.0 passed **455 of 600 (75.8%)**: Morgan 165, Alex 145, Riley 145 of 200 each. The benchmark's published Hermes + Luna rows are Mem0 424, Hindsight 417, Honcho 411, Hermes built-in memory 394 and Supermemory 355.

This run is self-tested (November 23, 2026) and [awaits the maintainers' review](https://github.com/mem0ai/dolphinbench/pull/16). It is not a held-out result: the plugin's recall settings were chosen after reading failures on the public tasks. The judge is the benchmark's model, settings and prompts, sent through the OpenAI API. The $97.90 cost covers agent tokens only; the memory server's models ran self-hosted. The [method note](https://github.com/kagura-ai/dolphinbench/blob/kagura-memory-cloud-hermes-20261123/docs/submissions/kagura-memory-cloud-hermes-20261123.md) lists every departure from the benchmark's runner, and the [evidence ZIP](https://github.com/kagura-ai/dolphinbench/releases/tag/kagura-memory-cloud-hermes-20261123) holds all task conversations and saved judge answers.

## Retrieval and component evaluations

These repository documents explain the search and maintenance mechanisms rather than the public answer-quality scores:

| Question | Evidence |
|---|---|
| Does the right memory rank highly? | [Search Quality Benchmark](search-quality-benchmark.md) |
| How do direct retrieval and graph exploration differ? | [Neural Memory Evaluation](neural-memory-evaluation.md) |
| How are retrieval datasets and regression checks defined? | [Retrieval Benchmark](eval/retrieval-benchmark.md) |
| How are individual model judgments evaluated? | [Evaluation Documentation](eval/README.md) |
| How does retrieval feedback enter the development process? | [Retrieval Feedback & Eval Gate](eval/retrieval-feedback-and-eval-gate.md) |

A retrieval metric such as P@1 is not the probability that an assistant will answer a user's question correctly. Check each document's dataset, date, configuration, and metric before reusing a number.

## Try it on your own work

1. Save a few real decisions and their reasons in a dedicated context.
2. Start a new conversation and ask questions whose answers depend on those memories.
3. Check both the retrieved source and the final answer; include a decision that changed over time.
4. If measuring cost, count memory creation, retrieval, answering, caching, and hosting separately.

Start with the [first-memory walkthrough](../README.md#try-your-first-memory) and [memory-writing guide](chunking-guide.md).
