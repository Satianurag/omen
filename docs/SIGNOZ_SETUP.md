# SigNoz setup for omen (Agents of SigNoz)

omen uses **SigNoz Foundry**, the **official SigNoz MCP server**, and **OpenTelemetry Python auto-instrumentation** only — no custom telemetry shippers.

## 1. Deploy SigNoz (Foundry)

From this repo root:

```bash
curl -fsSL https://signoz.io/foundry.sh | bash
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
cp .env.example .env
# set OMEN_SIGNOZ_API_KEY and GEMINI_API_KEY
uv sync
opentelemetry-bootstrap --action=install
```

Set `OMEN_LLM=gemini`, `OMEN_MODEL=gemini-3.1-flash-lite`, and `GEMINI_API_KEY` (official [google-genai](https://github.com/googleapis/python-genai) SDK).

## 4. Instrument demo apps (official OTel)

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_TRACES_EXPORTER=otlp
export OTEL_SERVICE_NAME=petclinic

opentelemetry-instrument python examples/petclinic/app.py serve
```

## 5. Run omen with self-tracing

```bash
export OTEL_SERVICE_NAME=omen
opentelemetry-instrument omen pilot \
  --intent "load test recording a new visit" \
  --repo-path examples/petclinic \
  --target-base-url http://localhost:8400 \
  --signoz-service petclinic
```

## 6. End-to-end verify

```bash
uv run python scripts/verify_scenario.py petclinic
```

`verify_scenario.py` re-execs under official `opentelemetry-instrument` when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, so each run publishes `omen.run` / `omen.step.*` spans for the dashboard.

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
