# Security threat model — Phases 10–14

Review before implementation: the package has trusted operator composition roots,
validated aggregates and scoped tools, but no authenticated user boundary. Raw
repositories and checkpointers are trusted infrastructure, not public APIs. Main
risks are constructing grants from user/model IDs, retaining personal content in
checkpoint history, bypassing approval through legacy mutations and over-exporting
profiles through provider/trace clients.

| Threat | Enforced mitigation | Residual risk |
| --- | --- | --- |
| Cross-user resource reads/writes | Opaque bearer authentication, hashed tokens with expiry/revocation; owner/resource checks at API service boundary | Local token possession grants that account; no SSO/MFA |
| Prompt injection / unauthorized tools | Fixed tool schemas, server grants, exact-version approval; retrieved content treated as data | Human must review prose; prompts alone are not protection |
| Secret exposure | Allowlisted JSON logs, static error responses, no raw exception/trace payloads | Trusted process/file administrator can read local data |
| Unsafe outbound URL | No URL-fetch endpoint; fixed provider endpoint; explicit model export and metadata-only telemetry | Operator-installed adapters must honor policy |
| Malicious imports | Bounded JSON only, extension/media-type validation, no archive/PDF/DOCX parsing | No antivirus/content-disarm pipeline; unsupported files rejected |
| Resource exhaustion | Request body limits, bounded pagination/jobs/context/model calls/deadlines | Single-host cooperative limits, not distributed DoS protection |
| Forged/stale approval, duplicate effects | Immutable hashed proposal, authenticated event, persisted claim, no retry of uncertain effects | No distributed exactly-once or cancellation after dispatch |
| Personal data retention | Explicit delete/revoke operations, reference checks and fail-closed retention conflict | Sensitive action audit records retained; not full account erasure/GDPR compliance |

Authentication and permission checks belong in application code. Never expose raw
repository, arbitrary SQL/shell, checkpoint blobs or principal construction to a
model or public caller. Development operator commands remain trusted capabilities.
Production deployment requires TLS termination, token rotation/expiry, restricted
filesystem permissions, backup retention and a reviewed identity/retention policy.

## Chat transcript boundary

ConversationService enforces owner checks independently of browser state and routes
only a literal command allowlist. It cannot confirm profile facts or sensitive actions.
Resource selections are owner-checked again before workflow execution. Existing exact
proposal/version/hash approval endpoints remain authoritative. Transcript messages,
operation reservations and clarification state live in conversations.sqlite3, outside
checkpoints and confirmed memory. Deletion cascades only through transcript records;
retained business audits and backups follow their separate policies. Tokens remain in
page memory and are never added to operation metadata. UI text uses safe DOM insertion.

Limits and idempotency reduce replay/resource risks; a SQLite transcript write lock
currently serializes operations across conversations. This local design is not a
multi-worker throughput guarantee. Readiness now checks the transcript schema too.
See chat.md for privacy limits, backup inclusion, recovery and verification boundaries.
