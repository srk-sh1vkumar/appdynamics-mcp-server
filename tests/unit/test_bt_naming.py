"""
tests/unit/test_bt_naming.py

Unit tests for services/bt_naming.py — convention detection, consistency
scoring, outlier identification, and name suggestion.
"""

from __future__ import annotations

from services.bt_naming import (
    analyze_bt_naming,
    consistency_label,
    detect_convention,
    suggest_name,
)

# ---------------------------------------------------------------------------
# detect_convention
# ---------------------------------------------------------------------------


class TestDetectConvention:
    def test_url_path_majority(self):
        names = ["/api/orders", "/api/users", "/api/payments", "PlaceOrder"]
        assert detect_convention(names) == "url_path"

    def test_http_verb_prefix_majority(self):
        names = ["GET /orders", "POST /orders", "PUT /orders/123", "/api/users"]
        assert detect_convention(names) == "http_verb_prefix"

    def test_dot_class_majority(self):
        names = ["OrderService.create", "UserService.get", "PaymentController.process"]
        assert detect_convention(names) == "dot_class"

    def test_pascal_label_majority(self):
        names = ["PlaceOrder", "GetUser", "ProcessPayment", "CancelOrder"]
        assert detect_convention(names) == "pascal_label"

    def test_snake_label_majority(self):
        names = ["place_order", "get_user", "process_payment", "cancel_order"]
        assert detect_convention(names) == "snake_label"

    def test_empty_list_returns_unclassified(self):
        assert detect_convention([]) == "unclassified"

    def test_all_unclassified(self):
        names = ["???", "!!!ORDER", "123-abc"]
        assert detect_convention(names) == "unclassified"

    def test_single_name(self):
        assert detect_convention(["/api/v1/orders"]) == "url_path"


# ---------------------------------------------------------------------------
# consistency_label
# ---------------------------------------------------------------------------


class TestConsistencyLabel:
    def test_above_90_is_consistent(self):
        assert consistency_label(95.0) == "consistent"
        assert consistency_label(90.0) == "consistent"

    def test_70_to_90_is_mostly_consistent(self):
        assert consistency_label(80.0) == "mostly_consistent"
        assert consistency_label(70.0) == "mostly_consistent"

    def test_below_70_is_inconsistent(self):
        assert consistency_label(69.9) == "inconsistent"
        assert consistency_label(0.0) == "inconsistent"


# ---------------------------------------------------------------------------
# analyze_bt_naming
# ---------------------------------------------------------------------------


class TestAnalyzeBtNaming:
    def _bts(self, names):
        return [{"name": n, "id": i} for i, n in enumerate(names)]

    def test_empty_list(self):
        result = analyze_bt_naming([])
        assert result["dominant_convention"] == "unknown"
        assert result["consistency_score"] == 0.0
        assert result["outliers"] == []

    def test_fully_consistent_url_paths(self):
        bts = self._bts(["/api/orders", "/api/users", "/api/payments"])
        result = analyze_bt_naming(bts)
        assert result["consistency_label"] == "consistent"
        assert result["consistency_score"] == 100.0
        assert result["outliers"] == []

    def test_inconsistent_mix(self):
        bts = self._bts([
            "/api/orders", "/api/users", "/api/payments",
            "PlaceOrder", "GET /api/cart",
        ])
        result = analyze_bt_naming(bts)
        assert result["consistency_label"] in ("mostly_consistent", "inconsistent")
        assert len(result["outliers"]) == 2

    def test_outliers_capped_at_20(self):
        # 3 consistent + 25 outliers
        consistent = ["/api/route"] * 3
        outliers = ["PlaceOrder_" + str(i) for i in range(25)]
        bts = self._bts(consistent + outliers)
        result = analyze_bt_naming(bts)
        assert len(result["outliers"]) <= 20

    def test_total_bts_analysed(self):
        bts = self._bts(["/api/a", "/api/b", "BadName"])
        result = analyze_bt_naming(bts)
        assert result["total_bts_analysed"] == 3

    def test_breakdown_sums_to_total(self):
        bts = self._bts(["/api/orders", "PlaceOrder", "GET /api/users"])
        result = analyze_bt_naming(bts)
        total = sum(result["breakdown"].values())
        assert total == 3

    def test_recommendation_present(self):
        bts = self._bts(["/api/orders", "/api/users", "PlaceOrder"])
        result = analyze_bt_naming(bts)
        assert isinstance(result["recommendation"], str)
        assert len(result["recommendation"]) > 10

    def test_consistent_recommendation_text(self):
        bts = self._bts(["/api/a", "/api/b", "/api/c", "/api/d", "/api/e"])
        result = analyze_bt_naming(bts)
        assert "No remediation needed" in result["recommendation"]

    def test_bts_without_name_key_ignored(self):
        bts = [{"id": 1}, {"name": "/api/orders"}, {"name": ""}]
        result = analyze_bt_naming(bts)
        assert result["total_bts_analysed"] == 1


# ---------------------------------------------------------------------------
# suggest_name
# ---------------------------------------------------------------------------


class TestSuggestName:
    def test_pascal_label_from_url_path(self):
        result = suggest_name("/api/place-order", "pascal_label")
        assert result == "PlaceOrder"

    def test_snake_label_from_url_path(self):
        result = suggest_name("/api/place-order", "snake_label")
        assert result == "place_order"

    def test_url_path_from_label(self):
        result = suggest_name("PlaceOrder", "url_path")
        assert result == "/placeorder"

    def test_http_verb_prefix_fallback(self):
        result = suggest_name("PlaceOrder", "http_verb_prefix")
        assert result.startswith("GET /")

    def test_dot_class_two_tokens(self):
        result = suggest_name("/order/create", "dot_class")
        assert result == "Order.create"

    def test_dot_class_single_token(self):
        result = suggest_name("/orders", "dot_class")
        assert result == "Orders"

    def test_strips_api_version_tokens(self):
        result = suggest_name("/api/v1/orders", "pascal_label")
        # api, v1 should be filtered; only "orders" remains
        assert result == "Orders"

    def test_empty_name_passthrough(self):
        result = suggest_name("", "pascal_label")
        assert result == ""

    def test_unknown_convention_passthrough(self):
        original = "some-random-thing"
        result = suggest_name(original, "unclassified")
        assert result == original

    def test_http_verb_stripped_for_pascal(self):
        result = suggest_name("GET /api/orders", "pascal_label")
        assert "GET" not in result
        assert result == "Orders"
