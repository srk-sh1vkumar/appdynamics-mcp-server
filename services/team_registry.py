"""
services/team_registry.py

Team-to-application scoping for multi-tenant MCP deployments.

Design decisions:
- Teams are defined in controllers.json under a top-level "teams" key.
  No database, no external service — the config file is the source of truth.
- app_pattern uses fnmatch glob syntax ("payments-*", "checkout-*", "*").
  A pattern of "*" grants access to all apps (used for platform/SRE teams).
- upn_domains is a list of email suffixes. A UPN "@payments.corp" matches
  domain "@payments.corp". If a UPN matches no team, the fallback is
  "no team" — the caller sees only apps in controllers they have AppD access
  to, but list_applications is unscoped (legacy behaviour).
- get_team_for_upn() returns the FIRST matching team. Teams should be
  configured from narrowest to broadest pattern (payments before platform).
- filter_apps() applies the team's app_pattern to a list of app names.
  It returns the full list if the team has pattern "*" or is None.
- This module is stateless — load it once at startup via load_teams().
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Team:
    name: str
    app_pattern: str          # fnmatch glob, e.g. "payments-*" or "*"
    upn_domains: list[str]    # e.g. ["@payments.corp", "@sre.corp"]
    controllers: list[str]    # controller names this team can access


_teams: list[Team] = []


def load_teams(config: dict[str, Any]) -> None:
    """Populate the module-level team list from a parsed controllers.json dict."""
    global _teams
    raw = config.get("teams", [])
    _teams = [
        Team(
            name=t["name"],
            app_pattern=t.get("app_pattern", "*"),
            upn_domains=t.get("upn_domains", []),
            controllers=t.get("controllers", []),
        )
        for t in raw
    ]
    logger.info(
        "team_registry: loaded %d team(s): %s",
        len(_teams),
        [t.name for t in _teams],
    )


def get_teams() -> list[Team]:
    return list(_teams)


def get_team_for_upn(upn: str) -> Team | None:
    """
    Return the first team whose upn_domains matches the caller's UPN suffix.
    Returns None if the UPN matches no configured team (unscoped access).
    """
    upn_lower = upn.lower()
    for team in _teams:
        for domain in team.upn_domains:
            if upn_lower.endswith(domain.lower()):
                return team
    return None


def filter_apps(app_names: list[str], team: Team | None) -> list[str]:
    """
    Filter a list of application names to those accessible by the given team.
    Returns the full list if team is None or pattern is "*".
    """
    if team is None or team.app_pattern == "*":
        return app_names
    pattern = team.app_pattern.lower()
    return [n for n in app_names if fnmatch.fnmatch(n.lower(), pattern)]


def filter_app_entries(entries: list[Any], team: Team | None) -> list[Any]:
    """
    Filter a list of AppEntry (or any object with a .name attribute) by team pattern.
    """
    if team is None or team.app_pattern == "*":
        return entries
    pattern = team.app_pattern.lower()
    return [e for e in entries if fnmatch.fnmatch(e.name.lower(), pattern)]


def can_access_controller(upn: str, controller_name: str) -> bool:
    """
    Return True if the UPN's team can access the given controller.
    If the UPN has no team, access is unrestricted (legacy behaviour).
    """
    team = get_team_for_upn(upn)
    if team is None:
        return True
    return controller_name in team.controllers or not team.controllers
