# Deployment path — single host, one worker

Target: a private Linux host with local persistent storage, one Uvicorn worker on
loopback, and a separately managed HTTPS reverse proxy. This matches the tested
SQLite/checkpointer design. No cloud account, domain, certificate or deployment
exists for this project. Local build/start/readiness/restart tests are verified on
macOS; the Linux systemd unit and reverse proxy/TLS setup are deployment instructions,
not an already verified production service. Containers add no required isolation or
storage guarantee here and are intentionally omitted. No Kubernetes/HA claim.

## Reproducible installation and startup

Use Python 3.12.x (local tests: 3.12.4), uv 0.13.6 and Node 22 for UI tests. On the
selected host, create a dedicated unprivileged account, private data directory and
protected environment file. Install the reviewed source checkout at /opt/careerpilot.

```sh
uv sync --locked
sh scripts/check.sh
uv build
# Copy deploy/production.env.example to a protected environment location.
# Replace directory/public-origin placeholders, export settings through the supervisor.
uv run --locked careerpilot-server --migrate
uv run --locked careerpilot-server --issue-token YOUR-OWNER-UUID
uv run --locked careerpilot-server
```

Production mode requires an absolute existing private data directory (0700) and a
real HTTPS public origin. Placeholder .invalid origins, missing values and relative
paths are rejected. Development-only --init and synthetic API seeding are disabled
in production. --migrate explicitly initializes/upgrades storage without issuing
credentials. The server never silently migrates authoritative business schema during
normal startup. Operator-issued tokens expire after eight hours, are stored as hashes,
and plaintext is written only once to an exclusive 0600 operator access file.
Protect/remove that file after secure handoff. Never put tokens in URLs or source.

The process binds 127.0.0.1, trusts forwarded headers only from loopback, allows only
configured hostnames, uses one worker, 32 concurrent connections, five-second keepalive
and existing workflow/model/SQLite limits. Configure the reverse proxy with TLS,
128-KiB body cap, conservative connection/rate limits, at least the configured active
request timeout, no caching of authenticated responses, and Authorization header
forwarding. Do not expose the backend port publicly. Public-origin validation does
not itself install TLS or validate a real certificate.

The example deploy/careerpilot.service supplies a dedicated account, private umask,
filesystem restrictions, memory/task limits and restart policy. Install/review it
with actual paths/account and a supervisor environment file. The repository includes
a GitHub Actions quality workflow, ready if hosted on GitHub; no Git remote/platform
was identifiable, and no remote CI run is claimed. Actions currently use version tags;
pin reviewed commit SHAs when adopting the workflow in an organization.

## State and migration

Keep business.sqlite3 (schema 3), security.sqlite3, memory.sqlite3 (independent schema 1),
checkpoints.sqlite3, simulation.sqlite3 and jobs/ definition snapshots together on a
private local volume. SQLite WAL/SHM files must stay with open databases. Do not use
multiple hosts, a network filesystem or separate inconsistent checkpoint/registry
restores. There is no production PostgreSQL adapter. Optional invocation journals
configured outside this root require their own backup policy.

Before migration: stop requests/service, back up, retain the matching source/lockfile,
then run --migrate and readiness/fixture checks. v1/v2 → v3 is additive/atomic and
preserves old aggregate payloads. Unknown schemas fail. No downgrade is implemented;
rollback requires restoring a matched pre-migration backup and compatible code,
never merely pointing older code at a newer database. The new cancelled action
reason is an additive payload change that old readers may reject despite schema 3.

## Health and readiness

/health proves the process responds; /ready checks business schema/transaction and
security-store connectivity, returns 503 on failure without exceptions/credentials.
Readiness is not evidence of external provider readiness, queue health, disk capacity,
backup correctness or TLS. Monitor those separately. Recovered uncertain actions stay
unknown; no automatic external resubmission occurs.

## Launch checklist

- Re-run all checks/evaluation and dependency advisory audit against the actual release.
- Validate Linux supervisor, reverse proxy/TLS and storage permissions on target hardware.
- Exercise a stopped-service backup/restore and authorized checkpoint resume.
- Review opaque-token identity/rotation/retention needs; integrate SSO/MFA if required.
- Resolve data-retention conflicts and account erasure policy before real personal data.
- Test workload/concurrency and define recovery objectives; no RTO/RPO is measured here.
- Keep model export/telemetry disabled until consent, permissions and redaction are reviewed.
- Do not enable a live sending adapter without a separate idempotency/reconciliation review.
- Obtain operational ownership, incident contacts and rollback approval. No production launch
  has been performed or recommended merely because this checklist exists.
