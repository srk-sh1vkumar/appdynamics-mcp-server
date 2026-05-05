"""
tests/unit/test_enterprise_mode.py

Tests for the two-mode architecture:
  - single_user: get_user_role always returns CONFIGURE_ALERTING, require_permission passes.
  - enterprise: get_user_role uses RBACClient, _map_appd_role maps role names to tiers,
    _require_app_access enforces per-app filtering, list_applications respects the app set.

require_permission is always enforcing (not a no-op) — passes trivially in single-user
because the role is always CONFIGURE_ALERTING (which has all tools).
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth.appd_auth import (
    AppDRole,
    _map_appd_role,
    get_user_role,
    require_permission,
)


# ---------------------------------------------------------------------------
# _map_appd_role tier mapping
# ---------------------------------------------------------------------------

class TestMapAppDRole:

    def test_admin_keywords_map_to_configure_alerting(self):
        assert _map_appd_role(["administrator"]) == AppDRole.CONFIGURE_ALERTING
        assert _map_appd_role(["Admin-SRE"]) == AppDRole.CONFIGURE_ALERTING
        assert _map_appd_role(["configure_alerting"]) == AppDRole.CONFIGURE_ALERTING

    def test_sre_maps_to_troubleshoot(self):
        assert _map_appd_role(["SRE-Payments"]) == AppDRole.TROUBLESHOOT
        assert _map_appd_role(["devops-team"]) == AppDRole.TROUBLESHOOT
        assert _map_appd_role(["troubleshoot-role"]) == AppDRole.TROUBLESHOOT

    def test_unknown_role_name_maps_to_view(self):
        assert _map_appd_role(["read-only-analyst"]) == AppDRole.VIEW

    def test_empty_roles_maps_to_denied(self):
        assert _map_appd_role([]) == AppDRole.DENIED

    def test_admin_keyword_beats_sre_keyword(self):
        assert _map_appd_role(["admin-sre-user"]) == AppDRole.CONFIGURE_ALERTING

    def test_case_insensitive(self):
        assert _map_appd_role(["ADMINISTRATOR"]) == AppDRole.CONFIGURE_ALERTING
        assert _map_appd_role(["SRE"]) == AppDRole.TROUBLESHOOT


# ---------------------------------------------------------------------------
# get_user_role — single-user (no rbac_client) always returns CONFIGURE_ALERTING
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGetUserRoleSingleUser:

    async def test_no_rbac_client_returns_configure_alerting(self):
        role = await get_user_role("alice@example.com")
        assert role == AppDRole.CONFIGURE_ALERTING

    async def test_dev_upn_returns_configure_alerting(self):
        role = await get_user_role("dev@local")
        assert role == AppDRole.CONFIGURE_ALERTING


# ---------------------------------------------------------------------------
# get_user_role — enterprise (with rbac_client) maps RBAC role to tier
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGetUserRoleEnterprise:

    def _mock_rbac(self, roles: list[str]):
        rbac = AsyncMock()
        rbac.get_user_by_name.return_value = {
            "id": 1,
            "name": "alice",
            "roles": [{"id": i + 1, "name": r} for i, r in enumerate(roles)],
            "groups": [],
        }
        return rbac

    async def test_admin_role_returns_configure_alerting(self):
        rbac = self._mock_rbac(["Payments-Administrator"])
        # Clear session cache between tests
        from auth.appd_auth import _sessions
        _sessions.pop("alice@example.com", None)

        role = await get_user_role("alice@example.com", rbac)
        assert role == AppDRole.CONFIGURE_ALERTING

    async def test_sre_role_returns_troubleshoot(self):
        rbac = self._mock_rbac(["Payments-SRE"])
        _sessions = __import__("auth.appd_auth", fromlist=["_sessions"])._sessions
        _sessions.pop("alice@example.com", None)

        role = await get_user_role("alice@example.com", rbac)
        assert role == AppDRole.TROUBLESHOOT

    async def test_unknown_role_returns_view(self):
        rbac = self._mock_rbac(["read-only"])
        from auth.appd_auth import _sessions
        _sessions.pop("alice@example.com", None)

        role = await get_user_role("alice@example.com", rbac)
        assert role == AppDRole.VIEW

    async def test_user_not_found_returns_denied(self):
        rbac = AsyncMock()
        rbac.get_user_by_name.return_value = None
        from auth.appd_auth import _sessions
        _sessions.pop("nobody@example.com", None)

        role = await get_user_role("nobody@example.com", rbac)
        assert role == AppDRole.DENIED

    async def test_rbac_exception_returns_denied(self):
        rbac = AsyncMock()
        rbac.get_user_by_name.side_effect = RuntimeError("RBAC API unreachable")
        from auth.appd_auth import _sessions
        _sessions.pop("error@example.com", None)

        role = await get_user_role("error@example.com", rbac)
        assert role == AppDRole.DENIED

    async def test_result_is_cached(self):
        rbac = self._mock_rbac(["SRE-Team"])
        from auth.appd_auth import _sessions
        _sessions.pop("cached@example.com", None)

        await get_user_role("cached@example.com", rbac)
        await get_user_role("cached@example.com", rbac)

        assert rbac.get_user_by_name.call_count == 1


# ---------------------------------------------------------------------------
# require_permission — always enforcing
# ---------------------------------------------------------------------------

class TestRequirePermission:

    def test_configure_alerting_can_call_all_tools(self):
        from auth.appd_auth import _CONFIGURE_ALERTING_TOOLS
        for tool in _CONFIGURE_ALERTING_TOOLS:
            require_permission(AppDRole.CONFIGURE_ALERTING, tool)  # must not raise

    def test_view_can_call_view_tools(self):
        from auth.appd_auth import _VIEW_TOOLS
        for tool in _VIEW_TOOLS:
            require_permission(AppDRole.VIEW, tool)

    def test_view_cannot_call_troubleshoot_tools(self):
        with pytest.raises(PermissionError, match="TROUBLESHOOT"):
            require_permission(AppDRole.VIEW, "list_snapshots")

    def test_view_cannot_call_configure_alerting_tools(self):
        with pytest.raises(PermissionError, match="CONFIGURE_ALERTING"):
            require_permission(AppDRole.VIEW, "get_policies")

    def test_troubleshoot_cannot_call_configure_alerting_only_tools(self):
        with pytest.raises(PermissionError, match="CONFIGURE_ALERTING"):
            require_permission(AppDRole.TROUBLESHOOT, "archive_snapshot")

    def test_denied_cannot_call_any_tool(self):
        with pytest.raises(PermissionError):
            require_permission(AppDRole.DENIED, "list_applications")

    def test_error_message_includes_required_role(self):
        with pytest.raises(PermissionError) as exc_info:
            require_permission(AppDRole.VIEW, "list_snapshots")
        assert "TROUBLESHOOT" in str(exc_info.value)
        assert "VIEW" in str(exc_info.value)


# ---------------------------------------------------------------------------
# _require_app_access — enterprise vs single-user
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestRequireAppAccess:

    async def _run(self, is_enterprise: bool, rbac_client, allowed_apps: frozenset[str], app_name: str):
        """Helper: patch main's IS_ENTERPRISE, _rbac_clients, and user_resolver.resolve."""
        import main

        with (
            patch.object(main, "IS_ENTERPRISE", is_enterprise),
            patch.dict(main._rbac_clients, {"production": rbac_client} if rbac_client else {}, clear=True),
            patch("main.user_resolver.resolve", new=AsyncMock(return_value=allowed_apps)),
        ):
            await main._require_app_access("alice@example.com", "production", app_name)

    async def test_single_user_mode_never_raises(self):
        # Even with a non-empty _rbac_clients and denied app — single-user skips entirely
        rbac = AsyncMock()
        await self._run(is_enterprise=False, rbac_client=rbac, allowed_apps=frozenset(), app_name="PaymentService")

    async def test_enterprise_allowed_app_passes(self):
        rbac = AsyncMock()
        await self._run(
            is_enterprise=True, rbac_client=rbac,
            allowed_apps=frozenset({"PaymentService", "OrderService"}),
            app_name="PaymentService",
        )

    async def test_enterprise_denied_app_raises(self):
        rbac = AsyncMock()
        with pytest.raises(PermissionError, match="PaymentService"):
            await self._run(
                is_enterprise=True, rbac_client=rbac,
                allowed_apps=frozenset({"OrderService"}),
                app_name="PaymentService",
            )

    async def test_enterprise_no_rbac_client_passes(self):
        # No rbac_client configured for controller — fallback: allow all
        import main

        with (
            patch.object(main, "IS_ENTERPRISE", True),
            patch.dict(main._rbac_clients, {}, clear=True),
        ):
            await main._require_app_access("alice@example.com", "production", "AnyApp")

    async def test_enterprise_empty_allowed_set_denies(self):
        # resolve() returned empty frozenset — user found but no app access
        rbac = AsyncMock()
        with pytest.raises(PermissionError):
            await self._run(
                is_enterprise=True, rbac_client=rbac,
                allowed_apps=frozenset(),
                app_name="PaymentService",
            )


# ---------------------------------------------------------------------------
# _get_role — mode-aware dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestGetRoleDispatch:

    async def test_single_user_always_configure_alerting(self):
        import main

        with (
            patch.object(main, "IS_ENTERPRISE", False),
            patch.dict(main._rbac_clients, {}, clear=True),
        ):
            role = await main._get_role("alice@example.com", "production")

        assert role == AppDRole.CONFIGURE_ALERTING

    async def test_enterprise_without_rbac_client_uses_none(self):
        import main

        with (
            patch.object(main, "IS_ENTERPRISE", True),
            patch.dict(main._rbac_clients, {}, clear=True),
            patch("main.get_user_role", new=AsyncMock(return_value=AppDRole.TROUBLESHOOT)) as mock_gur,
        ):
            role = await main._get_role("alice@example.com", "production")

        mock_gur.assert_called_once_with("alice@example.com", None)
        assert role == AppDRole.TROUBLESHOOT

    async def test_enterprise_with_rbac_client_passes_client(self):
        import main

        fake_rbac = MagicMock()
        with (
            patch.object(main, "IS_ENTERPRISE", True),
            patch.dict(main._rbac_clients, {"production": fake_rbac}, clear=True),
            patch("main.get_user_role", new=AsyncMock(return_value=AppDRole.VIEW)) as mock_gur,
        ):
            role = await main._get_role("alice@example.com", "production")

        mock_gur.assert_called_once_with("alice@example.com", fake_rbac)
        assert role == AppDRole.VIEW
