"""A small demo target for omen: a healthy baseline plus one flawed new endpoint.

    OTEL_SERVICE_NAME=petclinic opentelemetry-instrument python examples/petclinic/app.py serve

GET /api/owners, GET /api/vets and GET /healthz are fine under load. The "new"
endpoint POST /api/visits writes inside a held SQLite IMMEDIATE transaction with a
short busy timeout and no connection pooling, so concurrent writers collide and
SQLite raises "database is locked". That is invisible serially and only bites under
concurrency, the classic load-only regression.

Traces export to SigNoz via the official OpenTelemetry Python auto-instrumentation
(``opentelemetry-instrument`` + FastAPI instrumentor). See docs/SIGNOZ_SETUP.md.
"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace

DB_PATH = Path(tempfile.gettempdir()) / "omen_petclinic.db"

app = FastAPI(title="omen petclinic", version="1.0.0")


def _init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS visits (id INTEGER PRIMARY KEY, pet TEXT, note TEXT)")
    conn.commit()
    conn.close()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/owners")
def list_owners() -> list[dict]:
    return [{"id": 1, "name": "George Franklin"}, {"id": 2, "name": "Betty Davis"}]


@app.get("/api/vets")
def list_vets() -> list[dict]:
    return [{"id": 1, "name": "James Carter", "specialty": "radiology"}]


@app.post("/api/visits")
def create_visit(request: Request, visit: dict) -> JSONResponse:
    """New in this change: record a visit. Writes inside a held IMMEDIATE transaction
    with a 50ms busy timeout and no pooling, so it serializes and locks under load."""
    pet = str(visit.get("pet", "unknown"))
    note = str(visit.get("note", ""))
    span = trace.get_current_span()
    conn = sqlite3.connect(DB_PATH, timeout=0.25)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO visits (pet, note) VALUES (?, ?)", (pet, note))
        time.sleep(0.015)
        conn.commit()
    except sqlite3.OperationalError as exc:
        conn.rollback()
        msg = str(exc)
        span.record_exception(exc)
        span.set_status(trace.Status(trace.StatusCode.ERROR, msg))
        span.set_attribute("db.system", "sqlite")
        span.set_attribute("db.statement", msg)
        span.set_attribute("error.detail", msg)
        return JSONResponse(status_code=500, content={"error": msg})
    finally:
        conn.close()
    return JSONResponse(status_code=201, content={"pet": pet, "note": note})


def main() -> None:
    import json
    import sys

    _init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        (Path(__file__).parent / "openapi.json").write_text(json.dumps(app.openapi(), indent=2))
        print("wrote openapi.json")
        return
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8400, log_level="warning")


if __name__ == "__main__":
    main()
