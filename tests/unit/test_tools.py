"""
tests/unit/test_tools.py

End-to-end tool tests using mocked AppDClient and mocked auth helpers.

Patching strategy (matches actual main.py imports):
- client.appd_client.get_client         → returns mock AppDClient
- main.check_and_wait                   → no-op AsyncMock
- main._get_role                        → returns AppDRole.TROUBLESHOOT
- main.require_permission               → no-op (imported into main's namespace)
- services.license_check.require_license → no-op (or raises for disabled tests)
- utils.cache.get                       → always returns None (bypass cache)
- main.health_svc.compute_health        → returns mock HealthStatus

Tool parameter names match actual signatures (controller_name=, app_name=).

License failure behaviour: require_license raises RuntimeError, which propagates
from the tool. The MCP host converts this to an error message for the user.
Permission failure behaviour: require_permission raises PermissionError.
Both are tested explicitly here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from models.types import AppDRole, HealthStatus

# ---------------------------------------------------------------------------
# Import tool functions lazily
# ---------------------------------------------------------------------------

def _tool(name: str):
    import main as m
    return getattr(m, name)


# ---------------------------------------------------------------------------
# Core fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def patched_main(mock_appd_client):
    """
    Patch all stateful dependencies so tools can be called in isolation.
    Uses TROUBLESHOOT role so all tool permission checks pass by default.
    Cache always misses so the mock client is always called.
    """
    mock_registry = MagicMock()
    mock_registry.is_warm.return_value = False  # force live client path
    with (
        patch("main.get_client", return_value=mock_appd_client),
        patch("main.check_and_wait", new=AsyncMock(return_value=None)),
        patch("main._get_role", new=AsyncMock(return_value=AppDRole.TROUBLESHOOT)),
        patch("main.require_permission"),           # no-op — permission already granted
        patch("services.license_check.require_license"),  # no-op — licensed by default
        patch("utils.cache.get", new=AsyncMock(return_value=None)),  # always cache miss
        patch("main._apps_registry", mock_registry),   # never warm — always hit client
    ):
        yield mock_appd_client


# ---------------------------------------------------------------------------
# list_applications
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestListApplications:
    async def test_happy_path(self, patched_main):
        result = await _tool("list_applications")(controller_name="test")
        assert "<appd_data>" in result

    async def test_empty_result(self, patched_main):
        patched_main.list_applications.return_value = []
        result = await _tool("list_applications")(controller_name="test")
        assert result is not None

    async def test_http_500_propagates(self, patched_main):
        patched_main.list_applications.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock(status_code=500)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await _tool("list_applications")(controller_name="test")

    async def test_http_401_propagates(self, patched_main):
        patched_main.list_applications.side_effect = httpx.HTTPStatusError(
            "401 Unauthorized", request=MagicMock(), response=MagicMock(status_code=401)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await _tool("list_applications")(controller_name="test")

    async def test_http_403_propagates(self, patched_main):
        patched_main.list_applications.side_effect = httpx.HTTPStatusError(
            "403 Forbidden", request=MagicMock(), response=MagicMock(status_code=403)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await _tool("list_applications")(controller_name="test")


# ---------------------------------------------------------------------------
# get_business_transactions
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGetBusinessTransactions:
    async def test_happy_path(self, patched_main):
        result = await _tool("get_business_transactions")(
            app_name="ecommerce-app", controller_name="test"
        )
        assert result is not None

    async def test_health_checks_excluded_by_default(self, patched_main):
        from tests.conftest import BT_LIST_RESPONSE
        patched_main.get_business_transactions.return_value = BT_LIST_RESPONSE
        result = await _tool("get_business_transactions")(
            app_name="ecommerce-app",
            controller_name="test",
            include_health_checks=False,
        )
        assert result is not None


# ---------------------------------------------------------------------------
# get_health_violations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGetHealthViolations:
    async def test_happy_path(self, patched_main):
        result = await _tool("get_health_violations")(
            app_name="ecommerce-app", controller_name="test"
        )
        assert result is not None

    async def test_http_500_propagates(self, patched_main):
        patched_main.get_health_violations.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock(status_code=500)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await _tool("get_health_violations")(
                app_name="ecommerce-app", controller_name="test"
            )

    async def test_http_429_propagates(self, patched_main):
        patched_main.get_health_violations.side_effect = httpx.HTTPStatusError(
            "429", request=MagicMock(), response=MagicMock(status_code=429)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await _tool("get_health_violations")(
                app_name="ecommerce-app", controller_name="test"
            )


# ---------------------------------------------------------------------------
# analyze_snapshot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAnalyzeSnapshot:
    async def test_happy_path(self, patched_main):
        from tests.conftest import SNAPSHOT_DETAIL_RESPONSE
        patched_main.get_snapshot_detail.return_value = SNAPSHOT_DETAIL_RESPONSE
        result = await _tool("analyze_snapshot")(
            app_name="ecommerce-app",
            snapshot_guid="abc-123-guid",
            controller_name="test",
        )
        assert result is not None

    async def test_license_disabled_raises_runtime_error(self, patched_main):
        """
        require_license raises RuntimeError for unlicensed modules.
        The MCP host surfaces this as an error message to the user.
        """
        with patch(
            "services.license_check.require_license",
            side_effect=RuntimeError(
                "APM Pro (Snapshots) license not detected. This tool is disabled."
            ),
        ):
            with pytest.raises(RuntimeError, match="license"):
                await _tool("analyze_snapshot")(
                    app_name="ecommerce-app",
                    snapshot_guid="abc-123-guid",
                    controller_name="test",
                )

    async def test_http_404_propagates(self, patched_main):
        patched_main.get_snapshot_detail.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock(status_code=404)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await _tool("analyze_snapshot")(
                app_name="ecommerce-app",
                snapshot_guid="missing-guid",
                controller_name="test",
            )


# ---------------------------------------------------------------------------
# compare_snapshots
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCompareSnapshots:
    async def test_happy_path_explicit_baseline(self, patched_main):
        from tests.conftest import GOLDEN_SNAPSHOT_RESPONSE, SNAPSHOT_DETAIL_RESPONSE
        patched_main.get_snapshot_detail.side_effect = [
            SNAPSHOT_DETAIL_RESPONSE,
            GOLDEN_SNAPSHOT_RESPONSE,
        ]
        result = await _tool("compare_snapshots")(
            app_name="ecommerce-app",
            failed_snapshot_guid="abc-123-guid",
            healthy_snapshot_guid="golden-snap-guid",
            controller_name="test",
        )
        assert result is not None


# ---------------------------------------------------------------------------
# get_eum_overview — license check enforced
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGetEumOverview:
    async def test_license_disabled_raises_runtime_error(self, patched_main):
        with patch(
            "services.license_check.require_license",
            side_effect=RuntimeError("EUM license not detected."),
        ):
            with pytest.raises(RuntimeError, match="license"):
                await _tool("get_eum_overview")(
                    app_name="MyEcomApp", controller_name="test"
                )

    async def test_happy_path(self, patched_main):
        from tests.conftest import EUM_OVERVIEW_RESPONSE
        patched_main.get_eum_overview.return_value = EUM_OVERVIEW_RESPONSE
        result = await _tool("get_eum_overview")(
            app_name="MyEcomApp", controller_name="test"
        )
        assert result is not None


# ---------------------------------------------------------------------------
# query_analytics_logs — license check
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestQueryAnalyticsLogs:
    async def test_license_disabled_raises_runtime_error(self, patched_main):
        with patch(
            "services.license_check.require_license",
            side_effect=RuntimeError("Analytics license not detected."),
        ):
            with pytest.raises(RuntimeError, match="license"):
                await _tool("query_analytics_logs")(
                    adql_query="SELECT * FROM transactions",
                    controller_name="test",
                )


# ---------------------------------------------------------------------------
# get_server_health
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGetServerHealth:
    async def test_happy_path(self, patched_main):
        mock_health = HealthStatus(
            status="healthy",
            version="1.0.0",
            vault="connected",
            controllers={"test": "reachable"},
            token_expiry="2h 30m",
            degradation_mode="FULL",
            cache_hit_rate=0.75,
            requests_last_hour=120,
            active_users=3,
            licensed_modules=["eum", "analytics"],
            disabled_tools=[],
        )
        with patch(
            "main.health_svc.compute_health", new=AsyncMock(return_value=mock_health)
        ):
            result = await _tool("get_server_health")()
        assert result is not None


# ---------------------------------------------------------------------------
# save_runbook
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSaveRunbook:
    async def test_happy_path(self, patched_main, tmp_path):
        with patch("services.runbook_generator.RUNBOOKS_DIR", tmp_path):
            result = await _tool("save_runbook")(
                app_name="ecommerce-app",
                bt_name="/api/checkout",
                issue_summary="NullPointerException in CheckoutService",
                root_cause="Null token returned by vault at line 142",
                resolution="Add null check before vault.getSecret()",
                confidence="HIGH",
                investigation_steps=["Checked metrics", "Analyzed snapshot"],
            )
        import json
        rb = json.loads(result)
        assert rb["incident"] is not None
        files = list(tmp_path.glob("ecommerce-app-*.json"))
        assert len(files) == 1


# ---------------------------------------------------------------------------
# Rate limiter — check_and_wait called with correct UPN
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limiter_called_with_upn(patched_main):
    with patch("main.check_and_wait", new=AsyncMock(return_value=None)) as mock_rl:
        await _tool("list_applications")(
            controller_name="test", upn="alice@example.com"
        )
        mock_rl.assert_called_once_with(
            "alice@example.com",
            tool_name="list_applications",
            team_name=None,
        )


# ---------------------------------------------------------------------------
# Permission error raised for insufficient role
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_permission_error_raised(patched_main):
    """Tools raise PermissionError when the user's AppD role is insufficient."""
    with patch(
        "main.require_permission", side_effect=PermissionError("Insufficient role")
    ):
        with pytest.raises(PermissionError):
            await _tool("list_applications")(controller_name="test")


# ---------------------------------------------------------------------------
# stitch_async_trace — partial failure (one app 404s, other returns results)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stitch_async_trace_partial_failure(patched_main):
    from tests.conftest import SNAPSHOT_LIST_RESPONSE

    call_count = 0

    async def side_effect(app_name, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return SNAPSHOT_LIST_RESPONSE
        raise httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock(status_code=404)
        )

    patched_main.list_snapshots.side_effect = side_effect

    # stitch_async_trace catches per-app failures and returns partial results
    result = await _tool("stitch_async_trace")(
        correlation_id="corr-id-xyz",
        app_names=["app-a", "app-b"],
        controller_name="test",
    )
    assert result is not None
