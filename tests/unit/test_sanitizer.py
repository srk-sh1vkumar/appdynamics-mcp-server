"""
tests/unit/test_sanitizer.py

Unit tests for utils/sanitizer.py.

Coverage targets:
- redact_string: email, JWT, Bearer token, 16-digit card number
- redact_dict: top-level and nested sensitive keys
- wrap_as_untrusted: <appd_data> XML wrapper format
- sanitize_and_wrap: combined redaction + wrapping
- sanitize: redact without wrapping (dict input)
"""

from __future__ import annotations

from utils.sanitizer import (
    redact_dict,
    redact_string,
    sanitize,
    sanitize_and_wrap,
    wrap_as_untrusted,
)

# ---------------------------------------------------------------------------
# redact_string
# ---------------------------------------------------------------------------

class TestRedactString:
    def test_email_redacted(self):
        result = redact_string("Contact alice@example.com for details")
        assert "alice@example.com" not in result
        assert "[EMAIL_REDACTED]" in result

    def test_email_at_start(self):
        result = redact_string("alice@example.com is the owner")
        assert "alice@example.com" not in result

    def test_jwt_redacted(self):
        # Minimal realistic JWT (three base64url segments)
        jwt = (
            "eyJhbGciOiJSUzI1NiJ9"
            ".eyJzdWIiOiJ1c2VyMSJ9"
            ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"
        )
        result = redact_string(f"Authorization: Bearer {jwt}")
        assert jwt not in result

    def test_bearer_token_redacted(self):
        result = redact_string("Authorization: Bearer mysecrettoken123")
        assert "mysecrettoken123" not in result
        assert "[TOKEN_REDACTED]" in result

    def test_card_number_redacted(self):
        result = redact_string("Card: 4532015112830366")
        assert "4532015112830366" not in result
        assert "[CARD_REDACTED]" in result

    def test_card_with_spaces_redacted(self):
        result = redact_string("Card: 4532 0151 1283 0366")
        assert "4532 0151 1283 0366" not in result

    def test_no_pii_unchanged(self):
        text = "Average response time was 1200ms with error rate 3.5%"
        assert redact_string(text) == text

    def test_multiple_pii_redacted(self):
        text = "User alice@example.com used card 4532015112830366"
        result = redact_string(text)
        assert "alice@example.com" not in result
        assert "4532015112830366" not in result

    def test_empty_string(self):
        assert redact_string("") == ""


# ---------------------------------------------------------------------------
# redact_dict
# ---------------------------------------------------------------------------

class TestRedactDict:
    def test_password_key_redacted(self):
        data = {"name": "alice", "password": "s3cr3t"}
        result = redact_dict(data)
        assert result["password"] == "[REDACTED]"
        assert result["name"] == "alice"

    def test_api_key_redacted(self):
        data = {"api_key": "abc123secret"}
        result = redact_dict(data)
        assert result["api_key"] == "[REDACTED]"

    def test_token_key_redacted(self):
        data = {"access_token": "Bearer eyJ..."}
        result = redact_dict(data)
        assert result["access_token"] == "[REDACTED]"

    def test_secret_key_redacted(self):
        data = {"client_secret": "verysecret"}
        result = redact_dict(data)
        assert result["client_secret"] == "[REDACTED]"

    def test_nested_dict_redacted(self):
        data = {
            "user": {
                "name": "alice",
                "password": "hunter2",
            }
        }
        result = redact_dict(data)
        assert result["user"]["password"] == "[REDACTED]"
        assert result["user"]["name"] == "alice"

    def test_list_of_dicts_redacted(self):
        data = {
            "credentials": [
                {"name": "alice", "api_key": "key1"},
                {"name": "bob", "api_key": "key2"},
            ]
        }
        result = redact_dict(data)
        assert result["credentials"][0]["api_key"] == "[REDACTED]"
        assert result["credentials"][1]["api_key"] == "[REDACTED]"

    def test_string_values_with_pii_redacted(self):
        data = {"message": "Contact alice@example.com for support"}
        result = redact_dict(data)
        assert "alice@example.com" not in result["message"]

    def test_non_sensitive_keys_unchanged(self):
        data = {"application": "ecommerce-app", "error_rate": 3.5}
        result = redact_dict(data)
        assert result["application"] == "ecommerce-app"
        assert result["error_rate"] == 3.5

    def test_empty_dict(self):
        assert redact_dict({}) == {}

    def test_case_insensitive_sensitive_keys(self):
        # Keys like "Password", "API_KEY" should also be redacted
        data = {"Password": "oops"}
        result = redact_dict(data)
        assert result["Password"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# wrap_as_untrusted
# ---------------------------------------------------------------------------

class TestWrapAsUntrusted:
    def test_wraps_in_appd_data_tags(self):
        content = "some data"
        result = wrap_as_untrusted(content)
        assert result.startswith("<appd_data>")
        assert result.endswith("</appd_data>")
        assert "some data" in result

    def test_dict_serialised_as_json(self):
        data = {"key": "value"}
        result = wrap_as_untrusted(data)
        assert "<appd_data>" in result
        assert '"key"' in result

    def test_list_serialised(self):
        data = [1, 2, 3]
        result = wrap_as_untrusted(data)
        assert "<appd_data>" in result
        assert "1" in result

    def test_empty_string_wrapped(self):
        result = wrap_as_untrusted("")
        assert "<appd_data>" in result
        assert "</appd_data>" in result


# ---------------------------------------------------------------------------
# sanitize_and_wrap
# ---------------------------------------------------------------------------

class TestSanitizeAndWrap:
    def test_redacts_and_wraps(self):
        data = {
            "message": "User alice@example.com triggered error",
            "password": "s3cr3t",
        }
        result = sanitize_and_wrap(data)
        assert "<appd_data>" in result
        assert "alice@example.com" not in result
        assert "s3cr3t" not in result

    def test_non_pii_preserved(self):
        data = {"application": "ecommerce-app", "error_rate": 3.5}
        result = sanitize_and_wrap(data)
        assert "ecommerce-app" in result

    def test_string_input(self):
        result = sanitize_and_wrap("Call alice@example.com")
        assert "alice@example.com" not in result
        assert "<appd_data>" in result


# ---------------------------------------------------------------------------
# sanitize (dict in, dict out — no wrapping)
# ---------------------------------------------------------------------------

class TestSanitize:
    def test_returns_string(self):
        data = {"name": "alice", "password": "hunter2"}
        result = sanitize(data)
        assert isinstance(result, str)

    def test_sensitive_keys_removed(self):
        import json
        data = {"name": "alice", "api_key": "abc123"}
        result = sanitize(data)
        parsed = json.loads(result)
        assert parsed["api_key"] == "[REDACTED]"

    def test_pii_in_values_redacted(self):
        import json
        data = {"note": "Contact alice@example.com"}
        result = sanitize(data)
        parsed = json.loads(result)
        assert "alice@example.com" not in parsed["note"]
