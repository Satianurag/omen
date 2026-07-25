# omen — Divines disaster, crafts the cure

**Track 01 · AI & Agent Observability**

## Inspiration

Load tests tell you *that* something broke. They rarely tell you *why* on the server. A green k6 run can hide a latency cliff; a red k6 run can be rate-limiting, not a bug. SREs still grep logs and stitch traces by hand.

We built **omen** so an agent can load-test a change, correlate client-side k6 metrics with server-side SigNoz traces in the same window, name the root cause, and propose a validated fix — before production.

## What it does

omen is a governed Burr state machine ([Theodosia](https://msradam.github.io/theodosia/)) exposed as a single MCP `step` tool. The driving agent never talks to k6 or SigNoz directly; omen orchestrates both upstream MCP servers:

1. **Scaffold & generate** a k6 script from OpenAPI (deterministic baseline + Gemini `gemini-3.1-flash-lite`)
2. **Validate & run** through the official Grafana k6 MCP server
3. **Preflight & correlate** via the official SigNoz MCP server (`signoz_aggregate_traces`, `signoz_search_traces`, `signoz_get_trace_details`)
4. **Detect anomalies** from p95 time-series over the test window
5. **Analyze** with cited evidence, **screen** with an independent Guardian groundedness check, **report** with verdict + remediation diff

Five demo apps simulate real failure modes invisible to k6 alone:

| App | Failure | Verdict |
|-----|---------|---------|
| petclinic | SQLite lock under concurrency | server-side regression + `database is locked` |
| gateway | Rate limiting (429) | client-side throttling (4xx, zero 5xx) |
| storefront | Checkout latency cliff | latency degradation, zero errors |
| feed | Event ingestion slowdown | latency degradation, zero errors |
| orders | Downstream pool exhaustion | server-side regression + `payment upstream timed out` |

## How we used SigNoz

- **Deployment:** SigNoz Foundry (`casting.yaml`) — self-hosted traces on OTLP gRPC `:4317`, MCP on `:8000`
- **Instrumentation:** Official `opentelemetry-instrument` on demo apps and omen itself (`omen.run` / `omen.step.*` spans)
- **Correlation:** SigNoz MCP v0.9 filters on `response_status_code`, `has_error`, `db.statement`, and `error.detail`
- **Root cause:** Span search + trace detail enrichment; bypasses false-positive string classification in upstream wrappers
- **Dashboard:** `scripts/setup_dashboard.py` provisions a **omen overview** dashboard from OTLP spans

## Built with

- Python 3.12, Burr, Theodosia, FastMCP
- Grafana `mcp-k6` (Docker), SigNoz `signoz-mcp-server` v0.9
- Google GenAI SDK (`gemini-3.1-flash-lite`) for writer + Guardian screen
- OpenTelemetry Python auto-instrumentation

## Try it

```bash
foundryctl cast -f casting.yaml
cp .env.example .env   # set OMEN_SIGNOZ_API_KEY, GEMINI_API_KEY
uv sync && uv run opentelemetry-bootstrap --action=install
uv run python scripts/verify_scenario.py petclinic
```

## Demo video

Record a fresh cast with `./scripts/record_demo_cast.sh` (outputs `docs/assets/omen-run.cast` and SVG in `docs/assets/`).

## Links

- Repo: *(add your GitHub URL before submit)*
- SigNoz dashboard: `http://localhost:8080` (self-hosted)
- Hackathon: [Agents of SigNoz](https://www.wemakedevs.org/hackathons/signoz)
