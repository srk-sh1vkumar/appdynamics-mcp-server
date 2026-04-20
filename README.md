# AppDynamics MCP Server

A production-grade **Model Context Protocol (MCP)** server for AppDynamics APM, enabling fully autonomous end-to-end incident investigation — from alert detection to root-cause analysis — without requiring human intervention at each step.

---

## Features

- **29 MCP tools** covering applications, business transactions, snapshots, metrics, health rules, EUM, database visibility, analytics, infrastructure, and golden baseline management
- **Multi-controller** support — investigate across several AppDynamics environments in a single session
- **Autonomous investigation** — 16-step investigation sequence baked into the system prompt
- **Read-only** — no write operations to AppDynamics; safe to connect to production
- **Per-user permission enforcement** — AppDynamics RBAC is the sole authority (fail-closed)
- **Graceful license degradation** — unlicensed modules return helpful messages, not cryptic 404s
- **Production caching layer** — per-data-type TTLCache (L1) + diskcache (L2); `TwoLayerCache` with Pydantic validation and structural eviction of corrupt entries
- **Event-driven cache invalidation** — deployment detection (BT count shift), app restart detection (APP_CRASH/NODE_RESTART violations), and manual golden override
- **Golden baseline registry** — per-BT golden snapshot with 24h TTL, shared across users, crash-safe disk persistence
- **Persistent registries** — `AppsRegistry` and `BTRegistry` survive MCP restarts for mid-incident continuity
- **Token-bucket rate limiter** — global (10 req/s) and per-user (5 req/s)
- **PII redaction** — email addresses, JWTs, Bearer tokens, card numbers stripped before tool output
- **Audit log** — structured JSON on stderr for every tool invocation
- **K8s liveness probe** — minimal asyncio HTTP server on port 8080

---

## Prerequisites

| Requirement | Version |
|-------------|---------|
| Python | 3.13+ |
| uv | latest (`pip install uv`) |
| AppDynamics controller | 22.x or later |
| HashiCorp Vault | optional (dev mode uses env vars) |

---

## Quick Start

```bash
# 1. Clone / copy the project
cd appdynamics-mcp-server

# 2. Copy environment template
cp .env.example .env
# Edit .env with your credentials

# 3. Edit controllers.json (see below)

# 4. Install dependencies
uv sync

# 5. Run
uv run python -m main
```

---

## Environment Variables

All sensitive configuration is provided via environment variables. **Never hard-code credentials.**

### Vault Mode

| Variable | Required | Description |
|----------|----------|-------------|
| `VAULT_MODE` | No | `mock` (default, reads env vars) or `hashicorp` |
| `VAULT_ADDR` | hashicorp only | Vault server address, e.g. `https://vault.example.com` |
| `VAULT_TOKEN` | hashicorp only | Vault access token |
| `VAULT_NAMESPACE` | No | Vault namespace (Enterprise) |

### Credentials (mock / dev mode)

For each controller, env var names are derived from the `vaultPath` in `controllers.json` by stripping the `secret/` prefix, replacing `/` with `_`, and uppercasing.

**Example** — `vaultPath: "secret/appdynamics/production"`:

| Variable | Description |
|----------|-------------|
| `APPDYNAMICS_PRODUCTION_CLIENT_ID` | OAuth2 client ID |
| `APPDYNAMICS_PRODUCTION_CLIENT_SECRET` | OAuth2 client secret |

### Liveness Probe

| Variable | Default | Description |
|----------|---------|-------------|
| `HEALTH_PORT` | `8080` | Port for K8s liveness HTTP server |
| `HEALTH_HOST` | `0.0.0.0` | Bind address |

### Cache

| Variable | Default | Description |
|----------|---------|-------------|
| `DISKCACHE_DIR` | `data/diskcache` | Persistent cache directory |
| `DISKCACHE_SIZE_LIMIT_GB` | `2` | Max disk cache size in GB |

---

## controllers.json

Place this file in the project root. All keys are **camelCase**.

```json
{
  "controllers": [
    {
      "name": "production",
      "url": "https://mycompany.saas.appdynamics.com",
      "account": "mycompany",
      "globalAccount": "mycompany_abc123xyz",
      "timezone": "America/New_York",
      "appPackagePrefix": "com.mycompany",
      "analyticsUrl": "https://analytics.api.appdynamics.com",
      "vaultPath": "secret/appdynamics/production"
    },
    {
      "name": "staging",
      "url": "https://mycompany-staging.saas.appdynamics.com",
      "account": "mycompany-staging",
      "globalAccount": "mycompany-staging_abc123xyz",
      "timezone": "UTC",
      "appPackagePrefix": "com.mycompany",
      "analyticsUrl": "https://analytics.api.appdynamics.com",
      "vaultPath": "secret/appdynamics/staging"
    }
  ]
}
```

### Field Reference

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Unique controller identifier used as the `controller` parameter in tool calls |
| `url` | Yes | Base URL of the AppDynamics controller (no trailing slash) |
| `account` | Yes | AppDynamics account name |
| `globalAccount` | Yes | Global account name (used for OAuth2 token endpoint) |
| `timezone` | No | IANA timezone for display formatting (default: `UTC`) |
| `appPackagePrefix` | No | Java package prefix used to identify application frames in stack traces |
| `analyticsUrl` | No | AppDynamics Analytics / Events Service URL (required for `query_analytics_logs`) |
| `vaultPath` | Yes | Vault path where `client_id` and `client_secret` are stored |

---

## MCP Client Configuration

### Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "appdynamics": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/absolute/path/to/appdynamics-mcp-server",
        "python",
        "-m",
        "main"
      ],
      "env": {
        "APPDYNAMICS_PRODUCTION_CLIENT_ID": "your_client_id",
        "APPDYNAMICS_PRODUCTION_CLIENT_SECRET": "your_client_secret"
      }
    }
  }
}
```

### Cursor

Add to `.cursor/mcp.json` (project-level) or `~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "appdynamics": {
      "command": "uv",
      "args": [
        "run",
        "--project",
        "/absolute/path/to/appdynamics-mcp-server",
        "python",
        "-m",
        "main"
      ],
      "env": {
        "APPDYNAMICS_PRODUCTION_CLIENT_ID": "your_client_id",
        "APPDYNAMICS_PRODUCTION_CLIENT_SECRET": "your_client_secret"
      }
    }
  }
}
```

---

## HashiCorp Vault Configuration

In production, credentials are fetched from Vault using the KV secrets engine.

### KV v2 path layout (recommended)

```
secret/
└── appdynamics/
    ├── production/
    │   ├── client_id      → <OAuth2 client ID>
    │   └── client_secret  → <OAuth2 client secret>
    └── staging/
        ├── client_id
        └── client_secret
```

### KV v1 path layout

```
secret/appdynamics/production   →  { "client_id": "...", "client_secret": "..." }
```

Set `VAULT_MODE=hashicorp` and provide `VAULT_ADDR` + `VAULT_TOKEN` in the environment.

---

## License Detection & Graceful Degradation

At startup, the server probes the AppDynamics controller to detect which modules are licensed. Unlicensed tools return a clear, human-readable message instead of a cryptic API error.

| Module | Tools Gated | Degradation Mode |
|--------|-------------|------------------|
| APM Pro | `list_snapshots`, `analyze_snapshot`, `compare_snapshots`, `archive_snapshot` | `NO_SNAPSHOTS` |
| Analytics | `query_analytics_logs` | `NO_ANALYTICS` |
| End User Monitoring | `get_eum_overview`, `get_eum_page_performance`, `get_eum_js_errors`, `get_eum_ajax_requests`, `get_eum_geo_performance`, `correlate_eum_to_bt` | `NO_EUM` |
| Database Visibility | `get_database_performance` | — |
| Full | all tools active | `FULL` |

Disabled tools are reported in `get_server_health` under `disabled_tools`.

---

## Kubernetes Liveness Probe

The server starts a minimal asyncio HTTP server on port 8080 that responds to any request with:

```json
{"status": "ok"}
```

Configure in your K8s deployment:

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 30
  failureThreshold: 3
```

---

## Adding a New Tool

1. **Add the AppD API call** to [client/appd_client.py](client/appd_client.py) as an async method.
2. **Add input/output types** to [models/types.py](models/types.py) — Pydantic for API boundaries, `@dataclass` for internal domain objects.
3. **Register the tool** in [main.py](main.py) using the `@mcp.tool()` decorator. Follow the existing pattern:
   - Call `await check_and_wait(upn)` for rate limiting
   - Resolve the controller to a client via `get_client(controller_name)`
   - Fetch data, sanitize via `sanitize_and_wrap()`
   - Truncate to budget via `truncate_to_budget()`
   - Emit `audit_log()`
4. **If the tool requires a license**, call `license_check.require_license("module_name")` near the top of the tool function and add the tool name to `_MODULE_TOOLS` in [services/license_check.py](services/license_check.py).
5. **Write tests** in [tests/unit/test_tools.py](tests/unit/test_tools.py) covering happy path, 401/403/404/429/500 error cases, and license-disabled behaviour.
6. Update the tool count in this README.

---

## Docker

```bash
# Build
docker build -t appd-mcp-server .

# Run with mock credentials (no real controller required)
echo '{"controllers":[]}' > controllers.json
docker run -i \
  -p 8080:8080 \
  -e VAULT_MODE=mock \
  -v $(pwd)/controllers.json:/app/controllers.json:ro \
  appd-mcp-server

# Run with real credentials
docker run -i \
  -p 8080:8080 \
  -e VAULT_MODE=mock \
  -e APPDYNAMICS_PRODUCTION_CLIENT_ID=your_client_id \
  -e APPDYNAMICS_PRODUCTION_CLIENT_SECRET=your_client_secret \
  -v $(pwd)/controllers.json:/app/controllers.json:ro \
  appd-mcp-server
```

The `-i` flag is required — MCP communicates over stdin/stdout and the container exits immediately without it.

The liveness probe is available at `http://localhost:8080/health` once the container starts.

---

## Running Tests

```bash
# All unit tests
uv run pytest tests/unit/ -v

# With coverage report (HTML output in htmlcov/)
uv run pytest tests/unit/ --cov=. --cov-report=html

# Specific test file
uv run pytest tests/unit/test_snapshot_parser.py -v
```

**Current status**: 217 tests, 0 failures. Coverage: parsers 95–99%, sanitizer 98%, bt_classifier 100%, cache/registry layer fully covered by 47 dedicated unit tests.

---

## Project Structure

```
appdynamics-mcp-server/
├── main.py                        # FastMCP server, all 29 tools, startup sequence
├── pyproject.toml                 # Project config, dependencies (Python 3.13+)
├── controllers.json               # Controller definitions (camelCase keys)
├── .env.example                   # Environment variable template
├── Dockerfile                     # Multi-stage alpine build
│
├── models/
│   └── types.py                   # Enums, dataclasses, Pydantic models
│
├── utils/
│   ├── cache.py                   # TwoLayerCache + CachedSnapshotAnalysis + module-level API
│   ├── cache_keys.py              # Centralised UPN-namespaced key builder
│   ├── rate_limiter.py            # Token bucket (global + per-user)
│   ├── sanitizer.py               # PII redaction + prompt injection protection
│   └── timezone.py                # UTC normalization + display formatting
│
├── registries/
│   ├── apps_registry.py           # AppEntry + AppsRegistry (TTLCache L1 + diskcache L2)
│   ├── bt_registry.py             # BTEntry + BTRegistry (TTLCache L1 + diskcache L2)
│   └── golden_registry.py         # GoldenSnapshot + GoldenRegistry (24h TTL, shared)
│
├── auth/
│   ├── vault_client.py            # Mock + HashiCorp Vault credential fetching
│   └── appd_auth.py               # TokenManager, session cache, permission gating
│
├── client/
│   └── appd_client.py             # httpx async client, retry, all AppD API methods
│
├── parsers/
│   ├── stack/
│   │   ├── java.py
│   │   ├── nodejs.py
│   │   ├── python_parser.py
│   │   └── dotnet.py
│   └── snapshot_parser.py         # Language detection, smoking gun, baseline scoring
│
├── services/
│   ├── bt_classifier.py           # Criticality scoring, health-check detection
│   ├── cache_invalidator.py       # Event-driven invalidation (deployment/restart/manual)
│   ├── license_check.py           # Module license detection + tool gating
│   ├── runbook_generator.py       # JSON runbook generation + recurring detection
│   └── health.py                  # Health aggregation + K8s liveness probe
│
├── tests/
│   ├── conftest.py                # Shared fixtures
│   ├── mocks/
│   │   └── appd_server.py         # httpx MockTransport for unit tests
│   ├── unit/
│   │   ├── test_cache.py          # 47 tests: TwoLayerCache, registries, invalidator
│   │   ├── test_snapshot_parser.py
│   │   ├── test_bt_classifier.py
│   │   ├── test_sanitizer.py
│   │   └── test_tools.py
│   ├── integration/
│   │   └── test_full_flow.py      # End-to-end flow tests (discovery → analysis)
│   └── contract/
│       └── test_appd_response_shapes.py  # AppD API response shape contracts
│
├── runbooks/                      # Auto-generated JSON runbooks (git-ignored)
└── data/
    ├── diskcache/                 # Module-level persistent cache (git-ignored)
    ├── two_layer_cache/           # TwoLayerCache disk layer (git-ignored)
    └── registry/
        ├── apps/                  # AppsRegistry diskcache (git-ignored)
        ├── bts/                   # BTRegistry diskcache (git-ignored)
        └── golden/                # GoldenRegistry diskcache (git-ignored)
```

---

## Security Notes

- **Read-only**: The server issues only GET requests (plus one POST for Analytics). It never modifies AppDynamics configuration.
- **Fail-closed permissions**: If the AppDynamics RBAC check fails for any reason, the tool call is rejected.
- **PII redaction**: All tool output is scanned for email addresses, JWT tokens, Bearer credentials, and 16-digit card numbers before being returned to the LLM.
- **Prompt injection protection**: All AppDynamics data is wrapped in `<appd_data>` XML tags so the LLM can distinguish controller data from instructions.
- **No shell execution**: The server never spawns subprocesses or executes shell commands.
- **UPN-namespaced cache**: Each user's cached data is keyed by their UPN — no cross-user data leakage.
