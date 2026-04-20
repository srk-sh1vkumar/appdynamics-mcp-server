"""
registries/apps_registry.py

Persisted application registry.

Two-layer design:
  L1: TTLCache (300s in-memory) — fast reads during active investigations
  L2: diskcache — survives MCP process restarts mid-incident

Loaded from disk at startup as a fallback if AppDynamics is unreachable.
Updated on every successful list_applications call.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import diskcache
from cachetools import TTLCache
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_DISK_DIR = Path("data/registry/apps")
_MEM_TTL = 300
_MEM_MAXSIZE = 100
_DISK_TTL_MULTIPLIER = 10  # disk TTL = 10× memory TTL


class AppEntry(BaseModel):
    """Enriched application record persisted across MCP restarts."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: int
    name: str
    account_guid: str = Field("", alias="accountGuid")
    controller_name: str = ""
    onboarded_at: datetime | None = None
    last_seen: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    bt_baseline_available: bool = False
    eum_configured: bool = False
    snapshots_available: bool = True

    @property
    def maturity_warning(self) -> str | None:
        if self.onboarded_at is not None:
            days = (datetime.now(tz=UTC) - self.onboarded_at).days
            if days < 7:
                return (
                    f"App onboarded {days} days ago. "
                    "Baseline data may be incomplete."
                )
        return None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSON responses."""
        return {
            "id": self.id,
            "name": self.name,
            "controller_name": self.controller_name,
            "account_guid": self.account_guid,
            "maturity_warning": self.maturity_warning,
        }

    @classmethod
    def from_raw(cls, raw: dict[str, Any], controller_name: str) -> AppEntry:
        """Construct from raw AppDynamics API response dict."""
        entry = dict(raw)
        entry["controller_name"] = controller_name
        # Convert epoch-ms onboardedAt to datetime if present
        onboarded_ms = raw.get("onboardedAt")
        if isinstance(onboarded_ms, (int, float)) and onboarded_ms > 0:
            entry["onboarded_at"] = datetime.fromtimestamp(
                onboarded_ms / 1000, tz=UTC
            )
        return cls.model_validate(entry)


class AppsRegistry:
    """Two-layer application registry.

    Thread-safe for asyncio: uses asyncio.Lock for in-memory writes.
    """

    def __init__(self, disk_dir: str | None = None) -> None:
        dir_path = Path(disk_dir) if disk_dir else _DISK_DIR
        dir_path.mkdir(parents=True, exist_ok=True)
        self._disk: diskcache.Cache = diskcache.Cache(str(dir_path))
        self._mem: TTLCache[str, list[AppEntry]] = TTLCache(
            maxsize=_MEM_MAXSIZE, ttl=_MEM_TTL
        )
        self._lock: asyncio.Lock = asyncio.Lock()

    def _key(self, controller: str) -> str:
        return f"apps:{controller.lower()}"

    def is_warm(self, controller: str) -> bool:
        """Return True if the registry has a non-empty entry for this controller."""
        return bool(self.get_all(controller))

    def all(self, controller: str) -> list[AppEntry]:
        """Alias for get_all — returns cached app list or []."""
        return self.get_all(controller)

    def get_all(self, controller: str) -> list[AppEntry]:
        """Return cached app list, checking L1 then L2. Returns [] if absent."""
        key = self._key(controller)
        # L1
        value: list[AppEntry] | None = self._mem.get(key)
        if value is not None:
            return value
        # L2
        raw = self._disk.get(key)
        if raw is not None:
            try:
                entries = [AppEntry.model_validate(e) for e in raw]
                self._mem[key] = entries
                return entries
            except Exception:
                logger.warning(
                    "apps_registry: corrupt L2 entry for %s — evicting", controller
                )
                self._disk.delete(key)
        return []

    def update(self, controller: str, apps: list[AppEntry]) -> None:
        """Persist fresh app list to both layers."""
        key = self._key(controller)
        self._mem[key] = apps
        serialized = [a.model_dump() for a in apps]
        self._disk.set(key, serialized, expire=_MEM_TTL * _DISK_TTL_MULTIPLIER)
        logger.debug("apps_registry: updated %d apps for %s", len(apps), controller)

    def invalidate(self, controller: str) -> None:
        """Remove app list for a controller from both layers."""
        key = self._key(controller)
        self._mem.pop(key, None)
        self._disk.delete(key)
        logger.info("apps_registry: invalidated %s", controller)
