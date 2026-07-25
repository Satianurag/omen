"""Publish a finished run to SigNoz via the official OpenTelemetry Python SDK.

After ``report``, emits one ``omen.run`` span plus child ``omen.step`` spans for the
agent's state-machine walk. Gated on ``OTEL_EXPORTER_OTLP_ENDPOINT``; a no-op when
unset so a run never fails for lack of telemetry export.

Run omen under ``opentelemetry-instrument`` (see docs/SIGNOZ_SETUP.md) so the OTLP
exporter is configured by the official distro.
"""

from __future__ import annotations

import os
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode


def publish_configured() -> bool:
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"))


def build_event(report: dict[str, Any]) -> dict[str, Any]:
    """Flatten the report into span attributes for the SigNoz dashboard."""
    rr = report.get("run_result") or {}
    corr = report.get("correlation") or {}
    findings = corr.get("findings") or {}
    worst = findings.get("worst_path") or {}
    top_error = findings.get("top_error") or {}
    anomaly = report.get("anomalies") or {}
    endpoints = report.get("endpoints_tested") or []
    grounded = (report.get("groundedness") or {}).get("grounded")
    session = report.get("session") or {}

    failed_rate = rr.get("http_req_failed_rate")
    return {
        "app_id": session.get("app_id"),
        "verdict": report.get("verdict"),
        "recommendation": report.get("recommendation"),
        "analysis": report.get("analysis"),
        "grounded": grounded,
        "steps_total": len(report.get("steps") or []),
        "mode": report.get("mode"),
        "endpoints": ", ".join(f"{e.get('method')} {e.get('path')}" for e in endpoints) or None,
        "endpoint_count": len(endpoints),
        "k6_reqs": rr.get("http_reqs"),
        "k6_p95_ms": rr.get("http_req_duration_p95_ms"),
        "k6_failed_pct": round(failed_rate * 100, 1) if failed_rate is not None else None,
        "srv_events": findings.get("total_events"),
        "srv_5xx": findings.get("server_errors"),
        "srv_4xx": findings.get("client_errors"),
        "srv_p95_ms": findings.get("p95_ms"),
        "worst_path": worst.get("path"),
        "worst_err_pct": worst.get("err_pct"),
        "worst_p95_ms": worst.get("p95_ms"),
        "root_cause": top_error.get("error_message"),
        "root_cause_count": top_error.get("count"),
        "forecaster": anomaly.get("forecaster"),
        "forecast_p95_ms": anomaly.get("forecast_p95_ms"),
        "anomalous_buckets": anomaly.get("anomalous_buckets"),
        "forecast_breaches": anomaly.get("forecast_breaches"),
    }


def build_step_events(report: dict[str, Any]) -> list[dict[str, Any]]:
    app_id = (report.get("session") or {}).get("app_id")
    events = []
    for step in report.get("steps") or []:
        events.append(
            {
                "app_id": app_id,
                "seq": step.get("seq"),
                "phase": step.get("phase"),
                "card": step.get("card"),
                "card_num": step.get("card_num"),
                "status": step.get("status"),
                "tool_calls": step.get("tool_calls"),
                "tools": step.get("tools") or None,
            }
        )
    return events


def _set_attrs(span: trace.Span, fields: dict[str, Any]) -> None:
    for key, value in fields.items():
        if value is not None:
            span.set_attribute(f"omen.{key}", str(value))


def publish_run(report: dict[str, Any], **_kwargs: Any) -> bool:
    """Emit run + step spans via the configured OTel tracer. Never raises."""
    if not publish_configured():
        return False
    tracer = trace.get_tracer("omen")
    try:
        with tracer.start_as_current_span("omen.run") as run_span:
            _set_attrs(run_span, build_event(report))
            run_span.set_attribute("omen.agent", "omen")
            for step in build_step_events(report):
                phase = step.get("phase") or "step"
                with tracer.start_as_current_span(f"omen.step.{phase}") as step_span:
                    _set_attrs(step_span, step)
                    step_span.set_status(Status(StatusCode.OK))
            run_span.set_status(Status(StatusCode.OK))
        return True
    except Exception:
        return False
