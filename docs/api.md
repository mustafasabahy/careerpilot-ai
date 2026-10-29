# Local API and interface

Smallest stack: FastAPI + Uvicorn, existing Pydantic schemas and plain static
HTML/CSS/JavaScript modules. No frontend build step or CDN. API contract is generated
at /openapi.json and /docs. Business comparison, generation and approval are delegated
to existing services/graphs by the transport-independent Portal composition facade.
The API is deliberately offline/deterministic: no model credential enables personal
profile export through this server. External actions use the simulator only.

```sh
uv sync --locked
uv run --locked careerpilot-server --directory work/server --init
# Read the private work/server/dev-access.json locally; do not commit/share it.
uv run --locked careerpilot-server --directory work/server
# Open http://127.0.0.1:8000; paste token, Connect, Load synthetic examples.
```

All /api routes require an operator-issued opaque bearer token. Tokens are random,
hashed in a separate store, expire and can be revoked. Owner IDs never come from
request bodies or the model. Resource ownership is checked for every profile, job,
assessment, document version, application and workflow. Cross-user IDs are unavailable
(404); authentication is 401. Validation is 422 without echoed inputs, conflicts/
retention are 409, staged approval is 202. Success models wrap data; lists include
items/offset/next_offset. Limits are server enforced; request bodies cap at 128 KiB.
No arbitrary file, URL, shell, SQL or checkpoint access endpoint exists.

| Area | Routes |
| --- | --- |
| Profiles | GET/POST /api/profiles; GET/DELETE /api/profiles/{id}; POST /revisions |
| Preferences/confirmed memory | GET/PUT /api/preferences; DELETE /api/preferences/{id}?version=N |
| Jobs | GET /api/jobs?query=&offset=&limit=; POST /api/jobs/import; GET /api/jobs/{id} |
| Analysis | POST /api/analysis (AnalysisRequest); GET /api/assessments/{id} |
| Drafts/interview plan | POST /api/preparations (PreparationRequest), including source fact citations, diff and practice plan |
| Documents | GET /api/documents; GET /api/documents/{id}/latest; GET /api/versions/{id}; DELETE /api/documents/{id} |
| Applications | GET/POST /api/applications; POST /api/applications/{id}/proposals |
| Workflow | GET /api/threads and /{id}; GET /{id}/history; POST /{id}/decision, /recover, /cancel |
| Health | GET /health (process); GET /ready (business/security connectivity and schema) |

Profile edits create immutable new snapshots; existing applications retain their
original candidate reference. User confirmation is explicit, not external factual
verification. Imported JobRecord contains validated job/criterion definitions;
registered definitions are saved under UUID-only filenames and checked against the
authoritative job on load. Search filters a bounded page of owned local jobs, so
pagination must be followed to search later pages. The current interface shows the
first page, a preview limitation rather than a global job-board search.

Approvals are long-lived workflows spanning HTTP requests and server restarts.
The API returns a paused/pending state and the complete reviewable proposal. Only
an authenticated exact-hash/version decision can resume it. GET state/list supports
reconnection; history exposes business audit events, never checkpoint blobs.
No streaming protocol is implemented, and no generic background model queue is
claimed. Active deterministic operations are synchronous and bounded; the UI shows
loading until the actual response and never displays invented progress. Recover
reuses a saved decision, never creates approval or retries uncertain sending.

The UI covers profiles/preferences, registered jobs/import, fit/evidence, reviewable
CV/cover drafts, interview practice, application tracking, staged messages/submission,
approval/rejection/revision and audit inspection. Tokens remain in page memory and
are not saved in localStorage. Untrusted strings use textContent; CSP denies inline
scripts/frames/objects and no CORS credentials are enabled. The local preview is not
a production identity system. No PDF/DOCX/file-upload parser exists; multipart and
unsupported schemas fail. Profile deletion purges related local records only if
sensitive action audit retention permits it. Referenced documents return conflict.
Security revocation precedes destructive writes; failed purges remain fail-closed.

Backend tests use actual TestClient requests and two synthetic accounts. UI tests
use Node's built-in runner for state/approval binding/text insertion; these are not
a claim of complete browser automation or accessibility certification.

```sh
node --check src/careerpilot/web/app.js
node --test tests/ui/logic.test.mjs
sh scripts/check.sh
```

## Conversation API

The primary UI is now a persistent deterministic chat. Existing routes above remain.
POST/GET /api/conversations create/list owner-scoped conversations; GET/DELETE
/api/conversations/{id} retrieve a paginated transcript/delete it; POST /messages
submits bounded text plus a UUID idempotency_key and optional typed resource selection;
POST /operations/{operation_id}/refresh refreshes results or explicitly completes an
interrupted deterministic operation. All are under /api/conversations/{id} and require
the existing bearer credential. Full response schemas, limits, deletion and recovery
semantics are documented in chat.md and generated OpenAPI.
