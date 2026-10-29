# ADR-008: Exact-version approval and conservative side-effect recovery

Status: accepted for Phase 8 local development.

## Problem

Historical document approvals do not bind message/target/status-change content.
Graph nodes restart around interrupts; placing side effects there risks duplication.
No database transaction can atomically include a remote service effect.

## Decision

Keep historical behavior, add immutable sensitive-action proposals and authenticated
user decision events bound to owner/thread/version/content hash. Use a separate
reference-only approval graph. Persist proposals before starting; pause without
writes; commit explicit user decisions before Command(resume=event_id). Revalidate
current permissions, prerequisites, target and expiry before durable execution claim.

Add business schema v3 so local application mutation, attempt, outcome and audit can
commit together. For external simulation, commit a unique attempt/idempotency key
before calling an explicit adapter outside the transaction. Replays never retry an
unfinished attempt automatically: preserve an unknown outcome for reconciliation.
Require new proposal ID/thread/version and fresh approval after revision requests.

## Consequences

Approval is an actual persisted event, not model prose or a graph boolean. Hashes,
foreign keys, unique constraints and immutable SQL records make changes reviewable.
Simulator delivery is explicitly not a submitted application. No new dependency,
live sending service or credentials are needed. Existing low-level business methods
remain trusted internal capabilities; a later API must not expose approval bypasses.

Conservative recovery can report unknown even when no effect happened, or while a
concurrent first attempt is completing. Idempotency is not distributed exactly-once
execution. Provider reconciliation, cancellation after dispatch, actual authentication,
production queues and live adapters require later review. Schema v3 cannot be opened
by old code; backup/downgrade and data-retention policies remain explicit limitations.
