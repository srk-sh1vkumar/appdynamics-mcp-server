# main.py Service Extraction Plan

## Context

`main.py` is the FastMCP entry point. It registers all 35 tools with the `mcp = FastMCP(...)` instance
via `@mcp.tool()` decorators. Because the `mcp` instance lives in `main.py`, all decorator registrations
must stay there — moving them elsewhere creates circular imports.

What we extract is the **business logic** inside each tool function body, not the registration.

## The Pattern

```
services/<name>.py      ← async run(client, ...) — all logic, independently testable
main.py wrapper         ← rate limit → RBAC → call service.run() → sanitize_and_wrap → audit_log
```

Established by `services/incident_correlator.py` (correlate_incident_window, 2026-04).

## Why Extract?

- Service modules are unit-testable with a plain `AsyncMock` client — no MCP plumbing needed.
- `main.py` stays as a thin registration + cross-cutting concerns layer.
- Extracted logic can be reused across tools (e.g., team_health fan-out could be called from correlate_incident_window in the future).
- Test isolation: existing tests patch `main.get_client`, `main._get_role`, etc. — extracting logic to services requires no changes to existing test patches.

## Constraint

All `@mcp.tool()` decorators stay in `main.py`. FastMCP router modularization is a future item once service extraction is proven stable.

---

## Extraction Candidates

### Tier 1 — Extract (high complexity, clean boundaries)

| Tool | Target module | Lines | Key logic |
|------|--------------|-------|-----------|
| `get_team_health_summary` | `services/team_health.py` | ~112 | Semaphore fan-out across apps, sort, aggregation |
| `stitch_async_trace` | `services/trace_stitcher.py` | ~113 | Correlation ID search, gap calculation, ordered trace |
| `compare_snapshots` | `services/snapshot_comparator.py` | ~105 | Golden registry lookup, auto-scoring, diff |
| `analyze_snapshot` | `services/snapshot_analyzer.py` | ~85 | Stack parse, hot path, exception strategy hints |

### Tier 2 — Leave as-is (thin pass-throughs, no extraction value)

Single API call + format tools: `list_controllers`, `list_applications`, `search_metric_tree`,
`get_metrics`, `get_bt_baseline`, `get_bt_detection_rules`, `load_api_spec`, `list_snapshots`,
`archive_snapshot`, `set_golden_snapshot`, `get_health_violations`, `get_policies`,
`get_infrastructure_stats`, `get_jvm_details`, `get_exit_calls`, `get_agent_status`,
`get_errors_and_exceptions`, `get_database_performance`, `get_network_kpis`,
`query_analytics_logs`, `get_eum_overview`, `get_eum_page_performance`, `get_eum_js_errors`,
`get_eum_ajax_requests`, `get_eum_geo_performance`, `correlate_eum_to_bt`, `get_server_health`,
`save_runbook` (already uses `runbook_generator` service).

`get_tiers_and_nodes` — borderline (30-line loop), leave unless reuse reason emerges.

---

## Service Signatures

### `services/team_health.py`

```python
async def run(
    client: Any,
    app_names: list[str],
    duration_mins: int,
) -> dict[str, Any]
```

Wrapper resolves `app_names` from `_apps_registry` or live call (stays in main.py), then passes list to `run()`.

### `services/trace_stitcher.py`

```python
async def run(
    client: Any,
    correlation_id: str,
    app_names: list[str],
    duration_mins: int,
) -> dict[str, Any]
```

### `services/snapshot_comparator.py`

```python
async def run(
    client: Any,
    app_name: str,
    failed_snapshot_guid: str,
    golden_registry: Any,               # GoldenRegistry instance from main.py
    healthy_snapshot_guid: str | None = None,
    controller_name: str = "production",
) -> dict[str, Any]
```

`_golden_registry` is a global in main.py — passed explicitly so the service has no import dependency on main.

### `services/snapshot_analyzer.py`

```python
async def run(
    client: Any,
    app_name: str,
    snapshot_guid: str,
    app_package_prefix: str = "",
    exception_strategies: dict[str, str] | None = None,
) -> dict[str, Any]
```

`EXCEPTION_STRATEGIES` dict moves to `snapshot_analyzer.py` as a module constant.
`app_package_prefix` is sourced from the matching `ControllerConfig.app_package_prefix` in `_controllers` — main.py wrapper resolves it before calling `run()`.

---

## Execution Order

Each extraction is a 3-step atomic operation:
1. Write `services/<name>.py` with `run()` function
2. Update `main.py` wrapper to call `service.run(client=client, ...)`
3. Write service-layer tests in `tests/unit/test_<name>.py`
4. Run full suite (265 tests) — zero regressions expected

Priority: team_health → trace_stitcher → snapshot_comparator → snapshot_analyzer

---

## Test Impact

Existing `patched_main` fixture patches:
- `main.get_client`
- `main.check_and_wait`
- `main._get_role`
- `main.require_permission`
- `main.audit_log`

None of these are in service modules. Extracting logic to services does **not** require changes to any existing test.
