"""Unit tests for SigNoz MCP payload parsers (v5 shapes)."""

from __future__ import annotations

from omen.parse import (
    _FILTER_4XX,
    _FILTER_5XX,
    _FILTER_ROOT_CAUSE,
    build_correlation_mcp_calls,
    enrich_root_causes,
    merge_by_path,
    scalar_value,
    summarize_anomalies,
    summarize_root_causes,
)


def test_correlation_mcp_calls_use_status_code_filters():
    calls = build_correlation_mcp_calls("petclinic", 1_000.0, 2_000.0)
    assert calls["rollup_5xx"]["filter"] == _FILTER_5XX
    assert calls["rollup_4xx"]["filter"] == _FILTER_4XX
    assert "by_path_p95" in calls
    assert calls["root_cause"]["filter"] == _FILTER_ROOT_CAUSE
    assert calls["root_cause_fallback"]["filter"] == _FILTER_5XX


def test_summarize_root_causes_uses_error_detail():
    payload = {
        "data": {
            "data": {
                "results": [
                    {
                        "rows": [
                            {
                                "data": {
                                    "error.detail": "payment upstream timed out",
                                    "http.route": "/api/order",
                                    "response_status_code": "504",
                                }
                            }
                        ]
                    }
                ]
            }
        }
    }
    causes = summarize_root_causes(payload)
    assert causes[0]["error_message"] == "payment upstream timed out"


def test_summarize_root_causes_uses_db_statement():
    payload = {
        "data": {
            "data": {
                "results": [
                    {
                        "rows": [
                            {
                                "data": {
                                    "db.statement": "database is locked",
                                    "response_status_code": "500",
                                    "http.route": "/api/visits",
                                }
                            }
                        ]
                    }
                ]
            }
        }
    }
    causes = summarize_root_causes(payload)
    assert causes[0]["error_message"] == "database is locked"


def test_summarize_root_causes_skips_bare_status_codes():
    payload = {
        "data": {
            "data": {
                "results": [
                    {
                        "rows": [
                            {
                                "data": {
                                    "status_message": "database is locked",
                                    "response_status_code": "500",
                                    "http.route": "/api/visits",
                                }
                            }
                        ]
                    }
                ]
            }
        }
    }
    causes = summarize_root_causes(payload)
    assert causes[0]["error_message"] == "database is locked"


def test_enrich_root_causes_from_trace_details():
    search = {"data": [{"response_status_code": "500", "http.route": "/api/visits"}]}
    details = {
        "data": {
            "data": {
                "results": [
                    {
                        "rows": [
                            {
                                "data": {
                                    "status_message": "database is locked",
                                    "has_error": True,
                                }
                            }
                        ]
                    }
                ]
            }
        }
    }
    causes = enrich_root_causes(summarize_root_causes(search), [details])
    assert any("database is locked" in c["error_message"] for c in causes)


def test_merge_by_path_includes_p95():
    count = {"data": {"data": {"results": [{"data": [["/api/checkout", 10]], "columns": [{"name": "http.route"}, {"name": "__result_0"}]}]}}}
    err = {"data": {"value": 0}}
    p95 = {
        "data": {
            "data": {
                "results": [
                    {
                        "columns": [{"name": "http.route"}, {"name": "__result_0"}],
                        "data": [["/api/checkout", 1_500_000_000]],
                    }
                ]
            }
        }
    }
    rows = merge_by_path(count, err, p95)
    assert rows[0]["path"] == "/api/checkout"
    assert rows[0]["p95_ms"] == 1500.0


def test_summarize_anomalies_detects_rising_trend():
    buckets = [
        {"_time": 1, "p95_ms": 20},
        {"_time": 2, "p95_ms": 22},
        {"_time": 3, "p95_ms": 80},
        {"_time": 4, "p95_ms": 120},
    ]
    summary = summarize_anomalies(buckets, [], floor_ms=40.0)
    assert summary["anomalous_buckets"] >= 1
    assert summary["forecast_p95_ms"] is not None


def test_scalar_value_from_v5_scalar_payload():
    payload = {
        "data": {
            "data": {
                "results": [
                    {
                        "columns": [{"name": "__result_0"}],
                        "data": [[42]],
                    }
                ]
            }
        }
    }
    assert scalar_value(payload) == 42
