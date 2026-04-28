# AppDynamics MCP Server

Connect your AI assistant (Claude Desktop or Cursor) directly to your AppDynamics
controller for autonomous, end-to-end incident investigation — from alert to root cause.

Two deployment modes controlled by a single env var:

| Mode | Use case | Credentials | RBAC |
|------|----------|-------------|------|
| `single_user` | Personal / local dev | Env vars (client ID + secret) | Caller always has full access |
| `enterprise` (default) | Team / org-wide | HashiCorp Vault | Per-user, per-app AppD RBAC enforcement |

---

## Prerequisites

- Python 3.13+, [uv](https://docs.astral.sh/uv/)
- An AppDynamics controller with an **OAuth2 API Client**
- Claude Desktop or Cursor

---

## Quick Start — Single-User Mode

### 1. Clone and install

```bash
git clone https://github.com/srk-sh1vkumar/appdynamics-mcp-server.git
cd appdynamics-mcp-server
uv sync
```

### 2. Configure your controller

```bash
cp controllers.json.example controllers.json
```

Edit `controllers.json`:

```json
{
  "controllers": [
    {
      "name": "production",
      "url": "https://your-account.saas.appdynamics.com",
      "account": "your-account-name",
      "globalAccount": "your-global-account-name",
      "timezone": "UTC",
      "appPackagePrefix": "com.yourcompany",
      "analyticsUrl": "https://analytics.api.appdynamics.com"
    }
  ]
}
```

### 3. Set credentials

```bash
cp .env.example .env
```

Edit `.env`:

```bash
APPDYNAMICS_MODE=single_user

APPDYNAMICS_PRODUCTION_CLIENT_ID=your-client-name
APPDYNAMICS_PRODUCTION_CLIENT_SECRET=your-client-secret
```

The env var prefix is the controller `name` uppercased:
- `"production"` → `APPDYNAMICS_PRODUCTION_CLIENT_ID`
- `"staging"` → `APPDYNAMICS_STAGING_CLIENT_ID`

Use only the client name (e.g. `appd_mcp`), not `name@account` — the server appends
`@account` automatically from `controllers.json`.

**Create an OAuth2 API Client in AppDynamics:**
> Controller → Settings → Administration → API Clients → + Create
> Grant: Account Owner or equivalent admin role.

### 4. Test startup

```bash
uv run python main.py
```

Expected output:
```
[auth] Token refreshed for controller 'production'. Expires at ...
[main] AppDynamics MCP Server v1.0.0 started. Mode: single_user
```

### 5. Connect Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "appdynamics": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/appdynamics-mcp-server", "python", "main.py"],
      "env": {
        "APPDYNAMICS_MODE": "single_user",
        "APPDYNAMICS_PRODUCTION_CLIENT_ID": "your-client-id",
        "APPDYNAMICS_PRODUCTION_CLIENT_SECRET": "your-client-secret"
      }
    }
  }
}
```

### 6. Connect Cursor

Add to `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "appdynamics": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/appdynamics-mcp-server", "python", "main.py"],
      "env": {
        "MCP_TRANSPORT": "streamable-http",
        "APPDYNAMICS_MODE": "single_user",
        "APPDYNAMICS_PRODUCTION_CLIENT_ID": "your-client-id",
        "APPDYNAMICS_PRODUCTION_CLIENT_SECRET": "your-client-secret"
      }
    }
  }
}
```

---

## Enterprise Mode — Vault + Per-User RBAC

Enterprise mode is the default (`APPDYNAMICS_MODE=enterprise`). It requires:

1. **Two AppDynamics service accounts per controller** — a data account and an RBAC-admin account
2. **HashiCorp Vault** storing both sets of credentials

Add `vaultPath` and `rbacVaultPath` to each controller entry in `controllers.json`:

```json
{
  "controllers": [
    {
      "name": "production",
      "url": "https://your-account.saas.appdynamics.com",
      "account": "your-account-name",
      "globalAccount": "your-global-account-name",
      "timezone": "UTC",
      "appPackagePrefix": "com.yourcompany",
      "analyticsUrl": "https://analytics.api.appdynamics.com",
      "vaultPath": "secret/appdynamics/production",
      "rbacVaultPath": "secret/appdynamics/production/rbac"
    }
  ]
}
```

Set Vault connection env vars:

```bash
VAULT_MODE=hashicorp      # or: mock (uses env vars, same as single_user)
VAULT_URL=https://vault.internal
VAULT_TOKEN=your-vault-token
```

In enterprise mode:
- Credentials are fetched from Vault and refreshed automatically on rotation
- Every tool call resolves the caller's UPN to the set of AppDynamics applications
  they are permitted to access (via RBAC graph traversal: user → roles + groups → applicationPermissions)
- Access to an app not in the UPN's allowed set raises `PermissionError`
- RBAC results are cached per UPN (default 86400s, override with `APPDYNAMICS_RBAC_CACHE_TTL_S`)

---

## Available Tools (36)

### First-pass triage (run before any other tool)

| Tool | Tier | Description |
|------|------|-------------|
| `correlate_incident_window` | TROUBLESHOOT | Parallel fetch of health violations, snapshots, BT summary, exceptions, and change indicators in one call. Returns `triage_summary` + chronological `timeline`. `include_deploys=True` by default. |

### Discovery & navigation

| Tool | Tier | Description |
|------|------|-------------|
| `list_controllers` | VIEW | List configured controllers |
| `list_applications` | VIEW | Apps accessible to the calling UPN |
| `search_metric_tree` | VIEW | Browse metric hierarchy |
| `get_metrics` | VIEW | Time-series metric data |
| `get_tiers_and_nodes` | VIEW | All tiers and nodes for an app |
| `get_agent_status` | VIEW | Agent reporting status per tier/node |

### Business transactions

| Tool | Tier | Description |
|------|------|-------------|
| `get_business_transactions` | VIEW | BTs sorted by error rate, healthchecks filtered |
| `get_bt_baseline` | VIEW | AppD baseline vs current; flags anomalies >2× |
| `get_bt_detection_rules` | VIEW | Auto and custom BT detection rules |
| `load_api_spec` | VIEW | Swagger/OpenAPI → BT path mapping |

### Snapshots

| Tool | Tier | Description |
|------|------|-------------|
| `list_snapshots` | TROUBLESHOOT | Find snapshots with pagination |
| `analyze_snapshot` | TROUBLESHOOT | Language-aware stack trace parse with PII redaction |
| `compare_snapshots` | TROUBLESHOOT | Differential vs golden baseline; Smoking Gun Report |
| `get_exit_calls` | TROUBLESHOOT | Outbound DB/HTTP/MQ calls with per-call latency |
| `set_golden_snapshot` | CONFIGURE_ALERTING | Designate a known-good snapshot as golden baseline |
| `archive_snapshot` | CONFIGURE_ALERTING | Prevent purge |

### Health & policies

| Tool | Tier | Description |
|------|------|-------------|
| `get_health_violations` | VIEW | Active + historical violations |
| `get_policies` | CONFIGURE_ALERTING | Alerting policies; flags policies with no action |

### Infrastructure

| Tool | Tier | Description |
|------|------|-------------|
| `get_infrastructure_stats` | VIEW | CPU, Memory, Disk I/O per tier/node |
| `get_jvm_details` | VIEW | Heap, GC time, thread counts, deadlocked threads |
| `get_network_kpis` | TROUBLESHOOT | Packet loss, RTT, retransmissions between tiers |
| `get_server_health` | VIEW | Server status, cache hit rates, licensed modules |

### Errors & diagnostics

| Tool | Tier | Description |
|------|------|-------------|
| `get_errors_and_exceptions` | TROUBLESHOOT | Active + stale exceptions |
| `get_database_performance` | TROUBLESHOOT | Top 10 slow queries (DB Visibility licence) |
| `stitch_async_trace` | TROUBLESHOOT | Correlation ID join across async service boundaries |
| `query_analytics_logs` | TROUBLESHOOT | ADQL via Events Service |

### Change correlation

| Tool | Tier | Description |
|------|------|-------------|
| `list_application_events` | VIEW | Fetch events + change_indicators for any window. Heuristics: explicit deploy marker, config change, rolling deploy, K8s pod turnover, isolated restart. Use for post-mortem look-back or wider windows. |

### EUM (End User Monitoring)

| Tool | Tier | Description |
|------|------|-------------|
| `get_eum_overview` | VIEW | Page load time, JS error rate, crash rate |
| `get_eum_page_performance` | TROUBLESHOOT | Per-page DNS/TCP/server/DOM/render breakdown |
| `get_eum_js_errors` | TROUBLESHOOT | JS errors with stack traces |
| `get_eum_ajax_requests` | TROUBLESHOOT | Ajax performance correlated to backend BTs |
| `get_eum_geo_performance` | TROUBLESHOOT | Performance by geography |
| `correlate_eum_to_bt` | TROUBLESHOOT | User-perceived impact of a backend BT issue |

### Team health & runbooks

| Tool | Tier | Description |
|------|------|-------------|
| `get_team_health_summary` | VIEW | Fan-out health check across all apps for a team |
| `save_runbook` | TROUBLESHOOT | Persist investigation runbook with recurring-incident detection |
| `correlate_incident_window` | TROUBLESHOOT | (see First-pass triage above) |

### Admin

| Tool | Tier | Description |
|------|------|-------------|
| `get_policies` | CONFIGURE_ALERTING | (see Health & policies above) |
| `archive_snapshot` | CONFIGURE_ALERTING | (see Snapshots above) |
| `set_golden_snapshot` | CONFIGURE_ALERTING | (see Snapshots above) |

---

## Permission Tiers

| Tier | AppDynamics role keywords | Access |
|------|--------------------------|--------|
| VIEW | (any authenticated user) | Read-only: metrics, BTs, health, agent status, events |
| TROUBLESHOOT | sre, devops, troubleshoot, engineer | VIEW + snapshots, errors, deep diagnostics, triage |
| CONFIGURE_ALERTING | admin, administrator, configure_alerting | TROUBLESHOOT + policies, archiving, golden baseline |

---

## Multiple Controllers

Add entries to `controllers.json` and matching env vars (single-user) or Vault paths (enterprise):

```bash
# single_user mode
APPDYNAMICS_PRODUCTION_CLIENT_ID=prod-client-id
APPDYNAMICS_PRODUCTION_CLIENT_SECRET=prod-client-secret
APPDYNAMICS_STAGING_CLIENT_ID=staging-client-id
APPDYNAMICS_STAGING_CLIENT_SECRET=staging-client-secret
```

All tools accept an optional `controller_name` parameter (default: `"production"`).

---

## Running Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

406 tests, 0 failures. Use the project venv (`.venv/bin/python`), not the system Python.

---

## Example Prompts

```
Investigate why PaymentService response times spiked in the last hour.

Show me all applications with open health violations.

Analyze snapshot abc-123 from CheckoutService and find the root cause.

Compare the failed snapshot with the golden baseline for the /checkout BT.

What changed in PaymentService between 14:00 and 15:00 yesterday?
```

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `Missing env vars: [...]` | Check `.env`. Controller `name` must match env var prefix (uppercased). |
| `401 Unauthorized` | Verify OAuth2 client has Account Owner or admin access. |
| `controllers.json not found` | Run from the project root, or set `--directory` in MCP config. |
| Tools missing (EUM, Analytics, DB) | Call `get_server_health` — license-gated tools listed under `disabled_tools`. |
| `PermissionError: Application '...' is not accessible` | Enterprise mode: UPN's AppD RBAC role doesn't grant access to that app. |
| `PermissionError: requires TROUBLESHOOT` | Caller's AppD role is VIEW. Use a higher-privileged account or ask admin. |
