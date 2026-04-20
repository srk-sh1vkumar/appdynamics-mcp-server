"""
utils/cache_keys.py

Centralised cache key builder.

ALL cache keys MUST include UPN as the first segment to prevent cross-user
data leakage. The sole exception is golden_key(), which is a shared registry
entry accessed only internally (never exposed via user-scoped cache reads).

Key format: {upn}:{controller}:{data_type}:{id1}:{id2}...
Example:    "shiva@co.com:production:bt_list:paymentservice"

WRONG: "production:bt_list:PaymentService"
RIGHT: "shiva@co.com:production:bt_list:paymentservice"
"""

from __future__ import annotations


def _norm(*parts: str) -> str:
    return ":".join(p.lower().replace(" ", "_") for p in parts)


def make_key(
    upn: str,
    controller: str,
    data_type: str,
    *identifiers: str,
) -> str:
    """Build a UPN-namespaced cache key.

    Format: {upn}:{controller}:{data_type}[:{id}...]
    """
    return _norm(upn, controller, data_type, *identifiers)


def snapshot_list_key(
    upn: str,
    controller: str,
    app: str,
    bt: str | None = None,
    error_only: bool = False,
) -> str:
    parts = [upn, controller, "snapshot_list", app]
    if bt:
        parts.append(bt)
    parts.append("errors_only" if error_only else "all")
    return _norm(*parts)


def parsed_snapshot_key(upn: str, controller: str, snapshot_guid: str) -> str:
    return _norm(upn, controller, "parsed_snapshot", snapshot_guid)


def golden_key(controller: str, app: str, bt: str) -> str:
    """Golden baseline is shared across users — stored without UPN prefix.

    This key is ONLY used internally by GoldenRegistry, never by the
    user-scoped cache layer.
    """
    return _norm("__golden__", controller, app, bt)


def bt_list_key(upn: str, controller: str, app: str) -> str:
    """Data type matches MEMORY_CACHE_CONFIG key 'business_transactions'."""
    return _norm(upn, controller, "business_transactions", app)


def app_list_key(upn: str, controller: str) -> str:
    """Data type matches MEMORY_CACHE_CONFIG key 'applications'."""
    return _norm(upn, controller, "applications")


def user_roles_key(upn: str, controller: str) -> str:
    return _norm(upn, controller, "user_roles")


def metric_values_key(
    upn: str,
    controller: str,
    app: str,
    metric_path: str,
    duration_mins: int = 60,
) -> str:
    return _norm(upn, controller, "metric_values", app, metric_path, str(duration_mins))


def infrastructure_stats_key(
    upn: str,
    controller: str,
    app: str,
    tier: str,
    node: str,
    duration_mins: int,
) -> str:
    return _norm(upn, controller, "infrastructure_stats", app, tier, node, str(duration_mins))


def tiers_and_nodes_key(upn: str, controller: str, app: str) -> str:
    return _norm(upn, controller, "tiers_and_nodes", app)


def bt_baseline_key(
    upn: str,
    controller: str,
    app: str,
    bt_name: str,
    duration_mins: int,
) -> str:
    return _norm(upn, controller, "bt_baseline", app, bt_name, str(duration_mins))


def user_app_access_key(upn: str, controller: str) -> str:
    """Key under which the resolved accessible-app frozenset is cached.

    Stored in user_resolver's in-process dict, not TwoLayerCache —
    RBAC data must not be written to diskcache.
    """
    return _norm(upn, controller, "user_app_access")
