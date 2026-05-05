# Single-User Mode — Design Document

**Branch:** `single-user-mode`
**Date:** 2026-04-21
**Status:** Design / Pre-implementation

---

## 1. Problem Statement

The enterprise edition of this server assumes a multi-user, multi-team deployment
with HashiCorp Vault, two service accounts per controller (data + RBAC admin), and
per-user application scoping via AppD RBAC. This is the right model for an org-wide
shared MCP deployment.

However, a meaningful second audience exists: **individual AppDynamics admins** who
want to connect their own LLM (Claude Desktop, Cursor) directly to a controller they
already have full access to. For this user, Vault is unavailable, RBAC scoping is
irrelevant (they see everything by design), and the enterprise auth pipeline is
unnecessary complexity.

**Goal:** A stripped-down edition of the same 29-tool server that works with a single
OAuth2 client ID/secret in a `.env` file. Zero Vault. Zero RBAC. Zero team config.
Five minutes from clone to working.

---

## 2. What Changes vs. Enterprise Edition

| Layer | Enterprise (`main`) | Single-User (`single-user-mode`) |
|-------|--------------------|---------------------------------|
| Credentials | HashiCorp Vault (`vault_client.py`) | Env vars directly (no Vault layer) |
| Auth mode | `VAULT_MODE=mock\|hashicorp` | Removed — always env var |
| RBAC account | Required (`rbacVaultPath`) | Removed entirely |
| User identity | UPN → RBAC traversal → app frozenset | UPN optional; all apps visible |
| Team scoping | `teams` block in `controllers.json` | Removed |
| `_require_app_access()` | Enforces per-user app set | No-op / removed |
| `user_resolver.py` | Core RBAC traversal service | Removed |
| `rbac_client.py` | RBAC admin API client | Removed |
| `team_registry.py` | Team-to-UPN-domain mapping | Removed |
| `vault_client.py` | Mock + HashiCorp implementations | Replaced with thin env-var reader |
| 29 tools | Full set | Identical — no changes |
| Parsers, cache, rate limiter, sanitizer | Unchanged | Identical — no changes |

**Files removed in this branch:**
- `auth/vault_client.py` → replaced by `auth/simple_credentials.py`
- `client/rbac_client.py` → deleted
- `services/user_resolver.py` → deleted
- `services/team_registry.py` → deleted

**Files modified in this branch:**
- `auth/appd_auth.py` → remove `require_permission` RBAC gate; simplify `get_role` to always return CONFIGURE_ALERTING (admin has full access)
- `main.py` → remove `_require_app_access()` calls, remove `_rbac_clients` init, remove `refresh_user_access` tool, simplify startup
- `controllers.json` → remove `vaultPath` / `rbacVaultPath` fields; credentials come from env vars
- `.env.example` → simplified — only controller URL + client ID/secret needed
- `README.md` → replaced with single-user quickstart guide

**Files unchanged:**
- All 29 tool handlers (logic is identical)
- `parsers/` (all 5 parser files)
- `registries/` (all 3 registries)
- `services/bt_classifier.py`, `runbook_generator.py`, `health.py`, `license_check.py`, `cache_invalidator.py`, `bt_naming.py`
- `utils/cache.py`, `cache_keys.py`, `rate_limiter.py`, `sanitizer.py`, `timezone.py`, `metrics.py`
- `models/types.py`
- `client/appd_client.py`
- `Dockerfile`, `pyproject.toml`, `uv.lock`

---

## 3. New Auth Layer: `auth/simple_credentials.py`

Replaces `auth/vault_client.py`. Reads credentials directly from environment
variables with no abstraction layer.

### Credential convention

Env var names are derived from the controller name in `controllers.json`:

```
Controller name: "production"
→ APPDYNAMICS_PRODUCTION_CLIENT_ID
→ APPDYNAMICS_PRODUCTION_CLIENT_SECRET

Controller name: "staging"
→ APPDYNAMICS_STAGING_CLIENT_ID
→ APPDYNAMICS_STAGING_CLIENT_SECRET
```

### Interface (matches existing `VaultClient` interface for drop-in replacement)

```python
class SimpleCredentials:
    """Read OAuth2 credentials directly from environment variables.

    Drop-in replacement for VaultClient in single-user deployments.
    No Vault dependency. No secret rotation. No namespace support.
    """

    def get_client_id(self, controller_name: str) -> str:
        key = f"APPDYNAMICS_{controller_name.upper()}_CLIENT_ID"
        value = os.environ.get(key, "")
        if not value:
            raise CredentialError(f"Missing env var: {key}")
        return value

    def get_client_secret(self, controller_name: str) -> str:
        key = f"APPDYNAMICS_{controller_name.upper()}_CLIENT_SECRET"
        value = os.environ.get(key, "")
        if not value:
            raise CredentialError(f"Missing env var: {key}")
        return value
```

The existing `TokenManager` in `auth/appd_auth.py` already calls
`vault_client.get_client_id(controller_name)` — swapping `SimpleCredentials`
in requires no changes to `TokenManager`.

---

## 4. Simplified `controllers.json`

Enterprise edition requires `vaultPath` and `rbacVaultPath` per controller.
Single-user edition only needs connection config — credentials come from env vars.

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

No `vaultPath`, no `rbacVaultPath`, no `teams` block.

---

## 5. Simplified `.env`

```bash
# Controller credentials — one pair per controller
APPDYNAMICS_PRODUCTION_CLIENT_ID=your-oauth2-client-id
APPDYNAMICS_PRODUCTION_CLIENT_SECRET=your-oauth2-client-secret

# Optional: second controller
# APPDYNAMICS_STAGING_CLIENT_ID=...
# APPDYNAMICS_STAGING_CLIENT_SECRET=...

# Health probe port (optional, default 8080)
HEALTH_PORT=8080

# Transport: stdio (Claude Desktop) | sse | streamable-http (Cursor)
MCP_TRANSPORT=stdio
```

Six lines. That's the full setup for a single-user deployment.

---

## 6. Permission Model

In single-user mode, the calling user is always treated as having
`CONFIGURE_ALERTING` (the highest permission level). This is correct because:

- The user connected with their own admin OAuth2 client — they have full
  access to the controller by definition
- There is no multi-user scenario to protect against
- RBAC enforcement between users is meaningless when there is only one user

`auth/appd_auth.py` `get_role()` returns `AppDRole.CONFIGURE_ALERTING`
unconditionally. `require_permission()` becomes a no-op. The permission
gate code is removed rather than bypassed to keep the implementation clean.

The `refresh_user_access` tool is removed (no RBAC cache to refresh).

---

## 7. UPN Handling

The `upn` parameter remains on all tool signatures for two reasons:
1. Audit log continuity — the field exists in the log schema
2. Cache key namespacing — all cache keys include UPN as the first segment

In single-user mode, `upn` defaults to `"local@user"` if not provided. It is
not resolved against AppD RBAC. The user is not required to pass it.

---

## 8. Files to Create / Modify Summary

### New files
| File | Description |
|------|-------------|
| `auth/simple_credentials.py` | Env-var credential reader (replaces vault_client.py) |
| `docs/SINGLE_USER_MODE_DESIGN.md` | This document |

### Modified files
| File | Change |
|------|--------|
| `auth/appd_auth.py` | Remove RBAC traversal; `get_role()` always returns CONFIGURE_ALERTING; remove `require_permission` body |
| `main.py` | Remove `_rbac_clients`, `_require_app_access()`, `refresh_user_access` tool, RBAC startup init; use `SimpleCredentials` |
| `models/types.py` | Remove `rbac_vault_path` from `ControllerConfig`; remove `vaultPath` requirement |
| `controllers.json` | Remove vault path fields |
| `.env.example` | Simplified to 6 lines |
| `README.md` | Single-user quickstart (5-minute setup) |

### Deleted files
| File | Reason |
|------|--------|
| `auth/vault_client.py` | Replaced by `auth/simple_credentials.py` |
| `client/rbac_client.py` | No RBAC account in single-user mode |
| `services/user_resolver.py` | No per-user app scoping |
| `services/team_registry.py` | No team concept in single-user mode |

---

## 9. What Is NOT Changed

The following are identical to `main` and must stay in sync via cherry-pick or
periodic merge from `main`:

- All 29 tool handler bodies
- All parsers (`parsers/stack/`, `parsers/snapshot_parser.py`)
- All registries (`registries/`)
- All utilities (`utils/`)
- All services except `team_registry.py` and `user_resolver.py`
- `client/appd_client.py`
- `models/types.py` (except `ControllerConfig` vault fields)
- `Dockerfile`, `pyproject.toml`
- All tests (adjusted for removed files)

---

## 10. Sync Strategy with `main`

Since tool logic is shared, bug fixes and new tools added to `main` should
be cherry-picked into `single-user-mode`:

```bash
# Cherry-pick a specific commit from main
git cherry-pick <commit-sha>

# Or periodically merge main into single-user-mode (tools/parsers/utils only)
git merge main --no-commit
# Review: keep auth/ and main.py changes from single-user-mode
# Accept: tools, parsers, utils, registries from main
```

The divergence points are well-defined (auth layer + main.py startup), so
merges should be low-conflict in practice.

---

## 11. Out of Scope for This Branch

- Multi-user support (that's `main`)
- Vault integration
- Team-based app filtering
- RBAC-based access scoping
- Per-team rate limiting
- Redis / multi-worker scaling

---

*Ready to implement once this design is approved.*
