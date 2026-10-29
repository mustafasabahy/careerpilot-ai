# Metadata-only observability and deterministic evaluation

Native LangChain/LangGraph tracing remains disabled to avoid uploading source text,
CVs or personal profile facts. Optional LangSmithObserver manually exports empty
inputs/outputs and an allowlist: correlation/workflow/operation UUIDs, node counts,
tool/model counts, retry count, latency, status and safe error code. Owner IDs,
messages, evidence, document bodies, prompts, provider errors and credentials are
not exported. Token counts are recorded only when supplied by the provider; cost
remains null because no pricing model is assumed. Local JSON logs work independently.

Configure TraceSettings with CAREERPILOT_TRACE_ENABLED=true, LANGSMITH_API_KEY,
CAREERPILOT_TRACE_PROJECT and a supported US/EU CAREERPILOT_TRACE_ENDPOINT, then
inject LangSmithObserver into RunControl. Explicit enablement without a key fails
validation; absence defaults to disabled. SDK calls use a fixed allowlisted endpoint,
2-second timeout and no automatic profile tracing. Export failure does not undo a
business action. No live telemetry or provider quality was verified during this
project; the metadata boundary is tested with a fake client. UUID metadata is still
pseudonymous data: review project permissions/retention before enabling export.

The manual integration uses installed langsmith 0.14.14 Client.create_run, checked
against the official [API instrumentation guide](https://docs.langchain.com/langsmith/trace-with-api).
This intentionally is not an unredacted model trace or a claim of full span-level
provider diagnostics.

Run offline regression evaluation:

```sh
uv run --locked careerpilot-evaluate --output work/eval-current.json --baseline evals/baseline-v1.json
```

Dataset evals/dataset-v1.json contains versioned synthetic scenarios and exact
expected fields. The harness runs actual extraction validation, evidence-based
analysis, unsupported-reference rejection, injection rejection, empty/duplicate
search, formatting/tool errors, approval denial and recovery idempotency. Results
include dataset hash, observed fields and individual pass/fail. Every deterministic
case must pass; any failure or changed dataset hash blocks the command. Baseline
results are generated from actual execution, not a model judgment. Change the
dataset version and review expected outcomes when intentionally changing contracts.
No probabilistic hiring metric, paid benchmark or LLM judge is used. This fixture
suite verifies known contracts, not general real-model extraction quality. The
separate process-crash tests supply stronger recovery evidence than the small
in-process evaluation scenario alone.

Final audit hardening also disables SDK runtime/environment metadata and OpenTelemetry,
rejects replica-endpoint environment configuration, and sets telemetry SDK retries to
zero. Deterministic baseline comparison now checks observed case results as well as
the dataset hash and 100% required-pass threshold. Browser API documentation is
self-hosted to preserve the strict CSP without remote scripts.

## Phase I multi-agent evaluation

The versioned offline dataset now includes deterministic checks for supervisor routing,
safe intent-classifier fallback, delegation limits, conflicting specialist references,
owner-isolated memory retrieval, provenance, and deletion. The baseline now contains 18
passing cases and is regenerated only from actual execution. Provider and scheduling
scenarios are intentionally not included: Phase G has no authorized commercial provider
and Phase H has no configured schedule or notification policy. Paid live-model tests remain
opt-in and are not part of the normal quality gate.
