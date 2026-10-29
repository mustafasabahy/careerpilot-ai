# CareerPilot AI — final technical audit

Reviewed 28 October 2026. This is a local technical-preview assessment of the actual
repository, not a production/security certification. Phases 9–13 were completed and
verified sequentially before this final review; see phase-results.md. Work stops after
this audit report. No real model/telemetry integration, recruiter message, application
submission, production deployment, coverage percentage or performance benchmark is
claimed.

## Verified capabilities and audit evidence

| Area | Verified implementation | Practical limit |
| --- | --- | --- |
| Architecture | Modular monolith; pure domain rules, application ports, separate workflows/adapters, inward import checks | Trusted composition/raw repositories are not public authorization APIs |
| Domain/data | Pydantic validation, missing/unknown/evidence states, UTC IDs/times, transitions, immutable versions, relational integrity and rollback | Declared evidence is not independent verification |
| LangGraph | Actual routing, typed state, reference-only context/approval checkpoints, interrupt/Command and reopened/process-restart recovery | Earlier graphs are not retrofitted with generic checkpoints |
| LangChain | Six scoped read tools, native structured extraction, validated fact-ID planning, explicit model construction | Actual provider quality/cost untested; API stays offline |
| Memory | Owner-scoped confirmed preferences/facts/goals/corrections; deletion invalidates old context | Memory forget is not whole-account erasure |
| Approval | Immutable exact-version/hash proposals, authenticated events, target/prerequisite/expiry checks, claim/outcome/audit | Unknown effects never automatically resend; no live sender |
| Reliability | Bounded retryable reads/models, Retry-After, zero hidden SDK retries, cooperative budgets/cancellation, operation metadata | No hard preemption/global billing cap/distributed exactly-once |
| Authentication | Random expiring hashed local tokens, revocation, per-resource ownership, two-account API denials | No SSO/MFA/production identity provider |
| Privacy/security | Safe errors/logs, request/import/outbound policies, self-only CSP, data-only tools, conditional deletion | Audit retention and backups require organizational policy |
| Observability/evaluation | Optional allowlisted LangSmith metadata, local logs, actual versioned offline dataset and baseline | No live cloud ingestion, broad model benchmark or LLM judge |
| API/UI | Validated FastAPI endpoints, static UI, actual end-to-end API flow, relevant workflow audit/history, explicit states | UI tests are limited; no complete authenticated browser E2E suite |
| Operations | Explicit startup/migration, health/readiness, private production configuration, backup/restore, CI template | Linux supervisor/TLS/remote CI/deployment not executed here |
| Dependencies | Locked package graph, successful build, current advisory scan | Advisory absence is not a guarantee of security |

## Scoped defects fixed during final review

1. Definition-file writes formerly followed SQL commit. They now complete inside
   the job transaction before commit; failure rolls back the authoritative job.
   Repeated identical imports are idempotent; conflicting definitions cannot overwrite
   an existing registered snapshot. Fail-closed orphan reservations do not break lists.
2. LangSmith's SDK can inject runtime/environment metadata automatically. The client
   now explicitly omits that information, disables OpenTelemetry/replica endpoints and
   hidden retries. Regression tests inspect the actual construction flags. No raw
   profile/document input/output is supplied to the exporter.
3. The stock API documentation UI depended on a CDN blocked by the self-only CSP.
   A local schema viewer now works without weakening CSP; verified in a real browser.
   OpenAPI also explicitly declares bearer security requirements for protected routes.
4. Runtime metrics now update under the resource-counter lock; a concurrent test
   verifies exactly 10 allowed model attempts out of 100 competing requests and
   consistent node/token counts.
5. Resource exhaustion now returns safe HTTP 429 (timeouts 504, busy storage 503,
   cancellation 409), rather than an unexplained generic server error.
6. The standalone manager script exposed the missing Python typed-package marker.
   Added py.typed and expanded strict mypy to scripts as well as source/tests.
   UI pre-connection/uncertain-state messages were also made explicit.

The production-configuration alias issue was caught and fixed in Phase 13 before
that phase passed: direct environment="production" input is no longer ignored.
Larger changes such as tenant-aware uniqueness, whole-account erasure, SSO, a live
reconciler and PostgreSQL were deliberately deferred with priorities in known-limitations.md.

## Commands actually executed and final results

Environment: macOS, Python 3.12.4, uv 0.13.6, Node 22.18.0. uv is workspace-local;
commands used PATH="$PWD/work/tooling-env/bin:$PATH" when needed.

The final `sh scripts/check.sh` executes:

| Command | Actual result |
| --- | --- |
| uv lock --check | Passed, 66 packages resolved |
| uv run --locked ruff format --check . | Passed, 153 files already formatted |
| uv run --locked ruff check . | Passed |
| uv run --locked mypy | Passed, 109 source/test/script files after typed-marker fix |
| uv run --locked pytest | 517 passed, 1 skipped; 518 collected |
| node --check src/careerpilot/web/app.js | Passed |
| node --check src/careerpilot/web/api-docs.js | Passed |
| node --test tests/ui/logic.test.mjs | 3 passed, 0 failed |
| uv run --locked careerpilot-evaluate --output work/eval-current.json --baseline evals/baseline-v1.json | 12/12 passed, deterministic baseline matched |

Additional completed verification:

- uv build: source archive and wheel built; wheel includes all six static web assets
  and the typed marker. Source archive includes versioned evaluation data.
- uv run --locked mypy scripts/manager_demo.py: passed independently after adding
  the typed marker (the initial standalone check failed and led to that correction).
- uv run --locked python scripts/manager_demo.py --directory work/final-manager-demo-reviewed
  --output outputs/manager-demo.json: real synthetic analysis/drafts, pause/reopen/
  approval/resume, repeated recovery with one effect, a separate unknown outcome,
  independent simulator receipt inspection, two counted retry attempts and 12/12 evals.
- uvx pip-audit --path .venv/lib/python3.12/site-packages --skip-editable --format json:
  pip-audit 2.10.1 completed, 64 distributions reported, no known vulnerabilities found.
  Earlier invocations with incompatible flags failed and were not counted as scans.
- Actual loopback server at 127.0.0.1:8765: /health, /ready, /, /docs, /openapi.json and
  /assets/app.js all returned HTTP 200; an authenticated synthetic seed returned 201.
  Credentials were read locally and never printed or exported. Browser inspection
  confirmed the responsive initial UI/controls and local API documentation render.
- Automated tests cover migrations/rollback, two-account API isolation, actual
  analysis→draft→application→approval flow, safe readiness failure, stopped-service
  backup/restore, a process killed after its simulated effect, subsequent restart
  and no duplicate effect or decision/claim.

The skipped test is the explicit paid live-LLM integration test; required opt-in was
not supplied. No cloud keys/accounts/services were assumed. No trace contents,
production status, coverage percentages or load-test claims were fabricated.

## Decisions and trade-offs

Pure evidence rules precede graph orchestration. Missing/unverified data stays
unknown. Conservative fact-selected template drafts favor factual reviewability
rather than free-form rewriting. Reference-only checkpoints and separate confirmed
memory make privacy/correction boundaries visible. Exact-version approval plus a
unique durable claim favors unknown/reconciliation over duplicate sending. SQLite
and a static UI keep the local deployment small; this choice limits HA/tenant scale.
Metadata-only manual tracing preserves privacy at the cost of full model traces.

## Preservation and packaging

No prior Phase 8 source file was deleted. Domain career models/calculation rules,
historical migrations and original examples remain available. The existing provider
factory test was updated to assert per-call bounded timeout; no quality gate was
weakened. Strict checking expanded to scripts. Existing dependency versions were not
upgraded. FastAPI/Uvicorn bring six new resolved packages; urllib3 is now declared
directly to configure telemetry retry behavior. Local tokens, SQLite files, environments,
caches, scratch data and previous outputs are excluded from the delivered source ZIP.
Git metadata/remote hosting are still absent. The GitHub workflow is supplied but
no remote CI run is claimed.

## Before a real production launch

Complete the prioritized identity/privacy/retention, tenant uniqueness, Linux/TLS,
backup restore-on-target, workload/rate-limit and incident-ownership review. Validate
live provider consent/quality/cost separately; do not activate a sender without
idempotency/reconciliation proof. Determine RTO/RPO and operational retention instead
of inferring them from tests. The supplied deployment/operations checklists and
known-limitations.md distinguish these remaining requirements from verified local
behavior. Manager demo instructions are in demo-script.md.


## Interactive UI verification follow-up — 2026-10-28

The synthetic browser walkthrough exercised mixed-requirement analysis, evidence-linked
CV and cover-letter drafts, interview preparation, application tracking, a paused
approval, approved local status change and recovery returning the same outcome ID.
A simulated recruiter message was then staged and left pending for human review.
No external message was sent and no real model call was made.

The walkthrough exposed an evidence display defect: the API returns evidence IDs,
while the UI expected embedded evidence. The UI now resolves IDs to profile/source
provenance, with an explicit fallback for unresolved references. A regression test
covers both cases.

After this fix, `PATH="$PWD/work/tooling-env/bin:$PATH" sh scripts/check.sh` passed:
lock validation, Ruff formatting (301 files) and lint, mypy (109 source files),
517 Python tests (1 live-model test intentionally skipped), both JavaScript syntax
checks, 4 UI logic tests, and 12/12 offline evaluations against the baseline.
