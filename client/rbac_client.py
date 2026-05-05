"""
client/rbac_client.py

AppDynamics RBAC Admin API client.

Separate from AppDClient — different auth token (admin-level), different API
surface (/controller/api/rbac/v1/), different error semantics (fail closed).

Design decisions:
- Uses its own TokenManager backed by the rbacVaultPath credential.
  The RBAC account must have Account Owner or equivalent read-only admin
  privileges to call /controller/api/rbac/v1/*.
- All methods fail closed: any exception returns an empty result rather than
  raising, so user_resolver can union an empty set and deny access cleanly.
- No tenacity retry on 4xx — RBAC errors are deterministic.
- Results are intentionally raw dicts; user_resolver owns the business logic
  of traversing users → groups → roles → apps.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from auth.appd_auth import TokenManager

logger = logging.getLogger(__name__)


class RBACClient:
    """
    Thin async client for the AppDynamics RBAC admin REST API.

    All GET methods return raw dicts/lists on success, or empty
    fallbacks on any error (fail closed).
    """

    def __init__(self, base_url: str, token_manager: TokenManager) -> None:
        self._base = base_url.rstrip("/")
        self._tm = token_manager
        self._http = httpx.AsyncClient(timeout=15.0)

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Core request
    # ------------------------------------------------------------------

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        token = await self._tm.get_token()
        url = f"{self._base}/controller/api/rbac/v1{path}"
        try:
            resp = await self._http.get(
                url,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
            if resp.status_code == 401:
                token = await self._tm.handle_401()
                resp = await self._http.get(
                    url,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error("[rbac] GET %s failed: %s", path, exc)
            return None

    # ------------------------------------------------------------------
    # User lookup
    # ------------------------------------------------------------------

    async def get_user_by_name(self, username: str) -> dict[str, Any] | None:
        """
        Find a user by name or email prefix.

        AppD RBAC API: GET /controller/api/rbac/v1/users?name={username}
        Returns the first matching user record, or None.

        Response shape:
          { "users": [ { "id": 1, "name": "alice", "emails": [...],
                         "roles": [{"id":1,"name":"SRE"}],
                         "groups": [{"id":5,"name":"payments-team"}] } ] }
        """
        data = await self._get("/users", params={"name": username})
        if not data:
            return None
        users: list[dict[str, Any]] = data.get("users", [])
        if not users:
            logger.warning("[rbac] No user found for name=%s", username)
            return None
        return users[0]

    # ------------------------------------------------------------------
    # Role lookup
    # ------------------------------------------------------------------

    async def get_role(self, role_id: int) -> dict[str, Any] | None:
        """
        Fetch a role by ID.

        AppD RBAC API: GET /controller/api/rbac/v1/roles/{id}
        Returns role record with applicationPermissions, or None.

        Response shape:
          { "id": 1, "name": "SRE-View",
            "applicationPermissions": [
              { "applicationName": "PaymentService",
                "canView": true, "canConfigure": false, "canDelete": false }
            ] }
        """
        data = await self._get(f"/roles/{role_id}")
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # Group lookup
    # ------------------------------------------------------------------

    async def get_group(self, group_id: int) -> dict[str, Any] | None:
        """
        Fetch a group by ID to get its roles.

        AppD RBAC API: GET /controller/api/rbac/v1/groups/{id}
        Returns group record with roles list, or None.

        Response shape:
          { "id": 5, "name": "payments-team",
            "roles": [ {"id": 2, "name": "Payments-SRE"} ] }
        """
        data = await self._get(f"/groups/{group_id}")
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # Health check — used by get_server_health
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        """Return True if the RBAC API is reachable with current credentials."""
        data = await self._get("/users", params={"name": "__ping__", "limit": "1"})
        return data is not None
