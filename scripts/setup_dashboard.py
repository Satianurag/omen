"""Provision the omen overview dashboard on a local SigNoz instance (idempotent).

Uses the official SigNoz REST API and ``SIGNOZ-API-KEY`` header — same auth as the
SigNoz MCP server. Dashboard panels are ClickHouse SQL over ``omen.run`` / ``omen.step.*``
OTel spans emitted by ``publish.py``.

    uv run python scripts/setup_dashboard.py

Env (from ``.env`` or shell):
    OMEN_SIGNOZ_URL      http://localhost:8080
    OMEN_SIGNOZ_API_KEY  service-account key with dashboard edit permission
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_TITLE = "omen overview"
DASHBOARD_JSON = ROOT / "docs" / "dashboard" / "omen_overview.json"


def _base_url() -> str:
    return os.environ.get("OMEN_SIGNOZ_URL", "http://localhost:8080").rstrip("/")


def _api_key() -> str:
    key = os.environ.get("OMEN_SIGNOZ_API_KEY", "").strip()
    if not key:
        print("OMEN_SIGNOZ_API_KEY is not set. Aborting.")
        sys.exit(1)
    return key


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    url = f"{_base_url()}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "SIGNOZ-API-KEY": _api_key(),
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        print(f"HTTP {exc.code} {method} {path}: {body[:400]}")
        sys.exit(1)


def _find_existing() -> str | None:
    out = _request("GET", "/api/v1/dashboards")
    for row in out.get("data") or []:
        title = (row.get("data") or {}).get("title") or row.get("title")
        if title == DASHBOARD_TITLE:
            return row.get("id")
    return None


def _payload() -> dict:
    spec = json.loads(DASHBOARD_JSON.read_text())
    inner = {
        "title": spec["title"],
        "description": spec.get("description", ""),
        "version": spec.get("version", "v5"),
        "layout": spec.get("layout", []),
        "widgets": spec.get("widgets", []),
        "panelMap": spec.get("panelMap", {}),
        "tags": spec.get("tags", ["omen"]),
    }
    return {"title": DASHBOARD_TITLE, "data": inner}


def main() -> None:
    load_dotenv(ROOT / ".env")
    if not DASHBOARD_JSON.exists():
        print(f"missing {DASHBOARD_JSON}")
        sys.exit(1)

    body = _payload()
    existing = _find_existing()
    if existing:
        _request("PUT", f"/api/v1/dashboards/{existing}", body)
        dash_id = existing
        print(f"dashboard {DASHBOARD_TITLE!r}: updated ({dash_id})")
    else:
        out = _request("POST", "/api/v1/dashboards", body)
        dash_id = out["data"]["id"]
        print(f"dashboard {DASHBOARD_TITLE!r}: created ({dash_id})")

    print(f"\nOpen: {_base_url()}/dashboard/{dash_id}")
    print("Panels read omen.run / omen.step.* spans (run omen under opentelemetry-instrument).")


if __name__ == "__main__":
    main()
