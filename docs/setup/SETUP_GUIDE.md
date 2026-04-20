# Setup Guide

## Prerequisites

| Requirement | Version | Notes |
|-------------|---------|-------|
| Python | 3.13+ | |
| uv | latest | `pip install uv` |
| AppDynamics controller | 22.x+ | SaaS or on-prem |
| HashiCorp Vault | optional | dev mode uses env vars |

## 1. Install dependencies

```bash
cd appdynamics-mcp-server
uv sync
```

## 2. Configure controllers.json

Create `controllers.json` in the project root:

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
    }
  ]
}
```

For a minimal startup with no real controller (e.g. local testing):

```json
{"controllers": []}
```

## 3. Set environment variables

```bash
cp .env.example .env
# Edit .env
```

### Mock mode (dev)

Env var names come from `vaultPath` — strip `secret/`, replace `/` with `_`, uppercase:

```bash
# vaultPath: "secret/appdynamics/production"
APPDYNAMICS_PRODUCTION_CLIENT_ID=your-client-id
APPDYNAMICS_PRODUCTION_CLIENT_SECRET=your-client-secret
VAULT_MODE=mock
```

### HashiCorp Vault (prod)

```bash
VAULT_MODE=hashicorp
VAULT_ADDR=https://vault.example.com
VAULT_TOKEN=hvs.your-token
```

Vault KV layout:
```
secret/appdynamics/production/client_id
secret/appdynamics/production/client_secret
```

## 4. Connect an MCP client

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "appdynamics": {
      "command": "uv",
      "args": ["run", "--project", "/absolute/path/to/appdynamics-mcp-server", "python", "-m", "main"],
      "env": {
        "VAULT_MODE": "mock",
        "APPDYNAMICS_PRODUCTION_CLIENT_ID": "your-client-id",
        "APPDYNAMICS_PRODUCTION_CLIENT_SECRET": "your-client-secret"
      }
    }
  }
}
```

### Cursor

`.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "appdynamics": {
      "command": "uv",
      "args": ["run", "--project", "/absolute/path/to/appdynamics-mcp-server", "python", "-m", "main"],
      "env": {
        "VAULT_MODE": "mock",
        "APPDYNAMICS_PRODUCTION_CLIENT_ID": "your-client-id",
        "APPDYNAMICS_PRODUCTION_CLIENT_SECRET": "your-client-secret"
      }
    }
  }
}
```

## 5. Docker

```bash
# Build
docker build -t appd-mcp-server .

# Run (mock mode, no real controller)
echo '{"controllers":[]}' > controllers.json
docker run -i \
  -p 8080:8080 \
  -e VAULT_MODE=mock \
  -v $(pwd)/controllers.json:/app/controllers.json:ro \
  appd-mcp-server
```

`-i` is required — MCP communicates over stdin/stdout.

Liveness probe: `curl http://localhost:8080/health` → `{"status": "ok"}`

## 6. Kubernetes

```yaml
livenessProbe:
  httpGet:
    path: /health
    port: 8080
  initialDelaySeconds: 10
  periodSeconds: 30
  failureThreshold: 3
```

Provide credentials via a Kubernetes Secret mounted as environment variables, or point `VAULT_MODE=hashicorp` at your Vault instance.
