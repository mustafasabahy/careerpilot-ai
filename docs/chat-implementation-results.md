# CareerPilot AI — persistent chat implementation report

## Outcome and architecture

Implemented an offline deterministic chat-first interface on the existing FastAPI,
SQLite, vanilla JavaScript and Portal/LangGraph stack. No dependencies or locked package
versions changed. Existing review/detail tools remain available. No general-purpose LLM,
streaming, uploads, external job search or real sending/submission was added.

Flow: chat.js → authenticated chat API → ConversationService → PortalChatBackend →
existing authorized Portal methods and workflows. The parser recognizes a bounded
English/Arabic command allowlist; selection and missing-data prompts are explicit.
No chat text becomes confirmed facts, memory or approval authority.

Transcript persistence uses a separate conversations.sqlite3 schema v1. Existing business
schema remains v3. Messages, ordering, owner/context selection, stable operations and
request keys are durable. A reservation commits before workflow execution; a second
transaction commits the assistant response and updated context. Existing idempotent
analysis/preparation runs use stable IDs/timestamps for recovery across that gap.
Cross-database atomicity or distributed exactly-once execution is not claimed.

## Files

Created:
- src/careerpilot/application/conversations.py — contracts, payload validation, parser,
  service and dependency ports.
- src/careerpilot/persistence/conversations.py — versioned SQLite schema and scoped
  repository transactions, unique request keys, ordered messages and guarded operation updates.
- src/careerpilot/chat_portal.py — authorized existing-workflow composition adapter.
- src/careerpilot/web/chat.js — chat interaction, history, result cards and approval controls.
- src/careerpilot/web/chat-logic.mjs — testable direction, selection, outcome and empty-state helpers.
- tests/test_conversations.py — 42 parametrized/integration test cases.
- tests/ui/chat.test.mjs — 6 new Node UI-helper tests.
- docs/chat.md and this report.

Modified:
- src/careerpilot/api/app.py — conversation endpoints, safe conflict handling and transcript readiness check.
- src/careerpilot/portal.py — include transcript initialization in explicit storage initialization/migration.
- src/careerpilot/backup.py — include transcript storage in stopped-service backup/restore.
- src/careerpilot/web/index.html and style.css — primary responsive chat shell, RTL and retained detail views.
- src/careerpilot/web/app.js — export existing API helper, preserve HTTP status on errors,
  dispatch authenticated-workspace events; existing business operations retained.
- scripts/check.sh — syntax-check new JavaScript and run all UI test modules.
- README.md; docs/architecture.md, api.md, demo-script.md, security.md and known-limitations.md.

No repository was initialized. Existing Git metadata is absent; unrelated work, synthetic
business records and historical phase deliverables were preserved.

## New API

Responses follow the existing `{data: ...}` convention and existing Bearer authentication.

| Endpoint | Input / output |
| --- | --- |
| POST /api/conversations | Server-assigned identity/timestamps; 201 Conversation |
| GET /api/conversations?offset=&limit= | Owner-scoped Page[Conversation] |
| GET /api/conversations/{id}?offset=&limit= | Transcript: context, ordered messages, next_offset and visible/interrupted operations |
| POST /api/conversations/{id}/messages | text, UUID idempotency_key, optional typed selection and offset → Operation |
| POST /api/conversations/{id}/operations/{operation_id}/refresh | Fresh state or explicit safe interrupted-run completion → Operation |
| DELETE /api/conversations/{id} | Delete owned transcript/message/operation records only |

Contracts include Conversation, MessageInput, Message, Operation, Transcript, Card,
Selection and Reference. Card payloads validate against existing analysis/preparation
models and typed application/approval/page payloads selected by card kind. IDs, roles
and owner/timestamps cannot be injected in a user message. Wrong-owner resources return
404, missing credentials 401, conflicting keys/state 409, invalid schemas 422, oversized
HTTP bodies 413. Lists/messages are paginated. See chat.md for exact limits.

## Commands and limits

| English | Arabic |
| --- | --- |
| Show my jobs | اعرض الوظائف |
| Analyze job | حلل الوظيفة |
| What information is missing? | ما المعلومات الناقصة؟ |
| Prepare my application | جهز التقديم |
| Show my applications | اعرض طلباتي |
| Show my pending approvals | اعرض الموافقات المعلقة |
| Select job | اختار وظيفة |
| Help | مساعدة |

Use resource-selection buttons when prompted. Job titles may be identical; stable ID
suffixes distinguish them. Arbitrary job names/IDs in natural-language prose are not
parsed. Unsupported text returns help. Clarification answers do not modify profile data;
explicitly confirmed profile/preference forms remain the edit path. English/Arabic command
switching is supported; domain draft/question prose can remain English.

Approval cards expose exact proposals and existing authenticated decision controls.
“yes”/“نعم” has no authority. Pending, approved-but-unproven, applied local changes,
simulated effects, definite failures, unknown and invalidated outcomes are labelled
separately. Reading/refreshing a conversation never resends an external action.

## Commands executed and actual verification

Commands were run with the existing local uv installation on PATH:
`PATH="$PWD/work/tooling-env/bin:$PATH"`.

Baseline before source edits:

`uv run --locked pytest tests/test_api.py tests/test_action_approval.py tests/test_restart_recovery.py -q`
→ **39 passed**.

Final `sh scripts/check.sh` exited 0 and ran:

| Command | Actual result |
| --- | --- |
| uv lock --check | Passed; 66 resolved packages |
| uv run --locked ruff format --check . | Passed; 306 files already formatted (includes local scratch Python files) |
| uv run --locked ruff check . | All checks passed |
| uv run --locked mypy | No issues in 113 source files |
| uv run --locked pytest | **559 passed, 1 skipped**, 560 collected, 8.92 seconds |
| node --check src/careerpilot/web/app.js | Passed |
| node --check src/careerpilot/web/api-docs.js | Passed |
| node --check src/careerpilot/web/chat.js | Passed |
| node --check src/careerpilot/web/chat-logic.mjs | Passed |
| node --test tests/ui/*.test.mjs | **10 passed, 0 failed** |
| uv run --locked careerpilot-evaluate --output work/eval-current.json --baseline evals/baseline-v1.json | **12/12 passed** |
| uv build | Source distribution and wheel built successfully |

The skipped test requires explicitly enabled live LLM configuration. No live API call
or paid model evaluation was made. An additional ZIP-content assertion verified that the
wheel includes both chat JavaScript assets and both conversation application/storage modules.

Intermediate implementation checks exposed a missing ChatConflict import and a mypy
TypeAdapter union-construction issue; both were fixed before the final passing gate.
Manual UI use found an incorrect empty-state label above existing approvals; the condition
was corrected and a Node regression test added. No failing required check remains.

New tests exercise bilingual commands, unsupported text, ambiguity, persisted clarification,
same-user conversation isolation, cross-user denial, request/key conflicts, duplicate/concurrent
submission, pagination, invalid roles/limits, revoked resources, chat-to-graph integration,
reopened storage and recovery after business commit, approval/unknown-outcome safety,
operation lifecycle validation, backup/restore and audit-preserving deletion.

Manual browser verification on localhost exercised Arabic job listing, job selection,
refresh during profile clarification, reconnect/resume, actual evidence-based analysis,
missing-data questions, actual CV/cover generation and existing approval-state cards.
Typing “نعم” produced help without approval. After a real server-process restart, reconnect
restored the same transcript and result cards. Final DOM inspection confirmed an existing
pending approval and absence of the incorrect empty-state text. Screenshots are delivered
separately. This is manual verification, not a maintained browser E2E suite or mobile/accessibility
certification. No browser-testing framework was added.

## Migration and local operation

For the already-running synthetic localhost instance, these commands ran successfully
with the old server stopped:

```sh
uv run --locked careerpilot-storage backup work/audit-server work/chat-before-migration --confirm-stopped
uv run --locked careerpilot-server --directory work/audit-server --migrate
uv run --locked careerpilot-server --directory work/audit-server --port 8765
```

The localhost service was restarted again on the final source and remains available at
http://127.0.0.1:8765. Existing synthetic applications and approval records remain intact.
The token was not rotated, stored in browser persistence or included in this report.

For another existing installation, stop and back it up, then run --migrate on its own
storage directory before startup. Fresh --init creates both existing business and new
transcript storage. Older backups without transcripts require --migrate after restoring.
Readiness checks the transcript schema. Unknown transcript schema versions are rejected.

Deletion cascades through that conversation's messages/operations. It does not erase
profile/documents/applications, approval audits, checkpoints or backups. This is not an
account-erasure or regulatory-compliance claim.

## Demo procedure

1. Open localhost, expand Workspace connection and connect using the same owner's token.
2. Use the saved synthetic account, or seed only an empty synthetic account.
3. Send “اعرض الوظائف”, then “حلل الوظيفة”; choose a job.
4. Refresh before choosing a profile. Reconnect; choose the profile from the saved prompt.
5. Inspect evidence, send “ما المعلومات الناقصة؟”, then “جهز التقديم”.
6. Open workspace details for existing tracking/proposal creation controls if needed.
7. Send “اعرض الموافقات المعلقة”. Inspect proposal/version/hash. Send “نعم” and verify
   no approval occurs; only use explicit controls to decide on a reviewed proposal.
8. Reconnect/restart and inspect the same results. The existing failure/recovery demo
   in demo-script.md covers uncertain sending without duplicate effects.

## Remaining limits

- Literal bounded commands, not general natural-language reasoning.
- Current drafts are evidence-backed templates; some domain text is English.
- No streaming/background worker, uploads, external search or real submission.
- Token re-entry after refresh remains intentional.
- SQLite transcript writes serialize across conversations; this is a local single-host preview.
- Business/transcript files are not one distributed transaction; recovery relies on stable
  identities and existing idempotent deterministic workflows.
- Historical audit retention/backups remain separate from transcript deletion.
- No live-model quality claim, production-readiness claim, broad browser regression suite,
  phone-device certification, coverage percentage or benchmark is claimed.
