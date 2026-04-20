# AppDynamics MCP Server — Gap Analysis

Identified 2026-04-16. Organized by severity. Each item links to the fix status.

---

## Bugs (Broken Today)

### BUG-01 — `compare_snapshots` never reads the golden registry ✅ Fix: #1
**File**: `main.py` `compare_snapshots()`
**Problem**: `set_golden_snapshot` writes to `_golden_registry`, but the auto-select path
in `compare_snapshots` ignores it. It fetches 100 candidates and re-scores them instead.
`set_golden_snapshot` is effectively a no-op.
**Fix**: Check `_golden_registry.get(controller_name, app_name, bt_name)` first in the
auto-select path. Only fall through to score-based selection if no manual golden exists.

### BUG-02 — `save_runbook` and `set_golden_snapshot` missing from permission sets ✅ Fix: #2
**File**: `auth/appd_auth.py`
**Problem**: `require_permission()` raises `PermissionError` for any role calling
`save_runbook` or `set_golden_snapshot` because neither appears in `_VIEW_TOOLS`,
`_TROUBLESHOOT_TOOLS`, or `_CONFIGURE_ALERTING_TOOLS`.
**Fix**: Add `save_runbook` to `_TROUBLESHOOT_TOOLS` and `set_golden_snapshot` to
`_CONFIGURE_ALERTING_TOOLS`.

### BUG-03 — `get_token()` missing null guard ✅ Fix: #3
**File**: `auth/appd_auth.py:62`
**Problem**: `get_token()` returns `self._cache.access_token` with a `# type: ignore`
instead of an assert. If `_refresh()` raises (e.g., Vault is down), `_cache` is still
`None` and the line raises `AttributeError`, not `AuthenticationError`.
**Fix**: Add `assert self._cache is not None` before the return, matching the pattern
already used in `handle_401()`.

---

## Discovery Gaps (Investigation Workflow Blockers)

### GAP-01 — No `get_tiers_and_nodes` tool ✅ Fix: #4
**Impact**: CRITICAL — blocks Steps 12 of the 16-step investigation flow.
`get_infrastructure_stats` and `get_jvm_details` both require `tier_name` and `node_name`
as arguments, but there is no tool to list an application's tiers or nodes. The LLM must
hallucinate these names or the user must provide them out of band.
**Fix**: Add `get_tiers_and_nodes(app_name, controller_name)` tool that calls the
`/rest/applications/{app}/tiers` and `/rest/applications/{app}/tiers/{tier}/nodes`
endpoints and returns a structured map.

### GAP-02 — No `get_exit_calls` tool ✅ Fix: #5
**Impact**: HIGH — the most actionable data in a slow-transaction investigation is
missing. Snapshot detail contains exit call data (outbound DB queries, HTTP calls to
downstream services, MQ publishes) but `analyze_snapshot` only surfaces `callChain`
segments and stack traces. There is no structured tool for "what external calls did
this snapshot make, and how long did each take?"
**Fix**: Add `get_exit_calls(app_name, snapshot_guid, controller_name)` that extracts
and formats `exitCalls` from the snapshot detail response.

---

## Functional Gaps

### GAP-03 — `compare_snapshots` token budget not set
**File**: `main.py` `TOKEN_BUDGETS`
**Problem**: `compare_snapshots` is not in `TOKEN_BUDGETS`, so it defaults to 1000
tokens. A full `SmokingGunReport` with 10 latency deviations + 10 exclusive methods
easily exceeds this and is silently truncated, producing malformed output.
**Fix**: Add `"compare_snapshots": 2000` to `TOKEN_BUDGETS`.

### GAP-04 — `save_runbook` does not detect recurring incidents
**File**: `main.py` `save_runbook()`, `services/runbook_generator.py`
**Problem**: `load_recent_runbooks()` exists specifically to detect recurring incidents,
but `save_runbook` never calls it. The tool does not warn when the same root cause
has appeared multiple times.
**Fix**: After saving, call `load_recent_runbooks(app_name, limit=5)` and include a
`"recurring_incidents"` field in the response if prior runbooks share the same root cause.

### GAP-05 — `stitch_async_trace` correlation match is too narrow
**File**: `main.py` `stitch_async_trace()`
**Problem**: Searches for the correlation ID only in `requestHeaders` and `userData`
string-casts. AppD's native cross-app correlation uses a `correlationInfo` field and
`exitCalls[].continuationID`. The tool misses most real async traces.
**Fix**: Also search `correlationInfo`, `exitCalls`, and `userData` fields. Add a
diagnostic warning when coverage < 100% explaining the possible causes.

### GAP-06 — `correlate_eum_to_bt` relies on `correlatedBt` field
**File**: `main.py` `correlate_eum_to_bt()`
**Problem**: AppD only populates `correlatedBt` when the EUM app and APM app are
explicitly linked in the UI. If not linked (common in large orgs), the tool returns
0 results with no explanation.
**Fix**: When `correlated == []`, return a diagnostic message explaining the possible
cause rather than an empty list.

### GAP-07 — Caching applied inconsistently
**Problem**: `get_health_violations` and `list_applications` use the two-layer cache.
Most other tools (`get_metrics`, `get_business_transactions`, `list_snapshots`,
`analyze_snapshot`, `get_infrastructure_stats`) hit the AppD API every call.
No clear policy governs which tools should be cached.
**Recommendation**: Cache `get_business_transactions` (TTL 5 min), `get_metrics`
(TTL 1 min), `get_infrastructure_stats` (TTL 2 min). Snapshots should not be cached
(each call is for a different GUID).

### GAP-08 — Rate limiter treats all tools equally
**Problem**: `query_analytics_logs` with a complex ADQL join is orders of magnitude
more expensive to AppD than `list_applications`, but both consume one token from the
same 5 tok/s per-user bucket.
**Recommendation**: Assign weights: analytics queries = 3 tokens, snapshot ops = 2
tokens, read-only list calls = 1 token.

### GAP-09 — No `get_agent_status` tool
**Impact**: MEDIUM — "Is my AppD agent reporting?" is a required first step when
symptoms appear. Without this, the LLM cannot distinguish a real regression from
broken instrumentation.
**Fix**: Add `get_agent_status(app_name, tier_name, controller_name)` calling
`/rest/applications/{app}/tiers/{tier}/nodes` with availability fields.

### GAP-10 — `load_api_spec` has unvalidated URL input
**File**: `main.py` `load_api_spec()`
**Problem**: `spec_url` is passed directly to `httpx.AsyncClient.get()` with no
validation. This is a low-severity SSRF risk when the MCP server has internal
network access.
**Fix**: Validate that `spec_url` matches `https://*.appdynamics.com/*` or the
configured controller URL before fetching.

---

## Minor Gaps

### GAP-11 — Audit log is ephemeral
**Problem**: `audit_log()` writes to Python logging (stderr). If the process restarts,
all audit history is lost.
**Fix**: Append structured JSON audit records to a rotating file
`audit/{YYYY-MM-DD}.jsonl` in addition to stderr.

### GAP-12 — `get_server_health` does not report rate limiter state
**Problem**: Health report shows vault, cache, controller reachability, but not rate
limit bucket fill levels or queued requests.
**Fix**: Expose `rate_limiter.get_stats()` in the health response.

### GAP-13 — `save_runbook` passes empty `tool_results`
**File**: `main.py` `save_runbook()`
**Problem**: `tool_results={}` is hardcoded. Runbooks are saved without the raw
tool outputs that generated the root cause, making them less useful for post-mortems.
**Fix**: Accept an optional `tool_results: dict` parameter in the tool signature.

---

## Scale & Multi-Tenancy Gaps

Identified 2026-04-16. These gaps prevent the server from serving multiple teams
or handling a controller with 3000+ applications.

### SCALE-01 — stdio transport = single connection (most critical)
**File**: `main.py:2043` — `mcp.run_stdio_async()`
**Problem**: The MCP server uses stdio transport, which is a one-process, one-client
model. It is designed for a single LLM session. Multiple teams cannot share one
process — each additional connection attempt has no stdin/stdout to attach to.
**Fix**: Switch to HTTP/SSE transport (`mcp.run_sse_async(host, port)`). FastMCP
supports this natively. Each team's LLM session connects over HTTP. Requires
adding startup config for host/port and deploying behind a reverse proxy.

### SCALE-02 — `list_applications` is unfiltered and unpaginated
**File**: `client/appd_client.py:206`
**Problem**: `GET /controller/rest/applications` returns all applications in one
response with no filter or pagination params. At 3000 apps this is a 600KB–1MB
payload, well beyond any token budget. The response is silently truncated and the
LLM sees only the first ~50 apps.

Additionally, `get_bt_detection_rules` calls `list_applications()` on every
invocation just to resolve a numeric app ID — a full 3000-app scan per call.
**Fix**:
1. Add `search` and `page_size`/`page_offset` params to `list_applications`.
2. Populate `AppsRegistry` at startup from the full list; use it for ID lookups
   instead of re-fetching.
3. Add `search_applications(query, team)` tool as the primary discovery interface.

### SCALE-03 — No team-to-app scoping (no tenancy at MCP layer)
**Problem**: There is no concept of "team" in the server. AppD's RBAC is the only
access control — if a user has VIEW on the controller they can query any of the
3000 apps. The MCP has no mechanism to restrict Team Payments to `payments-*` apps
or Team Checkout to `checkout-*` apps. An LLM session for one team can
inadvertently query another team's applications.
**Fix**: Add a `teams` block to `controllers.json`:
```json
"teams": [
  { "name": "payments", "app_pattern": "payments-*", "upn_domain": "@payments.corp" }
]
```
At tool call time, resolve the caller's UPN domain to a team and filter
`list_applications` results by the team's `app_pattern`. No changes to AppD itself.

### SCALE-04 — Global rate limit blocks concurrent team usage
**File**: `utils/rate_limiter.py:21-22`
**Problem**: The global bucket is 10 tok/s, burst 20. That is a ceiling of ~10
concurrent in-flight tool calls across the entire server. Two SREs investigating
simultaneously exhaust the global burst. All tools cost 1 token regardless of
their actual AppD API cost (`query_analytics_logs` ADQL ≈ 10× more expensive
than `list_applications`).
**Fix**:
1. Raise global limits (e.g. 50 tok/s, burst 100) for multi-user deployment.
2. Add per-team buckets in addition to per-user buckets.
3. Add tool weight multipliers (analytics = 3 tokens, snapshot ops = 2, reads = 1).

### SCALE-05 — Cache undersized for 3000 apps × multiple teams
**File**: `utils/cache.py:75` — `TTLCache(maxsize=10_000)`
**Problem**: With 3000 apps × 50+ users, cache keys (upn + controller + app + type)
exhaust the 10,000-entry L1 maxsize instantly, causing constant eviction and near-zero
hit rates. Per-type maxsizes are also undersized: `business_transactions maxsize=500`
covers only 500 distinct app BT-list responses out of 3000.
**Fix**:
1. Raise global maxsize to 100,000.
2. Raise per-type maxsizes proportionally.
3. Optionally namespace cache by team rather than UPN so team-shared reads
   (e.g. `list_applications`) are shared across all users on the same team.

### SCALE-06 — Shared service account token (no per-team credential isolation)
**File**: `auth/vault_client.py`, `auth/appd_auth.py`
**Problem**: All teams and users share the same OAuth2 service account token per
controller. There is no per-team credential scope. A user gets read access to all
3000 apps even if their AppD account would normally be restricted. Audit logs show
UPN but not which team, making cross-team attribution difficult.
**Fix**: Support per-team vault paths in `controllers.json`:
```json
"team_vault_paths": {
  "payments": "secret/appdynamics/production/payments",
  "checkout": "secret/appdynamics/production/checkout"
}
```
Each team's LLM session fetches its own scoped token. Requires per-team
`TokenManager` instances — the existing `TokenManager` class already supports
different vault paths.

### SCALE-07 — `stitch_async_trace` fires sequential API calls
**File**: `main.py:1597` — `for app_name in app_names`
**Problem**: The loop over `app_names` is sequential. For a trace spanning 10
services, it fires 10 sequential `list_snapshots` calls. At scale (many services,
slow AppD), this serialises what should be a parallel fan-out.
**Fix**: Replace with `asyncio.gather` behind a semaphore (e.g. max 10 concurrent):
```python
sem = asyncio.Semaphore(10)
results = await asyncio.gather(*[fetch(app, sem) for app in app_names])
```

### SCALE-08 — No cross-app aggregate tool
**Problem**: The single most useful capability for a team managing 50–3000 apps is
"show me which of my apps are degraded right now." Every current tool is per-app.
Getting a health summary across 50 apps requires 50 sequential tool calls — not
feasible in a single LLM context window.
**Fix**: Add `get_team_health_summary(team, controller_name)` that fans out
`get_health_violations` across all apps matching the team's `app_pattern` in
parallel (using `asyncio.gather` + semaphore), then returns a ranked summary of
apps with open violations. This is the highest-value aggregate tool for SRE use.

---

## Identity-Scoped Application Access (Enhancement 007)

Identified 2026-04-17. Addresses the risk that any authenticated user can see all
applications on the controller, regardless of what their AppDynamics RBAC actually permits.

### Design

Two service accounts per controller:

| Account | Vault path suffix | Purpose |
|---------|------------------|---------|
| Data account | `vaultPath` (existing) | All AppD operational API calls (metrics, snapshots, BTs, etc.) |
| RBAC account | `rbacVaultPath` (new) | Admin RBAC API only — user/role/group lookups |

**Flow on first tool call from a UPN:**

```
UPN declared by user → user_resolver.resolve(upn, controller)
  → RBACClient: GET /controller/api/rbac/v1/users?name={upn}
  → Get user's direct roles + group IDs
  → For each group: GET /controller/api/rbac/v1/groups/{id} → get role IDs
  → For each role: GET /controller/api/rbac/v1/roles/{id} → get applicationPermissions
  → Union all app names where canView=true → frozenset[str]
  → Cache against (upn, controller) with 1800s TTL
  → list_applications filters results to this set
  → per-app tools reject app_name not in this set (PermissionError)
```

**Key decisions:**
- RBAC account requires AppD admin-level token (Account Owner or equivalent read-only admin role)
- Data account remains VIEW-level — no change to existing token
- Fail closed: any RBAC lookup error → empty app set → PermissionError on all app tools
- UPN declared by user in stdio (trust model); comes from request auth in HTTP/SSE mode
- App set cached 30 min per UPN per controller — role changes take up to 30 min to propagate

**New files:**
- `client/rbac_client.py` — RBAC admin API client (separate auth token, `/controller/api/rbac/v1/` surface)
- `services/user_resolver.py` — traverses user → groups → roles → apps, returns `frozenset[str]`

**Modified files:**
- `models/types.py` — `ControllerConfig.rbac_vault_path: str`
- `controllers.json` — `rbacVaultPath` field per controller
- `utils/cache_keys.py` — `user_app_access_key(upn, controller)`
- `utils/cache.py` — `user_app_access` cache type (TTL 1800s)
- `.env.example` — `APPDYNAMICS_PRODUCTION_RBAC_CLIENT_ID/SECRET`
- `main.py` — filter `list_applications`; guard all per-app tools

---

## Fix Priority Order

### Original gaps (functional) — all ✅ closed
| Priority | Item | Status |
|----------|------|--------|
| P0 | BUG-01: golden registry in compare | ✅ Done |
| P0 | BUG-02: permission sets | ✅ Done |
| P0 | BUG-03: get_token null guard | ✅ Done |
| P1 | GAP-01: get_tiers_and_nodes tool | ✅ Done |
| P1 | GAP-02: get_exit_calls tool | ✅ Done |
| P1 | GAP-03: compare_snapshots token budget | ✅ Done |
| P1 | GAP-04: recurring incident detection | ✅ Done |
| P2 | GAP-05: stitch_async_trace search fields | ✅ Done |
| P2 | GAP-06: correlate_eum_to_bt diagnostic | ✅ Done |
| P2 | GAP-09: get_agent_status tool | ✅ Done |
| P2 | GAP-10: spec_url SSRF validation | ✅ Done |
| P3 | GAP-07: consistent caching | ✅ Done |
| P3 | GAP-08: weighted rate limiting | ✅ Done (closed by SCALE-04 — tool weights implemented in utils/rate_limiter.py) |
| P3 | GAP-11: audit log file persistence | ✅ Done |
| P3 | GAP-12: rate limiter in health | ✅ Done |
| P3 | GAP-13: tool_results in runbook | ✅ Done |

### Scale & multi-tenancy gaps
| Priority | Item | Status |
|----------|------|--------|
| P0 | SCALE-01: HTTP/SSE transport | ✅ Done |
| P0 | SCALE-02: paginated + filtered list_applications | ✅ Done |
| P0 | SCALE-03: team-to-app scoping | ✅ Done |
| P1 | SCALE-04: rate limit tuning + tool weights | ✅ Done |
| P1 | SCALE-05: cache sizing for 3000 apps | ✅ Done |
| P1 | SCALE-07: asyncio.gather in stitch_async_trace | ✅ Done |
| P1 | SCALE-08: get_team_health_summary aggregate tool | ✅ Done |
| P2 | SCALE-06: per-team credential isolation | 🔶 Partially closed — user-facing concern solved by ENH-007 (per-user RBAC app scoping); audit team attribution fixed (2026-04-17); per-team OAuth tokens (defense-in-depth) still deferred |

### Identity-scoped application access
| Priority | Item | Status |
|----------|------|--------|
| P0 | ENH-007: per-user app scoping via AppD RBAC | ✅ Done |

---

## Concurrency Gaps

Identified 2026-04-17. These gaps affect the server's ability to handle concurrent
load from multiple users (10–200+ simultaneous sessions).

### CONC-01 — CPU-bound parsers block the asyncio event loop
**File**: `parsers/snapshot_parser.py`, `main.py` `analyze_snapshot()`, `stitch_async_trace()`
**Impact**: HIGH — one heavy `analyze_snapshot` call on a large trace (500+ frames,
.NET or Java) blocks every other in-flight request until it finishes. asyncio is
single-threaded; synchronous CPU work inside an `async` handler is a full event-loop
stall, not just a slow response.
**Affected tools**: `analyze_snapshot`, `compare_snapshots`, `stitch_async_trace`
(multi-service fan-out with per-app parsing), `.NET`/Java stack trace parsing.
**Fix**: Wrap the CPU-heavy parser calls in `asyncio.to_thread()`:
```python
result = await asyncio.to_thread(parse_snapshot_detail, raw)
```
This offloads the work to a thread pool (default: `min(32, cpu_count+4)` threads),
freeing the event loop to serve other requests while parsing runs.

### CONC-02 — RBAC cache uses a single global lock across all UPNs
**File**: `services/user_resolver.py:_cache_lock`
**Impact**: MEDIUM — at 9am when 50 SREs simultaneously start sessions, each RBAC
lookup (user → groups → roles → apps = 3–5 serial HTTP calls to AppD RBAC) is
serialised behind one `asyncio.Lock`. The 50th user could wait several minutes before
their lookup even starts. Different UPNs have no data dependency on each other and
should resolve in parallel.
**Fix**: Per-UPN locks using a `defaultdict`:
```python
_upn_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

async with _upn_locks[cache_key]:
    # only same-UPN requests serialised; different UPNs run concurrently
```
The global `_cache_lock` can be retired; the per-UPN lock covers both the cache
read-check and the write-back atomically without blocking other users.

### CONC-03 — Multiple uvicorn workers break all shared in-process state
**Impact**: HIGH for horizontal scaling — all shared state is in-process memory:
- `_golden_registry`, `_bt_registry`, `_apps_registry` (registries)
- Rate limiter buckets (`_global_bucket`, `_user_buckets`, `_team_buckets`)
- `_sessions` dict in `auth/appd_auth.py` (user role cache)
- `_app_access_cache` in `services/user_resolver.py` (RBAC app-access cache)

Running 2+ uvicorn workers (the natural way to use multiple CPU cores) gives each
worker its own independent copy of all of this. Rate limits are per-worker (not
per-server), cache invalidations don't cross workers, RBAC cache is duplicated.
**Fix**: Extract shared mutable state to Redis:
- Rate limiter buckets → Redis sorted sets or Lua scripts (atomic token consume)
- `_sessions` role cache → Redis hash with TTL
- `_app_access_cache` RBAC cache → Redis hash with TTL
- Registries → Redis hash (or keep per-worker and accept eventual consistency)

The AppD data cache (diskcache L2) already survives restarts and is file-based,
so it doesn't need Redis — only the in-memory coordination state does.

---

### Concurrency capacity summary (single process, current state)

| Concurrent users | Behaviour |
|-----------------|-----------|
| 1–10 | No issues |
| 10–50 | Fine for read-heavy workloads; CPU parsers are the risk on large traces |
| 50–200 | RBAC cold-start contention at peak hours (CONC-02); parser blocking noticeable |
| 200+ | AppD's own API rate limits become the ceiling before the MCP does |

### Concurrency gaps priority
| Priority | Item | Status |
|----------|------|--------|
| P1 | CONC-01: CPU parsers block event loop (`asyncio.to_thread`) | ✅ Done |
| P1 | CONC-02: Single global RBAC lock (per-UPN locks) | ✅ Done |
| P2 | CONC-03: Multi-worker shared state (Redis extraction) | ⬜ Future |

**Caching strategy decision (2026-04-17):**
Current L1/L2 model (TTLCache in-process + diskcache file-based) is the right
choice for a single-process deployment serving up to ~200 concurrent users. Redis
is deferred until multi-worker or multi-replica scaling is required. Rationale:
diskcache is zero infrastructure, survives restarts, and already handles the
persistence requirement. Redis adds operational overhead (separate service to
deploy, monitor, secure) that is not justified until CONC-03 is a real bottleneck.
When Redis is adopted, only shared mutable state needs to move (rate limiter
buckets, session role cache, RBAC app-access cache) — the AppD data cache
(diskcache L2) stays file-based.
