# Local related-index experiment (#2728)

This is a default-off experiment, not a rollout. `SLEEP_RELATED_INDEX_ENABLED=true`
adds a bounded index phase to full Sleep; `RECALL_RELATED_INDEX_ENABLED=true`
allows at most two extra candidates from two seeds, in the same context. They
still share Recall's existing top-k and response character budget. The two
flags are independent; leave both unset in deployments.

The index contains source IDs, versions, fingerprints and summary offsets. It
does not store generated facts or answers. Deleted, changed or superseded
sources are ineligible. A stale semantic relation is never renewed just by
copying a new fingerprint: it needs a newly discovered edge. Incremental
scanning excludes deleted/edited endpoints before its limit. Index actions
participate in Sleep audit/rollback. Recall uses an isolated read session so
cancelling its optional read cannot invalidate the main transaction.
The phase runs immediately after discovery, before maintenance can change
metadata timestamps. When extra candidates are added, that recall skips
Hebbian learning to avoid training on derived evidence (including scores
transferred by folding). Ordinary access accounting remains active. Empty or
failed expansion keeps normal learning unchanged.

## Prerequisites

Use a **disposable local** PostgreSQL database named `kmc_related_eval_<name>`,
local Qdrant and Redis, and a local OpenAI-compatible inference endpoint with
the chosen embedding, Sleep judge and answer models already loaded. Set
`DATABASE_URL`, `QDRANT_URL`, `REDIS_URL`, `SELF_HOSTED_BASE_URL`,
`EMBEDDING_PROVIDER=self_hosted`, `EMBEDDING_MODEL` and `ENABLE_NEURAL_MEMORY=false`
explicitly. The small fixture freezes neural over-fetch off so it does not
exhaust all sources before related expansion; this is recorded in the manifest. The runner
refuses non-loopback endpoints or an unmarked database. Apply migrations to
that disposable database first (`uv run alembic upgrade head`). Never point
the test fixtures at a database whose data you want to keep. Leave
`SUPERSEDE_VERIFY_MODE` unset or `off`: with `llm` the write path asks a judge
while the fixture is ingested, and that judge is not the frozen
configuration's. It resolves from `SLEEP_LLM_PROVIDER` / `SLEEP_LLM_MODEL`,
else the managed lane, else a hosted default, so an unset provider is not
"no judge". The runner refuses a judge that does not resolve to `self_hosted`.

Install the locked development dependencies with `uv sync --locked --extra dev`
in `backend/`. Supply a local Hugging Face `tokenizer.json` matching the answer
model; its SHA-256 and the installed tokenizer version are recorded. Models
must use immutable local revisions; record those revision identifiers in the
configuration. No tokenizer or model is downloaded by this runner.

Create a local configuration file **outside the checkout**, for example:

```json
{
  "embedding_model": "qwen3-embedding:0.6b",
  "answer_model": "YOUR_LOCAL_ANSWER_MODEL",
  "sleep_model": "YOUR_LOCAL_JUDGE_MODEL",
  "model_revisions": {"embedding": "LOCAL_DIGEST", "answer": "LOCAL_DIGEST", "sleep": "LOCAL_DIGEST"},
  "tokenizer": "C:/local-models/answer/tokenizer.json",
  "repetitions": 4,
  "k": 3,
  "max_chars": 10000,
  "tolerances": {
    "coverage_drop": 0.0,
    "correctness_drop": 0.0,
    "irrelevance_increase": 0.05,
    "latency_ratio": 1.25,
    "tokens_ratio": 1.10,
    "min_improvement": 0.05
  },
  "usd_per_million": {"embedding": 0.0, "input": 0.0, "output": 0.0}
}
```

These are example tolerances: choose and freeze yours **before running**.
`max_chars` takes the recall tool's own range (10,000 to 100,000): the
evidence is the reply that tool sends, and it refuses a smaller budget.
Zero rates mean zero declared marginal inference cost, not free hardware or
electricity. Provider-reported token usage may be absent; serialized response
tokens are always counted by the supplied tokenizer and reported separately.

From `backend/`, in PowerShell:

```powershell
$env:PYTHONPATH = 'src'
uv run python -m tests.eval.related_index_runner --config C:/local-eval/config.json --output C:/local-eval/run-001 --check
uv run python -m tests.eval.related_index_runner --config C:/local-eval/config.json --output C:/local-eval/run-001
```

The same in bash:

```bash
export PYTHONPATH=src
uv run python -m tests.eval.related_index_runner --config ~/local-eval/config.json --output ~/local-eval/run-001 --check
uv run python -m tests.eval.related_index_runner --config ~/local-eval/config.json --output ~/local-eval/run-001
```

The output directory must not exist and must be outside **every git checkout**.
The manifest is written before measurements. Besides the configuration it
records the runner's git commit and whether the checkout was dirty, the recall
parameters the run uses (`full`, `verbosity`, `use_rerank`) and the process
settings that shape a run, as the code resolves them: the expansion timeout
after its 25 to 500 ms clamp, the supersede settings and the write-path judge.
A `RECALL_RELATED_INDEX_TIMEOUT_MS` that is not an integer fails `--check`. Do not attach outputs to issues,
PRs or public benchmarks. The runner creates an isolated workspace/context and
deletes only those fixture memories and that workspace in its cleanup path.
The shared eval cleanup is best effort; use disposable local infrastructure
and remove the disposable database if an interrupted run leaves data behind.

## What is measured

Only fictional source statements enter ingestion and Sleep, under opaque ids:
the ingester tags each memory with its document id and tags reach the answer
model with the evidence, so source labels stay in the runner. Questions,
required-source labels and expected facts remain separate. Discovery uses the
normal embedding/judge phase, followed by the related-index phase, once. The
result is a single post-Sleep snapshot shared by both arms. Model IDs,
revisions, prompts, tokenizer, scoring rubric, k/max_chars, local cost rates,
tolerances, warm-up and repetitions are in the frozen manifest.

In the measurement process only, post-Recall access/promotion and Hebbian
writers are disabled. Ranking inputs are held fixed instead of allowing one
arm's learning to contaminate the next: raw memory, edge, related-index and
search-config snapshots must have exactly the same hash before and after
every trial. Drift aborts the comparison. All retrieval, permission checks,
embeddings, Sleep discovery and answer calls are real. This is a read-only
retrieval experiment; its timings are not production write/learning latency.

Each question receives one off/on warm-up pair, excluded from measurements.
Even measurement rounds use off/on, odd rounds on/off; repetitions must be
even. Answer calls use temperature 0, one attempt and a 512-token output cap.
The deterministic rubric accepts only the exact requested facts with all
required citations present in the retrieved evidence, with no extra facts or
citations. Empty facts/citations represent abstention. This conservative
structured-answer rubric is not a general natural-language judge.
Requested field names are defined separately from expected values; the
unanswerable probe still requests an amount, without revealing that it is absent.

Reports include source coverage, irrelevant evidence, grounded correctness,
serialized response tokens, provider-reported online tokens, tool/provider
calls, declared online cost, and retrieval/end-to-end p50/p95 latency. Sleep
phase usage and cost are separate from online totals. Negative or inconclusive
results are valid. No verdict enables the feature automatically.
The evidence is the reply the MCP `recall` tool sends for that request,
built by the function the tool itself calls: the deployment's default
projection (compact unless `MCP_RECALL_DEFAULT_VERBOSITY` says otherwise)
with content on the top `k` results, fitted and cut to `max_chars`. Its
serialized form is what the tokenizer counts.
Each trial records the related candidates the expansion added, how many of
them reached the bounded evidence the model saw, and a digest of that
evidence. An expansion that timed out or failed returns the baseline reply,
and a degraded recall never attempts one (`not_attempted`); the trial records
why (`expansion_skipped`) and the report carries the rate per arm
(`expansion_skips`), so a fallback is not read as "nothing was linked". Off/on trials of the same probe and repetition are paired: only a
pair whose on-arm evidence holds an index extra and differs from the off arm
credits the index, and a pair with identical evidence but a different score
counts as answer-model noise. "promising-local-only" needs at least one
changed pair and a coverage or correctness gain above `min_improvement` plus
that noise rate; otherwise the result is inconclusive. Regression gates use
serialized response tokens and retrieval and end-to-end p95 latency, and
provider-reported tokens only when both arms report them
(`provider_tokens_measured`). A failed answer call, or a reply that is not a JSON
object (`MalformedAnswer`), is recorded on its trial (`answer_error`) and
scores as wrong; a higher error rate on the on arm is a regression. An
unparseable reply still counts the tokens it spent; a call that got no reply
reports none.
Declared Sleep cost excludes ingestion and hardware and uses the same local
input rate for standard, cached and cache-write tokens.

## Checks versus live runs

```powershell
uv run pytest tests/services/sleep/test_related_index.py tests/eval/test_related_index_runner.py --no-cov
uv run pytest tests/integration/test_2728_related_index.py --no-cov -rs
```

The first command uses mocks and pure protocol checks; it is **not** evidence
of retrieval quality. The second uses `TEST_DATABASE_URL` and skips explicitly
if PostgreSQL is unavailable (CI sets `REQUIRE_TEST_DATABASE=1`). A successful
`--check` validates only the protocol; only a completed live run produces
`results.json`. Measured trials are appended to `trials.jsonl` as they finish,
so an aborted run keeps them. No live measurement is included in version control.
