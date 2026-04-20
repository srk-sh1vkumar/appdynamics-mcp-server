"""
auth/vault_client.py

Vault client for fetching OAuth2 credentials.

Design decisions:
- Abstract base class VaultClient with get_secret(path) and get_credentials(path).
- MockVaultClient maps Vault paths to env vars for local dev — never touches disk.
  Path "secret/appdynamics/production/client_id" → env var
  "APPDYNAMICS_PRODUCTION_CLIENT_ID"
- HashiCorpVaultClient supports both KV v1 and KV v2 secret engines.
- factory create_vault_client() reads VAULT_MODE env var to select implementation.
- fetch_credentials_with_retry() implements the 3-attempt exponential backoff
  required by requirements Section 13 (Startup Errors). On exhaustion: sys.exit(1).
"""

from __future__ import annotations

import asyncio
import os
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class VaultCredentials:
    client_id: str
    client_secret: str


class VaultClient(ABC):
    @abstractmethod
    async def get_secret(self, path: str) -> str: ...

    @abstractmethod
    async def get_credentials(self, path: str) -> VaultCredentials: ...


# ---------------------------------------------------------------------------
# Mock (env vars, local dev)
# ---------------------------------------------------------------------------


class MockVaultClient(VaultClient):
    """
    Maps vault paths to env vars.
    secret/appdynamics/production/client_id
      → APPDYNAMICS_PRODUCTION_CLIENT_ID
    """

    async def get_secret(self, path: str) -> str:
        env_key = path.replace("secret/", "").replace("/", "_").upper()
        value = os.environ.get(env_key)
        if not value:
            raise RuntimeError(
                f"[MockVaultClient] Env var '{env_key}' not set "
                f"(from vault path '{path}')"
            )
        return value

    async def get_credentials(self, path: str) -> VaultCredentials:
        client_id = await self.get_secret(f"{path}/client_id")
        client_secret = await self.get_secret(f"{path}/client_secret")
        return VaultCredentials(client_id=client_id, client_secret=client_secret)


# ---------------------------------------------------------------------------
# HashiCorp Vault (production)
# ---------------------------------------------------------------------------


class HashiCorpVaultClient(VaultClient):
    def __init__(self, vault_url: str, vault_token: str) -> None:
        self._url = vault_url.rstrip("/")
        self._token = vault_token

    async def get_secret(self, path: str) -> str:
        url = f"{self._url}/v1/{path}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers={"X-Vault-Token": self._token})
            resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        data: dict[str, Any] = body.get("data", {})
        inner: dict[str, Any] = data.get("data", data)   # KV v2 nests under data.data
        key = path.split("/")[-1]
        value = inner.get("value") or inner.get(key)
        if not value:
            raise RuntimeError(f"[VaultClient] No value at path '{path}'")
        return str(value)

    async def get_credentials(self, path: str) -> VaultCredentials:
        url = f"{self._url}/v1/{path}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers={"X-Vault-Token": self._token})
            resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        data: dict[str, Any] = body.get("data", {})
        inner: dict[str, Any] = data.get("data", data)
        client_id = inner.get("client_id")
        client_secret = inner.get("client_secret")
        if not client_id or not client_secret:
            raise RuntimeError(f"[VaultClient] Missing credentials at '{path}'")
        return VaultCredentials(
            client_id=str(client_id), client_secret=str(client_secret)
        )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_vault_client() -> VaultClient:
    mode = os.environ.get("VAULT_MODE", "mock")
    if mode == "mock":
        print(
            "[vault] MockVaultClient — reading credentials from env vars",
            file=sys.stderr,
        )
        return MockVaultClient()
    vault_url = os.environ.get("VAULT_URL")
    vault_token = os.environ.get("VAULT_TOKEN")
    if not vault_url or not vault_token:
        print(
            "[vault] VAULT_URL + VAULT_TOKEN required when VAULT_MODE != mock",
            file=sys.stderr,
        )
        sys.exit(1)
    return HashiCorpVaultClient(vault_url, vault_token)


# ---------------------------------------------------------------------------
# Startup-safe fetch with retry (3 attempts, exponential backoff)
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3


async def fetch_credentials_with_retry(
    vault: VaultClient, path: str
) -> VaultCredentials:
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return await vault.get_credentials(path)
        except Exception as exc:
            last_exc = exc
            print(
                f"[vault] Attempt {attempt}/{_MAX_RETRIES} failed: {exc}",
                file=sys.stderr,
            )
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(2 ** (attempt - 1))

    print(
        f"[vault] All {_MAX_RETRIES} attempts failed. Last: {last_exc}. Exiting.",
        file=sys.stderr,
    )
    sys.exit(1)
