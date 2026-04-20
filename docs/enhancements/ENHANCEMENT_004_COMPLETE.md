---
name: Enhancement 004 Complete
description: Docker build on python:3.13-alpine — liveness probe and SIGTERM verified
type: project
---

# Enhancement 004 — Docker Build & K8s Deployment Validation

**Status**: Complete | **Date**: 2026-04-12 | **Actual hours**: 1

## Verification Results

| Check | Result |
|-------|--------|
| `docker build` | Pass — python:3.13-alpine, multi-stage |
| User in container | `uid=1001(mcp) gid=1001(mcp)` |
| `curl localhost:8080/health` | `{"status": "ok"}` |
| SIGTERM handling | `[health] Received signal 15. Initiating graceful shutdown.` |

## Bugs Fixed

### Critical: liveness server killed between event loops

**Symptom**: `curl localhost:8080/health` — connection refused.

**Root cause**: `main()` was:
```python
asyncio.run(startup())   # loop 1 — starts liveness task, then closes loop → task dies
mcp.run()                # loop 2 — new loop, liveness server gone
```

**Fix**: merge into a single event loop using `mcp.run_stdio_async()`:
```python
def main() -> None:
    async def _main() -> None:
        await startup()
        await mcp.run_stdio_async()   # same loop — liveness task survives
    asyncio.run(_main())
```

### Env var naming: `SECRET_APPDYNAMICS_*` vs `APPDYNAMICS_*`

**Symptom**: MockVaultClient docstring, `.env.example`, and README all said `SECRET_APPDYNAMICS_PRODUCTION_CLIENT_ID`. Actual transformation strips `secret/` prefix → `APPDYNAMICS_PRODUCTION_CLIENT_ID`.

**Fix**: corrected docstring in `auth/vault_client.py`, `.env.example`, and README.

## Docker Notes

- `-i` flag required when running the container — MCP uses stdin/stdout; the process exits immediately without it
- Empty `{"controllers":[]}` in `controllers.json` allows startup without real AppD credentials (license detection fails non-fatally)
- `apk upgrade --no-cache` added to both builder and production stages to apply Alpine security patches at build time
- Python version upgraded from 3.11 → 3.13 (EOL Oct 2029, better performance, all deps compatible)
