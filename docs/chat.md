# Persistent deterministic chat

This is an explicitly bounded demo assistant, not general natural-language intelligence.
No new dependencies, LLM calls, external search, uploads, streaming or real submission
are introduced. The old workspace/review views remain under “Workspace details”.
Tokens still live only in page memory: after refresh, enter the same owner's token
and Connect to restore that owner's conversations. No localStorage token is used.

## Commands and selection

Matching ignores surrounding whitespace, case and trailing `.?!` punctuation.
Supported English examples:

| Command |
| --- |
| Show my jobs |
| Analyze job |
| What information is missing? |
| Prepare my application |
| Show my applications |
| Show my pending approvals |
| Select |
| Help |

Short aliases `jobs`, `analyze`, `prepare`, `missing information` also work.
“Select job” clears selected job/profile context and starts a fresh selection.
Use returned resource buttons, not arbitrary names or UUIDs in free text, to resolve
ambiguous jobs/profiles. The server never silently chooses the first available profile.
The pending intended command, selected IDs and expected selection kind are durable.
Next-page controls page through available selections/results. Empty results are explicit.
Unsupported text (including “yes”) produces help and never grants approval.
Clarification answers do not update candidate facts: use the existing explicitly
confirmed profile/preferences forms to change those records.

Analysis delegates to Portal.analyze; preparation to Portal.prepare with its existing
fact-selection planner and deterministic renderer. Missing-information questions reuse
build_intelligence; no fit calculations or approval rules are implemented in chat.
Approval listings display current states for owned workflow threads, including pending
and completed/uncertain states. They are not a new approval store. Existing paginated
local jobs are not global discovery. Document prose remains a constrained draft.

## Architecture and storage

- application/conversations.py: Pydantic boundary contracts, bounded bilingual parser,
  ConversationService, backend/store ports, clarification orchestration.
- chat_portal.py: composition adapter invoking authorized Portal services.
- persistence/conversations.py: transaction-scoped SQLite adapter/schema version 1.
- api/app.py: authenticated conversation transport alongside unchanged business routes.
- web/chat.js and chat-logic.mjs: transcript, selection/result/approval cards, composer,
  bilingual prompts, RTL content direction, safe text rendering and existing detail links.

conversations.sqlite3 is separate from business.sqlite3, memory.sqlite3 and graph
checkpoints. Conversations contain owner, UTC timestamps, selected resource IDs and
pending selection state. Ordered user/assistant messages have server-generated UUIDs
and timestamps. Operations contain the original bounded request/key, immutable run
identity/context and status/response references. The client cannot submit assistant roles,
owner IDs, message IDs or timestamps. A unique (conversation, request key) constraint
rejects key reuse for different payloads. Operation updates cannot reopen terminal runs.

Every public service method checks authenticated conversation ownership first.
Resource IDs are revalidated before execution; historical cards are revalidated on read
and become unavailable if access is revoked. The transcript is user-visible history,
not authoritative memory and not a grant of tool permissions. Conversation content is
not added to application logs or LangSmith metadata. Untrusted content uses textContent.

Limits: 4,000 characters/user message; 128 KiB HTTP request and result-card bounds;
1,000 transcript messages/conversation; 100 conversations/owner; pages up to 50 entries.
Selection/results use 20-entry pages. The service serializes transcript writes with
BEGIN IMMEDIATE, allowing one in-flight operation/conversation. SQLite's single writer
also serializes operations across conversations in this first local implementation.

## Reservation, replay and recovery

The user message and running operation reservation commit before any business operation.
The response, final context and terminal operation update commit together. Business and
transcript databases are not one atomic transaction. Analysis/preparation IDs and creation
timestamps derive from the durable operation, so explicit recovery replays the existing
idempotent workflow instead of creating another draft set. No chat-dispatched command
executes external actions. A process loss leaves a visible running operation; Continue
operation reuses that reservation after ownership checks. Model/background job recovery
is outside this scope.

On network uncertainty, the page retains the original request key for Retry and blocks
new sends until retry/reconnect. Reload after authentication reads persisted messages;
it does not silently resend. Retrying an existing key returns the same operation.
GET transcript refreshes approval cards from authoritative state; it never resumes or
executes approvals. The explicit approval control re-fetches and checks the displayed
proposal identity/version/hash before posting the existing decision payload. Recovery
uses the existing approval endpoint and never turns unknown sending into an automatic retry.

## API contracts

All paths require the current Bearer credential; responses use `{data: ...}`.

| Method/path | Contract |
| --- | --- |
| POST /api/conversations | No user-controlled identity; 201 Conversation |
| GET /api/conversations?offset=0&limit=20 | Page[Conversation], next_offset |
| GET /api/conversations/{id}?offset=0&limit=30 | Transcript: conversation, ordered messages, next_offset, operations associated with visible responses plus interrupted operations |
| POST /api/conversations/{id}/messages | MessageInput: text, UUID idempotency_key, optional selection `{kind: job/candidate, id: UUID}`, optional offset; Operation result |
| POST /api/conversations/{id}/operations/{op}/refresh | Read fresh result or explicitly complete an interrupted deterministic run; Operation |
| DELETE /api/conversations/{id} | Delete owned transcript/messages/operation records; `{status: deleted}` |

Operation states are running/completed/failed/unknown; external action status remains
in the existing typed approval response (pending/approved/executing/applied/simulated/
failed/unknown/invalidated/rejected/revision_requested). A completed listing can contain
an unknown external outcome: it means retrieval completed, not that sending succeeded.
Authentication fails with 401; cross-owner/unknown resources 404; incompatible request
keys or selection state 409; invalid schemas 422; HTTP body limits 413. Safe errors
contain no raw input/provider exception. OpenAPI includes the new Pydantic models.

## Migration and deletion

Stop the existing service and back up its directory before running:

```sh
uv run --locked careerpilot-server --directory work/server --migrate
uv run --locked careerpilot-server --directory work/server
```

Fresh `--init` includes transcript schema creation. Existing business schema remains v3;
chat uses its own explicit v1 initialization. Unknown/unversioned existing chat databases
are rejected without replacement. Stopped-service backup/restore now includes the chat
file. An older backup without it requires --migrate after restoration.

Deleting a conversation physically deletes its messages and operation records via foreign
key cascades. It does not delete applications, documents, profile facts, approval records,
audit events, checkpoints or business operation results. Those retain existing lifecycle
and retention rules. Backups can retain deleted transcripts until operators expire them.
Deletion is not full account erasure or a compliance claim. Historical re-entry of the same
request after deletion cannot use that deleted transcript (404).

## Verification and limitations

Python tests cover actual API and graph-backed analysis/preparation, bilingual commands,
ambiguity, ownership/revocation, same-owner conversation isolation, duplicate/concurrent
requests, paging, reload/reopen, replay after business commit, approval cards, unknown
outcomes and audit-preserving deletion. Node tests cover direction, clarification controls,
text safety and distinct approval/outcome states. Existing safety/restart suites remain.
No browser automation framework was added; manual browser verification is reported
separately and is not a maintained end-to-end regression suite or accessibility certification.
Messages are bounded literal commands; generated domain explanations may remain English.
No automatic translation or semantic paraphrase understanding is claimed. Long transcripts
are paged, not sent to a language model. Concurrency/scaling, SSO, stream reconnection,
rich document editing and general-model chat are deferred.

## Chat LLM integration (Phase 2)

The API now composes `ConversationalAgent`, `PortalAgentTools` and a provider-neutral
`ChatModel`. The existing deterministic command path remains available in demo mode.
`OPENAI_API_KEY` selects OpenAI when chat mode is not explicitly set; the model is
configured by `CAREERPILOT_CHAT_MODEL` or the shared `OPENAI_MODEL` alias (with the
provider-specific setting taking precedence). Explicitly setting
`CAREERPILOT_CHAT_MODE=demo` keeps all chat offline even when a key is available.
Explicit `llm` without a key returns setup guidance, not a silent fallback.
No paid call runs at startup. The ordinary tests block socket access.

The existing SQLite JSON records accept additive fields with defaults; no SQL schema
migration or data reset is needed. Operations record mode, execution claims, tool
fingerprints and safe counts. Model waits do not hold a transcript write transaction.
Unknown interrupted writes must be inspected, never blindly replayed.
