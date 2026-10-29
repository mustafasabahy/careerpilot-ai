# Phase 2 data model

All entity IDs are UUIDs generated once and preserved during serialization and
state changes. All datetime fields reject naive values and normalize aware values
to UTC. Calendar dates (education, employment, certifications) remain dates.
Models reject extra fields, are frozen, and use tuples for nested collections.
Unsafe Pydantic construct/copy APIs are not validation boundaries: the persistence
adapter revalidates complete aggregates before inserts and validates every read.

## Concepts and storage

| Concept | Data and rules | Storage |
| --- | --- | --- |
| Evidence | UUID, creation time, origin, evidence locator | Owned/nested with claim or record |
| Claim[T] | Explicit missing/unknown/known state, typed value, evidence | Nested JSON |
| CandidateProfile | Display-name and authorization claims; career record collections | `candidates` |
| WorkExperience | Employer/title, start/end dates, current-status claim, evidence | Profile-owned records |
| Skill | Name, proficiency claim: beginner/intermediate/advanced/expert, evidence | Profile-owned records |
| Education | Institution/qualification, period, evidence | Profile-owned records |
| Certification | Name/issuer, issued/expiry dates, evidence | Profile-owned records |
| LanguageProficiency | Language, CEFR A1–C2/native proficiency claim, evidence | Profile-owned records |
| JobSource | Normalized source name, optional external ID/URL, retrieval time | Nested in job; identity projected into columns |
| JobPosting | Title/employer/source, requirements | `jobs` |
| JobRequirement | UUID, description, category, mandatory true/false/unspecified, evidence | Job-owned records |
| JobFitAssessment | Candidate/job IDs, per-requirement outcomes/evidence, optional AI score | `assessments` |
| ApplicationDocument | Candidate ownership and resume/cover-letter kind | `documents` |
| DocumentVersion | Document ID, positive sequential number, predecessor, content, AI-origin flag | `versions` |
| JobApplication | Candidate/job IDs, selected document version, lifecycle state/times | `applications` |
| ApprovalRequest | Exact application/version IDs, expiry, pending/approved/rejected decision/time | `approvals` |
| AuditEvent | Application ID, fixed actor/action codes, typed before/after states | `audit_events` |

Profiles contain career facts, not job/application/documents or governance records.
Evidence is scoped to a claim, proficiency or record; a record's evidence applies to
that record's fields as a whole. Evidence IDs/locators are retained without storing
raw source documents. Profile child IDs must be unique within the aggregate.

### Missing, unknown, false and inferred data

An omitted `work_authorized` defaults to `Claim(state="missing")`. An explicitly
unknown value is `Claim(state="unknown")`. An explicit negative is
`Claim(state="known", value=False, evidence=(...))`; strict boolean validation
rejects strings and integers. Missing/unknown claims cannot carry values. Known
claims require both a value and evidence. JSON preserves these distinctions.
Optional end dates mean no date was supplied; they never imply current employment.

Evidence origins distinguish `user_statement`, `source_document`, `ai_inference`.
An inference may be represented as a known *assertion*, but is not a confirmed
fact. `Claim.user_confirmed` is derived only from user-statement evidence. A caller
cannot submit a `user_confirmed` field. Source documents are not automatically
user confirmation. A later authenticated input/review boundary must establish that
evidence origin declarations are trustworthy; Pydantic cannot authenticate actors.

### Fit assessment rules

Outcomes are `met`, `unmet`, `unknown`. Decided outcomes require evidence. Results
must reference requirements of the assessment's job, without duplicates. The pure
`assessment.decision(job)` method returns:

- `ineligible` if any explicit mandatory requirement has an evidenced unmet result.
- `needs_review` if a mandatory requirement lacks a trusted met result, mandatory
  status is unspecified, or the job contains no requirements.
- `eligible` otherwise, meaning only that the explicitly mandatory rules are cleared.

AI inference alone is not trusted to satisfy/reject a requirement. User-statement
or source-document evidence is required for that rule result. Optional requirement
failures do not block eligibility. The optional finite 0–100 `ai_score` is stored
as advisory metadata and is never read by the decision rule or application lifecycle.
An eligibility result is not an application submission or a legal determination.

### Application lifecycle

| From | Allowed next states |
| --- | --- |
| draft | ready, withdrawn |
| ready | draft, submitted, withdrawn |
| submitted | interviewing, rejected, withdrawn |
| interviewing | offered, rejected, withdrawn |
| offered | accepted, rejected, withdrawn |
| accepted / rejected / withdrawn | None (terminal) |

Only draft applications may be inserted. Preparing/submitting an application
requires a selected document version; versions can only be attached/replaced in
draft. State changes preserve identity and enforce monotonically increasing update
times. The repository loads the persisted state before calling the domain rule.
All application changes and approval operations write fixed audit events atomically.

A status records local tracking information; changing it does not send documents
or call an external service. Approval models a user decision for an exact version,
with exclusive expiry (`decision_time < expires_at`), and may be decided once only.
A pending approval for a superseded version cannot be decided. Older approval
records remain intact when a draft selects a newer version. Approval-based external
submission authorization is deferred until an actual submission use case exists;
Phase 2 does not enforce a submission approval gate or claim to perform submissions.

### Documents and references

Version 1 has no predecessor. Later versions must reference the same document's
immediately preceding version number and cannot predate it. `(document_id, number)`
is unique. Version content is immutable via the repository, maximum 200,000
characters, and is never logged. `generated_by_ai=None` means origin is unspecified;
explicit `False` remains distinct from unknown origin. Document kinds cover resume/cover letter;
a single selected version per application is a deliberate Phase 2 limit.

Foreign keys enforce candidate/job/document/application references. Composite
foreign keys enforce that the selected document belongs to the application's
candidate and that approvals belong to that same candidate. Adapter validation
also checks the exact currently selected version when requesting/deciding approval.
Assessment requirement references are validated against job-owned JSON records;
they are not SQL foreign keys. Direct SQL is not a supported application write path
and can bypass domain/JSON/audit rules; relational constraints still protect keys.

## Duplicate detection

Database constraints protect `(source_name, external_id)` and canonical URL
independently, plus primary IDs. Source names are stripped/casefolded; external IDs
remain case-sensitive. Missing external IDs/URLs remain NULL and do not collapse
unidentified manual jobs. `find_duplicate_jobs` returns all source/URL matches,
including conflicts where those keys point to two distinct existing jobs.
Insertion rejects matches; it never silently merges or overwrites records.

HTTP URLs are parsed/normalized by Pydantic; credential-bearing URLs are rejected.
Canonicalization removes fragments and common tracking keys (`utm_*`, `gclid`,
`fbclid`), sorts query pairs, and preserves scheme, path case/trailing slash and
nontracking query values. Thus HTTP/HTTPS, distinct paths and meaningful query IDs
remain distinct. Redirect resolution, fuzzy deduplication and provider-specific URL
rules are deferred. Do not pass secret-bearing query URLs or raw personal data.

## Local persistence usage

```python
from pathlib import Path

from careerpilot.domain.profile import CandidateProfile
from careerpilot.persistence.sqlite import SQLiteDatabase

Path("work").mkdir(exist_ok=True)
db = SQLiteDatabase(Path("work/careerpilot-dev.sqlite3"))
db.initialize()
profile = CandidateProfile()  # No real user data.
with db.transaction() as repository:
    repository.add(profile)
with db.transaction() as repository:
    restored = repository.get(CandidateProfile, profile.id)
    assert restored == profile
```

Run such local scripts with `uv run --locked python script.py`. No database file is
created by configuration import, package import, tests outside their temporary
folders, or the existing health command. Parent directories must exist. File-backed
databases are supported; `:memory:` is rejected because transactions use separate
connections. Do not retain a repository after leaving its transaction.

## Transactions, errors and compatibility

Transactions use one connection and explicit `BEGIN IMMEDIATE`, commit on normal
exit and rollback on every escaping exception. Foreign keys are enabled on each
connection. Per-operation savepoints protect mutations plus automatic audit even
if callers catch an error inside a larger transaction. SQLite serializes writers;
keep blocks short and do not perform network operations inside them. Busy timeouts
(surface as sqlite3 operational errors) occur after 5 seconds; automatic retries
and high-contention concurrency are not implemented or claimed tested.

`DuplicateJob` is a specialized `IntegrityViolation`. Other uniqueness/ownership
failures use `IntegrityViolation`; loading nonexistent references uses `NotFound`.
Invalid domain inputs use Pydantic `ValidationError` or pure-rule `ValueError`.
SQL values are parameterized; only allowlisted internal identifiers enter SQL text.
No database error payload, document content or candidate record is logged.

Schema version 1 is tracked by `PRAGMA user_version`. Initialization is idempotent
and atomic, rejects unknown versions, and refuses an existing unversioned database
with user tables without deleting anything. There was no database in Phase 1, so
there is no data migration from that phase. No dependency or lockfile changes were
needed. Python 3.12.x remains supported; SQLite 3.53.4 was tested locally.

The repository is intentionally insert/read-oriented with explicit lifecycle and
document-selection updates. It does not provide arbitrary entity replacement,
profile editing/deletion, credential storage or production access controls.
Audit rows and document versions have no update/delete operation. They are not a
tamper-proof compliance ledger against direct file access. SQLite files are plaintext
local development artifacts: use synthetic data only and keep them out of Git.

Before PostgreSQL or model/schema changes, add explicit versioned migrations for
both relational columns and JSON payloads, backups and roundtrip/upgrade tests.
A PostgreSQL adapter must implement the same protocol/invariants and adapt JSONB,
UUID/time types, locking, SQL dialect, identity indexes, savepoints and audit ordering.
This adapter has not been implemented or tested. Nested aggregates may need
normalization for search/reporting; no speculative ORM or migration engine is added.
The one-application-per-candidate/job constraint, terminal states, one selected
version, evidence granularity and future edit/snapshot policy need review before
reapplication, multiple attachments or profile updates are introduced.

## Implementation references

- [Python 3.12 sqlite3: transaction control and parameterized statements](https://docs.python.org/3.12/library/sqlite3.html)
- [SQLite foreign keys and composite parent keys](https://www.sqlite.org/foreignkeys.html)
- [Pydantic validation patterns](https://pydantic.dev/docs/validation/latest/concepts/validators/)

Verified against the local Python 3.12.4 / SQLite 3.53.4 / Pydantic 2.14.1 runtime.
At completion of Phase 2, LangChain/LangGraph were uninstalled. Phase 3 now adds
the locked deterministic graph documented in [job-analysis-workflow.md](job-analysis-workflow.md),
without changing schema v1 or the existing persisted entity formats.

## Phase 6 additive model and migration

The current schema is v2. Historical v1 behavior above describes Phase 2.
`PreparationRecord` snapshots candidate/job/assessment IDs, rules and normalized
criteria, evidence-linked facts/explanations/questions/practice, planner/model and
renderer/prompt versions, original resume version and two DraftMetadata records.
Each metadata record retains document/version/predecessor IDs, cited fact IDs,
SHA-256 content hash and mandatory review flag. Bodies remain immutable versions.
The new `preparations` table enforces assessment/candidate/job linkage and
version/candidate ownership with composite foreign keys. The adapter additionally
checks document kinds, predecessors and hashes.

`SQLiteDatabase.initialize()` upgrades v1 atomically and idempotently. Existing
profiles, assessments and CV payloads are not rewritten. Failure rolls back the
schema/index/version change. Backup non-disposable data first; no downgrade is
provided and old code rejects v2. Read [the preparation guide](career-preparation.md)
for provenance, conflict detection, authorization and transaction limits.

## Phase 7: independent confirmed memory

Business schema remains v2. A separate memory database (application-tagged schema
v1) holds owner-scoped typed memory records, monotonically increasing versions/owner
revisions and thread ownership/tombstones. Job/location/work-arrangement preferences,
confirmed career facts, career goals and explicit corrections have separate schemas.
Corrections atomically retire a referenced fact/version, without copying old text
into execution history. No business entity or JSON aggregate is migrated or changed.
A third file is owned by LangGraph's SQLite saver and contains reference-only
checkpoints, not authoritative career data. See
[memory/context storage and deletion policy](memory-and-checkpointing.md).

## Phase 8: staged actions and business schema v3

Business schema is now v3. Added action_proposals, action_states, action_decisions,
action_attempts, action_outcomes and action_audits. Proposal content includes the
current application fingerprint, exact message/status-change or resume-version
hash, configured target snapshot, owner, thread, version and expiry. Immutable user
decisions bind all identity/version/hash fields with a composite foreign key. One
decision/attempt/outcome per proposal and one successor per revision are enforced.
SQL triggers prevent UPDATE/DELETE of immutable action records. State remains a
separate mutable lifecycle projection. No legacy approval/document payload changes.

Explicit initialize() upgrades v1/v2 atomically, retaining prior records; old code
rejects v3 and downgrade is unsupported. The prior migration test retains its
preservation/rollback checks and now expects current schema v3. New tests exercise
v2 upgrade and rollback separately. Memory/checkpoint schemas remain unchanged.
Details and retention/security limits are in [action-approval.md](action-approval.md).
