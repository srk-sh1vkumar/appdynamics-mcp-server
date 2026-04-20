# Developer Guide

## Adding a New Tool

### 1. Add the API method to `client/appd_client.py`

```python
async def get_my_data(self, app_name: str, duration_mins: int = 60) -> list[dict]:
    url = f"{self._base}/rest/applications/{app_name}/my-endpoint"
    resp = await self._get(url, params={"time-range-type": "BEFORE_NOW", "duration-in-mins": duration_mins})
    return resp.json()
```

### 2. Add types to `models/types.py`

Use `@dataclass` for internal domain objects, Pydantic `BaseModel` for API boundaries.

### 3. Register the tool in `main.py`

```python
@mcp.tool()
async def my_new_tool(
    app_name: str,
    duration_mins: int = 60,
    controller_name: str = "production",
    upn: str = "dev@local",
) -> str:
    """One-line description shown to the LLM."""
    start = time.monotonic()
    rate_msg = await check_and_wait(upn)
    role = await _get_role(upn, controller_name)
    require_permission(role, "my_new_tool")
    status = "success"
    try:
        # Use cache_keys module for consistent UPN-namespaced keys
        cache_key = cache_keys.make_key(upn, controller_name, "my_data", app_name)
        cached = await cache_mod.get(cache_key, upn)
        if cached:
            return _wrap_cached(cached, rate_msg)

        client = get_client(controller_name)
        data = await client.get_my_data(app_name, duration_mins)
        await cache_mod.set(cache_key, data, cache_mod.CACHE_TTLS["metrics"])
        out = truncate_to_budget(sanitize_and_wrap(data), "my_new_tool")
        return (rate_msg + "\n" + out) if rate_msg else out
    except Exception:
        status = "error"
        raise
    finally:
        audit_log("my_new_tool", upn, role.value, {"app_name": app_name},
                  controller_name, int((time.monotonic() - start) * 1000), status)
```

### 4. License gate (if required)

If the tool requires a licensed module, add near the top of the tool function:

```python
license_check.require_license("snapshots")  # or "eum", "analytics", "db_visibility"
```

And register the tool in `services/license_check.py`:

```python
_MODULE_TOOLS: dict[str, list[str]] = {
    "snapshots": [..., "my_new_tool"],
    ...
}
```

### 5. Write tests

In `tests/unit/test_tools.py`:

```python
@pytest.mark.asyncio
class TestMyNewTool:
    async def test_happy_path(self, patched_main):
        result = await _tool("my_new_tool")(app_name="ecommerce-app", controller_name="test")
        assert result is not None
        assert "<appd_data>" in result

    async def test_http_500_propagates(self, patched_main):
        patched_main.get_my_data.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock(status_code=500)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await _tool("my_new_tool")(app_name="ecommerce-app", controller_name="test")
```

### 6. Update the tool count

Update the tool count in `README.md` and in the `summary.tools_count` field of `APPDYNAMICS_MCP_ENHANCEMENT_TRACKER.yaml`.

---

## Working with Registries

The three registries (`AppsRegistry`, `BTRegistry`, `GoldenRegistry`) are process-global singletons instantiated in `main.py`. Update them after every successful data fetch so the registry stays fresh for mid-incident continuity across restarts.

### Updating on fetch

```python
# After a successful list_applications call:
_apps_registry.update(controller_name, [AppEntry.from_raw(a, controller_name) for a in apps])

# After a successful get_business_transactions call:
_bt_registry.update(controller_name, app_name, [BTEntry.from_enriched(b) for b in bts])
```

### Deployment and restart detection

`get_business_transactions` checks for deployment by comparing the cached BT count with the fresh count. A shift of more than 2 BTs triggers invalidation:

```python
if old_total > 0 and abs(new_total - old_total) > 2:
    _cache_invalidator.on_deployment_detected(controller_name, app_name)
```

`get_health_violations` triggers restart detection when `APP_CRASH` or `NODE_RESTART` appears:

```python
if violation.get("type") in ("APP_CRASH", "NODE_RESTART"):
    _cache_invalidator.on_app_restart_detected(controller_name, affected_app)
```

Do not add detection logic outside these two tools unless there is a clear signal to act on.

---

## Cache Key Conventions

Always use `utils/cache_keys.py` to build cache keys — never construct them inline.

```python
from utils import cache_keys

key = cache_keys.make_key(upn, controller, "my_data_type", app_name)
# → "alice@acme.com:production:my_data_type:my_app"

key = cache_keys.bt_list_key(upn, controller, app_name)
key = cache_keys.snapshot_list_key(upn, controller, app_name, error_only=True)
key = cache_keys.parsed_snapshot_key(upn, controller, guid)
```

**Golden key** — no UPN, shared across users:

```python
key = cache_keys.golden_key(controller, app_name, bt_name)
# → "__golden__:production:my_app:checkout_bt"
```

Never build a golden key with a UPN prefix — golden baselines are intentionally shared.

---

## Adding a Language Stack Parser

Create `parsers/stack/mylang.py` implementing `parse(stack_trace: str) -> ParsedStack`.

`ParsedStack` fields:

| Field | Type | Description |
|-------|------|-------------|
| `language` | `StackLanguage` | The detected language |
| `culprit_frame` | `StackFrame \| None` | First app-owned frame |
| `caused_by_chain` | `list[str]` | Exception messages in causal order |
| `top_app_frames` | `list[StackFrame]` | Up to 5 app-owned frames |
| `full_stack_preview` | `str` | First ~5 lines of the raw trace |

`StackFrame` fields: `class_name`, `method_name`, `file_name`, `line_number`, `is_app_frame`.

Register the language in `parsers/snapshot_parser.py` `detect_language()` and the dispatch dict.

---

## Testing Patterns

### Patching strategy in `test_tools.py`

```python
patch("main.get_client", return_value=mock_appd_client)       # local binding in main.py
patch("main.check_and_wait", new=AsyncMock(return_value=None))
patch("main._get_role", new=AsyncMock(return_value=AppDRole.TROUBLESHOOT))
patch("main.require_permission")                               # no-op
patch("services.license_check.require_license")               # no-op (module reference)
patch("utils.cache.get", new=AsyncMock(return_value=None))    # always cache miss
```

**Key rule**: `from x import y` creates a local binding. Always patch the name in the module that uses it (`main.get_client`), not the source (`client.appd_client.get_client`).

### Cache bypass

Always patch `utils.cache.get` to return `None` in tool tests — otherwise a cached result from a previous test will skip the mock client call and the expected error won't be raised.

---

## Security Rules

1. **Never return raw AppD data** — always pass through `sanitize_and_wrap()`.
2. **Always call `require_permission`** — before any data fetch.
3. **Always call `audit_log`** in the `finally` block — even on error.
4. **Cache keys must include UPN** — use `cache_keys.make_key(upn, controller, type, id)`.
5. **NEVER_CACHE enforcement** — do not cache `raw_snapshot_json`, `adql_query_results`, or `active_health_violations_realtime`. These are in `utils/cache.py:NEVER_CACHE`.
6. **Raw snapshot JSON must never be stored** — only `CachedSnapshotAnalysis` (the parsed, PII-redacted result) may be cached. The raw payload (~500 KB) is discarded after parsing.
7. **Golden keys have no UPN** — `golden_key()` is shared across users by design. Never add a UPN to a golden cache key.
