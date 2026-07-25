"""A demo target for omen: client-side throttling (429), not server faults (5xx).

    OTEL_SERVICE_NAME=gateway opentelemetry-instrument python examples/gateway/app.py serve

GET /api/quote is guarded by a 40 req/s token bucket — under load most requests get 429.
Traces via official ``opentelemetry-instrument``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="omen gateway", version="1.0.0")

_RATE = 40.0
_BURST = 40.0
_bucket_lock = threading.Lock()
_tokens = _BURST
_refilled_at = time.monotonic()


def _take_token() -> bool:
    global _tokens, _refilled_at
    with _bucket_lock:
        now = time.monotonic()
        _tokens = min(_BURST, _tokens + (now - _refilled_at) * _RATE)
        _refilled_at = now
        if _tokens >= 1.0:
            _tokens -= 1.0
            return True
        return False


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/status")
def status() -> dict:
    return {"region": "us-east-1", "healthy": True}


@app.get("/api/quote")
def quote(request: Request) -> JSONResponse:
    if not _take_token():
        return JSONResponse(status_code=429, content={"error": "rate limited"})
    return JSONResponse(status_code=200, content={"symbol": "ACME", "price": 42.0})


def main() -> None:
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        (Path(__file__).parent / "openapi.json").write_text(json.dumps(app.openapi(), indent=2))
        print("wrote openapi.json")
        return
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8403, log_level="warning")


if __name__ == "__main__":
    main()
