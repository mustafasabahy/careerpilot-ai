# ADR-004: Provider-neutral structured extraction before deterministic analysis

- Status: Accepted for Phase 4
- Date: 2026-10-28
- Extends: ADR-001 through ADR-003 without changing the modular monolith

## Context

Real extraction is needed without provider classes in domain/application/graph
code, accidental paid tests, ungrounded candidate facts or weakened existing
validation. The current JobPosting model requires title/employer, while source text
may omit or contradict them. The Phase 3 graph already calculates transparent fit
results and does not need to be replaced by an LLM-driven agent.

## Decision

Add an application-owned JobModel port and Pydantic extraction schemas. Select mock
or OpenAI through separately validated environment settings; construct clients only
in the provider factory. Use supported ChatOpenAI.with_structured_output with native
strict JSON schema. Reject automatic legacy tool-calling fallback. Require explicit
real provider/model/credential; do not infer application credentials from Codex.

Treat documents as untrusted user data under fixed system policy, with no tools,
model-selected permissions or candidate inputs. Validate schema and source snippets,
attach system-owned inference provenance, and retain missing/unknown/contradictory
states. Surface explicit errors for provider, parsing, truncation/refusal and
ungrounded data; never replace a failed real request with a mock success.

Prepare unreviewed job snapshots and feed their definitions into the existing
unchanged graph. Do not change candidate facts, introduce a model hiring score or
promote inferred mandatory flags to confirmed hard constraints. If job identity is
missing/conflicting, return extraction-only requiring review instead of inventing
values or making a broad domain/schema change. All extracted results require review.

Default to deterministic fixture-only mock. Normal tests deny sockets and skip paid
integration. Require an explicit CLI flag, environment opt-in and real settings for
live tests. Use actual LangChain/SDK HTTP simulation for offline integration checks.

## Consequences

Only one real provider adapter is added initially; other providers implement the
same port and add explicit factory/configuration support. No unnecessary SDKs,
agent, tool executor, server or migration engine is introduced. Schema v1 and all
old domain/persistence behavior remain compatible. Existing runtime dependencies
already supplied LangChain core; it is now declared directly for provider imports.
OpenAI SDK errors are isolated in its adapter. httpx2 is a direct development
requirement for the installed SDK's in-memory transport/error test types.

Source quotes prove origin, not truth/entailment; review is still required.
Qualifications/years and location/arrangement are retained, not automatically
matched as new business rules. Durable source/extraction history and human promotion
are deferred, and real-model quality/server compatibility remains unverified until
an authorized operator runs the opt-in smoke test. Real requests transmit job text,
can incur token/retry costs, and are nondeterministic. No paid call was made here.

Configuration, supported modes, costs, error taxonomy, trust limits, sample output,
verified versions and official references are in ../llm-integration.md.
