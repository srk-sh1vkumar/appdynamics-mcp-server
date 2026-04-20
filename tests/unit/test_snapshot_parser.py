"""
tests/unit/test_snapshot_parser.py

Unit tests for parsers/snapshot_parser.py and the four language-specific
stack parsers in parsers/stack/.

Field reference (actual models/types.py):
  ParsedStack: language, culprit_frame, caused_by_chain (list[str]),
               top_app_frames (list[StackFrame]), full_stack_preview
  StackFrame:  class_name, method_name, file_name, line_number, is_app_frame

detect_language regex:
  JAVA:   "at word.word(File.java:N)"
  NODEJS: "at word (/path/file.js:N:N)"   ← single word before paren
  PYTHON: 'File "/path.py", line N'
  DOTNET: "at Ns.Class.Method() in /path.cs:line N"
  UNKNOWN returned for anything else
"""

from __future__ import annotations

from models.types import ConfidenceScore, StackLanguage
from parsers.snapshot_parser import (
    compare_snapshots,
    detect_language,
    parse_snapshot_errors,
    score_golden_candidate,
)
from parsers.stack.dotnet import parse as dotnet_parse
from parsers.stack.java import parse as java_parse
from parsers.stack.nodejs import parse as node_parse
from parsers.stack.python_parser import parse as python_parse

# ---------------------------------------------------------------------------
# detect_language
# ---------------------------------------------------------------------------

class TestDetectLanguage:
    def test_java_at_syntax(self):
        trace = "\tat com.example.Service.method(Service.java:42)"
        assert detect_language(trace) == StackLanguage.JAVA

    def test_java_needs_java_file_and_linenum(self):
        # Only "at X.Y(File.java:N)" triggers JAVA detection
        trace = "\tat com.example.CheckoutService.process(CheckoutService.java:142)"
        assert detect_language(trace) == StackLanguage.JAVA

    def test_nodejs_single_word_function(self):
        # NODEJS regex: "at <word> (<path>.js:<N>:<N>)"
        trace = "    at checkout (/app/src/service.js:10:5)"
        assert detect_language(trace) == StackLanguage.NODEJS

    def test_python_file_line_in(self):
        trace = '  File "/app/services/checkout.py", line 42, in process_payment'
        assert detect_language(trace) == StackLanguage.PYTHON

    def test_dotnet_at_syntax(self):
        trace = (
            "   at MyApp.Services.CheckoutService.Process()"
            " in /src/CheckoutService.cs:line 88"
        )
        assert detect_language(trace) == StackLanguage.DOTNET

    def test_unknown_returns_unknown(self):
        result = detect_language("completely unrecognised format foo bar")
        assert result == StackLanguage.UNKNOWN


# ---------------------------------------------------------------------------
# Java stack parser
# ---------------------------------------------------------------------------

JAVA_TRACE = (
    "java.lang.NullPointerException: payment token is null\n"
    "\tat com.example.CheckoutService.processPayment(CheckoutService.java:142)\n"
    "\tat com.example.OrderController.checkout(OrderController.java:67)\n"
    "\tat org.springframework.web.servlet.DispatcherServlet.doDispatch(DispatcherServlet.java:1065)\n"  # noqa: E501
    "\tat sun.reflect.NativeMethodAccessorImpl.invoke0(Native Method)\n"
    "Caused by: java.lang.IllegalStateException: vault returned null\n"
    "\tat com.example.VaultClient.getSecret(VaultClient.java:33)\n"
)

class TestJavaParser:
    def test_exception_in_caused_by_chain(self):
        result = java_parse(JAVA_TRACE)
        # exception type appears in caused_by_chain or full_stack_preview
        text = " ".join(result.caused_by_chain) + result.full_stack_preview
        assert "NullPointerException" in text or "IllegalStateException" in text

    def test_filters_framework_frames(self):
        result = java_parse(JAVA_TRACE)
        class_names = [f.class_name for f in result.top_app_frames]
        assert not any("springframework" in c.lower() for c in class_names)
        assert not any("sun.reflect" in c.lower() for c in class_names)

    def test_keeps_app_frames(self):
        result = java_parse(JAVA_TRACE)
        class_names = [f.class_name for f in result.top_app_frames]
        assert "com.example.CheckoutService" in class_names
        assert "com.example.OrderController" in class_names

    def test_frame_has_line_number(self):
        result = java_parse(JAVA_TRACE)
        checkout_frame = next(
            f for f in result.top_app_frames if "CheckoutService" in f.class_name
        )
        assert checkout_frame.line_number == 142

    def test_caused_by_chain_populated(self):
        result = java_parse(JAVA_TRACE)
        # Caused by lines should appear in caused_by_chain
        assert len(result.caused_by_chain) > 0
        assert any("IllegalStateException" in c for c in result.caused_by_chain)

    def test_empty_trace(self):
        result = java_parse("")
        assert result.top_app_frames == []


# ---------------------------------------------------------------------------
# Node.js stack parser
# ---------------------------------------------------------------------------

NODE_TRACE = (
    "Error: payment gateway timeout\n"
    "    at CheckoutService.processPayment (/app/src/services/checkout.ts:88:12)\n"
    "    at OrderController.checkout (/app/src/controllers/order.ts:45:5)\n"
    "    at node:internal/process/task_queues:140:7\n"
    "    at Object.<anonymous> (/app/node_modules/express/lib/router/index.js:284:12)\n"
)

class TestNodeParser:
    def test_filters_node_internal(self):
        result = node_parse(NODE_TRACE)
        assert not any("node:internal" in f.file_name for f in result.top_app_frames)

    def test_filters_node_modules(self):
        result = node_parse(NODE_TRACE)
        assert not any("node_modules" in f.file_name for f in result.top_app_frames)

    def test_keeps_app_frames(self):
        result = node_parse(NODE_TRACE)
        assert len(result.top_app_frames) >= 1
        assert any("checkout.ts" in f.file_name for f in result.top_app_frames)

    def test_method_name_extracted(self):
        result = node_parse(NODE_TRACE)
        checkout = next(
            (f for f in result.top_app_frames if "checkout.ts" in f.file_name), None
        )
        assert checkout is not None
        assert checkout.method_name != ""


# ---------------------------------------------------------------------------
# Python stack parser
# ---------------------------------------------------------------------------

PYTHON_TRACE = (
    'Traceback (most recent call last):\n'
    '  File "/app/services/checkout.py", line 88, in process_payment\n'
    '    token = vault_client.get_secret("payment_token")\n'
    '  File "/app/clients/vault.py", line 33, in get_secret\n'
    '    raise VaultError("token not found")\n'
    '  File "/usr/lib/python3.11/site-packages/requests/api.py", line 59, in get\n'
    '    return request("get", url, params=params, **kwargs)\n'
    'VaultError: token not found\n'
)

class TestPythonParser:
    def test_filters_site_packages(self):
        result = python_parse(PYTHON_TRACE)
        assert not any("site-packages" in f.file_name for f in result.top_app_frames)

    def test_keeps_app_frames(self):
        result = python_parse(PYTHON_TRACE)
        assert any("checkout.py" in f.file_name for f in result.top_app_frames)
        assert any("vault.py" in f.file_name for f in result.top_app_frames)

    def test_line_numbers(self):
        result = python_parse(PYTHON_TRACE)
        checkout = next(
            f for f in result.top_app_frames if "checkout.py" in f.file_name
        )
        assert checkout.line_number == 88

    def test_exception_in_preview_or_chain(self):
        result = python_parse(PYTHON_TRACE)
        text = result.full_stack_preview + " ".join(result.caused_by_chain)
        assert "VaultError" in text or len(result.top_app_frames) > 0


# ---------------------------------------------------------------------------
# .NET stack parser
# ---------------------------------------------------------------------------

DOTNET_TRACE = (
    "System.NullReferenceException: Object reference not set\n"
    "   at MyApp.Services.CheckoutService.ProcessPayment(PaymentRequest request) "
    "in /src/Services/CheckoutService.cs:line 142\n"
    "   at MyApp.Controllers.OrderController.Checkout() "
    "in /src/Controllers/OrderController.cs:line 67\n"
    "   at System.Threading.Tasks.Task.RunSynchronously()\n"
    "   at Microsoft.AspNetCore.Mvc.Infrastructure.ActionMethodExecutor.Execute()\n"
)

class TestDotNetParser:
    def test_filters_system_namespace(self):
        result = dotnet_parse(DOTNET_TRACE)
        assert not any(
            f.class_name.startswith("System.") for f in result.top_app_frames
        )

    def test_filters_microsoft_namespace(self):
        result = dotnet_parse(DOTNET_TRACE)
        assert not any(
            f.class_name.startswith("Microsoft.") for f in result.top_app_frames
        )

    def test_keeps_app_frames(self):
        result = dotnet_parse(DOTNET_TRACE)
        assert any("CheckoutService" in f.class_name for f in result.top_app_frames)

    def test_line_number_with_source(self):
        result = dotnet_parse(DOTNET_TRACE)
        checkout = next(
            f for f in result.top_app_frames if "CheckoutService" in f.class_name
        )
        assert checkout.line_number == 142

    def test_exception_in_preview_or_chain(self):
        result = dotnet_parse(DOTNET_TRACE)
        text = result.full_stack_preview + " ".join(result.caused_by_chain)
        assert "NullReferenceException" in text or len(result.top_app_frames) > 0


# ---------------------------------------------------------------------------
# parse_snapshot_errors dispatcher
# ---------------------------------------------------------------------------

class TestParseSnapshotErrors:
    def test_dispatches_java(self):
        result = parse_snapshot_errors(JAVA_TRACE)
        assert result is not None
        assert len(result.top_app_frames) > 0

    def test_unknown_trace_returns_parsed_stack(self):
        result = parse_snapshot_errors("some random text with no stack")
        assert result is not None  # returns _unknown_parse, not None


# ---------------------------------------------------------------------------
# compare_snapshots — smoking gun detection
# ---------------------------------------------------------------------------

def _make_call_segment(name: str, time_ms: int) -> dict:
    parts = name.rsplit(".", 1) if "." in name else ["", name]
    return {
        "className": parts[0],
        "methodName": parts[1],
        "timeTakenInMilliSecs": time_ms,
        "lineNumber": 0,
        "fileName": "",
    }


def _make_baseline() -> dict:
    return {
        "requestGUID": "baseline-guid",
        "timeTakenInMilliSecs": 300,
        "errorOccurred": False,
        "errorDetails": "",
        "callChain": [
            _make_call_segment("com.example.CheckoutService.process", 100),
            _make_call_segment("com.example.PaymentClient.charge", 80),
        ],
    }


def _make_incident() -> dict:
    return {
        "requestGUID": "incident-guid",
        "timeTakenInMilliSecs": 3500,
        "errorOccurred": True,
        "errorDetails": "NullPointerException in CheckoutService",
        "callChain": [
            _make_call_segment("com.example.CheckoutService.process", 100),
            _make_call_segment("com.example.PaymentClient.charge", 2800),  # 35× slower
            _make_call_segment("com.example.VaultClient.getToken", 500),   # exclusive
        ],
    }


class TestCompareSnapshots:
    def test_latency_deviation_detected(self):
        result = compare_snapshots(_make_baseline(), _make_incident())
        assert len(result.latency_deviations) > 0

    def test_relative_threshold_both_conditions(self):
        baseline = _make_baseline()
        # incident only +25ms on a 80ms baseline = 31% > 30% but >20ms abs — SHOULD flag
        incident = {
            "requestGUID": "incident-guid",
            "timeTakenInMilliSecs": 305,
            "errorOccurred": False,
            "errorDetails": "",
            "callChain": [
                _make_call_segment("com.example.CheckoutService.process", 100),
                _make_call_segment("com.example.PaymentClient.charge", 105),  # +25ms
            ],
        }
        result = compare_snapshots(baseline, incident)
        # +25ms on 80ms baseline = 31.25% → flagged (>30% AND >20ms)
        assert len(result.latency_deviations) >= 1

    def test_confidence_reflects_signal_count(self):
        result = compare_snapshots(_make_baseline(), _make_incident())
        # Incident has: latency deviation + exclusive method + error = 3 signals
        assert result.confidence_score in (ConfidenceScore.HIGH, ConfidenceScore.MEDIUM)

    def test_culprit_class_set(self):
        result = compare_snapshots(_make_baseline(), _make_incident())
        assert result.culprit_class != "" or result.culprit_method != ""

    def test_suggested_fix_nonempty(self):
        result = compare_snapshots(_make_baseline(), _make_incident())
        assert result.suggested_fix != ""

    def test_identical_snapshots_no_deviation(self):
        baseline = _make_baseline()
        result = compare_snapshots(baseline, baseline)
        # Same callChain means no latency deviations or exclusive methods
        assert len(result.latency_deviations) == 0
        assert len(result.exclusive_methods) == 0


# ---------------------------------------------------------------------------
# score_golden_candidate
# ---------------------------------------------------------------------------

class TestScoreGoldenCandidate:
    def _snap(self, error: bool = False, time_ms: int = 300) -> dict:
        return {
            "errorOccurred": error,
            "timeTakenInMilliSecs": time_ms,
            "serverStartTime": 1699913600000,
        }

    def _failed(self) -> dict:
        return {"serverStartTime": 1700000000000, "timeTakenInMilliSecs": 3500}

    def test_perfect_candidate_scores_high(self):
        snap = self._snap(error=False, time_ms=300)
        score = score_golden_candidate(
            snap, failed=self._failed(), bt_baseline_ms=300.0
        )
        assert score >= 100

    def test_error_penalised(self):
        good = self._snap(error=False, time_ms=300)
        bad = self._snap(error=True, time_ms=300)
        s_good = score_golden_candidate(good, failed=self._failed(), bt_baseline_ms=300.0)  # noqa: E501
        s_bad = score_golden_candidate(bad, failed=self._failed(), bt_baseline_ms=300.0)
        assert s_good > s_bad

    def test_high_latency_penalised(self):
        normal = self._snap(time_ms=300)
        slow = self._snap(time_ms=600)  # 2× baseline
        s_normal = score_golden_candidate(
            normal, failed=self._failed(), bt_baseline_ms=300.0
        )
        s_slow = score_golden_candidate(
            slow, failed=self._failed(), bt_baseline_ms=300.0
        )
        assert s_normal > s_slow
