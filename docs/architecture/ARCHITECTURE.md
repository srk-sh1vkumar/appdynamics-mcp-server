# Architecture Overview

## System Architecture Diagram

```mermaid
graph TB
    %% ── External actors ────────────────────────────────────────────────────
    subgraph Clients["🖥️ MCP Clients"]
        CD[Claude Desktop]
        CU[Cursor IDE]
    end

    subgraph External["☁️ External Systems"]
        AppD[AppDynamics SaaS Controller\nREST API]
        Vault[HashiCorp Vault\nKV Secrets Engine]
        VaultMock[MockVaultClient\nenv vars — dev mode]
    end

    subgraph Monitoring["📊 Monitoring Hub"]
        Prom[Prometheus\nport 9091]
        Grafana[Grafana\nport 3002]
    end

    %% ── MCP Server ──────────────────────────────────────────────────────────
    subgraph MCP["🐍 AppDynamics MCP Server  •  Python 3.13  •  stdio transport"]

        subgraph Entry["Entry Point"]
            Main[main.py\n29 @mcp.tool() functions\n16-step system prompt]
        end

        subgraph AuthLayer["🔐 Auth"]
            VM[vault_client.py\nMock / HashiCorp]
            TM[appd_auth.py\nTokenManager + RBAC]
        end

        subgraph ClientLayer["🌐 HTTP Client"]
            AC[appd_client.py\nhttpx async\nretry + 401 refresh]
        end

        subgraph ParseLayer["🔍 Parsers"]
            SP[snapshot_parser.py\nlanguage detect\nsmoking gun\nbaseline scoring]
            JP[java.py]
            NP[nodejs.py]
            PP[python_parser.py]
            DP[dotnet.py]
        end

        subgraph SvcLayer["⚙️ Services"]
            BT[bt_classifier.py\ncriticality scoring]
            LC[license_check.py\ntool gating]
            RG[runbook_generator.py\nJSON runbooks]
            HS[health.py\nliveness + metrics]
            CI[cache_invalidator.py\nevent-driven invalidation]
        end

        subgraph CacheLayer["💾 Cache & Registries"]
            CA[cache.py\nTwoLayerCache\nper-type TTLCache L1\ndiskcache L2\nCachedSnapshotAnalysis]
            CK[cache_keys.py\ncentralised key builder\nUPN-namespaced]
            AR[apps_registry.py\nAppEntry\nL1 TTLCache + L2 disk]
            BR[bt_registry.py\nBTEntry\nL1 TTLCache + L2 disk]
            GR[golden_registry.py\nGoldenSnapshot\n24h TTL\nshared across users]
        end

        subgraph UtilLayer["🛠️ Utils"]
            RL[rate_limiter.py\ntoken bucket\n10 req/s global\n5 req/s per-user]
            SN[sanitizer.py\nPII redact\nprompt injection guard]
            TZ[timezone.py]
            ME[metrics.py\nPrometheus counters]
        end

        subgraph Models["📐 Models"]
            TY[types.py\nenums + dataclasses\nPydantic models]
        end

        subgraph Probe["🏥 HTTP — port 8080"]
            HP[GET /health\n→ status ok]
            MP[GET /metrics\n→ Prometheus text]
        end
    end

    %% ── Connections ─────────────────────────────────────────────────────────
    CD -->|stdin / stdout\nMCP protocol| Main
    CU -->|stdin / stdout\nMCP protocol| Main

    Main --> RL
    Main --> TM
    Main --> LC
    Main --> CA
    Main --> CK
    Main --> AC
    Main --> SP
    Main --> BT
    Main --> RG
    Main --> SN
    Main --> ME
    Main --> AR
    Main --> BR
    Main --> GR
    Main --> CI

    CI --> BR
    CI --> GR
    CI --> CA

    SP --> JP
    SP --> NP
    SP --> PP
    SP --> DP

    TM --> VM
    VM -->|VAULT_MODE=hashicorp| Vault
    VM -->|VAULT_MODE=mock| VaultMock
    TM -->|Bearer token| AC
    AC -->|REST API calls| AppD

    HS --> HP
    HS --> MP
    ME --> MP

    Prom -->|scrape :8080/metrics| MP
    Grafana -->|query| Prom
```

---

## Module Map

```
main.py                       ← FastMCP server entry point (29 tools)
│
├── auth/
│   ├── vault_client.py       ← Credential fetching (Mock + HashiCorp Vault)
│   └── appd_auth.py          ← TokenManager, RBAC permission gating
│
├── client/
│   └── appd_client.py        ← httpx async client for AppDynamics REST API
│
├── parsers/
│   ├── snapshot_parser.py    ← Language detection, smoking gun, baseline scoring
│   └── stack/
│       ├── java.py
│       ├── nodejs.py
│       ├── python_parser.py
│       └── dotnet.py
│
├── services/
│   ├── bt_classifier.py      ← BT criticality scoring, health-check filtering
│   ├── cache_invalidator.py  ← Event-driven invalidation (deployment/restart/manual)
│   ├── license_check.py      ← Module license detection + tool gating
│   ├── runbook_generator.py  ← JSON runbook output + recurring detection
│   └── health.py             ← HealthStatus aggregation + K8s liveness probe
│
├── registries/
│   ├── apps_registry.py      ← AppEntry + AppsRegistry (TTLCache L1 + diskcache L2)
│   ├── bt_registry.py        ← BTEntry + BTRegistry (TTLCache L1 + diskcache L2)
│   └── golden_registry.py    ← GoldenSnapshot + GoldenRegistry (24h TTL, shared)
│
├── models/
│   └── types.py              ← Enums, dataclasses, Pydantic models
│
└── utils/
    ├── cache.py              ← TwoLayerCache + CachedSnapshotAnalysis + module-level API
    ├── cache_keys.py         ← Centralised UPN-namespaced key builder
    ├── rate_limiter.py       ← Token bucket: global 10 req/s, per-user 5 req/s
    ├── sanitizer.py          ← PII redaction + prompt injection protection
    └── timezone.py           ← UTC normalisation, human-readable formatting
```

## Request Lifecycle

```
MCP host (Claude Desktop / Cursor)
    │  stdin/stdout (stdio transport)
    ▼
main.py @mcp.tool()
    │
    ├─► check_and_wait(upn)              ← rate limiter (token bucket)
    ├─► _get_role(upn, controller)       ← RBAC check via AppD API (cached 30min)
    ├─► require_permission(role, ...)    ← fail-closed gate
    ├─► license_check.require_license() ← gate for EUM/Analytics/Snapshots
    │
    ├─► cache_mod.get(key, upn)          ← L1: per-type TTLCache, L2: diskcache
    │       hit → return cached
    │       miss ↓
    │
    ├─► get_client(controller_name)      ← AppDClient (httpx, retry, auth)
    │       └─► AppD REST API
    │
    ├─► [deployment detection]           ← get_business_transactions
    │       if BT count shifts > 2 vs cached:
    │           _cache_invalidator.on_deployment_detected()
    │               ├─► bt_registry.invalidate(controller, app)
    │               └─► golden_registry.invalidate_app(controller, app)
    │
    ├─► [restart detection]              ← get_health_violations
    │       if APP_CRASH or NODE_RESTART in violations:
    │           _cache_invalidator.on_app_restart_detected()
    │               └─► golden_registry.invalidate_app(controller, app)
    │
    ├─► registry updates                 ← apps_registry / bt_registry
    ├─► cache_mod.set(key, value, ttl)
    ├─► sanitize_and_wrap(data)          ← PII redaction + <appd_data> wrap
    ├─► truncate_to_budget(out, tool)    ← token budget enforcement
    ├─► audit_log(tool, upn, ...)        ← structured JSON to stderr
    │
    └─► return str to MCP host
```

## Cache Design

### Two-layer architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  utils/cache.py  (module-level API — used by all 29 tools)       │
│  utils/cache.py  TwoLayerCache class — structured/validated data │
│                                                                    │
│  L1: per-data-type TTLCache (in-process, asyncio.Lock protected)  │
│  L2: diskcache.Cache (file-backed, survives restarts)             │
└──────────────────────────────────────────────────────────────────┘
           │ write-through on miss
           ▼
┌──────────────────────────────────────────────────────────────────┐
│  registries/ (dedicated per-entity stores)                        │
│                                                                    │
│  apps_registry.py  — AppEntry list per controller                 │
│  bt_registry.py    — BTEntry list per (controller, app)           │
│  golden_registry.py— GoldenSnapshot per (controller, app, bt)     │
│                       24h TTL, shared across users (no UPN)       │
└──────────────────────────────────────────────────────────────────┘
           │ event-driven invalidation
           ▼
┌──────────────────────────────────────────────────────────────────┐
│  services/cache_invalidator.py                                    │
│  on_deployment_detected  → bt_registry + golden_registry          │
│  on_app_restart_detected → golden_registry only                   │
│  on_manual_golden_override → single BT golden entry               │
│  on_cache_validation_failure → evict corrupt key                  │
└──────────────────────────────────────────────────────────────────┘
```

### Per-data-type TTL and maxsize

| Data type | L1 TTL | L1 maxsize | Persist to disk |
|-----------|--------|------------|-----------------|
| `applications` | 300s | 100 | optional |
| `business_transactions` | 300s | 500 | optional |
| `metric_tree` | 600s | 1000 | optional |
| `metric_values` | 60s | 500 | optional |
| `health_violations` | 30s | 200 | optional |
| `user_roles` | 1800s | 200 | optional |
| `snapshot_list` | 30s | 200 | optional |
| `parsed_snapshot` | 3600s | 100 | never (in-memory only) |

### NEVER_CACHE types

The following data types must never be cached (runtime guard in `NEVER_CACHE`):

- `raw_snapshot_json` — raw payload is ~500 KB, immutable only within a GUID's lifetime
- `adql_query_results` — analytics queries are time-ranged; staleness is unacceptable
- `active_health_violations_realtime` — must always be fresh

### Key format

```
{upn}:{controller}:{data_type}:{identifier...}
```

UPN is always the first segment — prevents cross-user data leakage if keys are logged.
All segments are lowercased and spaces are replaced with `_`.

**Exception**: `golden_key()` uses `__golden__:{controller}:{app}:{bt}` — no UPN,
because golden baselines are shared across all users of the same app.

### CachedSnapshotAnalysis

Stores the parsed, PII-redacted result of snapshot analysis — not the raw JSON.

- Only the derived fields are stored: `language_detected`, `error_details`, `hot_path`,
  `top_call_segments`, `culprit_frame`, `caused_by_chain`.
- TTL: 3600s (GUIDs are immutable — content never changes).
- Persisted in-memory only (`persist_to_disk=False`).
- Pydantic validation on every read; corrupt entries are automatically evicted.

### Pydantic validation on cache reads

`TwoLayerCache._try_validate()` applies on every L1/L2 read:

| Cached value | Behaviour |
|---|---|
| `dict` matching model schema | Validated, returned as model instance |
| `dict` failing model schema | Evicted + warning logged, fetch fresh |
| `list`, model instance, other | Passed through as-is (cannot schema-validate) |

### Golden Registry

`GoldenRegistry` maintains the 24-hour golden baseline per BT:

- In-memory dict mirror for O(1) reads during active investigations.
- `diskcache` persistence for crash-safe recovery across restarts.
- Invalidated by: deployment detection, app restart, manual `set_golden_snapshot` override, or TTL age.
- `get_stats()` reports `total_entries`, `entries_expiring_soon`, `manually_promoted`.

## Auth Flow

```
Startup
  └─► VaultClient.get_credentials(vault_path)
        └─► TokenManager.initialise()
              └─► POST /controller/api/oauth/access_token
                    └─► stores access_token + expiry

Per-request
  └─► TokenManager.get_token()
        ├─► if expired: refresh via OAuth
        └─► return Bearer token → injected into httpx request headers
```

## License Degradation

At startup, the server probes the controller and sets a `DegradationMode`:

| Mode | Condition | Gated tools |
|------|-----------|-------------|
| `FULL` | All modules licensed | none |
| `NO_SNAPSHOTS` | APM Pro missing | `list_snapshots`, `analyze_snapshot`, `compare_snapshots`, `archive_snapshot` |
| `NO_ANALYTICS` | Analytics missing | `query_analytics_logs` |
| `NO_EUM` | EUM missing | `get_eum_*`, `correlate_eum_to_bt` |

Gated tools raise `RuntimeError` with a clear message; the MCP host surfaces it as an error to the user.

## Security Invariants

1. **Read-only** — only GET requests (+ one Analytics POST). No AppD write operations.
2. **Fail-closed** — any exception in RBAC or license check rejects the call.
3. **PII redaction** — every tool output passes through `sanitize_and_wrap()` before return.
4. **Prompt injection** — AppD data is always wrapped in `<appd_data>...</appd_data>`.
5. **UPN-namespaced cache** — cross-user data leakage is structurally impossible. All user-scoped cache keys start with UPN as the first segment.
6. **No shell execution** — no `subprocess`, no `os.system`.
7. **NEVER_CACHE enforcement** — `raw_snapshot_json`, `adql_query_results`, and `active_health_violations_realtime` are statically excluded from caching. Raw snapshot JSON (~500 KB) is never stored; only the parsed, PII-redacted `CachedSnapshotAnalysis` is kept.
8. **Golden baseline isolation** — `GoldenRegistry` keys use `__golden__:` prefix (no UPN) and are shared read-only across users; writes require explicit `set_golden_snapshot` tool call with full audit trail.
