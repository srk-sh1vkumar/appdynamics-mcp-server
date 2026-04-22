"""
auth/appd_auth.py

OAuth2 token lifecycle and AppDynamics tool permission gates.

Single-user mode: the caller is always treated as CONFIGURE_ALERTING
(full access). No role lookup is performed against AppDynamics — the
admin connected with their own credentials and sees everything.

Token lifecycle:
- TokenManager fetches credentials from SimpleCredentials (env vars).
- Proactive refresh at T-30min via background asyncio task.
- On 401: re-fetch credentials and retry once.
"""

from __future__ import annotations

import asyncio
import sys
import time
from datetime import UTC, datetime, timedelta

from auth.simple_credentials import Credentials, SimpleCredentials, fetch_credentials_with_retry
from models.types import AppDRole, TokenCache

TOKEN_VALIDITY_S = 6 * 3600
REFRESH_BEFORE_S = 30 * 60
SESSION_TTL_S = 1800


# ---------------------------------------------------------------------------
# Token manager
# ---------------------------------------------------------------------------


class TokenManager:
    def __init__(
        self, creds: SimpleCredentials, controller_name: str, token_url: str
    ) -> None:
        self._creds = creds
        self._controller_name = controller_name
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

        creds: Credentials = await fetch_credentials_with_retry(
            self._creds, self._controller_name
        )
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
# Role — single-user mode always returns CONFIGURE_ALERTING
# ---------------------------------------------------------------------------


async def get_user_role(upn: str, *_args: object, **_kwargs: object) -> AppDRole:
    """Single-user mode: caller always has full admin access."""
    return AppDRole.CONFIGURE_ALERTING


def invalidate_session(upn: str) -> None:
    """No-op in single-user mode — no session cache."""


# ---------------------------------------------------------------------------
# Tool permission gates — kept for structural compatibility.
# Since get_user_role always returns CONFIGURE_ALERTING, every gate passes.
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
})

_ROLE_TOOLS: dict[AppDRole, frozenset[str]] = {
    AppDRole.VIEW: _VIEW_TOOLS,
    AppDRole.TROUBLESHOOT: _TROUBLESHOOT_TOOLS,
    AppDRole.CONFIGURE_ALERTING: _CONFIGURE_ALERTING_TOOLS,
    AppDRole.DENIED: frozenset(),
}


def require_permission(role: AppDRole, tool_name: str) -> None:
    """No-op in single-user mode — role is always CONFIGURE_ALERTING."""
