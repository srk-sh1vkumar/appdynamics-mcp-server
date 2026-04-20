---
name: Enhancement 002 Complete
description: Test suite execution — 121 tests passing, 63% coverage
type: project
---

# Enhancement 002 — Test Suite Execution & Coverage Report

**Status**: Complete | **Date**: 2026-04-12 | **Actual hours**: 4

## Results

| Metric | Value |
|--------|-------|
| Total tests | 121 |
| Passing | 121 |
| Failing | 0 |
| Run time | 0.41s |
| Overall coverage | 63% |

### Coverage by module

| Module | Coverage |
|--------|----------|
| `services/bt_classifier.py` | 100% |
| `utils/sanitizer.py` | 98% |
| `parsers/stack/python_parser.py` | 97% |
| `parsers/stack/java.py` | 96% |
| `parsers/stack/nodejs.py` | 96% |
| `parsers/stack/dotnet.py` | 95% |
| `parsers/snapshot_parser.py` | 82% |
| `main.py` | 39% (expected — integration paths need real HTTP) |
| `client/appd_client.py` | 26% (expected — mocked in unit tests) |

## Bugs Fixed During Testing

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| `TTLCache.set()` AttributeError | `cachetools.TTLCache` has no `.set()` method | Use dict-style `_mem[key] = value` |
| `.NET` parser line number = 0 | Regex `([\w\.<>\[\], ]+)\s+in` didn't skip method args `(...)` before `in` | Added `\(.*?\)` to regex |
| `stitch_async_trace` propagated per-app 404 | No try/except around per-app `list_snapshots` call | Wrapped in `try/except`, append to `missing`, `continue` |
| `detect_language` returned JAVA for unknown traces | Default fall-through was JAVA | Changed to return `StackLanguage.UNKNOWN` |
| `wrap_as_untrusted` rejected non-str input | Type annotation was `str` only | Changed to `Any`, JSON-serialise non-strings |
| `sanitize()` returns `str` not `dict` | Function JSON-serialises output | Tests updated to `json.loads(result)` before asserting |
| `patch("client.appd_client.get_client")` had no effect | `from ... import get_client` creates local binding | Changed to `patch("main.get_client")` |
| MockVaultClient env var name wrong in docstring | Docstring said `SECRET_APPDYNAMICS_*`, actual key is `APPDYNAMICS_*` | Fixed docstring, `.env.example`, and README |

## Test File Inventory

| File | Tests | Coverage target |
|------|-------|----------------|
| `tests/unit/test_snapshot_parser.py` | 37 | parsers/, snapshot logic |
| `tests/unit/test_bt_classifier.py` | — | `services/bt_classifier.py` |
| `tests/unit/test_sanitizer.py` | — | `utils/sanitizer.py` |
| `tests/unit/test_tools.py` | — | `main.py` tool functions |
