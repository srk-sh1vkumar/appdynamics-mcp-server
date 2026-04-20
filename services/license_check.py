"""
services/license_check.py

AppDynamics license detection at server startup.

Design decisions:
- License state is detected once at startup via probe requests and stored in
  a module-level LicenseState instance. Tool handlers read from this state.
- Disabled tools are reported in the health endpoint under "disabled_tools".
- Graceful degradation: unlicensed tools return a clear message instead of
  a cryptic 404 or permission error from AppD.
- detect_licenses() is called after the first successful token fetch so
  probes use a valid service account token.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from models.types import DegradationMode, LicenseState

if TYPE_CHECKING:
    from client.appd_client import AppDClient

# Module-level singleton populated at startup
_state: LicenseState = LicenseState()

# Map from module name to the tools that require it
_MODULE_TOOLS: dict[str, list[str]] = {
    "eum": [
        "get_eum_overview",
        "get_eum_page_performance",
        "get_eum_js_errors",
        "get_eum_ajax_requests",
        "get_eum_geo_performance",
        "correlate_eum_to_bt",
    ],
    "database_visibility": ["get_database_performance"],
    "analytics": ["query_analytics_logs"],
    "snapshots": [
        "list_snapshots", "analyze_snapshot", "compare_snapshots", "archive_snapshot"
    ],
}


async def detect_and_store(client: AppDClient) -> LicenseState:
    """Probe AppD for licensed modules and store state. Called once at startup."""
    global _state
    modules = await client.detect_licenses()
    _state = LicenseState(
        eum=modules.get("eum", False),
        database_visibility=modules.get("database_visibility", False),
        analytics=modules.get("analytics", False),
        snapshots=modules.get("snapshots", True),
    )
    print(
        f"[license] Detected modules: eum={_state.eum}, "
        f"db_visibility={_state.database_visibility}, "
        f"analytics={_state.analytics}, "
        f"snapshots={_state.snapshots}",
        file=sys.stderr,
    )
    return _state


def get_state() -> LicenseState:
    return _state


def get_licensed_modules() -> list[str]:
    modules: list[str] = []
    if _state.eum:
        modules.append("eum")
    if _state.database_visibility:
        modules.append("db_visibility")
    if _state.analytics:
        modules.append("analytics")
    if _state.snapshots:
        modules.append("snapshots")
    return modules


def get_disabled_tools() -> list[str]:
    disabled: list[str] = []
    module_enabled = {
        "eum": _state.eum,
        "database_visibility": _state.database_visibility,
        "analytics": _state.analytics,
        "snapshots": _state.snapshots,
    }
    for module, tools in _MODULE_TOOLS.items():
        if not module_enabled.get(module, True):
            disabled.extend(tools)
    return disabled


def require_license(module: str) -> None:
    """Raise RuntimeError with a graceful message if module is not licensed."""
    enabled = {
        "eum": _state.eum,
        "database_visibility": _state.database_visibility,
        "analytics": _state.analytics,
        "snapshots": _state.snapshots,
    }
    if not enabled.get(module, True):
        module_names = {
            "eum": "End User Monitoring (EUM)",
            "database_visibility": "Database Visibility",
            "analytics": "Analytics / Events Service",
            "snapshots": "APM Pro (Snapshots)",
        }
        name = module_names.get(module, module)
        raise RuntimeError(
            f"{name} license not detected on this controller."
            " This tool is disabled. Contact your AppDynamics administrator."
        )


def get_degradation_mode() -> DegradationMode:
    if not _state.snapshots:
        return DegradationMode.NO_SNAPSHOTS
    if not _state.analytics:
        return DegradationMode.NO_ANALYTICS
    if not _state.eum:
        return DegradationMode.NO_EUM
    return DegradationMode.FULL
