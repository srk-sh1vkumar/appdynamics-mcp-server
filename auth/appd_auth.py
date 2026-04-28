"""
auth/appd_auth.py

OAuth2 token lifecycle and AppDynamics tool permission gates.

Two modes (controlled by APPDYNAMICS_MODE env var, default: enterprise):

  enterprise   — VaultClient credentials, AppD RBAC role lookup via
                 RBACClient, per-app access filtering, enforcing gates.

  single_user  — SimpleCredentials (env vars), caller is always
                 CONFIGURE_ALERTING, gates enforce but trivially pass.

Token lifecycle:
- TokenManager accepts either a VaultClient (enterprise) or a SimpleCredentials
  instance (single-user). Duck-typed: if get_credentials is async → vault path.
- Proactive refresh at T-30min via background asyncio task.
- On 401: re-fetch credentials and retry once.

Permission model:
- require_permission() is always enforcing. In single-user mode it passes
  trivially because get_user_role() always returns CONFIGURE_ALERTING, which
  has access to every tool.
- Fail closed: any error in role lookup → AppDRole.DENIED.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from models.types import AppDRole, TokenCache

if TYPE_CHECKING:
    from client.rbac_client import RBACClient

TOKEN_VALIDITY_S = 6 * 3600
REFRESH_BEFORE_S = 30 * 60
SESSION_TTL_S = 1800


# ---------------------------------------------------------------------------
# Token manager — supports both VaultClient and SimpleCredentials
# ---------------------------------------------------------------------------


class TokenManager:
    def __init__(
        self, creds: Any, controller_name: str, token_url: str, account: str = ""
    ) -> None:
        self._creds = creds
        self._controller_name = controller_name
        self._token_url = token_url
        self._account = account
        self._cache: TokenCache | None = None
        self._lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None

    async def initialise(self) -> None:
        """Fetch initial token and start background refresh task."""
        await self._refresh()
        self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def get_token(self) -> str:
        """Return a valid access token (refresh is proactive, not lazy)."""
        async with self._lock:
            if self._cache is None:
                await self._refresh()
            assert self._cache is not None
            return self._cache.access_token

    async def handle_401(self) -> str:
        """Re-fetch credentials and token on 401. Retry once."""
        async with self._lock:
            self._cache = None
            await self._refresh()
            assert self._cache is not None
            return self._cache.access_token

    async def _refresh(self) -> None:
        import base64

        import httpx

        # Duck-type dispatch: VaultClient.get_credentials is async; SimpleCredentials is sync.
        if asyncio.iscoroutinefunction(getattr(self._creds, "get_credentials", None)):
            from auth.vault_client import fetch_credentials_with_retry as _fetch
        else:
            from auth.simple_credentials import fetch_credentials_with_retry as _fetch

        raw = await _fetch(self._creds, self._controller_name)

        qualified_id = (
            f"{raw.client_id}@{self._account}" if self._account else raw.client_id
        )
        basic = base64.b64encode(
            f"{qualified_id}:{raw.client_secret}".encode()
        ).decode()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                self._token_url,
                headers={"Authorization": f"Basic {basic}"},
                data={
                    "grant_type": "client_credentials",
                    "client_id": qualified_id,
                    "client_secret": raw.client_secret,
                },
            )
            resp.raise_for_status()
            body = resp.json()

        token = body.get("access_token")
        if not token:
            raise RuntimeError(f"[auth] No access_token in response: {body}")

        expires_in = int(body.get("expires_in", TOKEN_VALIDITY_S))
        now = datetime.now(tz=UTC)
        self._cache = TokenCache(
            access_token=token,
            expires_at=now + timedelta(seconds=expires_in),
            refresh_scheduled_at=now + timedelta(seconds=expires_in - REFRESH_BEFORE_S),
        )
        print(
            f"[auth] Token refreshed for controller '{self._controller_name}'. "
            f"Expires at {self._cache.expires_at.isoformat()}",
            file=sys.stderr,
        )

    async def _refresh_loop(self) -> None:
        """Background task: sleep until refresh window, then refresh silently."""
        while True:
            if self._cache:
                now = datetime.now(tz=UTC)
                sleep_s = max(
                    0,
                    (self._cache.refresh_scheduled_at - now).total_seconds(),
                )
                await asyncio.sleep(sleep_s)
                try:
                    async with self._lock:
                        await self._refresh()
                except Exception as exc:
                    print(
                        f"[auth] Background token refresh failed: {exc}",
                        file=sys.stderr,
                    )
                    if self._cache:
                        remaining = (
                            self._cache.expires_at - datetime.now(tz=UTC)
                        ).total_seconds()
                        print(
                            f"[auth] Existing token valid for {remaining:.0f}s",
                            file=sys.stderr,
                        )
                    await asyncio.sleep(60)
            else:
                await asyncio.sleep(10)

    def token_expiry_human(self) -> str:
        if not self._cache:
            return "no token"
        remaining = (self._cache.expires_at - datetime.now(tz=UTC)).total_seconds()
        remaining = max(0, remaining)
        h = int(remaining // 3600)
        m = int((remaining % 3600) // 60)
        return f"{h}h {m}m"


# ---------------------------------------------------------------------------
# Role — mode-aware: session cache + RBAC lookup in enterprise,
#                    always CONFIGURE_ALERTING in single-user.
# ---------------------------------------------------------------------------

_sessions: dict[str, tuple[AppDRole, float]] = {}  # upn → (role, cached_at)


def _map_appd_role(roles: list[str]) -> AppDRole:
    """Map AppD role name strings to the three-tier permission model."""
    lowered = [r.lower() for r in roles]
    admin_kws = ("administrator", "configure_alerting", "alerting", "admin")
    if any(kw in r for r in lowered for kw in admin_kws):
        return AppDRole.CONFIGURE_ALERTING
    tshoot_kws = ("troubleshoot", "sre", "devops", "debug", "engineer")
    if any(kw in r for r in lowered for kw in tshoot_kws):
        return AppDRole.TROUBLESHOOT
    if roles:
        return AppDRole.VIEW
    return AppDRole.DENIED


async def get_user_role(
    upn: str,
    rbac_client: "RBACClient | None" = None,
) -> AppDRole:
    """
    Single-user mode (rbac_client=None): always CONFIGURE_ALERTING.
    Enterprise mode (rbac_client provided): look up AppD RBAC role, cached per session.
    Fail closed — any error returns DENIED.
    """
    if rbac_client is None:
        return AppDRole.CONFIGURE_ALERTING

    cached = _sessions.get(upn)
    if cached and (time.time() - cached[1]) < SESSION_TTL_S:
        return cached[0]

    try:
        user = await rbac_client.get_user_by_name(upn)
        role_names: list[str] = (
            [r.get("name", "") for r in user.get("roles", [])] if user else []
        )
        role = _map_appd_role(role_names)
    except Exception as exc:
        print(f"[auth] Role lookup failed for {upn}: {exc}. Denying.", file=sys.stderr)
        return AppDRole.DENIED

    _sessions[upn] = (role, time.time())
    return role


def invalidate_session(upn: str) -> None:
    """Invalidate cached session on mid-session 403 or explicit refresh."""
    _sessions.pop(upn, None)


# ---------------------------------------------------------------------------
# Tool permission gates
# ---------------------------------------------------------------------------

_VIEW_TOOLS: frozenset[str] = frozenset({
    "list_controllers", "list_applications", "search_metric_tree",
    "get_metrics", "get_business_transactions", "get_bt_baseline",
    "load_api_spec", "get_health_violations", "get_eum_overview",
    "get_eum_page_performance", "get_eum_js_errors", "get_eum_ajax_requests",
    "get_eum_geo_performance", "get_infrastructure_stats",
    "get_jvm_details", "get_network_kpis", "get_server_health",
    "get_tiers_and_nodes", "get_agent_status", "get_bt_detection_rules",
    "get_team_health_summary", "list_application_events",
})

_TROUBLESHOOT_TOOLS: frozenset[str] = _VIEW_TOOLS | frozenset({
    "list_snapshots", "analyze_snapshot", "compare_snapshots",
    "get_errors_and_exceptions", "get_database_performance",
    "stitch_async_trace", "correlate_eum_to_bt", "query_analytics_logs",
    "get_exit_calls", "save_runbook", "correlate_incident_window",
})

_CONFIGURE_ALERTING_TOOLS: frozenset[str] = _TROUBLESHOOT_TOOLS | frozenset({
    "get_policies", "archive_snapshot", "set_golden_snapshot",
})

_ROLE_TOOLS: dict[AppDRole, frozenset[str]] = {
    AppDRole.VIEW: _VIEW_TOOLS,
    AppDRole.TROUBLESHOOT: _TROUBLESHOOT_TOOLS,
    AppDRole.CONFIGURE_ALERTING: _CONFIGURE_ALERTING_TOOLS,
    AppDRole.DENIED: frozenset(),
}


def require_permission(role: AppDRole, tool_name: str) -> None:
    """Raise PermissionError if the role cannot call this tool."""
    allowed = _ROLE_TOOLS.get(role, frozenset())
    if tool_name not in allowed:
        min_role = (
            AppDRole.VIEW if tool_name in _VIEW_TOOLS
            else AppDRole.TROUBLESHOOT if tool_name in _TROUBLESHOOT_TOOLS
            else AppDRole.CONFIGURE_ALERTING
        )
        raise PermissionError(
            f"Tool '{tool_name}' requires {min_role.value} permission. "
            f"Your AppDynamics role: {role.value}."
        )
