#!/bin/sh
set -eu
uv lock --check
uv run --locked ruff format --check .
uv run --locked ruff check .
uv run --locked mypy
uv run --locked pytest
node --check src/careerpilot/web/app.js
node --check src/careerpilot/web/api-docs.js
node --check src/careerpilot/web/chat.js
node --check src/careerpilot/web/chat-logic.mjs
node --test tests/ui/*.test.mjs
uv run --locked careerpilot-evaluate --output work/eval-current.json --baseline evals/baseline-v1.json
