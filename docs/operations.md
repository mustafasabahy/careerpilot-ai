# Operations and recovery

## Backups and restore

Stop the service before a coordinated snapshot. The operator CLI requires explicit
--confirm-stopped; it cannot prove the supervisor is stopped. SQLite's backup API
captures WAL contents safely; the stopped requirement ensures consistency across
business, security, memory, checkpoint and simulator files. jobs/ is copied with
symlink checks. Plaintext credential handoff files are deliberately excluded.

```sh
uv run --locked careerpilot-storage backup /private/data /private/backups/snapshot-001 --confirm-stopped
uv run --locked careerpilot-storage restore /private/backups/snapshot-001 /private/restored-data
```

Targets must be new, never overwrite the running database. Restore includes owner
hashes, so revoke/rotate credentials after a suspected compromise. Validate /ready,
integrity/foreign-key checks, job definition consistency and a known paused workflow
before switching paths. Store backups encrypted/off-host under restricted access;
the supplied tool does not implement encryption, scheduling, retention rotation or
cryptographic signing. Define and exercise those operational controls. Recommended
retention is an organization decision, not a claim of GDPR compliance. Test restores
regularly; no measured recovery time or recovery point promise exists.

## Monitoring

Collect allowlisted application JSON logs with correlation/workflow/operation IDs.
Watch readiness failures, storage busy/integrity errors, resource-limit failures,
unknown outcomes, retry rate and HTTP 401/404/409/5xx counts at the proxy. Check disk
space/WAL growth, certificate expiry, credential expiry and backup freshness.
Optional LangSmith exports metadata only; local logs do not depend on it. Provider
usage/cost is unknown unless supplied/measured; never infer a hiring probability.
Disable request/header/body logging at proxies and tracing agents, especially
Authorization and CV/profile payloads. Uvicorn access logs are disabled by default.

## Incidents

For data exposure: restrict network access, revoke affected owner tokens with
careerpilot-server --revoke-owner UUID, preserve minimal protected forensic evidence,
rotate any provider/telemetry keys in the provider's administration and the secret
store, then issue new short-lived local tokens. No secrets are read from a Codex
subscription. Review retained logs/backups and applicable notification obligations
with the responsible organization; do not infer compliance from this software.

For uncertain sending: stop automatic processing, inspect immutable proposal,
authenticated decision, claim, outcome and independent simulator/provider receipt.
Never repeat an application submission merely because the HTTP caller timed out.
Current live external sending does not exist. Reconciliation is manual; unknown
outcomes remain sealed until a separately reviewed business process resolves them.

For crash/restart: start the same compatible code against the paired data root,
read the owned thread, recover the saved decision. Completed steps/outcomes are
reused; unfinished claimed effects become unknown. Noncheckpointed analysis can be
rerun with stable request identity; do not treat arbitrary journal running entries
as successful or indiscriminately replay side effects. Cancellation before claim
can invalidate the action; after dispatch it cannot unsend it.

For corruption/migration failure: stop service, retain a protected copy, check
integrity, restore a matched backup/code version to a fresh directory and validate
before cutover. Do not edit immutable approval hashes or remove uniqueness guards
just to make recovery succeed. No automatic cross-file repair or HA failover exists.

## Deletion and retention

Profile deletion revokes related access then removes dependent local business data
only when no sensitive-action audit blocks it. Referenced document deletion returns
409. Audit-retained data and backups require a reviewed separate retention/erasure
process; memory forget is not whole-account deletion. SQLite deletion is logical,
not forensic page/WAL erasure. Application access is fail-closed if a purge fails.

CLI administration is trusted and local; keep it unavailable to API/model tools.
Review permissions before using real personal data. This release is a technical
preview with verified local behaviors, not a production security certification.
