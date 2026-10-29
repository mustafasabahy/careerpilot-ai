# ADR-003: Deterministic LangGraph job-analysis workflow

- Status: Accepted for Phase 3
- Date: 2026-10-28
- Extends: ADR-001 and ADR-002

## Context

The first executable career workflow must demonstrate reliable orchestration without
LLMs, keys or external searches, while preserving the existing domain/persistence
boundaries and explaining evidence rather than inventing a hiring score.

## Decision

Use LangGraph 1.2.18 StateGraph with typed input/state/output schemas, narrow nodes,
explicit conditional edges and a local public runner. Keep criterion normalization
and baseline matching in pure domain functions; use application services to load,
validate and persist through the existing repository port. Bind deterministic
fixture criteria to stable requirement IDs and retain source hard/soft/unspecified
importance. Missing/unverified data remains unknown, not an invented negative.

Represent outcomes as reasons, evidence IDs and counts, without weights, scores or
probabilities. Separate execution status from eligibility decision. Return typed
expected request/not-found errors; propagate broken rules, assessment mismatch and
infrastructure failures. Persist only after independently validating the calculation.
Derive stable assessment IDs from caller-supplied run identity/time, policy and
normalized criteria; reuse identical domain records and reject conflicts.

Keep full job/profile/document bodies outside graph state. State retains reference
IDs, required normalized criteria and a transient calculation for validation. Clear
raw request and transient calculation when no longer needed. Add no remote
checkpointer, agent, provider SDK or workflow server.

Declare LangSmith directly because its documented tracing_context(enabled=False)
allows a scoped opt-out from host tracing configuration. It is already required
transitively by LangGraph; no tracing service/account/key is added. Verify local
execution under enabled host tracing flags with a socket-denial test.

## Consequences

The workflow is executable and deterministic for immutable source records and
explicit policy. There is no AI reasoning or free-text requirement extraction.
Unknown/unsupported requirements require review. SQLite schema v1 and the existing
health command stay compatible. Reports include rules/explanations, but schema v1
does not durably store a normalization/policy history ledger. Future mutable source
records require snapshot-aware IDs and an explicit migration/history design.

LangGraph expands transitive dependencies; no standalone LangChain agent package is
needed. Offline execution after installation is tested, while initial dependency
installation still requires a package index/cache. No production retries, resumable
checkpoints, authentication or legal/market/hiring predictions are provided.

Official version-specific references, diagram, node/edge explanations, policy and
samples are recorded in ../job-analysis-workflow.md.
