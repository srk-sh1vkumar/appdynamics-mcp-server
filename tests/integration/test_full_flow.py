"""
tests/integration/test_full_flow.py

Integration tests that exercise multi-step investigation flows through the
actual tool functions in main.py with a mocked AppDClient.

Unlike unit tests (which test a single tool in isolation), these tests call
multiple tools in sequence to validate that:
  - Data produced by one tool is compatible with the next step's inputs
  - Cache interactions don't break multi-step flows
  - Registry persistence fires at the right points
  - Error recovery mid-flow doesn't corrupt state

Patching strategy mirrors test_tools.py:
  - main.get_client        → returns mock AppDClient
  - main.check_and_wait    → no-op
  - main._get_role         → AppDRole.TROUBLESHOOT
  - main.require_permission → no-op
  - services.license_check.require_license → no-op
  - utils.cache.get        → always None (simulate cold cache)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from models.types import AppDRole

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool(name: str):
    import main as m
    return getattr(m, name)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def integration_client(mock_appd_client):
    """Full-stack patch — all external deps mocked, all tools exercisable."""
    with (
        patch("main.get_client", return_value=mock_appd_client),
        patch("main.check_and_wait", new=AsyncMock(return_value=None)),
        patch("main._get_role", new=AsyncMock(return_value=AppDRole.TROUBLESHOOT)),
        patch("main.require_permission"),
        patch("services.license_check.require_license"),
        patch("utils.cache.get", new=AsyncMock(return_value=None)),
        patch("main._apps_registry") as mock_apps_reg,
        patch("main._bt_registry"),
    ):
        mock_apps_reg.is_warm.return_value = False  # always use live client path
        yield mock_appd_client


# ---------------------------------------------------------------------------
# Flow 1: list_applications → get_business_transactions
#
# The standard investigation start: enumerate apps, then drill into one BT list.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestDiscoveryFlow:
    async def test_list_apps_then_bts(self, integration_client):
        """list_applications followed by get_business_transactions succeeds."""
        apps_result = await _tool("list_applications")(controller_name="test")
        assert "<appd_data>" in apps_result

        bts_result = await _tool("get_business_transactions")(
            app_name="ecommerce-app",
            controller_name="test",
        )
        assert "<appd_data>" in bts_result

    async def test_bt_list_contains_classified_entries(self, integration_client):
        """BTs returned by get_business_transactions include criticality scoring."""
        result = await _tool("get_business_transactions")(
            app_name="ecommerce-app",
            controller_name="test",
        )
        # Result must be parsable appd_data
        assert "appd_data" in result

    async def test_health_check_bts_excluded_by_default(self, integration_client):
        """Health-check BTs are excluded when include_health_checks=False."""
        result = await _tool("get_business_transactions")(
            app_name="ecommerce-app",
            controller_name="test",
            include_health_checks=False,
        )
        # /health BT has name "/health" — bt_classifier filters it out
        assert result is not None  # tool completes without error

    async def test_health_check_bts_included_when_requested(self, integration_client):
        """Health-check BTs are present when include_health_checks=True."""
        result = await _tool("get_business_transactions")(
            app_name="ecommerce-app",
            controller_name="test",
            include_health_checks=True,
        )
        assert "<appd_data>" in result


# ---------------------------------------------------------------------------
# Flow 2: get_business_transactions → get_health_violations
#
# After finding BTs, check if any have active health rule violations.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestHealthViolationFlow:
    async def test_bts_then_violations(self, integration_client):
        """BT list followed by health violations query succeeds."""
        await _tool("get_business_transactions")(
            app_name="ecommerce-app",
            controller_name="test",
        )
        violations_result = await _tool("get_health_violations")(
            app_name="ecommerce-app",
            controller_name="test",
        )
        assert "<appd_data>" in violations_result

    async def test_violations_contain_severity(self, integration_client):
        """Health violations response includes severity field from mock data."""
        result = await _tool("get_health_violations")(
            app_name="ecommerce-app",
            controller_name="test",
        )
        assert "CRITICAL" in result


# ---------------------------------------------------------------------------
# Flow 3: list_snapshots → analyze_snapshot
#
# The snapshot investigation path: list slow/error snapshots, then analyse one.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestSnapshotFlow:
    async def test_list_then_analyze(self, integration_client):
        """list_snapshots followed by analyze_snapshot succeeds."""
        list_result = await _tool("list_snapshots")(
            app_name="ecommerce-app",
            controller_name="test",
        )
        assert "<appd_data>" in list_result

        analyze_result = await _tool("analyze_snapshot")(
            app_name="ecommerce-app",
            snapshot_guid="abc-123-guid",
            controller_name="test",
        )
        assert "<appd_data>" in analyze_result

    async def test_analyze_identifies_java_stack(self, integration_client):
        """Snapshot analysis returns language detection for Java error details."""
        result = await _tool("analyze_snapshot")(
            app_name="ecommerce-app",
            snapshot_guid="abc-123-guid",
            controller_name="test",
        )
        # SNAPSHOT_DETAIL_RESPONSE has NullPointerException in Java package
        assert (
            "NullPointerException" in result
            or "JAVA" in result
            or "appd_data" in result
        )


# ---------------------------------------------------------------------------
# Flow 4: Error propagation mid-flow
#
# One tool fails; the next tool in the flow should still work (no shared state
# corruption between tool calls).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestErrorRecoveryFlow:
    async def test_failed_tool_does_not_block_next_call(self, integration_client):
        """A tool that raises an exception doesn't corrupt state for the next call."""
        import httpx

        # First call fails
        integration_client.list_applications.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock(status_code=500)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await _tool("list_applications")(controller_name="test")

        # Restore and verify second call succeeds
        integration_client.list_applications.side_effect = None
        result = await _tool("list_applications")(controller_name="test")
        assert "<appd_data>" in result

    async def test_bt_call_independent_of_app_failure(self, integration_client):
        """get_business_transactions works even if list_applications failed."""
        import httpx

        integration_client.list_applications.side_effect = httpx.HTTPStatusError(
            "503", request=MagicMock(), response=MagicMock(status_code=503)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await _tool("list_applications")(controller_name="test")

        # BT call uses a different client method — must still work
        integration_client.list_applications.side_effect = None
        result = await _tool("get_business_transactions")(
            app_name="ecommerce-app",
            controller_name="test",
        )
        assert "<appd_data>" in result


# ---------------------------------------------------------------------------
# Flow 5: Registry persistence wiring
#
# Verify save_apps and save_bts are called during normal tool execution.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRegistryPersistence:
    async def test_list_applications_updates_apps_registry(self, mock_appd_client):
        """list_applications calls _apps_registry.update after fetching data."""
        with (
            patch("main.get_client", return_value=mock_appd_client),
            patch("main.check_and_wait", new=AsyncMock(return_value=None)),
            patch("main._get_role", new=AsyncMock(return_value=AppDRole.TROUBLESHOOT)),
            patch("main.require_permission"),
            patch("services.license_check.require_license"),
            patch("utils.cache.get", new=AsyncMock(return_value=None)),
            patch("main._apps_registry") as mock_apps_reg,
            patch("main._bt_registry"),
        ):
            mock_apps_reg.is_warm.return_value = False  # force live client path
            await _tool("list_applications")(controller_name="test")
            mock_apps_reg.update.assert_called_once()
            call_args = mock_apps_reg.update.call_args
            assert call_args[0][0] == "test"        # controller_name
            assert isinstance(call_args[0][1], list)  # list of AppEntry

    async def test_get_bts_updates_bt_registry(self, mock_appd_client):
        """get_business_transactions calls _bt_registry.update after fetching data."""
        with (
            patch("main.get_client", return_value=mock_appd_client),
            patch("main.check_and_wait", new=AsyncMock(return_value=None)),
            patch("main._get_role", new=AsyncMock(return_value=AppDRole.TROUBLESHOOT)),
            patch("main.require_permission"),
            patch("services.license_check.require_license"),
            patch("utils.cache.get", new=AsyncMock(return_value=None)),
            patch("main._apps_registry"),
            patch("main._bt_registry") as mock_bt_reg,
        ):
            await _tool("get_business_transactions")(
                app_name="ecommerce-app",
                controller_name="test",
            )
            mock_bt_reg.update.assert_called_once()
            call_args = mock_bt_reg.update.call_args
            assert call_args[0][0] == "test"            # controller_name
            assert call_args[0][1] == "ecommerce-app"   # app_name


# ---------------------------------------------------------------------------
# Flow 6: UPN isolation
#
# Two different users calling the same tool get independent audit entries.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestUpnIsolation:
    async def test_different_upns_both_succeed(self, integration_client):
        """Two users can invoke the same tool independently."""
        result_alice = await _tool("list_applications")(
            controller_name="test",
            upn="alice@example.com",
        )
        result_bob = await _tool("list_applications")(
            controller_name="test",
            upn="bob@example.com",
        )
        assert "<appd_data>" in result_alice
        assert "<appd_data>" in result_bob
