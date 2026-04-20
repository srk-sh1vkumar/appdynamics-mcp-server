---
name: Enhancement 006 Complete
description: Production caching layer — registries, golden baseline registry, event-driven cache invalidation, 29th tool
type: project
---

# Enhancement 006 — Production Caching Layer

**Status**: Complete | **Date**: 2026-04-12 | **Actual hours**: 12

## What Was Built

A full production caching layer on top of the existing two-layer cache, adding structured
per-entity registries, Pydantic-validated caching, event-driven invalidation, a golden
baseline registry, and a new `set_golden_snapshot` tool (the 29th MCP tool).

---

## Deliverables

| File / Module | Purpose |
|---------------|---------|
| `utils/cache_keys.py` | Centralised UPN-namespaced key builder (`make_key`, `golden_key`, helper functions) |
| `utils/cache.py` (extended) | `TwoLayerCache` class, `CachedSnapshotAnalysis` model, `NEVER_CACHE` set, per-type stats |
| `registries/apps_registry.py` | `AppEntry` + `AppsRegistry` (TTLCache L1 + diskcache L2) |
| `registries/bt_registry.py` | `BTEntry` + `BTRegistry` (TTLCache L1 + diskcache L2) |
| `registries/golden_registry.py` | `GoldenSnapshot` + `GoldenRegistry` (24h TTL, shared across users) |
| `services/cache_invalidator.py` | `CacheInvalidator` — 4 event handlers (deployment, restart, manual override, validation failure) |
| `tests/unit/test_cache.py` | 47 unit tests across all new modules |
| `tests/integration/test_full_flow.py` | 6 end-to-end flow test classes |
| `tests/contract/test_appd_response_shapes.py` | 8 AppD API response shape contract test classes |
| `main.py` (extended) | `set_golden_snapshot` tool, deployment detection in `get_business_transactions`, restart detection in `get_health_violations`, extended `get_server_health` cache stats |

---

## Architecture Changes

### New: `TwoLayerCache` class (`utils/cache.py`)

```
Read path:  L1 (per-type TTLCache) → L2 (diskcache) → fetch_fn → populate both
Write path: Always L1. L2 only when persist_to_disk=True.
Validation: Pydantic on every cache read.
  - Dict passes validation  → return model instance
  - Dict fails validation   → evict + log + fetch fresh
  - List / non-dict         → pass through as-is
```

Per-data-type TTL and maxsize:

| Data type | TTL | maxsize |
|-----------|-----|---------|
| `applications` | 300s | 100 |
| `business_transactions` | 300s | 500 |
| `metric_tree` | 600s | 1000 |
| `metric_values` | 60s | 500 |
| `health_violations` | 30s | 200 |
| `user_roles` | 1800s | 200 |
| `snapshot_list` | 30s | 200 |
| `parsed_snapshot` | 3600s | 100 |

### New: `CachedSnapshotAnalysis` model

Stores only the parsed, PII-redacted snapshot analysis result. Raw snapshot JSON (~500 KB)
is intentionally excluded. TTL=3600s (GUIDs are immutable). In-memory only.

### New: `NEVER_CACHE` set

Types that must never be cached regardless of caller intent:
- `raw_snapshot_json`
- `adql_query_results`
- `active_health_violations_realtime`

### New: Registries

Three independent two-layer stores:

| Registry | Key | Invalidated by |
|----------|-----|----------------|
| `AppsRegistry` | `apps:{controller}` | Manual `invalidate()` |
| `BTRegistry` | `bts:{controller}:{app}` | Deployment detection |
| `GoldenRegistry` | `__golden__:{controller}:{app}:{bt}` | Deployment, restart, manual override, 24h TTL |

Golden baseline keys carry no UPN — they are shared across all users of the same app/BT.

### New: `CacheInvalidator` event handlers

| Event | Trigger signal | Invalidates |
|-------|---------------|-------------|
| `on_deployment_detected` | BT count shifts >2 vs cached | `bt_registry` + `golden_registry` for app |
| `on_app_restart_detected` | `APP_CRASH` or `NODE_RESTART` in health violations | `golden_registry` for app only |
| `on_manual_golden_override` | `set_golden_snapshot` tool called | Single BT golden entry |
| `on_cache_validation_failure` | Pydantic validation error on cache read | Single corrupt key |

All handlers are fail-safe (never raise) and log to the audit trail.

### New: `set_golden_snapshot` tool (29th tool)

Allows an SRE to manually promote a specific snapshot GUID as the golden baseline for a BT.
Fetches the snapshot, scores it via `score_golden_candidate()`, records the previous golden
for audit, persists to `GoldenRegistry`, and triggers `on_manual_golden_override`.

---

## Key Design Decisions

- **`async def set()` name shadowing**: The module-level `async def set()` in `cache.py` shadows
  Python's builtin `set` constructor. Fixed by using dict-merge iteration `{**hits, **misses}`
  instead of `set(x) | set(y)` in stats functions.

- **List values in `TwoLayerCache`**: Lists cannot be Pydantic-validated against a single model.
  `_try_validate` returns `(None, False)` for non-dict values — `False` means do not evict;
  the caller returns the raw value as-is.

- **Golden keys without UPN**: Requirement §16 says all keys must include UPN, but golden
  baselines are shared across users. Resolved: `golden_key()` uses `__golden__:` prefix,
  never stored in the user-scoped `TwoLayerCache`.

- **Python 3.13 consistency**: `pyproject.toml` `requires-python = ">=3.13"`,
  `ruff target-version = "py313"`, `mypy python_version = "3.13"` — all aligned.

---

## Verification

```
ruff check utils/cache_keys.py utils/cache.py registries/ services/cache_invalidator.py
→ All checks passed!

mypy utils/cache_keys.py utils/cache.py registries/ services/cache_invalidator.py
→ Success: no issues found in 7 source files

pytest tests/unit/test_cache.py -v
→ 47 passed in 0.27s

pytest tests/
→ 217 passed in 0.67s
```
