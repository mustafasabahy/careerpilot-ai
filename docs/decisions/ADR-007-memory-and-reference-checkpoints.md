# ADR-007: Confirmed memory and reference-only checkpoints

Status: accepted for Phase 7 local development.

## Context

Existing graphs keep sensitive aggregates transiently. Adding a checkpointer to
those graphs unchanged would retain source facts and drafts in execution history,
complicating correction/deletion. LangGraph thread IDs alone do not authorize access.
Assistant output and context summaries must not become confirmed candidate facts.

## Decision

Add a small career-context review StateGraph with primitive reference-only state,
explicit human interrupt/resume and the official SQLite checkpointer. Use an isolated
in-memory saver in tests. Keep checkpoints separate from authoritative business
schema v2 and from a new confirmed-memory/ownership database. Bind an authenticated
principal outside graph input and check thread ownership at each exposed operation.

Use typed confirmed preference/fact/correction/goal records, optimistic versions and
an owner-wide revision that invalidates stale context. Retrieve bounded current
context on demand. Keep omission summaries ephemeral and non-authoritative.
Store corrections atomically while retiring their original statement. Commit
thread deletion tombstones before physical checkpoint purge; allow safe retries.
Use short SQLite reservations for local concurrency; make no model calls under them.

## Consequences

Local restart, history, isolation, deletion and concurrency are executable/tested.
Existing entry points/domain data retain their behavior; they are not retrofitted
with generic checkpointing. Memory never silently changes profile facts or CVs.
No embedding infrastructure or automatic assistant-memory extraction is needed.

Global local write serialization limits throughput. The two files require paired
backup/restore; no cross-file atomic transaction or production durability is claimed.
Context edits require a new review thread. Minimal UUID tombstones remain after
logical deletion, and backups/page erasure need a separate policy. Authentication,
account erasure, long-running model job recovery, automatic draft continuation and
PostgreSQL remain deferred. A later expansion must review checkpoint contents and
compatible graph versions rather than enabling persistence indiscriminately.
