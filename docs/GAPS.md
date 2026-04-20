# AppDynamics MCP Server — Gap Analysis

Last updated: 2026-04-19. Original gaps identified 2026-04-16; concurrency and
identity-scoped access gaps identified 2026-04-17.

---

## Open Items

### SCALE-06 — Per-team credential isolation 🔶 Partially closed

**File**: `auth/vault_client.py`, `auth/appd_auth.py`
**Problem**: All teams share the same OAuth2 service account token per controller.
There is no per-team credential scope at the AppD API layer.
**Current state**: The user-facing risk (cross-team app visibility) is solved by
ENH-007 — per-user RBAC scoping means each user only sees their own permitted apps.
Audit team attribution was fixed 2026-04-17. What remains is defense-in-depth: each
team having its own scoped AppD service account so a compromised token cannot be used
to access another team's data.
**Remaining fix**: Support per-team vault paths in `controllers.json`:
```json
"team_vault_paths": {
  "payments": "secret/appdynamics/production/payments",
  "checkout": "secret/appdynamics/production/checkout"
}
```
Each team's LLM session fetches its own scoped token. Requires per-team
`TokenManager` instances — the existing `TokenManager` class already supports
different vault paths.
**Trigger to act**: When per-team AppD service accounts are a compliance or
security requirement beyond what per-user RBAC scoping already provides.

---

### CONC-03 — Multiple uvicorn workers break all shared in-process state ⬜ Future

**Impact**: HIGH for horizontal scaling. All shared state is in-process memory:
- `_golden_registry`, `_bt_registry`, `_apps_registry` (registries)
- Rate limiter buckets (`_global_bucket`, `_user_buckets`, `_team_buckets`)
- `_sessions` dict in `auth/appd_auth.py` (user role cache)
- `_app_access_cache` in `services/user_resolver.py` (RBAC app-access cache)

Running 2+ uvicorn workers gives each worker its own independent copy. Rate limits
are per-worker (not per-server), cache invalidations don't cross workers, RBAC cache
is duplicated.
**Fix**: Extract shared mutable state to Redis:
- Rate limiter buckets → Redis sorted sets or Lua scripts (atomic token consume)
- `_sessions` role cache → Redis hash with TTL
- `_app_access_cache` RBAC cache → Redis hash with TTL
- Registries → Redis hash (or keep per-worker and accept eventual consistency)

The AppD data cache (diskcache L2) already survives restarts and is file-based —
only in-memory coordination state needs Redis.

**Trigger to act**: When >200 concurrent users or multi-worker/multi-replica
deployment is required. Single process handles up to ~200 users comfortably.

**Caching strategy decision (2026-04-17):** Current L1/L2 model (TTLCache
in-process + diskcache file-based) is the right choice for single-process
deployment. Redis adds operational overhead (separate service to deploy, monitor,
secure) not justified until CONC-03 is a real bottleneck.

---

## Capacity Reference (current single-process state)

| Concurrent users | Behaviour |
|-----------------|-----------|
| 1–10 | No issues |
| 10–50 | Fine for read-heavy workloads; CPU parsers are the risk on large traces |
| 50–200 | Handles well; per-UPN RBAC locks prevent cold-start contention |
| 200+ | AppD's own API rate limits become the ceiling before the MCP does |

---

## Closed Items — Historical Record

All items below are fully resolved. Kept for audit trail and design rationale.

### Bugs

#### BUG-01 — `compare_snapshots` never reads the golden registry ✅ Done
**File**: `main.py` `compare_snapshots()`
**Problem**: `set_golden_snapshot` wrote to `_golden_registry` but the auto-select
path in `compare_snapshots` ignored it, re-scoring 100 candidates every call.
`set_golden_snapshot` was effectively a no-op.
**Fix**: Check `_golden_registry.get(controller_name, app_name, bt_name)` first in
the auto-select path; fall through to score-based selection only if no manual golden
exists.

#### BUG-02 — `save_runbook` and `set_golden_snapshot` missing from permission sets ✅ Done
**File**: `auth/appd_auth.py`
**Problem**: `require_permission()` raised `PermissionError` for any role calling
`save_runbook` or `set_golden_snapshot` because neither appeared in any permission set.
**Fix**: Added `save_runbook` to `_TROUBLESHOOT_TOOLS` and `set_golden_snapshot` to
`_CONFIGURE_ALERTING_TOOLS`.

#### BUG-03 — `get_token()` missing null guard ✅ Done
**File**: `auth/appd_auth.py:62`
**Problem**: `get_token()` returned `self._cache.access_token` with `# type: ignore`.
If `_refresh()` raised (Vault down), `_cache` was still `None`, producing
`AttributeError` instead of `AuthenticationError`.
**Fix**: Added `assert self._cache is not None` before the return.

---

### Discovery Gaps

#### GAP-01 — No `get_tiers_and_nodes` tool ✅ Done
**Problem**: `get_infrastructure_stats` and `get_jvm_details` required `tier_name`
and `node_name` but no tool existed to list them. LLM had to hallucinate names.
**Fix**: Added `get_tiers_and_nodes(app_name, controller_name)` calling
`/rest/applications/{app}/tiers` and `/rest/applications/{app}/tiers/{tier}/nodes`.

#### GAP-02 — No `get_exit_calls` tool ✅ Done
**Problem**: No structured tool for exit call data (outbound DB queries, HTTP calls,
MQ publishes) from a snapshot — the most actionable data in a slow-transaction
investigation.
**Fix**: Added `get_exit_calls(app_name, snapshot_guid, controller_name)` extracting
`exitCalls` from the snapshot detail response.

---

### Functional Gaps

#### GAP-03 — `compare_snapshots` token budget not set ✅ Done
**Problem**: `compare_snapshots` defaulted to 1000 tokens. A full `SmokingGunReport`
easily exceeded this and was silently truncated.
**Fix**: Added `"compare_snapshots": 2000` to `TOKEN_BUDGETS`.

#### GAP-04 — `save_runbook` does not detect recurring incidents ✅ Done
**Problem**: `load_recent_runbooks()` existed but `save_runbook` never called it.
No warning when the same root cause recurred.
**Fix**: After saving, calls `load_recent_runbooks(app_name, limit=5)` and includes
a `"recurring_incidents"` field in the response if prior runbooks share the root cause.

#### GAP-05 — `stitch_async_trace` correlation match too narrow ✅ Done
**Problem**: Searched correlation ID only in `requestHeaders` and `userData`. Missed
AppD's native `correlationInfo` field and `exitCalls[].continuationID`.
**Fix**: Also searches `correlationInfo`, `exitCalls`, and `userData`. Adds diagnostic
warning when coverage < 100%.

#### GAP-06 — `correlate_eum_to_bt` silent empty result ✅ Done
**Problem**: When EUM/APM apps are not linked in the UI, `correlated == []` with no
explanation.
**Fix**: Returns a diagnostic message explaining the possible cause when result is empty.

#### GAP-07 — Caching applied inconsistently ✅ Done
**Problem**: `get_health_violations` and `list_applications` used two-layer cache;
most other tools hit AppD API every call.
**Fix**: Added caching to `get_metrics` (TTL 1 min), `get_infrastructure_stats`
(TTL 2 min), `get_bt_baseline` (TTL 5 min), `get_tiers_and_nodes` (TTL 5 min).

#### GAP-08 — Rate limiter treats all tools equally ✅ Done
**Problem**: `query_analytics_logs` and `list_applications` both cost 1 token despite
orders-of-magnitude difference in AppD API cost.
**Fix**: Tool weight multipliers implemented in `utils/rate_limiter.py` as part of
SCALE-04 (analytics = 3 tokens, snapshot ops = 2, reads = 1).

#### GAP-09 — No `get_agent_status` tool ✅ Done
**Problem**: No way to check if an AppD agent was reporting — couldn't distinguish
real regression from broken instrumentation.
**Fix**: Added `get_agent_status(app_name, tier_name, controller_name)`.

#### GAP-10 — `load_api_spec` unvalidated URL (SSRF risk) ✅ Done
**File**: `main.py` `load_api_spec()`
**Problem**: `spec_url` passed directly to `httpx.AsyncClient.get()` with no
validation — low-severity SSRF risk.
**Fix**: Validates `spec_url` matches `https://*.appdynamics.com/*` or the configured
controller URL before fetching.

#### GAP-11 — Audit log ephemeral ✅ Done
**Problem**: `audit_log()` wrote to stderr only. All history lost on restart.
**Fix**: Appends structured JSON to rotating daily file `audit/YYYY-MM-DD.jsonl`.
`AUDIT_LOG_DIR` env var controls the directory (default: `audit/`).

#### GAP-12 — `get_server_health` missing rate limiter state ✅ Done
**Problem**: Health response showed vault, cache, controller reachability — not rate
limit bucket fill levels.
**Fix**: Added `rate_limiter.get_stats()` block to health response.

#### GAP-13 — `save_runbook` hardcoded empty `tool_results` ✅ Done
**File**: `main.py` `save_runbook()`
**Problem**: `tool_results={}` was hardcoded. Runbooks saved without raw tool outputs.
**Fix**: Added optional `tool_results: dict[str, Any] | None = None` parameter.

---

### Scale & Multi-Tenancy Gaps

#### SCALE-01 — stdio transport limits to single connection ✅ Done
**Problem**: stdio = one-process, one-client. Multiple teams could not share one
process.
**Fix**: Switched to HTTP/SSE/streamable-http transport via `MCP_TRANSPORT` env var.
`mcp.run_sse_async()` / `mcp.run_streamable_http_async()` available natively.

#### SCALE-02 — `list_applications` unfiltered and unpaginated ✅ Done
**Problem**: At 3000 apps, `GET /controller/rest/applications` returns a 600KB–1MB
payload, truncated by token budget.
**Fix**: Added `search` and `page_size`/`page_offset` params. `AppsRegistry` populated
at startup for ID lookups. Added `search_applications(query, team)` tool.

#### SCALE-03 — No team-to-app scoping ✅ Done
**Problem**: Any VIEW-level user could query any of 3000 apps. No MCP-layer tenancy.
**Fix**: Added `teams` block to `controllers.json` with `app_pattern` and `upn_domain`.
`list_applications` filtered by caller's team at tool call time.

#### SCALE-04 — Global rate limit too low for concurrent teams ✅ Done
**Problem**: Global 10 tok/s burst 20. Two SREs investigating simultaneously exhausted
the burst. All tools cost 1 token regardless of actual AppD API cost.
**Fix**: Raised to 50 tok/s burst 100. Added per-team buckets. Tool weight multipliers
(analytics = 3, snapshots = 2, reads = 1) in `utils/rate_limiter.py`.

#### SCALE-05 — Cache undersized for 3000 apps × multiple teams ✅ Done
**Problem**: 3000 apps × 50 users exhausted the 10,000-entry L1 TTLCache instantly.
**Fix**: Raised global maxsize to 100,000. Per-type maxsizes raised proportionally
(see `MEMORY_CACHE_CONFIG` in `utils/cache.py`).

#### SCALE-07 — `stitch_async_trace` sequential API calls ✅ Done
**File**: `main.py` — `for app_name in app_names`
**Problem**: 10-service trace fired 10 sequential `list_snapshots` calls.
**Fix**: Replaced with `asyncio.gather` behind a semaphore (max 10 concurrent).

#### SCALE-08 — No cross-app aggregate tool ✅ Done
**Problem**: No way to get health summary across 50+ apps without 50 sequential tool
calls — not feasible in one LLM context window.
**Fix**: Added `get_team_health_summary(team, controller_name)` — fans out
`get_health_violations` across all team apps in parallel, returns ranked summary.

---

### Identity-Scoped Application Access

#### ENH-007 — Per-user app scoping via AppD RBAC ✅ Done (2026-04-17)

**Design**: Two service accounts per controller:

| Account | Vault path suffix | Purpose |
|---------|------------------|---------|
| Data account | `vaultPath` (existing) | All AppD operational API calls |
| RBAC account | `rbacVaultPath` (new) | Admin RBAC API only — user/role/group lookups |

**Flow on first tool call from a UPN:**
```
UPN → user_resolver.resolve(upn, controller)
  → RBACClient: GET /controller/api/rbac/v1/users?name={upn}
  → user's direct roles + group IDs
  → for each group: GET /groups/{id} → role IDs
  → for each role: GET /roles/{id} → applicationPermissions
  → union app names where canView=true → frozenset[str]
  → cached per (upn, controller), TTL configurable via APPDYNAMICS_RBAC_CACHE_TTL_S
  → list_applications filters to this set
  → per-app tools reject app_name not in set (PermissionError)
```

**Key decisions:**
- Fail closed: any RBAC lookup error → empty app set → PermissionError on all app tools
- `refresh_user_access` tool (CONFIGURE_ALERTING) force-clears a UPN's RBAC cache
- Per-UPN `asyncio.Lock` via `defaultdict` prevents cold-start serialisation (CONC-02)

**New files**: `client/rbac_client.py`, `services/user_resolver.py`

---

### Concurrency Gaps

#### CONC-01 — CPU-bound parsers blocked the asyncio event loop ✅ Done (2026-04-17)
**Problem**: One heavy `analyze_snapshot` call blocked every other in-flight request.
asyncio is single-threaded; synchronous CPU work = full event-loop stall.
**Fix**: Wrapped `parse_snapshot_errors`, `find_hot_path`, `score_golden_candidate`,
and `_compare` in `asyncio.to_thread()`, offloading to the default thread pool.

#### CONC-02 — Single global RBAC lock serialised all UPN lookups ✅ Done (2026-04-17)
**Problem**: 50 SREs at 9am all blocked behind one `asyncio.Lock` for RBAC lookups
(3–5 serial HTTP calls each). The 50th user could wait minutes.
**Fix**: Per-UPN locks via `defaultdict(asyncio.Lock)` in `services/user_resolver.py`.
Different UPNs now resolve concurrently; only same-UPN requests are serialised.
