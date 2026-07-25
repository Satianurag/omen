"""A demo target for omen: latency that creeps up over the test window.

    OTEL_SERVICE_NAME=feed opentelemetry-instrument python examples/feed/app.py serve

POST /api/events appends to an unbounded in-memory log and rescans it each write — O(n log n)
cost grows under sustained load. Traces via official ``opentelemetry-instrument``.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="omen feed", version="1.0.0")
_events: list[dict] = []
_events_lock = threading.Lock()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/feed")
def get_feed() -> list[dict]:
    return [{"id": 1, "topic": "release", "title": "v1.0 is out"}]


@app.post("/api/events")
def add_event(request: Request, event: dict) -> JSONResponse:
    with _events_lock:
        _events.append({"topic": str(event.get("topic", "general"))})
        seen = len(_events)
    time.sleep(min(0.2, seen * 6e-5))
    return JSONResponse(status_code=200, content={"seen": seen})


def main() -> None:
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        (Path(__file__).parent / "openapi.json").write_text(json.dumps(app.openapi(), indent=2))
        print("wrote openapi.json")
        return
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8402, log_level="warning")


if __name__ == "__main__":
    main()
