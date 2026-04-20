"""
services/bt_naming.py

Business Transaction naming convention analysis.

Design decisions:
- Convention detection uses frequency voting over the dominant structural
  signal in BT names, not a hard-coded expected pattern. This means the
  tool adapts to whatever convention is already in use at each org.
- Four signals are scored: URL path style (/noun/verb), HTTP verb prefix
  (GET /path), dot-class style (Class.method), and plain label.
- Consistency score = % of BTs that match the dominant convention.
  Below 70% is "inconsistent"; 70-90% is "mostly consistent"; 90%+ is
  "consistent".
- Outliers are BTs that don't match the dominant convention — these are the
  actionable outputs. Maximum 20 reported to stay within token budget.
- suggest_name() proposes a canonical name for a given BT based on the
  dominant convention so Claude can draft remediation suggestions.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

# ---------------------------------------------------------------------------
# Convention detection
# ---------------------------------------------------------------------------

_URL_PATH = re.compile(r"^(/[\w\-\.~%:@]+)+$")
_HTTP_VERB = re.compile(r"^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+/", re.I)
_DOT_CLASS = re.compile(r"^\w[\w$]*\.\w[\w$]*")
_PASCAL_LABEL = re.compile(r"^[A-Z][a-z]+([A-Z][a-z]*)+$")
_SNAKE_LABEL = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)+$")


def _classify(name: str) -> str:
    if _HTTP_VERB.match(name):
        return "http_verb_prefix"
    if _URL_PATH.match(name):
        return "url_path"
    if _DOT_CLASS.match(name):
        return "dot_class"
    if _PASCAL_LABEL.match(name):
        return "pascal_label"
    if _SNAKE_LABEL.match(name):
        return "snake_label"
    return "unclassified"


_CONVENTION_LABELS: dict[str, str] = {
    "http_verb_prefix": "HTTP verb + path (e.g. GET /api/orders)",
    "url_path": "URL path (e.g. /api/v1/orders)",
    "dot_class": "Class.method (e.g. OrderService.create)",
    "pascal_label": "PascalCase label (e.g. PlaceOrder)",
    "snake_label": "snake_case label (e.g. place_order)",
    "unclassified": "No clear convention",
}


def detect_convention(bt_names: list[str]) -> str:
    """Return the dominant convention identifier for a list of BT names."""
    if not bt_names:
        return "unclassified"
    counts = Counter(_classify(n) for n in bt_names)
    return counts.most_common(1)[0][0]


# ---------------------------------------------------------------------------
# Consistency scoring
# ---------------------------------------------------------------------------

_CONSISTENCY_THRESHOLDS = {"consistent": 90.0, "mostly_consistent": 70.0}


def consistency_label(score: float) -> str:
    if score >= _CONSISTENCY_THRESHOLDS["consistent"]:
        return "consistent"
    if score >= _CONSISTENCY_THRESHOLDS["mostly_consistent"]:
        return "mostly_consistent"
    return "inconsistent"


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------


def analyze_bt_naming(bts: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Analyse a list of BT dicts (must have 'name' key).

    Returns a report with:
    - dominant_convention: the most common pattern
    - consistency_score: % of BTs matching that pattern
    - consistency_label: consistent / mostly_consistent / inconsistent
    - outliers: BTs that don't match the dominant pattern (max 20)
    - breakdown: count per convention type
    - recommendation: human-readable guidance
    """
    names = [b.get("name", "") for b in bts if b.get("name")]
    if not names:
        return {
            "dominant_convention": "unknown",
            "consistency_score": 0.0,
            "consistency_label": "inconsistent",
            "breakdown": {},
            "outliers": [],
            "recommendation": "No BTs found to analyse.",
        }

    classified = [(n, _classify(n)) for n in names]
    counts = Counter(c for _, c in classified)
    dominant = counts.most_common(1)[0][0]
    match_count = counts[dominant]
    score = round(match_count / len(names) * 100, 1)

    outliers = [
        {"name": n, "detected_pattern": c}
        for n, c in classified
        if c != dominant
    ][:20]

    breakdown = {
        _CONVENTION_LABELS.get(k, k): v
        for k, v in counts.most_common()
    }

    recommendation = _build_recommendation(dominant, score, outliers, names)

    return {
        "dominant_convention": _CONVENTION_LABELS.get(dominant, dominant),
        "convention_id": dominant,
        "consistency_score": score,
        "consistency_label": consistency_label(score),
        "total_bts_analysed": len(names),
        "breakdown": breakdown,
        "outliers": outliers,
        "recommendation": recommendation,
    }


def _build_recommendation(
    dominant: str,
    score: float,
    outliers: list[dict[str, Any]],
    all_names: list[str],
) -> str:
    label = _CONVENTION_LABELS.get(dominant, dominant)
    if score >= 90:
        return (
            f"Naming is consistent: {score}% of BTs follow '{label}'. "
            "No remediation needed."
        )

    count = len(outliers)
    examples = ", ".join(f"'{o['name']}'" for o in outliers[:3])
    action = _suggest_action(dominant)
    return (
        f"Naming is {consistency_label(score)}: {score}% follow '{label}'. "
        f"{count} BT(s) deviate — e.g. {examples}. "
        f"{action}"
    )


def _suggest_action(dominant: str) -> str:
    if dominant == "http_verb_prefix":
        return (
            "Rename deviating BTs to 'VERB /path' format. "
            "In AppDynamics, update the custom BT match rule 'name' field "
            "or use the transaction detection rename action."
        )
    if dominant == "url_path":
        return (
            "Rename deviating BTs to match the '/{path}' URL structure. "
            "Remove HTTP verb prefixes and class.method names from BT rules."
        )
    if dominant == "dot_class":
        return (
            "Rename deviating BTs to 'ClassName.methodName' format. "
            "Check custom entry-point rules for servlet/POJO overrides."
        )
    if dominant == "pascal_label":
        return (
            "Rename deviating BTs to PascalCase labels. "
            "Review auto-naming rules and update custom match rule name fields."
        )
    return (
        "Standardise BT names to a single convention. "
        "Use AppDynamics custom BT detection rules with explicit 'rename to' actions."
    )


# ---------------------------------------------------------------------------
# Naming suggestion
# ---------------------------------------------------------------------------


def suggest_name(raw_name: str, target_convention: str) -> str:
    """Suggest a canonical BT name in the target convention from a raw BT name."""
    # Strip leading/trailing whitespace and slashes for processing
    clean = raw_name.strip().lstrip("/")

    # Extract meaningful tokens (words, ignoring HTTP verbs and path separators)
    tokens = re.split(r"[\s/\.\-_]+", clean)
    tokens = [t for t in tokens if t and t.upper() not in (
        "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "API", "V1", "V2"
    )]

    if not tokens:
        return raw_name

    if target_convention == "pascal_label":
        return "".join(t.capitalize() for t in tokens)
    if target_convention == "snake_label":
        return "_".join(t.lower() for t in tokens)
    if target_convention == "url_path":
        return "/" + "/".join(t.lower() for t in tokens)
    if target_convention == "http_verb_prefix":
        # Can't reliably infer the HTTP verb — return URL path style
        return "GET /" + "/".join(t.lower() for t in tokens)
    if target_convention == "dot_class":
        if len(tokens) >= 2:
            return f"{tokens[0].capitalize()}.{tokens[-1]}"
        return tokens[0].capitalize()

    return raw_name
