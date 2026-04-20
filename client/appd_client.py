"""
client/appd_client.py

Per-controller AppDynamics API client — async httpx + tenacity retry.

Design decisions:
- One AppDClient instance per controller. Never a singleton. Controller
  auth tokens, base URLs, and account names are strictly isolated.
- All requests append ?output=JSON. Metric paths are URI-encoded with
  urllib.parse.quote() — handles parentheses and pipe chars in metric names.
- Endpoint stability tagging per spec (Part 3):
    STABLE:   /controller/rest/...    → production-safe
    UNSTABLE: /controller/restui/...  → internal, may break on SaaS update
  logging.warning() emitted on every UNSTABLE call.
- 401 flow: delegate to TokenManager.handle_401() → retry once → raise.
- 403 mid-session: invalidate_session(upn) then raise PermissionDeniedError.
- Analytics calls use a separate base URL and X-Events-API-* headers.
- Tenacity retry: 3 attempts, exponential backoff, only on network errors
  (not on 4xx/5xx — those are deterministic and should surface immediately).
- API version check at startup: compare known stable endpoint response shape
  against expected schema. Log warning if unexpected fields appear.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from auth.appd_auth import TokenManager, invalidate_session
from models.types import ControllerConfig

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class AppDError(Exception):
    pass


class AuthenticationError(AppDError):
    pass


class PermissionDeniedError(AppDError):
    pass


class ResourceNotFoundError(AppDError):
    pass


class RateLimitError(AppDError):
    pass


class ControllerError(AppDError):
    pass


_HTTP_ERRORS: dict[int, tuple[type[AppDError], str]] = {
    401: (
        AuthenticationError,
        "Authentication failed. Verify OAuth2 credentials in Vault.",
    ),
    403: (
        PermissionDeniedError,
        "Permission denied. Check API token scope for this app/tier.",
    ),
    404: (
        ResourceNotFoundError,
        "Resource not found. Use search_metric_tree to browse valid paths.",
    ),
    429: (RateLimitError, "AppDynamics rate limit hit. Will retry with backoff."),
    500: (
        ControllerError,
        "AppDynamics Controller error. Check controller health independently.",
    ),
}

# ---------------------------------------------------------------------------
# AppDClient
# ---------------------------------------------------------------------------


class AppDClient:
    def __init__(self, config: ControllerConfig, token_manager: TokenManager) -> None:
        self._config = config
        self._tm = token_manager
        self._http = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Core request method
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        upn: str = "",
        analytics: bool = False,
    ) -> Any:
        if "/restui/" in path:
            logger.warning("[appd] UNSTABLE endpoint called: %s", path)

        base = self._config.analytics_url if analytics else self._config.url
        qparams: dict[str, Any] = dict(params or {})
        if not analytics:
            qparams["output"] = "JSON"

        token = await self._tm.get_token()
        headers = self._build_headers(token, analytics)
        url = f"{base}{path}"

        try:
            resp = await self._http.get(url, params=qparams, headers=headers)
        except httpx.TransportError:
            raise  # tenacity will retry

        if resp.status_code == 401:
            token = await self._tm.handle_401()
            headers = self._build_headers(token, analytics)
            resp = await self._http.get(url, params=qparams, headers=headers)
            if resp.status_code == 401:
                raise AuthenticationError(
                    "Authentication failed. Verify OAuth2 credentials in Vault."
                )

        if resp.status_code == 403 and upn:
            invalidate_session(upn)

        self._raise_for_status(resp, upn)
        return resp.json()

    def _build_headers(self, token: str, analytics: bool) -> dict[str, str]:
        if analytics:
            return {
                "X-Events-API-AccountName": self._config.global_account,
                "X-Events-API-Key": os.environ.get("AD_EVENTS_API_KEY", ""),
                "Content-Type": "application/vnd.appd.events+json;v=2",
            }
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def _raise_for_status(self, resp: httpx.Response, upn: str = "") -> None:
        if resp.status_code < 400:
            return
        exc_class, msg = _HTTP_ERRORS.get(
            resp.status_code,
            (AppDError, f"AppDynamics returned HTTP {resp.status_code}"),
        )
        raise exc_class(msg)

    # ------------------------------------------------------------------
    # API version check (startup)
    # ------------------------------------------------------------------

    async def check_api_version(self) -> None:
        """
        Probe a known stable endpoint and validate response shape.
        Log a warning if unexpected fields appear or expected fields are missing.
        AppD SaaS updates silently — this catches breaking changes early.
        """
        try:
            data = await self._get("/controller/rest/serverstatus")
            if not isinstance(data, dict):
                logger.warning(
                    "[appd] /serverstatus returned unexpected type: %s", type(data)
                )
                return
            expected_keys = {"serverStatus", "accountName"}
            actual_keys = set(data.keys())
            missing = expected_keys - actual_keys
            if missing:
                logger.warning(
                    "[appd] API version check: missing expected fields: %s", missing
                )
        except Exception as exc:
            logger.warning("[appd] API version check failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    async def list_applications(
        self,
        search: str | None = None,
        page_size: int = 50,
        page_offset: int = 0,
    ) -> list[Any]:
        """
        Return applications from the controller.

        Args:
            search: Optional substring filter applied client-side (AppD REST API
                    does not support server-side search for applications).
            page_size: Number of results per page (applied after search filter).
            page_offset: Zero-based start index for pagination.
        """
        result = await self._get("/controller/rest/applications")
        apps: list[Any] = result if isinstance(result, list) else []
        if search:
            sl = search.lower()
            apps = [a for a in apps if sl in a.get("name", "").lower()]
        return apps[page_offset: page_offset + page_size]

    async def list_all_applications(self) -> list[Any]:
        """Return all applications without pagination.

        Used internally for registry seeding — not exposed to callers.
        """
        result = await self._get("/controller/rest/applications")
        return result if isinstance(result, list) else []

    async def search_metric_tree(self, app_name: str, path: str = "") -> list[Any]:
        encoded = quote(app_name, safe="")
        params = {"metric-path": path} if path else {}
        result = await self._get(
            f"/controller/rest/applications/{encoded}/metrics", params=params
        )
        return result if isinstance(result, list) else []

    async def get_metrics(
        self, app_name: str, metric_path: str, duration_mins: int
    ) -> list[Any]:
        encoded_app = quote(app_name, safe="")
        encoded_path = quote(metric_path, safe="")
        result = await self._get(
            f"/controller/rest/applications/{encoded_app}/metric-data",
            params={
                "metric-path": encoded_path,
                "time-range-type": "BEFORE_NOW",
                "duration-in-mins": duration_mins,
                "rollup": "true",
            },
        )
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Business Transactions
    # ------------------------------------------------------------------

    async def get_business_transactions(self, app_name: str) -> list[Any]:
        encoded = quote(app_name, safe="")
        result = await self._get(
            f"/controller/rest/applications/{encoded}/business-transactions"
        )
        return result if isinstance(result, list) else []

    async def get_bt_performance(
        self, app_name: str, bt_id: int, duration_mins: int
    ) -> dict[str, Any]:
        encoded = quote(app_name, safe="")
        result = await self._get(
            f"/controller/rest/applications/{encoded}/business-transactions/{bt_id}/performance",
            params={"duration-in-mins": duration_mins},
        )
        return result if isinstance(result, dict) else {}

    async def load_api_spec(self, spec_url: str) -> dict[str, Any]:
        """Fetch a Swagger/OpenAPI spec. Returns empty dict on failure."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(spec_url)
                resp.raise_for_status()
                result: dict[str, Any] = resp.json()
                return result
        except Exception as exc:
            logger.info("[appd] load_api_spec: could not fetch %s: %s", spec_url, exc)
            return {}

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    async def list_snapshots(
        self,
        app_name: str,
        bt_name: str | None,
        start_time_ms: int | None,
        end_time_ms: int | None,
        error_only: bool,
        page_size: int,
        page_offset: int,
    ) -> list[Any]:
        encoded = quote(app_name, safe="")
        params: dict[str, Any] = {
            "time-range-type": "BETWEEN_TIMES" if start_time_ms else "BEFORE_NOW",
            "duration-in-mins": 60,
            "maximum-results": page_size,
            "start-index": page_offset,
        }
        if start_time_ms:
            params["start-time"] = start_time_ms
        if end_time_ms:
            params["end-time"] = end_time_ms
        if bt_name:
            params["business-transaction-name"] = bt_name
        if error_only:
            params["user-experience"] = "ERROR"

        result = await self._get(
            f"/controller/rest/applications/{encoded}/request-snapshots", params=params
        )
        if isinstance(result, dict):
            segs: list[Any] = result.get("requestSegmentData", [])
            return segs
        return result if isinstance(result, list) else []

    async def get_snapshot_detail(
        self, app_name: str, request_guid: str
    ) -> dict[str, Any]:
        encoded = quote(app_name, safe="")
        result = await self._get(
            f"/controller/rest/applications/{encoded}/request-snapshots/{request_guid}"
        )
        return result if isinstance(result, dict) else {}

    async def archive_snapshot(
        self, app_name: str, request_guid: str
    ) -> dict[str, Any]:
        encoded = quote(app_name, safe="")
        result = await self._get(
            f"/controller/rest/applications/{encoded}/request-snapshots/{request_guid}/archive"
        )
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # Health & Policies
    # ------------------------------------------------------------------

    async def get_health_violations(
        self, app_name: str, duration_mins: int, include_resolved: bool
    ) -> list[Any]:
        encoded = quote(app_name, safe="")
        params: dict[str, Any] = {
            "time-range-type": "BEFORE_NOW",
            "duration-in-mins": duration_mins,
        }
        if include_resolved:
            params["includeResolvedViolations"] = "true"
        result = await self._get(
            f"/controller/rest/applications/{encoded}/problems/healthrule-violations",
            params=params,
        )
        return result if isinstance(result, list) else []

    async def get_policies(self, app_name: str) -> list[Any]:
        encoded = quote(app_name, safe="")
        result = await self._get(f"/controller/rest/applications/{encoded}/policies")
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Infrastructure
    # ------------------------------------------------------------------

    async def get_infrastructure_stats(
        self, app_name: str, tier_name: str, node_name: str | None, duration_mins: int
    ) -> list[Any]:
        encoded_app = quote(app_name, safe="")
        if node_name:
            path = (
                f"/controller/rest/applications/{encoded_app}"
                f"/nodes/{quote(node_name, safe='')}/node-details"
            )
        else:
            path = (
                f"/controller/rest/applications/{encoded_app}"
                f"/tiers/{quote(tier_name, safe='')}/nodes"
            )
        result = await self._get(path, params={"duration-in-mins": duration_mins})
        if isinstance(result, list):
            return result
        return [result] if isinstance(result, dict) else []

    async def get_jvm_details(
        self, app_name: str, tier_name: str, node_name: str, duration_mins: int
    ) -> dict[str, Any]:
        encoded_app = quote(app_name, safe="")
        result = await self._get(
            f"/controller/rest/applications/{encoded_app}"
            f"/nodes/{quote(node_name, safe='')}/jvms",
            params={"duration-in-mins": duration_mins},
        )
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # Errors & Exceptions
    # ------------------------------------------------------------------

    async def get_errors_and_exceptions(
        self, app_name: str, duration_mins: int
    ) -> list[Any]:
        encoded = quote(app_name, safe="")
        result = await self._get(
            f"/controller/rest/applications/{encoded}/problems/errors",
            params={"time-range-type": "BEFORE_NOW", "duration-in-mins": duration_mins},
        )
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------

    async def get_database_performance(
        self, app_name: str, db_name: str | None, duration_mins: int
    ) -> list[Any]:
        encoded = quote(app_name, safe="")
        params: dict[str, Any] = {"duration-in-mins": duration_mins}
        if db_name:
            params["database-name"] = db_name
        result = await self._get(
            f"/controller/rest/applications/{encoded}/databases/queries", params=params
        )
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    async def get_network_kpis(
        self, app_name: str, source_tier: str, dest_tier: str | None, duration_mins: int
    ) -> list[Any]:
        encoded = quote(app_name, safe="")
        params: dict[str, Any] = {
            "source-tier": source_tier,
            "duration-in-mins": duration_mins,
        }
        if dest_tier:
            params["destination-tier"] = dest_tier
        result = await self._get(
            f"/controller/rest/applications/{encoded}/network-requests", params=params
        )
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Analytics (separate base URL + auth)
    # ------------------------------------------------------------------

    async def query_analytics(
        self, adql_query: str, start_time: str | None, end_time: str | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"query": adql_query, "mode": "json"}
        if start_time:
            body["start"] = start_time
        if end_time:
            body["end"] = end_time
        headers = {
            "X-Events-API-AccountName": self._config.global_account,
            "X-Events-API-Key": os.environ.get("AD_EVENTS_API_KEY", ""),
            "Content-Type": "application/vnd.appd.events+json;v=2",
        }
        url = f"{self._config.analytics_url}/events/query"
        resp = await self._http.post(url, json=body, headers=headers)
        self._raise_for_status(resp)
        analytics_result: dict[str, Any] = resp.json()
        return analytics_result

    # ------------------------------------------------------------------
    # EUM (UNSTABLE /restui/ paths)
    # ------------------------------------------------------------------

    async def get_eum_overview(
        self, app_name: str, duration_mins: int
    ) -> dict[str, Any]:
        encoded = quote(app_name, safe="")
        result = await self._get(
            f"/controller/restui/eumApplications/{encoded}/summary",
            params={"duration-in-mins": duration_mins},
        )
        return result if isinstance(result, dict) else {}

    async def get_eum_page_performance(
        self, app_name: str, page_url: str | None, duration_mins: int
    ) -> list[Any]:
        encoded = quote(app_name, safe="")
        params: dict[str, Any] = {"duration-in-mins": duration_mins}
        if page_url:
            params["pageUrl"] = page_url
        result = await self._get(
            f"/controller/restui/eumApplications/{encoded}/pages/timings", params=params
        )
        return result if isinstance(result, list) else []

    async def get_eum_js_errors(self, app_name: str, duration_mins: int) -> list[Any]:
        encoded = quote(app_name, safe="")
        result = await self._get(
            f"/controller/restui/eumApplications/{encoded}/jsErrors",
            params={"duration-in-mins": duration_mins},
        )
        return result if isinstance(result, list) else []

    async def get_eum_ajax_requests(
        self, app_name: str, duration_mins: int
    ) -> list[Any]:
        encoded = quote(app_name, safe="")
        result = await self._get(
            f"/controller/restui/eumApplications/{encoded}/ajaxRequests",
            params={"duration-in-mins": duration_mins},
        )
        return result if isinstance(result, list) else []

    async def get_eum_geo_performance(
        self, app_name: str, duration_mins: int
    ) -> list[Any]:
        encoded = quote(app_name, safe="")
        result = await self._get(
            f"/controller/restui/eumApplications/{encoded}/geo",
            params={"duration-in-mins": duration_mins},
        )
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Tier / node discovery
    # ------------------------------------------------------------------

    async def get_tiers(self, app_name: str) -> list[Any]:
        encoded = quote(app_name, safe="")
        result = await self._get(
            f"/controller/rest/applications/{encoded}/tiers"
        )
        return result if isinstance(result, list) else []

    async def get_nodes(self, app_name: str, tier_name: str) -> list[Any]:
        encoded_app = quote(app_name, safe="")
        encoded_tier = quote(tier_name, safe="")
        result = await self._get(
            f"/controller/rest/applications/{encoded_app}"
            f"/tiers/{encoded_tier}/nodes"
        )
        return result if isinstance(result, list) else []

    async def get_exit_calls(
        self, app_name: str, request_guid: str
    ) -> list[Any]:
        """Return exit calls from a snapshot detail (outbound DB/HTTP/MQ calls)."""
        snap = await self.get_snapshot_detail(app_name, request_guid)
        exit_calls: list[Any] = snap.get("exitCalls", [])
        return exit_calls if isinstance(exit_calls, list) else []

    async def get_bt_detection_rules(self, app_name: str) -> dict[str, Any]:
        """
        Fetch BT detection rules (custom + auto) for an application.

        Uses /restui/transactiondetection/{app_id}/custom and /auto —
        UNSTABLE endpoints that require the numeric app ID, which is
        resolved from list_applications().
        """
        apps = await self.list_all_applications()
        app_id = next(
            (
                str(a.get("id", ""))
                for a in apps
                if a.get("name", "").lower() == app_name.lower()
            ),
            None,
        )
        if not app_id:
            return {
                "error": f"Application '{app_name}' not found on this controller.",
                "custom_rules": [],
                "auto_detection": {},
            }

        result: dict[str, Any] = {"app_id": app_id}
        try:
            custom = await self._get(
                f"/controller/restui/transactiondetection/{app_id}/custom"
            )
            result["custom_rules"] = custom if isinstance(custom, list) else []
        except Exception as exc:
            result["custom_rules"] = []
            result["custom_rules_error"] = str(exc)
        try:
            auto = await self._get(
                f"/controller/restui/transactiondetection/{app_id}/auto"
            )
            result["auto_detection"] = auto if isinstance(auto, dict) else {}
        except Exception as exc:
            result["auto_detection"] = {}
            result["auto_detection_error"] = str(exc)
        return result

    async def get_agent_status(
        self, app_name: str, tier_name: str | None = None
    ) -> list[Any]:
        """Return node availability records for an app (optionally filtered by tier)."""
        encoded_app = quote(app_name, safe="")
        if tier_name:
            encoded_tier = quote(tier_name, safe="")
            result = await self._get(
                f"/controller/rest/applications/{encoded_app}"
                f"/tiers/{encoded_tier}/nodes"
            )
        else:
            result = await self._get(
                f"/controller/rest/applications/{encoded_app}/nodes"
            )
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # User lookup (used by auth.get_user_role)
    # ------------------------------------------------------------------

    async def get_user_by_upn(self, upn: str, controller_name: str) -> dict[str, Any]:
        username = upn.split("@")[0]
        result = await self._get(f"/controller/rest/users/{quote(username, safe='')}")
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # License detection
    # ------------------------------------------------------------------

    async def detect_licenses(self) -> dict[str, bool]:
        modules: dict[str, bool] = {"snapshots": True}
        probes: list[tuple[str, str]] = [
            ("analytics", "/controller/rest/analytics/search/config"),
            ("eum", "/controller/restui/eumApplications"),
            ("database_visibility", "/controller/rest/databases"),
        ]
        for module, path in probes:
            try:
                await self._get(path)
                modules[module] = True
            except ResourceNotFoundError:
                modules[module] = True   # 404 = feature exists, just no entity
            except Exception:
                modules[module] = False
        return modules

    # ------------------------------------------------------------------
    # Controller ping
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        try:
            await self._get("/controller/rest/serverstatus")
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Per-controller registry
# ---------------------------------------------------------------------------

_clients: dict[str, AppDClient] = {}


def register(name: str, client: AppDClient) -> None:
    _clients[name] = client


def get_client(name: str) -> AppDClient:
    if name not in _clients:
        raise ValueError(
            f"Controller '{name}' not configured. Available: {list(_clients)}"
        )
    return _clients[name]


def all_clients() -> dict[str, AppDClient]:
    return _clients
