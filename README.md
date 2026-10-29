# CareerPilot AI

> Evidence before action. A local, explainable career workspace with a chat-first UI.

![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)
![LangGraph](https://img.shields.io/badge/Workflow-LangGraph-1f2937)
![Tests](https://img.shields.io/badge/tests-599%20passed-brightgreen)
![Mode](https://img.shields.io/badge/demo-offline%20%2F%20synthetic-blue)

## What it does

| Area | Capability |
|---|---|
| Chat | Bounded English/Arabic commands with persistent conversations |
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
