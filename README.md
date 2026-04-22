# AppDynamics MCP Server — Single-User Mode

Connect your AI assistant (Claude Desktop or Cursor) directly to your AppDynamics
controller using your own admin OAuth2 credentials. Five-minute setup.

> **Enterprise / multi-user deployment?** See the [`main`](../../tree/main) branch —
> it adds HashiCorp Vault, per-user RBAC app scoping, team filtering, and audit
> log persistence for shared org-wide deployments.

---

## Prerequisites

- Python 3.13+, [uv](https://docs.astral.sh/uv/)
- An AppDynamics controller with an **OAuth2 API Client** that has admin access
- Claude Desktop or Cursor

---

## 1. Clone and install

```bash
git clone https://github.com/srk-sh1vkumar/appdynamics-mcp-server.git
cd appdynamics-mcp-server
git checkout single-user-mode
uv sync
```

---

## 2. Configure your controller

Edit `controllers.json` with your AppDynamics controller details:

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

The `name` field is used to derive env var names (see next step).

---

## 3. Set credentials

```bash
cp .env.example .env
```

Edit `.env`:

```bash
APPDYNAMICS_PRODUCTION_CLIENT_ID=your-oauth2-client-id
APPDYNAMICS_PRODUCTION_CLIENT_SECRET=your-oauth2-client-secret
```

The env var prefix is the controller `name` uppercased:
- `"production"` → `APPDYNAMICS_PRODUCTION_CLIENT_ID`
- `"staging"` → `APPDYNAMICS_STAGING_CLIENT_ID`

**Create an API Client in AppDynamics:**
> Controller → Settings → Administration → API Clients → + Create
> Grant: Account Owner or equivalent admin role.

---

## 4. Test startup

```bash
uv run python main.py
```

You should see:
```
[auth] Token refreshed for controller 'production'. Expires at ...
[main] AppDynamics MCP Server v1.0.0 started. Controllers: ['production']. Mode: FULL
```

---

## 5. Connect Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "appdynamics": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/appdynamics-mcp-server", "python", "main.py"],
      "env": {
        "APPDYNAMICS_PRODUCTION_CLIENT_ID": "your-client-id",
        "APPDYNAMICS_PRODUCTION_CLIENT_SECRET": "your-client-secret"
      }
    }
  }
}
```

Restart Claude Desktop. The AppDynamics tools will appear in the tool list.

---

## 6. Connect Cursor

Add to `.cursor/mcp.json` in your project root:

```json
{
  "mcpServers": {
    "appdynamics": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/appdynamics-mcp-server", "python", "main.py"],
      "env": {
        "MCP_TRANSPORT": "streamable-http",
        "APPDYNAMICS_PRODUCTION_CLIENT_ID": "your-client-id",
        "APPDYNAMICS_PRODUCTION_CLIENT_SECRET": "your-client-secret"
      }
    }
  }
}
```

---

## Available Tools (28)

| Category | Tools |
|----------|-------|
| Discovery | `list_controllers`, `list_applications`, `search_applications`, `search_metric_tree`, `get_metrics` |
| Business Transactions | `get_business_transactions`, `get_bt_baseline`, `get_bt_detection_rules`, `load_api_spec` |
| Snapshots | `list_snapshots`, `analyze_snapshot`, `compare_snapshots`, `set_golden_snapshot`, `archive_snapshot`, `get_exit_calls` |
| Health & Policies | `get_health_violations`, `get_policies`, `get_infrastructure_stats`, `get_jvm_details`, `get_tiers_and_nodes`, `get_agent_status`, `get_team_health_summary` |
| Deep Diagnostics | `get_errors_and_exceptions`, `get_database_performance`, `get_network_kpis`, `query_analytics_logs`, `stitch_async_trace` |
| EUM | `get_eum_overview`, `correlate_eum_to_bt` |
| Server | `get_server_health`, `save_runbook`, `load_recent_runbooks` |

---

## Example Prompts

```
Investigate why PaymentService response times spiked in the last hour.

Show me all applications with open health violations.

Analyze snapshot abc-123 from CheckoutService and find the root cause.

Compare the failed snapshot with the golden baseline for the /checkout BT.
```

---

## Multiple Controllers

Add entries to `controllers.json` and matching env vars:

```bash
APPDYNAMICS_PRODUCTION_CLIENT_ID=prod-client-id
APPDYNAMICS_PRODUCTION_CLIENT_SECRET=prod-client-secret
APPDYNAMICS_STAGING_CLIENT_ID=staging-client-id
APPDYNAMICS_STAGING_CLIENT_SECRET=staging-client-secret
```

All tools accept an optional `controller_name` parameter (default: `"production"`).

---

## Troubleshooting

| Error | Fix |
|-------|-----|
| `Missing env vars: [...]` | Check `.env`. Controller `name` must match the env var prefix (uppercased). |
| `401 Unauthorized` | Verify the OAuth2 client has Account Owner or admin access. |
| `controllers.json not found` | Run from the project root, or set `--directory` in MCP config. |
| Tools missing (EUM, Analytics, DB) | Call `get_server_health` — license-gated tools are listed under `disabled_tools`. |
