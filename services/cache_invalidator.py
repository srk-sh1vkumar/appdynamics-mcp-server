"""
services/cache_invalidator.py

Event-driven cache invalidation.

Called by tool handlers when specific AppDynamics signals are detected.
Never raises — all methods are fail-safe so they never break tool execution.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from registries.bt_registry import BTRegistry
from registries.golden_registry import GoldenRegistry
from utils import cache as cache_module

logger = logging.getLogger(__name__)


class CacheInvalidator:
    """Coordinates cache invalidation across registries and the two-layer cache.

    Maintains an in-memory event log for the /health endpoint's
    ``invalidations_last_hour`` counter.
    """

    def __init__(
        self,
        golden_registry: GoldenRegistry,
        bt_registry: BTRegistry,
    ) -> None:
        self._golden = golden_registry
        self._bt = bt_registry
        # (event_type, timestamp) pairs — pruned lazily
        self._log: list[tuple[str, float]] = []

    # ------------------------------------------------------------------
    # Public event handlers
    # ------------------------------------------------------------------

    def on_deployment_detected(self, controller: str, app: str) -> None:
        """Triggered when a new BT appears or a BT's response time shifts >50%.

        Invalidates:
          - bt_registry for this app
          - golden_registry for ALL BTs in this app (pre-deployment golden invalid)

        Logs: WARNING with detection details.
        """
        try:
            self._bt.invalidate(controller, app)
            self._golden.invalidate_app(controller, app, reason="deployment_detected")
            # Also clear the module-level cache for this app's BTs
            import asyncio
            asyncio.get_event_loop().call_soon_threadsafe(
                asyncio.ensure_future,
                cache_module.invalidate_prefix(f":{controller}:business_transactions:{app}"),
            )
        except Exception:
            logger.exception(
                "cache_invalidator: on_deployment_detected failed (non-fatal)"
            )
        self._record("deployment")
        logger.warning(
            "cache_invalidator: deployment detected for %s/%s — "
            "bt_registry + golden_registry invalidated",
            controller, app,
        )

    def on_app_restart_detected(self, controller: str, app: str) -> None:
        """Triggered when APP_CRASH or NODE_RESTART health violation appears.

        Invalidates:
          - golden_registry for ALL BTs in this app (post-restart = new baseline)
          - Does NOT invalidate bt_registry (BTs survive restarts)

        Logs: WARNING.
        """
        try:
            self._golden.invalidate_app(controller, app, reason="app_restart_detected")
        except Exception:
            logger.exception(
                "cache_invalidator: on_app_restart_detected failed (non-fatal)"
            )
        self._record("restart")
        logger.warning(
            "cache_invalidator: app restart detected for %s/%s — "
            "golden_registry invalidated (bt_registry preserved)",
            controller, app,
        )

    def on_manual_golden_override(
        self,
        controller: str,
        app: str,
        bt: str,
        new_guid: str,
        promoted_by: str,
    ) -> None:
        """Triggered when an SRE calls set_golden_snapshot.

        Invalidates:
          - golden_registry entry for this specific BT only

        Logs: INFO with full audit trail.
        """
        try:
            self._golden.invalidate(controller, app, bt, reason="manual_override")
        except Exception:
            logger.exception(
                "cache_invalidator: on_manual_golden_override failed (non-fatal)"
            )
        self._record("manual_override")
        logger.info(
            "cache_invalidator: manual golden override for %s/%s/%s "
            "new_guid=%s promoted_by=%s",
            controller, app, bt, new_guid, promoted_by,
        )

    def on_cache_validation_failure(
        self,
        key: str,
        data_type: str,
        error: str,
    ) -> None:
        """Triggered when Pydantic validation fails on a cache read.

        Evicts the specific cache entry. Never raises.
        Logs: WARNING.
        """
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(cache_module.delete(key))
            else:
                loop.run_until_complete(cache_module.delete(key))
        except Exception:
            logger.exception(
                "cache_invalidator: on_cache_validation_failure eviction failed"
                " (non-fatal)"
            )
        self._record("validation_failure")
        logger.warning(
            "cache_invalidator: validation failure key=%s type=%s error=%s — evicted",
            key, data_type, error,
        )

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Return invalidation counts for the last hour."""
        now = time.time()
        cutoff = now - 3600
        self._log = [(t, ts) for t, ts in self._log if ts > cutoff]
        log = self._log
        return {
            "deployment_triggered": sum(1 for t, _ in log if t == "deployment"),
            "restart_triggered": sum(1 for t, _ in log if t == "restart"),
            "manual_override": sum(1 for t, _ in log if t == "manual_override"),
            "validation_failure": sum(1 for t, _ in log if t == "validation_failure"),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _record(self, event_type: str) -> None:
        self._log.append((event_type, time.time()))
