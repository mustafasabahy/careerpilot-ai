# CareerPilot AI

> Evidence before action. A local, explainable career workspace with a chat-first UI.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)
![LangGraph](https://img.shields.io/badge/Workflow-LangGraph-1f2937)
![Tests](https://img.shields.io/badge/tests-636%20passed-brightgreen)
![Mode](https://img.shields.io/badge/demo-offline%20%2F%20synthetic-blue)

## What it does

| Area | Capability |
|---|---|
| Chat | Bounded English commands with persistent conversations |
| Jobs | Local/synthetic discovery, normalization, and duplicate detection |
| Analysis | Evidence-based matches, gaps, unknowns, and hard/soft requirements |
| Documents | CV, cover-letter, and interview-practice drafts linked to evidence |
| Safety | Owner checks, immutable approval proposals, simulated external actions |
| Reliability | SQLite persistence, checkpoints, recovery, idempotency, audit events |
| AI | Optional provider adapter; deterministic demo works without an API key |

## System map

```mermaid
flowchart LR
  User([User]) --> UI[Chat UI]
  UI --> API[FastAPI]
  API --> Auth[Validate token + ownership]
  Auth --> Service[Conversation / application service]
  Service --> Graph[LangGraph]
  Graph --> Load[Load permitted profile + job]
  Load --> Rules[Pure evidence-based rules]
  Rules --> Result[Typed result]
  Rules --> Approval{Sensitive action?}
  Approval -->|No| Result
  Approval -->|Yes| Interrupt[Persist proposal + interrupt]
  Interrupt --> Human[Human approve / reject / revise]
  Human --> Action[Simulated or configured adapter]
  Action --> Result
  Result --> DB[(SQLite)]
  DB --> UI
```

### Where LangGraph fits

```mermaid
flowchart TD
  S((Start)) --> V[Validate request]
  V --> L[Load stable IDs]
  L --> N[Normalize requirements]
  N --> A[Run tested business calculation]
  A --> Q{Valid and enough data?}
  Q -->|Missing / invalid| M[Clarification or typed error]
  Q -->|Ready| R[Return assessment]
  R --> I{Approval needed?}
  I -->|No| E((End))
  I -->|Yes| P[interrupt: wait for explicit approval]
  P --> D{Decision}
  D -->|Approve| X[Execute once with idempotency]
  D -->|Reject| J[Persist rejection]
  D -->|Revise| N
  X --> E
  J --> E
  M --> E
```

LangGraph coordinates state, nodes, conditional routing, checkpoints, and interrupts.
Business rules remain ordinary Python outside the graph, so they are independently
testable. LangChain is limited to provider/tool boundaries; it does not own
authorization or approval decisions.

### Exactly how LangGraph is used

`src/careerpilot/workflows/job_analysis.py` builds the deterministic analysis
`StateGraph`:

```mermaid
flowchart TD
  Start((START)) --> Validate[validate_request]
  Validate --> LoadJob[load_job]
  LoadJob --> LoadProfile[load_profile]
  LoadProfile --> Normalize[normalize_requirements]
  Normalize --> Calculate[calculate_baseline]
  Calculate --> Store[validate_assessment]
  Store -->|complete| Return[return_result]
  Store -->|missing evidence| Missing[missing_information]
  Validate -. invalid .-> Fail[fail]
  LoadJob -. not found .-> Fail
  LoadProfile -. not found .-> Fail
  Return --> End((END))
  Missing --> End
  Fail --> End
```

Each node is small and typed. LangGraph coordinates the state and routes; the
actual fit calculation stays in `AnalysisService` and the domain baseline rules.
That separation lets the business behavior run in unit tests without a model.

The chat agent in `src/careerpilot/workflows/chat_agent.py` uses one checkpointed
thread per operation:

```text
model  ->  tools  ->  final
  ^         |
  +---------+  (resume from the saved stage)
```

The checkpoint stores coordination references only. The authoritative conversation
journal remains in SQLite. Owner checks, stable thread IDs, and recovery logic stop
one user's checkpoint from being reused by another user.

### Exactly how LangChain is used

`src/careerpilot/tools/langchain.py` exposes six bounded `StructuredTool` adapters:

| Tool | Purpose |
|---|---|
| `search_synthetic_jobs` | Search bundled synthetic postings |
| `search_local_jobs` | Search operator-registered local imports |
| `get_job` | Retrieve one authorized job |
| `get_candidate_facts` | Read profile facts without changing them |
| `get_company_info` | Read mock company metadata |
| `normalize_job_results` | Normalize and deduplicate authorized references |

Every tool validates Pydantic input and calls an already-authorized application
service. Tool arguments cannot contain file paths, SQL, shell commands, or arbitrary
URLs. Retrieved job text is untrusted content, never instructions or permissions.

`src/careerpilot/providers/factory.py` is the only place that constructs the optional
LangChain `ChatOpenAI` client. It validates provider settings, requires an explicit
application API key, sets timeouts and output limits, disables hidden retries, and
uses Pydantic structured-output schemas. The mock/fixture provider remains the
default for offline development; a ChatGPT/Codex subscription credential is not
used as an application API key.

### What stays outside both libraries

```mermaid
flowchart LR
  LG[LangGraph coordinates] --> App[Application services]
  LC[LangChain adapts models/tools] --> App
  App --> Domain[Pure domain rules]
  App --> Auth[Authorization + ownership]
  App --> DB[Persistence + audit]
```

- Business fit rules and evidence interpretation are plain Python/domain code.
- Authorization is enforced by the API and application layer.
- Approval is an authenticated user event, not an LLM statement.
- Side effects run only through explicit adapters with idempotency and audit records.

## Quick start

```sh
uv sync --locked
uv run --locked careerpilot-demo
uv run --locked careerpilot-server --directory work/demo-server --port 8765
```

Open <http://127.0.0.1:8765/> and use the development access token generated for the
local server. Choose **Load synthetic examples**, then try:

```text
Show my jobs
Analyze job
What information is missing?
Prepare my application
Show my pending approvals
```

All demo records are synthetic. Sending messages, submitting applications, and job
search outside local fixtures are simulated or disabled.

## Project shape

```text
src/careerpilot/
├── domain/          validated entities and business rules
├── application/     use cases, chat, security, and contracts
├── workflows/       LangGraph orchestration
├── providers/       optional model adapters
├── integrations/    local fixtures and simulated actions
├── persistence/     SQLite repositories and checkpoints
├── api/             FastAPI transport
└── web/             static chat-first interface
```

## Verify locally

```sh
PATH="$PWD/work/tooling-env/bin:$PATH" sh scripts/check.sh
```

The normal suite is offline. Live provider tests are opt-in and skipped unless explicitly
configured. See [docs/known-limitations.md](docs/known-limitations.md),
[docs/security.md](docs/security.md), and [docs/demo-script.md](docs/demo-script.md).

## Important boundaries

- No hiring probability is inferred from the baseline assessment.
- AI output cannot silently change confirmed profile facts.
- Approval is an authenticated application event, not a model statement.
- External effects are simulated unless a separately configured adapter is enabled.
- This is a local technical preview, not a production deployment.
