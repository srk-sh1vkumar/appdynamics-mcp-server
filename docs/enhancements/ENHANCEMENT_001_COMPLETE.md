---
name: Enhancement 001 Complete
description: Initial full 35-file production build of the AppDynamics MCP server
type: project
---

# Enhancement 001 — Initial Project Build

**Status**: Complete | **Date**: 2026-04-12 | **Actual hours**: 10

## What Was Built

Full production build of the AppDynamics MCP server in Python 3.13.

### Deliverables

| File / Module | Purpose |
|---------------|---------|
| `main.py` | FastMCP server with 28 tools, 16-step autonomous investigation system prompt |
| `models/types.py` | Full type system — enums, dataclasses, Pydantic models |
| `utils/cache.py` | Two-layer cache: TTLCache (in-process) + diskcache (persistent) |
| `utils/rate_limiter.py` | Token bucket — global 10 req/s, per-user 5 req/s |
| `utils/sanitizer.py` | PII redaction: email, JWT, Bearer, 16-digit card numbers |
| `utils/timezone.py` | UTC normalisation, human-readable duration formatting |
| `auth/vault_client.py` | MockVaultClient (env vars) + HashiCorpVaultClient |
| `auth/appd_auth.py` | TokenManager, session cache, RBAC permission gating |
| `client/appd_client.py` | httpx async client, retry, 401/403 handling, all AppD API methods |
| `parsers/snapshot_parser.py` | Language detection, smoking gun analysis, golden baseline scoring |
| `parsers/stack/{java,nodejs,python_parser,dotnet}.py` | Language-specific stack trace parsers |
| `services/bt_classifier.py` | Criticality scoring, health-check detection |
| `services/license_check.py` | Module license detection + tool gating with graceful degradation |
| `services/runbook_generator.py` | JSON runbook generation + recurring incident detection |
| `services/health.py` | Health aggregation + K8s liveness probe (asyncio HTTP, port 8080) |
| `tests/conftest.py` | Shared pytest fixtures + static fixture data |
| `tests/mocks/appd_server.py` | httpx MockTransport for unit tests |
| `Dockerfile` | Multi-stage python:3.13-alpine, non-root `mcp` user (uid 1001) |
| `README.md` | Full setup guide |
| `.env.example` | Environment variable template |

## Key Design Decisions

- **stdio transport** — MCP communicates over stdin/stdout; no TCP MCP port exposed
- **UPN-namespaced cache keys** — `{upn}:{controller}:{type}:{id}` prevents cross-user data leakage
- **Fail-closed RBAC** — AppDynamics role check failure rejects the tool call
- **Prompt injection protection** — all AppD data wrapped in `<appd_data>` XML tags
- **Graceful license degradation** — unlicensed tools return human-readable messages, not 404s
- **Two-level vault** — MockVaultClient for dev (env vars), HashiCorpVaultClient for prod

## Verification

- All 17 modules import cleanly: `uv run python -c "import main"`
- All 28 tool functions present in `main.py`
