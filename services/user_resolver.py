"""
services/user_resolver.py

Resolves a UPN to the set of AppDynamics application names that user is
permitted to see, by traversing the AppD RBAC graph:

    user → direct roles + group IDs
         → each group's roles
         → each role's applicationPermissions (canView=true)
         → union → frozenset[str] of accessible app names

Cache TTL is configurable via APPDYNAMICS_RBAC_CACHE_TTL_S (default 86400s —
daily). For orgs where RBAC changes rarely, weekly (604800s) is also fine.
Use the refresh_user_access tool or invalidate_user() to force-clear a UPN.

Fail closed: any RBAC API error produces an empty frozenset, which causes
all per-app tool calls to raise PermissionError. This prevents data leakage
on auth failures.

Concurrency design (CONC-02):
- Per-UPN asyncio.Lock via defaultdict — different UPNs resolve in parallel.
- Only concurrent requests for the same UPN are serialised, preventing double-
  fetch of the same RBAC graph. No global lock that would serialise 50 users
  hitting the server simultaneously at peak hours.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict

from client.rbac_client import RBACClient

logger = logging.getLogger(__name__)

# Configurable TTL — default 1 day. Set APPDYNAMICS_RBAC_CACHE_TTL_S to
# override (e.g. 1800 for session-scoped, 604800 for weekly).
_RBAC_CACHE_TTL_S: int = int(os.environ.get("APPDYNAMICS_RBAC_CACHE_TTL_S", "86400"))

# (upn, controller_name) → (frozenset[app_name], cached_at)
_app_access_cache: dict[tuple[str, str], tuple[frozenset[str], float]] = {}

# Per-UPN locks — concurrent requests for different UPNs run in parallel;
# only same-UPN concurrent requests are serialised to avoid double-fetch.
_upn_locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)


def invalidate_user(upn: str, controller_name: str) -> bool:
    """
    Force-clear the cached app set for a UPN on a controller.
    Returns True if an entry existed, False if not cached.
    Called by the refresh_user_access tool.
    """
    key = (upn.lower(), controller_name.lower())
    existed = key in _app_access_cache
    _app_access_cache.pop(key, None)
    if existed:
        logger.info("[rbac] Invalidated app access cache for %s on %s", upn, controller_name)
    return existed


def get_cache_stats() -> dict[str, object]:
    """Return cache stats for get_server_health."""
    now = time.time()
    entries = []
    for (upn, ctrl), (apps, cached_at) in _app_access_cache.items():
        age_s = int(now - cached_at)
        entries.append({
            "upn": upn,
            "controller": ctrl,
            "app_count": len(apps),
            "age_s": age_s,
            "ttl_remaining_s": max(0, _RBAC_CACHE_TTL_S - age_s),
        })
    return {
        "ttl_s": _RBAC_CACHE_TTL_S,
        "cached_users": len(_app_access_cache),
        "active_upn_locks": len(_upn_locks),
        "entries": entries,
    }


async def resolve(
    upn: str,
    controller_name: str,
    rbac_client: RBACClient,
) -> frozenset[str]:
    """
    Return the frozenset of app names accessible to upn on controller_name.

    Cached for _RBAC_CACHE_TTL_S seconds. Per-UPN lock ensures concurrent
    requests for the same UPN share one fetch; different UPNs run in parallel.
    Fails closed — any RBAC error returns an empty frozenset.
    """
    cache_key = (upn.lower(), controller_name.lower())

    async with _upn_locks[cache_key]:
        cached = _app_access_cache.get(cache_key)
        if cached and (time.time() - cached[1]) < _RBAC_CACHE_TTL_S:
            logger.debug(
                "[rbac] Cache hit for %s on %s (%d apps)", upn, controller_name, len(cached[0])
            )
            return cached[0]

        app_names = await _fetch_app_names(upn, rbac_client)
        _app_access_cache[cache_key] = (app_names, time.time())

    logger.info(
        "[rbac] Resolved %d apps for %s on %s (ttl=%ds)",
        len(app_names), upn, controller_name, _RBAC_CACHE_TTL_S,
    )
    return app_names


async def _fetch_app_names(upn: str, rbac_client: RBACClient) -> frozenset[str]:
    """
    Traverse AppD RBAC: user → direct roles + groups → group roles → app permissions.
    Returns frozenset of app names where canView=true. Empty on any error.
    """
    try:
        user = await rbac_client.get_user_by_name(upn)
        if not user:
            logger.warning("[rbac] User not found in AppD RBAC: %s — denying all apps", upn)
            return frozenset()

        role_ids: set[int] = set()

        for role in user.get("roles", []):
            if rid := role.get("id"):
                role_ids.add(int(rid))

        group_ids: list[int] = [
            int(g["id"]) for g in user.get("groups", []) if g.get("id")
        ]

        if group_ids:
            group_results = await asyncio.gather(
                *[rbac_client.get_group(gid) for gid in group_ids],
                return_exceptions=True,
            )
            for result in group_results:
                if isinstance(result, dict):
                    for role in result.get("roles", []):
                        if rid := role.get("id"):
                            role_ids.add(int(rid))

        if not role_ids:
            logger.warning("[rbac] No roles found for %s — denying all apps", upn)
            return frozenset()

        role_results = await asyncio.gather(
            *[rbac_client.get_role(rid) for rid in role_ids],
            return_exceptions=True,
        )

        app_names: set[str] = set()
        for result in role_results:
            if not isinstance(result, dict):
                continue
            for perm in result.get("applicationPermissions", []):
                if perm.get("canView") and (name := perm.get("applicationName")):
                    app_names.add(name)

        return frozenset(app_names)

    except Exception as exc:
        logger.error("[rbac] Failed to resolve apps for %s: %s — denying all", upn, exc)
        return frozenset()
