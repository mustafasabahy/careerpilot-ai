# CareerPilot AI final readiness report

Status: verified local technical preview. Demo mode works without credentials. The default
real-model path is wired and configuration-tested, but no paid provider call was made in this
environment, so provider quota, account access and live response quality remain unverified.

## Verified capabilities

- Owner-scoped FastAPI resources, persistent conversations and messages, additive agent
  execution metadata, and safe deletion of transcript records.
- Deterministic analysis, evidence-linked findings, versioned preparation drafts, synthetic
  local jobs and simulated external actions.
- Natural-language chat agent with LangChain `ChatOpenAI` tool calling, strict Pydantic tool
  schemas, bounded model/tool/history/output budgets, English/Arabic prompts, safe provider
  errors, and no silent LLM-to-demo fallback.
- LangGraph orchestration with a separate SQLite checkpointer, server-created operation threads,
  owner checks, restart tests, explicit recoverable/unknown states, and write-ahead markers that
  prevent blind replay of uncertain side effects.
- Existing approval interrupt, exact proposal version/hash validation, authenticated decision
  events, stale/foreign approval rejection, and simulated-only outcome labeling.
- Optional metadata-only LangSmith observer. Native prompt/document tracing is disabled.

## Commands actually run

`uv lock --check`; Ruff format/check; mypy; `uv run --locked pytest -q`; Node UI tests; and
`uv run --locked careerpilot-evaluate --output work/final-eval.json --baseline evals/baseline-v1.json`.

Latest results: **599 passed, 1 skipped** (`tests/test_llm_live.py`, explicit paid live test),
**11 UI tests passed**, and **12/12 offline evaluation cases passed**. The full gate is
`PATH="$PWD/work/tooling-env/bin:$PATH" sh scripts/check.sh`.

## Minimal setup

Demo:

```sh
uv sync --locked
cp .env.example .env
CAREERPILOT_CHAT_MODE=demo uv run --locked careerpilot-server --init
CAREERPILOT_CHAT_MODE=demo uv run --locked careerpilot-server
```

LLM mode uses the backend environment only. Add `OPENAI_API_KEY` to the ignored `.env`; the
default provider is OpenAI and the default model is `gpt-5.4-mini`. The key alone selects LLM
mode unless `CAREERPILOT_CHAT_MODE=demo` is explicitly set. `CAREERPILOT_CHAT_MODE=llm` without
a key returns actionable setup information and never silently downgrades. Restart after config
changes. No startup model call occurs.

## Simulated and remaining risks

Job search is local fixture/import search; recruiter delivery and application submission are
simulated. SQLite is single-host/local-worker storage. Live LLM/LangSmith delivery, provider
costs, production HA, enterprise SSO/MFA, public TLS, full browser accessibility and real
external reconciliation remain unverified. Unknown external outcomes are sealed and must not
be retried blindly. Review [known limitations](docs/known-limitations.md) before launch.

## Final launch work

Before real personal data or production deployment, configure an enterprise identity boundary,
TLS and rate limits, validate backup/restore and checkpoint retention on target hardware, decide
privacy/retention policy, add a real provider adapter with idempotency and reconciliation, and
run an explicitly authorized live-model smoke test.
