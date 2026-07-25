"""Run any omen demo scenario end-to-end (intent mode) against live SigNoz.

    uv run python scripts/verify_scenario.py feed
    uv run python scripts/verify_scenario.py [petclinic|storefront|feed|gateway|orders]

Starts the target app under ``opentelemetry-instrument``, drives the whole FSM
(real k6 + live SigNoz MCP + the configured model), and prints the verdict.
Prereqs: ``foundryctl cast -f casting.yaml`` and ``.env`` with ``OMEN_SIGNOZ_API_KEY``.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from dotenv import load_dotenv
from theodosia import UpstreamManager, bind_upstream
from theodosia.upstream import reset_upstream

from omen.app import build_application
from omen.upstream import k6_upstream_config, signoz_configured, signoz_upstream_config

ROOT = Path(__file__).resolve().parents[1]

SCENARIOS = {
    "petclinic": (8400, "petclinic", "load test recording a new visit"),
    "storefront": (8401, "storefront", "load test the checkout endpoint placing an order"),
    "feed": (8402, "feed", "load test recording new activity events"),
    "gateway": (8403, "gateway", "load test requesting a price quote"),
    "orders": (8404, "orders", "load test placing a new order"),
}


def _wait_for_app(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _ensure_otel_wrapped() -> None:
    """Re-exec under official ``opentelemetry-instrument`` so ``publish.py`` spans reach SigNoz."""
    if os.environ.get("OMEN_SKIP_OTEL_WRAP") or os.environ.get("_OMEN_OTEL_WRAPPED"):
        return
    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    _uv = os.environ.get("UV", "uv")
    env = {
        **os.environ,
        "_OMEN_OTEL_WRAPPED": "1",
        "OTEL_SERVICE_NAME": os.environ.get("OTEL_SERVICE_NAME", "omen"),
        "OTEL_TRACES_EXPORTER": os.environ.get("OTEL_TRACES_EXPORTER", "otlp"),
        "OTEL_EXPORTER_OTLP_PROTOCOL": os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc"),
    }
    subprocess.run([_uv, "run", "opentelemetry-instrument", sys.executable, *sys.argv], env=env, check=True)
    raise SystemExit(0)


async def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "petclinic"
    if name not in SCENARIOS:
        print(f"unknown scenario {name!r}. choose one of: {', '.join(SCENARIOS)}")
        return
    port, service, intent = SCENARIOS[name]
    app_dir = ROOT / "examples" / name
    url = f"http://127.0.0.1:{port}"

    load_dotenv(ROOT / ".env")
    if not signoz_configured():
        print("SigNoz is not configured in .env (OMEN_SIGNOZ_API_KEY). Aborting.")
        return

    app_env = {
        **os.environ,
        "OTEL_SERVICE_NAME": service,
        "OTEL_EXPORTER_OTLP_ENDPOINT": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"),
        "OTEL_EXPORTER_OTLP_PROTOCOL": os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc"),
        "OTEL_TRACES_EXPORTER": "otlp",
    }
    _uv = os.environ.get("UV", "uv")
    app = subprocess.Popen(
        [
            _uv,
            "run",
            "opentelemetry-instrument",
            "python",
            str(app_dir / "app.py"),
            "serve",
        ],
        cwd=str(ROOT),
        env=app_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_for_app(url):
        app.terminate()
        print(f"{name} app did not come up. Aborting.")
        return
    print(f"scenario:    {name} at {url} (service={service})")
    print(f"intent:      {intent}")
    print(
        f"llm backend: {os.environ.get('OMEN_LLM', 'ollama')} ({os.environ.get('OMEN_MODEL', '')}); k6 + SigNoz live"
    )

    upstream = UpstreamManager({"k6": k6_upstream_config(), "signoz": signoz_upstream_config()})
    token = bind_upstream(upstream)
    try:
        application = build_application()
        print("running real k6 load through the k6 MCP server...")
        _, _, state = await application.arun(
            halt_after=["report"],
            inputs={
                "repo_path": str(app_dir),
                "intent": intent,
                "target_base_url": url,
                "signoz_service": service,
            },
        )
    finally:
        await upstream.aclose()
        reset_upstream(token)
        app.terminate()

    report = state["report"]
    corr = report.get("correlation") or {}
    findings = corr.get("findings") or {}
    rr = report["run_result"] or {}
    anom = report.get("anomalies") or {}
    print("\nverdict:        ", report["verdict"])
    print("endpoints:      ", [f"{e['method']} {e['path']}" for e in report["endpoints_tested"]])
    print(
        f"k6 client-side:  {rr.get('http_reqs')} reqs, p95 {rr.get('http_req_duration_p95_ms')} ms, "
        f"{round((rr.get('http_req_failed_rate') or 0) * 100, 1)}% failed"
    )
    print(
        f"server-side:     {findings.get('total_events')} events, {findings.get('server_errors')} 5xx, "
        f"{findings.get('client_errors')} 4xx, p95 {findings.get('p95_ms')} ms"
    )
    if anom:
        print(
            f"anomaly scan:    {anom.get('method', 'forecast')} over {anom.get('buckets_analyzed', 0)} buckets, "
            f"peak {anom.get('peak_p95_ms')}ms, forecast {anom.get('forecast_p95_ms')}ms, "
            f"{anom.get('anomalous_buckets', 0)} anomalous, {anom.get('forecast_breaches', 0)} breach(es)"
        )
    print("\nby-path breakdown:")
    for row in corr.get("queries", {}).get("by_path", {}).get("rows", []):
        print("   ", json.dumps(row))
    if report.get("analysis"):
        print("\n=== analysis ===")
        for line in report["analysis"].splitlines():
            print("   ", line)
    if report.get("remediation"):
        print("\n=== proposed remediation (diff) ===")
        for line in report["remediation"].splitlines():
            print("   ", line)


if __name__ == "__main__":
    _ensure_otel_wrapped()
    asyncio.run(main())
