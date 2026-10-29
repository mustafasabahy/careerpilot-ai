# ADR-005: Scoped local discovery tools and bounded assessment

- Status: Accepted for Phase 5
- Date: 2026-10-28
- Extends: ADR-001 through ADR-004 without changing existing domain/persistence models

## Context

Discovery needs replaceable job/company sources and possible LLM tool bindings,
without allowing a model or retrieved job text to choose permissions, filesystem
paths, SQL/shell code or fetch endpoints. Existing deterministic assessment should
remain reusable. Commercial board access/terms/API support are not established.

## Decision

Define typed application-owned source ports, tool contracts, errors and an
out-of-band AccessGrant. Bind permissions and resource allowlists at application
composition after authentication; never accept authority in model args/state.
Ship only local fixture/operator-approved JSON snapshot and mock company adapters.
Import once with byte/record/identity bounds and retain source URL/retrieval metadata.

Use explicit optional LangChain StructuredTool bindings for six read/normalization
operations; keep the core independent of LangChain and keep Phase 4 extraction
model tool-free. Expose IDs and bounded queries, not arbitrary I/O. Disable tracing
on supported sync/async invoke. Label retrieved strings as untrusted data and rely
on application capability checks rather than source-text policy or keyword stripping.

Deduplicate transitively by stable IDs/source keys/canonical URLs, preserving all
member source references and first-record ordering. Reject differing same-ID data
and persisted conflicts rather than merge business definitions or overwrite prior
work. Search/normalize/deduplicate/assess in a typed bounded LangGraph; failures route
explicitly to error and no adapter failure becomes partial successful output.

Compose the existing AnalysisService with a transaction-scoped repository so the
whole bounded job/assessment operation commits or rolls back together. No network
calls occur in this write transaction. No domain/schema change or new dependency
is needed. No additional agent, microservice, scraper or generic HTTP tool is added.

## Consequences and limits

The tool boundary is executable and tested offline, including authorization,
malformed sources, timeout/rate-limit signals, byte/count bounds, transitive duplicates,
rollback and injection examples. Import paths are trusted operator configuration;
actual source content remains untrusted. Missing criteria remain unknown. Returned
fit decisions are advisory source-statement comparisons, not permission to apply.

AccessGrant does not authenticate users or implement dynamic production ACLs. The
timeout contract is enforced by future network transports, not a thread-killing
watchdog. Real provider rate scheduling and commercial API compatibility are not
claimed. First-page bounds can yield fewer unique results; there is no exhaustive
pagination/ranking. Duplicate references/search history are returned rather than
stored in a new ledger. Source reconciliation and durable provenance would require
an explicit later design/migration. Contracts and exact limits are in
../discovery-tools.md.
