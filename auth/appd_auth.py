"""
auth/appd_auth.py

OAuth2 token lifecycle and AppDynamics user authorisation.

Design decisions:
- TokenManager uses an asyncio background task for proactive refresh at
  T-30min. This never blocks tool calls — the background task refreshes
  silently while tool calls read the cached token.
- On 401 from AppD: handle_401() re-fetches from Vault and retries once.
  If still 401 after retry: raise AuthenticationError.
- User sessions are cached 30 min per UPN. A mid-session 403 invalidates
  the entry and forces a re-lookup on the next call (fail closed).
- FAIL CLOSED: any error in role lookup returns AppDRole.DENIED.
  Never return a permissive role on failure.
- AppD is the sole source of truth for permissions — no rbac.json.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from auth.vault_client import VaultClient, fetch_credentials_with_retry
from models.types import AppDRole, TokenCache

if TYPE_CHECKING:
    from client.appd_client import AppDClient

TOKEN_VALIDITY_S = 6 * 3600
REFRESH_BEFORE_S = 30 * 60
SESSION_TTL_S = 1800


# ---------------------------------------------------------------------------
# Token manager
# ---------------------------------------------------------------------------


class TokenManager:
    def __init__(self, vault: VaultClient, vault_path: str, token_url: str) -> None:
        self._vault = vault
        self._vault_path = vault_path
        self._token_url = token_url
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
        import httpx

        creds = await fetch_credentials_with_retry(self._vault, self._vault_path)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                self._token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret,
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
            f"[auth] Token refreshed for {self._vault_path}. "
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
                    # Continue with existing token; log time remaining
                    if self._cache:
                        remaining = (
                            self._cache.expires_at - datetime.now(tz=UTC)
                        ).total_seconds()
                        print(
                            f"[auth] Existing token valid for {remaining:.0f}s",
                            file=sys.stderr,
                        )
                    await asyncio.sleep(60)  # retry in 1 minute
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
# User session / permission cache
# ---------------------------------------------------------------------------

_sessions: dict[str, tuple[AppDRole, float]] = {}  # upn → (role, cached_at)


def _map_appd_role(roles: list[str]) -> AppDRole:
    """Map AppD role strings to the three-tier permission model."""
    lowered = [r.lower() for r in roles]
    admin_kws = ("administrator", "configure_alerting", "alerting")
    if any(kw in r for r in lowered for kw in admin_kws):
        return AppDRole.CONFIGURE_ALERTING
    tshoot_kws = ("troubleshoot", "sre", "devops", "debug")
    if any(kw in r for r in lowered for kw in tshoot_kws):
        return AppDRole.TROUBLESHOOT
    if roles:
        return AppDRole.VIEW
    return AppDRole.DENIED


async def get_user_role(
    upn: str, appd_client: AppDClient, controller_name: str
) -> AppDRole:
    """
    Return the cached AppD role for a UPN, fetching from AppD if not cached.
    FAIL CLOSED on any error — never return a permissive role on failure.
    """
    cached = _sessions.get(upn)
    if cached and (time.time() - cached[1]) < SESSION_TTL_S:
        return cached[0]

    try:
        user_data = await appd_client.get_user_by_upn(upn, controller_name)
        roles: list[str] = user_data.get("roles", [])
        if not roles:
            return AppDRole.DENIED
        role = _map_appd_role(roles)
    except Exception as exc:
        print(f"[auth] Role lookup failed for {upn}: {exc}. Denying.", file=sys.stderr)
        return AppDRole.DENIED

    _sessions[upn] = (role, time.time())
    return role


def invalidate_session(upn: str) -> None:
    """Invalidate cached session on mid-session 403."""
    _sessions.pop(upn, None)


# ---------------------------------------------------------------------------
# Tool permission gates (Section 4.3)
# ---------------------------------------------------------------------------

_VIEW_TOOLS: frozenset[str] = frozenset({
    "list_controllers", "list_applications", "search_metric_tree",
    "get_metrics", "get_business_transactions", "get_bt_baseline",
    "load_api_spec", "get_health_violations", "get_eum_overview",
    "get_eum_page_performance", "get_eum_js_errors", "get_eum_ajax_requests",
    "get_eum_geo_performance", "get_infrastructure_stats",
    "get_jvm_details", "get_network_kpis", "get_server_health",
    "get_tiers_and_nodes", "get_agent_status", "get_bt_detection_rules",
    "get_team_health_summary",
})

_TROUBLESHOOT_TOOLS: frozenset[str] = _VIEW_TOOLS | frozenset({
    "list_snapshots", "analyze_snapshot", "compare_snapshots",
    "get_errors_and_exceptions", "get_database_performance",
    "stitch_async_trace", "correlate_eum_to_bt", "query_analytics_logs",
    "get_exit_calls", "save_runbook",
})

_CONFIGURE_ALERTING_TOOLS: frozenset[str] = _TROUBLESHOOT_TOOLS | frozenset({
    "get_policies", "archive_snapshot", "set_golden_snapshot",
    "refresh_user_access",
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
