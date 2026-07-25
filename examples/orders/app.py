"""A demo target for omen: downstream timeout cascade (504 + latency).

    OTEL_SERVICE_NAME=orders opentelemetry-instrument python examples/orders/app.py serve

POST /api/order blocks on a limited downstream pool — under load returns 504 timeouts.
Traces via official ``opentelemetry-instrument``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

app = FastAPI(title="omen orders", version="1.0.0")
_downstream = threading.Semaphore(4)
_DOWNSTREAM_MS = 0.05
_WAIT_BUDGET_S = 0.15


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/catalog")
def catalog() -> list[dict]:
    return [{"sku": "WIDGET-1", "price": 9.99}]


@app.post("/api/order")
def create_order(request: Request, order: dict) -> JSONResponse:
    span = trace.get_current_span()
    got_slot = _downstream.acquire(timeout=_WAIT_BUDGET_S)
    if not got_slot:
        msg = "payment upstream timed out"
        span.record_exception(TimeoutError(msg))
        span.set_status(trace.Status(trace.StatusCode.ERROR, msg))
        span.set_attribute("rpc.system", "http")
        span.set_attribute("rpc.grpc.status_code", "DEADLINE_EXCEEDED")
        span.set_attribute("db.statement", msg)
        span.set_attribute("error.detail", msg)
        return JSONResponse(status_code=504, content={"error": msg})
    try:
        time.sleep(_DOWNSTREAM_MS)
    finally:
        _downstream.release()
    return JSONResponse(status_code=201, content={"order_id": 1, "status": "paid"})


def main() -> None:
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        (Path(__file__).parent / "openapi.json").write_text(json.dumps(app.openapi(), indent=2))
        print("wrote openapi.json")
        return
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8404, log_level="warning")


if __name__ == "__main__":
    main()
