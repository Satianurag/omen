# SigNoz setup for omen (Agents of SigNoz)

omen uses **SigNoz Foundry**, the **official SigNoz MCP server**, and **OpenTelemetry Python auto-instrumentation** only — no custom telemetry shippers.

## Prerequisites

- Docker Desktop (or Docker Engine) running
- [uv](https://docs.astral.sh/uv/) and Python 3.12+
- [k6](https://grafana.com/docs/k6/latest/set-up/install-k6/) **2.0+** on your `PATH` (local binary; see [README](../README.md#install))
- `foundryctl` on your `PATH` ([SigNoz Foundry](https://signoz.io/docs/install/docker) / Foundry install)

## 1. Deploy SigNoz (Foundry)

From this repo root:

```bash
# Install foundryctl once if you do not have it (Linux/macOS):
#   curl -fsSL https://signoz.io/foundry.sh | bash
# On Windows, install the foundryctl binary from SigNoz docs, then:

foundryctl cast -f casting.yaml
```

This starts:

| Service | URL |
|---------|-----|
| SigNoz UI | http://localhost:8080 |
| OTLP gRPC | http://localhost:4317 |
| SigNoz MCP (HTTP) | http://localhost:8000/mcp |

`casting.yaml` and `casting.yaml.lock` are committed for hackathon reproducibility.

## 2. API key

1. Open http://localhost:8080 → **Settings → Service Accounts**
2. Create a service account with **signoz-admin** role
3. Add a key → paste into `.env` as `OMEN_SIGNOZ_API_KEY` (key only on that line — no trailing comments; inline text breaks auth)

## 3. Configure omen

```bash
cp .env.example .env          # PowerShell: Copy-Item .env.example .env
# set OMEN_SIGNOZ_API_KEY and GEMINI_API_KEY
# keep OMEN_K6_CMD=k6 x mcp  (do not set OMEN_K6_DOCKER=1 on Windows)
uv sync
uv run omen warm-k6
```

Set `OMEN_LLM=gemini`, `OMEN_MODEL=gemini-3.1-flash-lite`, and `GEMINI_API_KEY` (official [google-genai](https://github.com/googleapis/python-genai) SDK).

### OpenTelemetry instrumentation packages

`uv sync` already installs the packages omen and the FastAPI demos need (`opentelemetry-distro[otlp]`, `opentelemetry-instrumentation-fastapi`, exporters).

Optional extra instrumentors via bootstrap (needs `pip` inside the venv; uv venvs often omit it):

```bash
uv run python -m ensurepip --upgrade
uv run opentelemetry-bootstrap --action=install
```

If bootstrap fails, you can still run demos and `verify_scenario.py` — the FastAPI + OTLP stack from `uv sync` is enough.

## 4. Instrument demo apps (official OTel)

Demo ports (see `scripts/verify_scenario.py`):

| App | Port | `OTEL_SERVICE_NAME` |
|-----|------|---------------------|
| petclinic | 8400 | petclinic |
| storefront | 8401 | storefront |
| feed | 8402 | feed |
| gateway | 8403 | gateway |
| orders | 8404 | orders |

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_TRACES_EXPORTER=otlp
export OTEL_SERVICE_NAME=petclinic

uv run opentelemetry-instrument python examples/petclinic/app.py serve
```

PowerShell:

```powershell
$env:OTEL_EXPORTER_OTLP_ENDPOINT='http://localhost:4317'
$env:OTEL_EXPORTER_OTLP_PROTOCOL='grpc'
$env:OTEL_TRACES_EXPORTER='otlp'
$env:OTEL_SERVICE_NAME='petclinic'
uv run opentelemetry-instrument python examples/petclinic/app.py serve
```

## 5. Run omen with self-tracing

Prefer `uv run omen …` unless you have activated `.venv` (bare `omen` is not on `PATH` after `uv sync` alone).

```bash
export OTEL_SERVICE_NAME=omen
uv run opentelemetry-instrument omen pilot \
  --intent "load test recording a new visit" \
  --repo-path examples/petclinic \
  --target-base-url http://localhost:8400 \
  --signoz-service petclinic
```

`omen pilot` walks the governed MCP `step` path and writes a full hash-chained `ledger.jsonl` (use `uv run omen sessions ls` then `uv run omen verify <app-id>`).

## 6. End-to-end verify (one command)

```bash
uv run python scripts/verify_scenario.py petclinic
```

This starts the demo under `opentelemetry-instrument`, runs the full FSM (real k6 + live SigNoz + your model), and prints the verdict. It re-execs under `opentelemetry-instrument` when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, so `omen.run` / `omen.step.*` spans reach the dashboard.

Note: `verify_scenario.py` drives Burr via `application.arun()` for a fast live check. For a multi-step ledger that `omen sessions ls` highlights the way the demo GIF does, use `omen pilot` (section 5).

## 7. Dashboard

Import the omen overview dashboard (ClickHouse panels over OTel spans):

```bash
uv run python scripts/setup_dashboard.py
```

Then open the URL printed, or **Dashboards → omen overview** in the SigNoz UI. JSON lives at `docs/dashboard/omen_overview.json` for manual **Import JSON** if you prefer.

## References

- [SigNoz Docker install](https://signoz.io/docs/install/docker)
- [SigNoz MCP server](https://signoz.io/docs/ai/signoz-mcp-server)
- [FastAPI OpenTelemetry instrumentation](https://signoz.io/docs/instrumentation/fastapi)
