"""A second demo target for omen: a latency regression with no error signal.

    OTEL_SERVICE_NAME=storefront opentelemetry-instrument python examples/storefront/app.py serve

GET /api/products, GET /api/products/{sku} and GET /healthz are fine under load.
POST /api/checkout serializes on a shared SQLite connection under load — p95 climbs,
zero errors. Traces export via official ``opentelemetry-instrument`` (see SIGNOZ_SETUP.md).
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

DB_PATH = Path(tempfile.gettempdir()) / "omen_storefront.db"

CATALOG = [
    {"sku": "BOOK-01", "name": "The Pragmatic Programmer", "price": 39.99},
    {"sku": "BOOK-02", "name": "Designing Data-Intensive Applications", "price": 54.50},
    {"sku": "BOOK-03", "name": "Release It!", "price": 44.00},
    {"sku": "BOOK-04", "name": "Site Reliability Engineering", "price": 0.00},
]

app = FastAPI(title="omen storefront", version="1.0.0")
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_db_lock = threading.Lock()


def _init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE IF NOT EXISTS products (sku TEXT PRIMARY KEY, name TEXT, price REAL)")
    conn.executemany(
        "INSERT OR REPLACE INTO products (sku, name, price) VALUES (?, ?, ?)",
        [(p["sku"], p["name"], p["price"]) for p in CATALOG],
    )
    conn.commit()
    conn.close()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/api/products")
def list_products() -> list[dict]:
    return CATALOG


@app.get("/api/products/{sku}")
def get_product(sku: str) -> JSONResponse:
    for p in CATALOG:
        if p["sku"] == sku:
            return JSONResponse(status_code=200, content=p)
    return JSONResponse(status_code=404, content={"error": "not found"})


@app.post("/api/checkout")
def checkout(request: Request, order: dict) -> JSONResponse:
    lines = order.get("items") or [{"sku": p["sku"], "qty": 1} for p in CATALOG[:3]]
    total = 0.0
    with _db_lock:
        for line in lines:
            row = _conn.execute(
                "SELECT price FROM products WHERE sku = ?", (str(line.get("sku", "")),)
            ).fetchone()
            time.sleep(0.012)
            total += (row[0] if row else 0.0) * int(line.get("qty", 1))
    return JSONResponse(status_code=201, content={"total": round(total, 2), "lines": len(lines)})


def main() -> None:
    import sys

    _init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "dump":
        (Path(__file__).parent / "openapi.json").write_text(json.dumps(app.openapi(), indent=2))
        print("wrote openapi.json")
        return
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8401, log_level="warning")


if __name__ == "__main__":
    main()
