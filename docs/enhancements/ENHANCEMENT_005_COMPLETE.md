---
name: Enhancement 005 Complete
description: Prometheus metrics endpoint + Monitoring Hub scrape target + Grafana dashboard
type: project
---

# Enhancement 005 — Monitoring Hub Integration

**Status**: Complete | **Date**: 2026-04-12 | **Actual hours**: 1

## What Was Built

### `utils/metrics.py` (new)

In-process Prometheus counter/gauge store with no external dependencies.

| Metric | Type | Description |
|--------|------|-------------|
| `appd_mcp_tool_calls_total{tool, status}` | counter | Invocations per tool, split by success/error |
| `appd_mcp_tool_duration_ms{tool}` | counter | Cumulative duration (ms) per tool |
| `appd_mcp_rate_limit_hits_total` | counter | Times the token bucket throttled a request |
| `appd_mcp_cache_hits_total` | counter | L1 + L2 cache hits |
| `appd_mcp_cache_misses_total` | counter | Cache misses |
| `appd_mcp_active_users` | gauge | Distinct UPNs seen since startup |
| `appd_mcp_requests_last_hour` | gauge | Tool calls in the rolling 60-minute window |

Thread-safe: plain dicts + `threading.Lock` (safe from concurrent asyncio callbacks).

### `services/health.py` — `/metrics` endpoint

Extended the existing asyncio HTTP server (port 8080) to route:
- `GET /health` → `{"status": "ok"}` (K8s liveness probe, unchanged)
- `GET /metrics` → Prometheus text format (`text/plain; version=0.0.4`)

No new port, no new dependency.

### `main.py` — metrics wired into `audit_log()`

Every tool call now calls `metrics_mod.record_tool_call(tool, status, duration_ms)` and `metrics_mod.record_upn(upn)` in the `audit_log()` function, so coverage is automatic — no per-tool changes needed.

### `utils/cache.py` — cache metrics wired in

`get()` calls `_metrics.record_cache_hit()` or `_metrics.record_cache_miss()` after every lookup.

### `monitoring-hub/prometheus/prometheus.yml` — scrape target added

```yaml
- job_name: 'appdynamics-mcp-server'
  scrape_interval: 15s
  metrics_path: '/metrics'
  static_configs:
    - targets: ['host.docker.internal:8080']
      labels:
        project: 'appdynamics-mcp-server'
        service: 'mcp-server'
        tier: 'api'
```

### `monitoring-hub/grafana/dashboards/appdynamics-mcp-monitoring.json`

10-panel dashboard:

| Row | Panels |
|-----|--------|
| Stat row | Total calls, Error rate, Cache hit rate, Active users, Requests/hour, Rate limit hits |
| Time series | Tool call rate by status (success/error) over time; Cache hits vs misses over time |
| Bar gauge | Top tools by call volume; Average tool duration (ms) |

## Verification

```bash
# Start the MCP server (mock mode, empty controllers)
echo '{"controllers":[]}' > /tmp/controllers_test.json
docker run -i -p 8080:8080 -e VAULT_MODE=mock \
  -v /tmp/controllers_test.json:/app/controllers.json:ro \
  appd-mcp-server

# Check metrics endpoint
curl http://localhost:8080/metrics
# → appd_mcp_tool_calls_total, appd_mcp_cache_hits_total, etc.

# Liveness probe still works
curl http://localhost:8080/health
# → {"status": "ok"}
```

To activate Grafana dashboard: start monitoring-hub with `docker-compose up -d` then open `http://localhost:3002` and navigate to the "AppDynamics MCP Server" dashboard.
