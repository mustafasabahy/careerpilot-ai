"""Historical schemas and additive preparation/action migrations."""

SCHEMA_VERSION = 3
SCHEMA = """
CREATE TABLE candidates (
    id TEXT PRIMARY KEY NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE jobs (
    id TEXT PRIMARY KEY NOT NULL,
    source_name TEXT NOT NULL,
    external_id TEXT,
    canonical_url TEXT UNIQUE,
    payload TEXT NOT NULL,
    UNIQUE(source_name, external_id)
);
CREATE TABLE assessments (
    id TEXT PRIMARY KEY NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    payload TEXT NOT NULL
);
CREATE TABLE documents (
    id TEXT PRIMARY KEY NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    payload TEXT NOT NULL,
    UNIQUE(id, candidate_id)
);
CREATE TABLE versions (
    id TEXT PRIMARY KEY NOT NULL,
    document_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    number INTEGER NOT NULL CHECK(number >= 1),
    previous_version_id TEXT,
    payload TEXT NOT NULL,
    UNIQUE(document_id, number),
    UNIQUE(id, document_id),
    UNIQUE(id, candidate_id),
    FOREIGN KEY(document_id, candidate_id) REFERENCES documents(id, candidate_id),
    FOREIGN KEY(previous_version_id, document_id) REFERENCES versions(id, document_id),
    CHECK((number = 1 AND previous_version_id IS NULL)
       OR (number > 1 AND previous_version_id IS NOT NULL))
);
CREATE TABLE applications (
    id TEXT PRIMARY KEY NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    document_version_id TEXT,
    status TEXT NOT NULL CHECK(status IN
        ('draft','ready','submitted','interviewing','offered','accepted','rejected','withdrawn')),
    payload TEXT NOT NULL,
    UNIQUE(candidate_id, job_id),
    UNIQUE(id, candidate_id),
    FOREIGN KEY(document_version_id, candidate_id) REFERENCES versions(id, candidate_id),
    CHECK(status IN ('draft','withdrawn') OR document_version_id IS NOT NULL)
);
CREATE TABLE approvals (
    id TEXT PRIMARY KEY NOT NULL,
    application_id TEXT NOT NULL,
    document_version_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    FOREIGN KEY(application_id, candidate_id) REFERENCES applications(id, candidate_id),
    FOREIGN KEY(document_version_id, candidate_id) REFERENCES versions(id, candidate_id)
);
CREATE TABLE audit_events (
    id TEXT PRIMARY KEY NOT NULL,
    application_id TEXT NOT NULL REFERENCES applications(id),
    payload TEXT NOT NULL
);
CREATE INDEX assessments_candidate ON assessments(candidate_id);
CREATE INDEX assessments_job ON assessments(job_id);
CREATE INDEX documents_candidate ON documents(candidate_id);
CREATE INDEX versions_candidate ON versions(candidate_id);
CREATE INDEX applications_job ON applications(job_id);
CREATE INDEX applications_version ON applications(document_version_id, candidate_id);
CREATE INDEX approvals_application ON approvals(application_id, document_version_id);
CREATE INDEX audit_application ON audit_events(application_id);
"""


MIGRATION_1_TO_2 = """
CREATE UNIQUE INDEX assessments_identity ON assessments(id, candidate_id, job_id);
CREATE TABLE preparations (
    id TEXT PRIMARY KEY NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    job_id TEXT NOT NULL REFERENCES jobs(id),
    assessment_id TEXT NOT NULL,
    resume_version_id TEXT NOT NULL,
    cover_version_id TEXT NOT NULL,
    original_resume_version_id TEXT,
    payload TEXT NOT NULL,
    FOREIGN KEY(assessment_id, candidate_id, job_id)
        REFERENCES assessments(id, candidate_id, job_id),
    FOREIGN KEY(resume_version_id, candidate_id) REFERENCES versions(id, candidate_id),
    FOREIGN KEY(cover_version_id, candidate_id) REFERENCES versions(id, candidate_id),
    FOREIGN KEY(original_resume_version_id, candidate_id) REFERENCES versions(id, candidate_id),
    CHECK(resume_version_id <> cover_version_id)
);
CREATE INDEX preparations_candidate_job ON preparations(candidate_id, job_id);
CREATE INDEX preparations_assessment ON preparations(assessment_id, candidate_id, job_id);
CREATE INDEX preparations_resume ON preparations(resume_version_id, candidate_id);
CREATE INDEX preparations_cover ON preparations(cover_version_id, candidate_id);
"""


MIGRATION_2_TO_3 = """
CREATE TABLE action_proposals (
    id TEXT PRIMARY KEY NOT NULL,
    thread_id TEXT NOT NULL UNIQUE,
    owner_id TEXT NOT NULL,
    application_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    previous_proposal_id TEXT UNIQUE REFERENCES action_proposals(id),
    content_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    FOREIGN KEY(application_id, candidate_id) REFERENCES applications(id, candidate_id),
    UNIQUE(id, thread_id, owner_id, version, content_hash)
);
CREATE TABLE action_states (
    proposal_id TEXT PRIMARY KEY REFERENCES action_proposals(id),
    status TEXT NOT NULL CHECK(status IN
      ('pending','approved','rejected','revision_requested','executing','simulated',
       'applied','failed','unknown','invalidated'))
);
CREATE TABLE action_decisions (
    id TEXT PRIMARY KEY NOT NULL,
    proposal_id TEXT NOT NULL UNIQUE,
    thread_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    FOREIGN KEY(proposal_id, thread_id, owner_id, version, content_hash)
      REFERENCES action_proposals(id, thread_id, owner_id, version, content_hash)
);
CREATE TABLE action_attempts (
    proposal_id TEXT PRIMARY KEY REFERENCES action_proposals(id),
    idempotency_key TEXT NOT NULL UNIQUE
);
CREATE TABLE action_outcomes (
    id TEXT PRIMARY KEY NOT NULL,
    proposal_id TEXT NOT NULL UNIQUE REFERENCES action_proposals(id),
    payload TEXT NOT NULL
);
CREATE TABLE action_audits (
    id TEXT PRIMARY KEY NOT NULL,
    proposal_id TEXT NOT NULL REFERENCES action_proposals(id),
    payload TEXT NOT NULL
);
CREATE INDEX action_proposals_owner ON action_proposals(owner_id, thread_id);
CREATE INDEX action_audits_proposal ON action_audits(proposal_id);
"""

# Trigger bodies contain semicolons, so initialize executes each complete statement.
ACTION_IMMUTABILITY_TRIGGERS = tuple(
    f"CREATE TRIGGER {table}_no_{operation.lower()} BEFORE {operation} ON {table} "
    "BEGIN SELECT RAISE(ABORT, 'Immutable action record'); END"
    for table in (
        "action_proposals",
        "action_decisions",
        "action_attempts",
        "action_outcomes",
        "action_audits",
    )
    for operation in ("UPDATE", "DELETE")
)
