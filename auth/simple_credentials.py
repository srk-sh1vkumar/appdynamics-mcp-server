"""
auth/simple_credentials.py

Direct env-var credential reader for single-user deployments.

Drop-in replacement for vault_client.py. No Vault dependency, no secret
rotation, no namespace support. Credentials are read directly from env vars
derived from the controller name:

  Controller "production"
    → APPDYNAMICS_PRODUCTION_CLIENT_ID
    → APPDYNAMICS_PRODUCTION_CLIENT_SECRET

  Controller "staging"
    → APPDYNAMICS_STAGING_CLIENT_ID
    → APPDYNAMICS_STAGING_CLIENT_SECRET

TokenManager passes the controller name as the "path" argument to
get_credentials() — SimpleCredentials translates that to env var names.
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass


@dataclass
class Credentials:
    client_id: str
    client_secret: str


class SimpleCredentials:
    """Read OAuth2 credentials directly from environment variables."""

    def get_credentials(self, controller_name: str) -> Credentials:
        prefix = f"APPDYNAMICS_{controller_name.upper()}"
        id_key = f"{prefix}_CLIENT_ID"
        secret_key = f"{prefix}_CLIENT_SECRET"

        client_id = os.environ.get(id_key, "")
        client_secret = os.environ.get(secret_key, "")

        missing = [k for k, v in [(id_key, client_id), (secret_key, client_secret)] if not v]
        if missing:
            print(
                f"[credentials] Missing env vars: {missing}. "
                "Set them in your .env file and restart.",
                file=sys.stderr,
            )
            sys.exit(1)

        return Credentials(client_id=client_id, client_secret=client_secret)


# ---------------------------------------------------------------------------
# Startup-safe fetch with retry (3 attempts, exponential backoff)
# Retained for compatibility with TokenManager._refresh()
# ---------------------------------------------------------------------------

_MAX_RETRIES = 3


async def fetch_credentials_with_retry(
    creds: SimpleCredentials, controller_name: str
) -> Credentials:
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return creds.get_credentials(controller_name)
        except SystemExit:
            raise
        except Exception as exc:
            last_exc = exc
            print(
                f"[credentials] Attempt {attempt}/{_MAX_RETRIES} failed: {exc}",
                file=sys.stderr,
            )
            if attempt < _MAX_RETRIES:
                await asyncio.sleep(2 ** (attempt - 1))

    print(
        f"[credentials] All {_MAX_RETRIES} attempts failed. Last: {last_exc}. Exiting.",
        file=sys.stderr,
    )
    sys.exit(1)
