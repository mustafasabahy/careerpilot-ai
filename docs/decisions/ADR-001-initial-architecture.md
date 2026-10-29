# ADR-001: Initial modular monolith foundation

- Status: Accepted for Phase 1
- Date: 2026-10-28

## Context

CareerPilot AI needs maintainable career workflows that can be reviewed, tested
without providers, and adapted to different transports and model vendors. The
starting folder is empty apart from scratch/output directories, has no Git metadata,
and supplies Python 3.12.4. There are no existing application conventions to retain.
Product rules, data model, hosting and provider requirements are not yet defined.

## Decision

Use a src-layout Python modular monolith with dependency flow from adapters toward
application use cases and domain rules. Keep workflow orchestration, API transport,
model vendors, external tools and storage outside business logic. Defer unused
modules and libraries rather than creating a superficial directory tree.

Use Python 3.12.x, pyproject.toml, uv 0.13.6 and its generated lockfile. Use Pydantic
and Pydantic Settings for validation/configuration, Ruff, strict mypy and pytest.
The initial executable is only a deterministic local process-health command.
Use standard-library logging with a small allowlisted JSON schema to avoid
serializing personal data. Load settings explicitly at startup.

Do not add LangChain/LangGraph, a web framework, provider SDK, database, agent or
telemetry service before a specific feature requires it. LangGraph nodes will
invoke independently tested use cases. Review official docs for the versions
actually locked when those dependencies are introduced.

Do not invent a repository platform. Provide a portable verification script and
defer platform-specific CI until hosting is known. Do not initialize a remote or
perform deployment as part of this phase.

## Alternatives

Microservices introduce operational and data-consistency costs without independent
scaling/team requirements. A framework-centric domain couples business behavior to
orchestration and makes deterministic tests harder. A full clean-architecture
scaffold with unused interfaces obscures current behavior. Unlocked dependencies
make review and reproduction less reliable.

## Consequences

A small runtime is straightforward to test and change. Domain and adapter modules
will be introduced with real behavior; documentation names boundaries before code
exists. Python support is deliberately narrow until a version matrix can be run.
Allowlisted logs improve privacy but omit detailed diagnostics. Future error codes
or exception diagnostics require deliberate design, not raw exception serialization.
No career feature or service readiness can be inferred from local health success.

## Revisit triggers

Reconsider process boundaries only for measured scaling/isolation or organizational
needs. Add application-owned ports when integrations are actually needed. Discuss
major architecture changes and proposed tradeoffs with the user before implementing.
Failed checks prevent completion of the phase.
