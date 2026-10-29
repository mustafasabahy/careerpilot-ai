# ADR-002: Evidence-bearing aggregates and local SQLite persistence

- Status: Accepted for Phase 2 foundation
- Date: 2026-10-28
- Extends: ADR-001 without changing the modular monolith or dependency direction

## Context

Career entities need validation, traceable facts, controlled lifecycle transitions,
stable serialization and local persistence without a model provider or server.
There was no existing database to migrate. PostgreSQL is a future option, not an
operational requirement of this phase.

## Decision

Introduce frozen Pydantic domain models grouped by profile, jobs/fit,
applications/documents and governance. Preserve UUIDs, require aware timestamps,
normalize to UTC, and represent missing/unknown/known claims explicitly. Retain
source/evidence metadata and distinguish AI inference from user confirmation.
Use pure domain methods for fit decisions, version lineage and lifecycle rules.

Introduce one typed, application-owned CareerRepository protocol for the concrete
storage operations needed now. Avoid separate empty repository classes or generic
CRUD inheritance. The SQLite implementation imports that protocol and domain
models; application/domain never import the adapter.

Use standard-library sqlite3 with explicit connection-scoped transactions,
foreign keys and uniqueness constraints. Keep profiles/jobs as validated JSON
aggregates, while projecting relational identities and cross-aggregate references
into columns. Use composite ownership foreign keys and validate requirement IDs
against job-owned records. Insert versions/audit records immutably through the
repository. Use savepoints to keep each mutation and its automatic audit atomic.
Use schema v1 with PRAGMA user_version and reject unsupported/unknown schemas.

Do not add an ORM, migration dependency, server, external service or LangGraph.
Do not change the existing health command to create a database. Approval records
track decisions; external execution/authorization policy is a separate future use
case. Advisory AI scores never control explicit business rules.

## Consequences and alternatives

SQLite requires no extra packages and supports real local relational constraints.
A JSON-only file would lack cross-reference/uniqueness and transactional guarantees.
An ORM could help once query/migration requirements justify it; adding one now
would expand the surface without a concrete need. Full normalization is deferred
until filtering/reporting needs justify more tables.

The hybrid format needs migrations for JSON and relational columns together.
SQLite is a single-writer local store with plaintext files; production controls,
PostgreSQL migrations/concurrency and tamper resistance are out of scope.
Append-oriented profile/job persistence avoids unreviewed edit/history semantics;
real profile editing and deletion must be designed explicitly. The exact model,
constraints, transition table and limitations are in ../data-model.md.
