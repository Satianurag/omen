"""Pure parsing helpers: diff -> endpoints, intent -> endpoints, k6 MCP payloads -> metrics.

None of these touch the network or a subprocess. The k6 MCP payload parsers are
written defensively because the upstream's ``metrics`` object nests differently
depending on the k6 version (flat numbers, ``{"count": ...}`` envelopes, or a
``{"values": {...}}`` wrapper).
"""

from __future__ import annotations

import json
import re
from typing import Any

from omen.state import Endpoint, RunResult

_ROUTE_DECORATOR = re.compile(r'@(?:app|router)\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)["\']')
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
# SigNoz MCP v0.9+ search syntax (see signoz-mcp-server traces_helper.go).
_FILTER_4XX = "response_status_code >= '400' AND response_status_code < '500'"
_FILTER_5XX = "response_status_code >= '500'"
_FILTER_ROOT_CAUSE = "has_error = true AND (db.statement EXISTS OR error.detail EXISTS)"
_HTTP_STATUS_ONLY = re.compile(r"^\d{3}$")


def extract_endpoints_from_diff(diff_text: str) -> list[Endpoint]:
    """Regex over added (`+`) diff lines for FastAPI route decorators."""
    endpoints: list[Endpoint] = []
    seen: set[tuple[str, str]] = set()
    for line in diff_text.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        match = _ROUTE_DECORATOR.search(line)
        if not match:
            continue
        method, path = match.group(1).upper(), match.group(2)
        if (method, path) not in seen:
            endpoints.append(Endpoint(method=method, path=path))
            seen.add((method, path))
    return endpoints


def score_intent(spec: dict, intent: str) -> list[Endpoint]:
    """Find endpoints relevant to the intent using the OpenAPI graph; falls back to token overlap."""
    try:
        from omen.graphrag import OpenAPIGraph, SubgraphRetriever  # noqa: F401

        graph = OpenAPIGraph.from_spec(spec)
        G = graph.graph
        intent_lc = intent.lower()
        scored: list[tuple[int, str]] = []

        for ep_id in graph.endpoints():
            data = G.nodes[ep_id]
            path = data.get("path", "")
            summary = data.get("summary", "").lower()
            tokens = [t for t in path.lower().split("/") if t and not t.startswith("{") and len(t) > 2]
            path_score = sum(1 for t in tokens if t in intent_lc)
            summary_score = sum(1 for w in summary.split() if len(w) > 3 and w in intent_lc)
            score = path_score + summary_score
            if score > 0:
                scored.append((score, ep_id))

        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            top = [ep_id for _, ep_id in scored[:3]]
        else:
            top = graph.endpoints()

        return [Endpoint(method=G.nodes[ep]["method"], path=G.nodes[ep]["path"]) for ep in top]

    except Exception:
        return _score_intent_fallback(spec, intent)


def _score_intent_fallback(spec: dict, intent: str) -> list[Endpoint]:
    """Token overlap fallback when the OpenAPI graph cannot be built."""
    intent_lc = intent.lower()
    scored: list[tuple[int, Endpoint]] = []
    paths = spec.get("paths", {}) or {}
    for path, ops in paths.items():
        if not isinstance(ops, dict):
            continue
        tokens = [t for t in path.lower().split("/") if t and not t.startswith("{") and len(t) > 2]
        path_score = sum(1 for t in tokens if t in intent_lc)
        for method, op in ops.items():
            if method.upper() not in _HTTP_METHODS:
                continue
            summary = (op.get("summary") or "").lower() if isinstance(op, dict) else ""
            score = path_score + sum(1 for w in summary.split() if len(w) > 3 and w in intent_lc)
            if score > 0:
                scored.append((score, Endpoint(method=method.upper(), path=path)))

    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return [ep for _, ep in scored[:3]]

    return [
        Endpoint(method=method.upper(), path=path)
        for path, ops in paths.items()
        if isinstance(ops, dict)
        for method in ops
        if method.upper() in _HTTP_METHODS
    ]


def retrieve_context(spec: dict | None, endpoints: list[Endpoint]) -> str | None:
    """Return GraphRAG schema context for the given endpoints as compact text, or None."""
    if not spec or not endpoints:
        return None
    try:
        from omen.graphrag import OpenAPIGraph, SubgraphRetriever

        graph = OpenAPIGraph.from_spec(spec)
        retriever = SubgraphRetriever(graph)
        ep_ids = [f"{ep.method} {ep.path}" for ep in endpoints]
        context = retriever.for_endpoints(ep_ids)
        text = context.to_text()
        return text or None
    except Exception:
        return None


def _signoz_window(earliest: float, latest: float) -> dict[str, Any]:
    return {
        "start": int(earliest * 1000),
        "end": int(latest * 1000),
        "searchContext": "omen correlate over load-test window",
    }


def build_correlation_mcp_calls(service: str, earliest: float, latest: float) -> dict[str, dict[str, Any]]:
    """SigNoz MCP ``signoz_aggregate_traces`` / ``signoz_search_traces`` arguments.

    Maps the four server-side questions k6 cannot answer onto official SigNoz MCP tools.
    Time-series calls omit ``stepInterval`` so SigNoz auto-selects a valid bucket (min 5s).
  """
    win = _signoz_window(earliest, latest)
    base = {**win, "service": service}
    return {
        "rollup_total": {**base, "aggregation": "count", "requestType": "scalar"},
        "rollup_5xx": {**base, "aggregation": "count", "filter": _FILTER_5XX, "requestType": "scalar"},
        "rollup_4xx": {**base, "aggregation": "count", "filter": _FILTER_4XX, "requestType": "scalar"},
        "rollup_p95": {
            **base,
            "aggregation": "p95",
            "aggregateOn": "duration_nano",
            "requestType": "scalar",
        },
        "timeline_errors": {
            **base,
            "aggregation": "count",
            "error": True,
            "requestType": "time_series",
        },
        "timeline_p95": {
            **base,
            "aggregation": "p95",
            "aggregateOn": "duration_nano",
            "requestType": "time_series",
        },
        "by_path": {
            **base,
            "aggregation": "count",
            "groupBy": "http.route",
            "requestType": "scalar",
            "limit": "20",
        },
        "by_path_errors": {
            **base,
            "aggregation": "count",
            "error": True,
            "groupBy": "http.route",
            "requestType": "scalar",
            "limit": "20",
        },
        "by_path_p95": {
            **base,
            "aggregation": "p95",
            "aggregateOn": "duration_nano",
            "groupBy": "http.route",
            "requestType": "scalar",
            "limit": "20",
        },
        "root_cause": {
            **win,
            "service": service,
            "filter": _FILTER_ROOT_CAUSE,
            "limit": "50",
            "searchContext": "omen root cause spans",
        },
        "root_cause_fallback": {
            **win,
            "service": service,
            "filter": _FILTER_5XX,
            "limit": "50",
            "searchContext": "omen root cause spans fallback",
        },
    }


def build_anomaly_mcp_calls(service: str, earliest: float, latest: float) -> dict[str, dict[str, Any]]:
    """Latency time-series for statistical onset (SigNoz ``signoz_aggregate_traces``)."""
    win = _signoz_window(earliest, latest)
    base = {**win, "service": service}
    return {
        "forecast": {
            **base,
            "aggregation": "p95",
            "aggregateOn": "duration_nano",
            "requestType": "time_series",
        },
    }


def summarize_anomalies(
    forecast: list[dict[str, Any]],
    anomalies: list[dict[str, Any]],
    method: str = "signoz aggregate_traces p95 time_series",
    *,
    floor_ms: float = 40.0,
) -> dict[str, Any]:
    """Fold p95 time-series buckets into a compact latency-trend verdict.

    When SigNoz does not return forecast bands, compare the median p95 of the
    first half of buckets to the second half to detect rising latency over the run.
    """
    breaches, actuals, forecasts = [], [], []
    for row in forecast:
        actual = _to_float(row.get("p95_ms"))
        upper = next((_to_float(v) for k, v in row.items() if k.lower().startswith("upper")), None)
        predicted = _to_float(row.get("forecast"))
        if actual is not None:
            actuals.append(actual)
        if predicted is not None:
            forecasts.append(predicted)
        if actual is not None and upper is not None and actual > upper:
            breaches.append({"time": row.get("_time"), "p95_ms": actual, "upper_ms": upper})

    flagged = [r for r in anomalies if r.get("probable_cause") or r.get("log_event_prob")]
    onsets = sorted([b["time"] for b in breaches] + [r.get("_time") for r in flagged if r.get("_time")])

    trend_forecast = forecasts[-1] if forecasts else None
    trend_buckets = 0
    if trend_forecast is None and len(actuals) >= 3:
        mid = max(1, len(actuals) // 2)
        early = actuals[:mid]
        late = actuals[mid:]
        early_med = sum(early) / len(early)
        late_med = sum(late) / len(late)
        trend_forecast = late_med
        if late_med > 1.15 * early_med and late_med >= floor_ms:
            trend_buckets = 1
            breaches.append({"time": forecast[-1].get("_time"), "p95_ms": late_med, "upper_ms": early_med})

    return {
        "method": method,
        "buckets_analyzed": len(actuals),
        "peak_p95_ms": max(actuals) if actuals else None,
        "forecast_p95_ms": round(trend_forecast, 2) if trend_forecast is not None else None,
        "forecast_breaches": len(breaches),
        "anomalous_buckets": len(flagged) or trend_buckets,
        "first_anomaly": onsets[0] if onsets else None,
        "peak_breach": max(breaches, key=lambda b: b["p95_ms"]) if breaches else None,
    }


def summarize_findings(results: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Synthesize the actionable verdict facts from the four query result sets."""
    rollup = (results.get("rollup") or [{}])[0]
    by_path = results.get("by_path") or []
    worst = by_path[0] if by_path else None

    onset = None
    for row in results.get("timeline") or []:
        if _to_int(row.get("errors")):
            onset = {"time": row.get("_time"), "errors": _to_int(row.get("errors"))}
            break

    causes = results.get("root_cause") or []
    top = causes[0] if causes else None

    return {
        "total_events": _to_int(rollup.get("total_events")),
        "server_errors": _to_int(rollup.get("server_errors")),
        "client_errors": _to_int(rollup.get("client_errors")),
        "p95_ms": _to_float(rollup.get("p95_ms")),
        "worst_path": (
            {"path": worst.get("path"), "err_pct": worst.get("err_pct"), "p95_ms": worst.get("p95_ms")}
            if worst and worst.get("path")
            else None
        ),
        "onset": onset,
        "top_error": (
            {"error_message": top.get("error_message"), "count": _to_int(top.get("count"))}
            if top and top.get("error_message")
            else None
        ),
    }


def _parse_search_results(parsed: dict) -> list[dict[str, Any]]:
    """Normalize ``signoz_search_traces`` span rows."""
    data = parsed.get("data")
    if not isinstance(data, dict):
        return []
    inner = data.get("data")
    if not isinstance(inner, dict):
        return []
    results = inner.get("results")
    if not isinstance(results, list) or not results:
        return []
    result = results[0]
    if not isinstance(result, dict):
        return []
    rows: list[dict[str, Any]] = []
    for row in result.get("rows") or []:
        if not isinstance(row, dict):
            continue
        span = row.get("data")
        if not isinstance(span, dict):
            continue
        route = span.get("http.route") or span.get("url.path")
        if not route and isinstance(span.get("name"), str):
            parts = span["name"].split()
            if len(parts) >= 2 and parts[0] in _HTTP_METHODS:
                route = parts[1]
        rows.append(
            {
                **span,
                "http.route": route,
                "statusMessage": span.get("status_message") or span.get("statusMessage"),
                "error.message": span.get("status_message") or span.get("statusMessage"),
            }
        )
    return rows


def _parse_builder_results(parsed: dict) -> list[dict[str, Any]]:
    """Normalize SigNoz v5 builder ``query_range`` payloads into row dicts."""
    data = parsed.get("data")
    if not isinstance(data, dict):
        return []
    inner = data.get("data")
    if not isinstance(inner, dict):
        return []
    results = inner.get("results")
    if not isinstance(results, list) or not results:
        return []
    result = results[0]
    if not isinstance(result, dict):
        return []

    aggs = result.get("aggregations")
    if isinstance(aggs, list) and aggs:
        rows: list[dict[str, Any]] = []
        for agg in aggs:
            if not isinstance(agg, dict):
                continue
            for series in agg.get("series") or []:
                if not isinstance(series, dict):
                    continue
                for pt in series.get("values") or []:
                    if not isinstance(pt, dict):
                        continue
                    ts = pt.get("timestamp")
                    val = pt.get("value")
                    rows.append({"_time": ts, "timestamp": ts, "value": val})
        return rows

    columns = result.get("columns") or []
    col_names = [c.get("name") for c in columns if isinstance(c, dict) and c.get("name")]
    data_rows = result.get("data") or []
    rows = []
    for row in data_rows:
        if not isinstance(row, list):
            continue
        mapped = dict(zip(col_names, row, strict=False))
        if "__result_0" in mapped:
            mapped["value"] = mapped.pop("__result_0")
        route = mapped.get("http.route")
        if route is not None:
            mapped["group"] = route
            mapped["http.route"] = route
        rows.append(mapped)
    return rows


def parse_mcp_json(data: Any) -> Any:
    """Decode a SigNoz MCP tool payload (``content[0].text`` JSON) or pass through dicts."""
    if isinstance(data, dict) and data.get("isError"):
        return data
    if isinstance(data, str):
        text = data.strip()
        if text.startswith("{"):
            try:
                obj, _ = json.JSONDecoder().raw_decode(text)
                return obj
            except json.JSONDecodeError:
                return {"error": text}
        return {"error": text}
    if isinstance(data, dict):
        content = data.get("content")
        if isinstance(content, list) and content:
            text = content[0].get("text") if isinstance(content[0], dict) else None
            if isinstance(text, str):
                if text.startswith("SigNoz API error"):
                    return {"error": text}
                if text.startswith("{"):
                    try:
                        obj, _ = json.JSONDecoder().raw_decode(text)
                        return obj
                    except json.JSONDecodeError:
                        return {"error": text}
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return {"error": text}
        if "data" in data or "results" in data or data.get("status") == "success":
            return data
    return data


def summarize_correlation(data: Any) -> list[dict[str, Any]]:
    """Pull tabular rows from a SigNoz MCP or legacy list/dict payload."""
    parsed = parse_mcp_json(data)
    if isinstance(parsed, dict) and parsed.get("error"):
        return []
    if isinstance(parsed, dict):
        search_rows = _parse_search_results(parsed)
        if search_rows:
            return search_rows
        builder_rows = _parse_builder_results(parsed)
        if builder_rows:
            return builder_rows
        for key in ("results", "rows", "data", "series"):
            rows = parsed.get(key)
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)]
        inner = parsed.get("data")
        if isinstance(inner, dict):
            if "value" in inner:
                return [inner]
            if isinstance(inner.get("rows"), list):
                return [r for r in inner["rows"] if isinstance(r, dict)]
        if isinstance(inner, list):
            return [r for r in inner if isinstance(r, dict)]
    if isinstance(parsed, list):
        return [r for r in parsed if isinstance(r, dict)]
    return []


def scalar_value(data: Any) -> float | int | None:
    rows = summarize_correlation(data)
    if not rows:
        return None
    row = rows[0]
    for key in ("value", "count", "count()", "p95(duration_nano)", "p95"):
        if key in row:
            return _to_float(row[key]) if "p95" in key or "duration" in key else _to_int(row[key])
    return _to_int(row.get("count")) or _to_float(row.get("value"))


def nano_to_ms(value: Any) -> float | None:
    v = _to_float(value)
    return round(v / 1_000_000, 2) if v is not None else None


def merge_timeline(errors_data: Any, p95_data: Any) -> list[dict[str, Any]]:
    """Merge error-count and p95 time_series buckets into correlate timeline rows."""
    err_rows = summarize_correlation(errors_data)
    p95_rows = summarize_correlation(p95_data)
    by_time: dict[Any, dict[str, Any]] = {}
    for row in err_rows:
        t = row.get("timestamp") or row.get("time") or row.get("_time")
        by_time[t] = {"_time": t, "errors": _to_int(row.get("value") or row.get("count")) or 0}
    for row in p95_rows:
        t = row.get("timestamp") or row.get("time") or row.get("_time")
        slot = by_time.setdefault(t, {"_time": t, "errors": 0})
        slot["p95_ms"] = nano_to_ms(row.get("value") or row.get("p95(duration_nano)"))
    return sorted(by_time.values(), key=lambda r: str(r.get("_time", "")))


def merge_by_path(count_data: Any, err_data: Any, p95_data: Any = None) -> list[dict[str, Any]]:
    counts = {r.get("http.route") or r.get("group"): r for r in summarize_correlation(count_data)}
    errors = {r.get("http.route") or r.get("group"): r for r in summarize_correlation(err_data)}
    p95s = {r.get("http.route") or r.get("group"): r for r in summarize_correlation(p95_data or [])}
    paths = set(counts) | set(errors) | set(p95s)
    out: list[dict[str, Any]] = []
    for path in paths:
        if not path:
            continue
        reqs = _to_int((counts.get(path) or {}).get("value") or (counts.get(path) or {}).get("count")) or 0
        errs = _to_int((errors.get(path) or {}).get("value") or (errors.get(path) or {}).get("count")) or 0
        p95_raw = (p95s.get(path) or {}).get("value") or (p95s.get(path) or {}).get("p95(duration_nano)")
        out.append(
            {
                "path": path,
                "reqs": reqs,
                "errors": errs,
                "err_pct": round(100 * errs / reqs, 1) if reqs else 0,
                "p95_ms": nano_to_ms(p95_raw),
            }
        )
    out.sort(key=lambda r: r["err_pct"], reverse=True)
    return out


def _span_error_message(row: dict[str, Any]) -> str | None:
    """Best-effort error text from a SigNoz span row."""
    for key in (
        "status_message",
        "statusMessage",
        "db.statement",
        "error.detail",
        "exception.message",
        "error.message",
        "exception_message",
        "exception.type",
    ):
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _meaningful_error_message(row: dict[str, Any]) -> str | None:
    msg = _span_error_message(row)
    if msg and not _HTTP_STATUS_ONLY.match(msg):
        return msg
    code = row.get("response_status_code") or row.get("http.response.status_code")
    if code and str(code) not in ("0", ""):
        return f"HTTP {code}"
    return msg


def summarize_root_causes(search_data: Any) -> list[dict[str, Any]]:
    """Top error messages from ``signoz_search_traces`` span rows."""
    rows = summarize_correlation(search_data)
    counts: dict[str, int] = {}
    for row in rows:
        msg = _meaningful_error_message(row) or "error"
        counts[str(msg)] = counts.get(str(msg), 0) + 1
    ranked = sorted(counts.items(), key=lambda x: -x[1])
    return [{"error_message": k, "count": v} for k, v in ranked[:3]]


def enrich_root_causes(
    causes: list[dict[str, Any]], trace_details: list[Any]
) -> list[dict[str, Any]]:
    """Fill in missing root-cause text from ``signoz_get_trace_details`` span trees."""
    top = str(causes[0].get("error_message", "")) if causes else ""
    if top and not _HTTP_STATUS_ONLY.match(top) and not top.startswith("HTTP "):
        return causes
    extra: dict[str, int] = {}
    for payload in trace_details:
        for row in summarize_correlation(payload):
            msg = _span_error_message(row)
            if msg and not _HTTP_STATUS_ONLY.match(msg):
                extra[msg] = extra.get(msg, 0) + 1
    if not extra:
        return causes
    merged = {c["error_message"]: c["count"] for c in causes}
    for msg, n in extra.items():
        merged[msg] = merged.get(msg, 0) + n
    return [{"error_message": k, "count": v} for k, v in sorted(merged.items(), key=lambda x: -x[1])[:3]]


_SCRIPT_FENCE = re.compile(r"```(?:javascript|js|typescript|ts|k6)?\s*\n(.*?)```", re.DOTALL)


def extract_options_block(script: str) -> str | None:
    """Pull the k6 ``export const options`` block from a script, if present."""
    idx = script.find("export const options")
    if idx < 0:
        return None
    start = script.find("{", idx)
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(script)):
        ch = script[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                semi = script.find(";", i)
                if semi < 0:
                    return None
                return script[idx : semi + 1]
    return None


def extract_default_function(script: str) -> str | None:
    """Return the ``export default function`` tail of a k6 script."""
    idx = script.find("export default function")
    if idx < 0:
        return None
    return script[idx:].strip()


def finalize_script(authored: str, scaffold: str) -> str:
    """Keep scaffold imports/options/BASE_URL; take only the default function from the model."""
    head_idx = scaffold.find("export default function")
    if head_idx < 0:
        return authored.strip() or scaffold
    head = scaffold[:head_idx].rstrip()
    body = extract_default_function(authored) or scaffold[head_idx:].strip()
    return head + "\n\n" + body + "\n"


def ensure_scaffold_options(script: str, scaffold: str) -> str:
    """Deprecated alias for :func:`finalize_script` (tests still cover options merge)."""
    return finalize_script(script, scaffold)


def extract_script(raw: Any) -> str:
    """Pull the k6 source out of a model response, stripping any markdown fence."""
    if not isinstance(raw, str):
        return ""
    match = _SCRIPT_FENCE.search(raw)
    return (match.group(1) if match else raw).strip()


def build_generation_description(
    endpoints: list[Endpoint],
    intent: str | None,
    scaffold: str,
    validation_error: str | None = None,
    schema_context: str | None = None,
) -> str:
    """Compose the request handed to k6's generate_script prompt and the model."""
    eps = "\n".join(f"  - {ep.method} {ep.path}" for ep in endpoints)
    parts = []
    if intent:
        parts.append(f"Intent: {intent}")
    parts.append("Target endpoints:\n" + eps)
    if schema_context:
        parts.append(
            "API schema context (request/response types for the tested endpoints):\n" + schema_context
        )
    parts.append(
        "Build on this deterministic scaffold. Keep it a single self-contained file with "
        "plain k6/http calls and no local imports. Use minimal think time (no sleep, or at "
        "most 0.01s) so virtual users produce real concurrency. Do not change the scaffold's "
        "`export const options` block (scenarios, rate, duration); only improve the default "
        "function's HTTP calls and checks:\n\n" + scaffold
    )
    if validation_error:
        parts.append(f"The previous attempt failed k6 validation:\n{validation_error}\nFix it.")
    return "\n\n".join(parts)


def build_fix_description(script: str, validation_error: str | None, endpoints: list[Endpoint]) -> str:
    """The repair request for the fix_script phase: the failing script plus the k6 error."""
    eps = "\n".join(f"  - {ep.method} {ep.path}" for ep in endpoints)
    return (
        "This k6 script failed validation. Fix it so it validates and runs. Keep it a single "
        "self-contained file with plain k6/http calls, no local imports, minimal think time, "
        "and leave the `export const options` block unchanged.\n\n"
        f"Target endpoints:\n{eps}\n\n"
        f"k6 validation error:\n{validation_error}\n\n"
        f"Failing script:\n{script}"
    )


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


_DOC_TERMS = ("http-requests", "thresholds", "checks", "scenarios")


def flatten_sections(payload: Any) -> list[dict[str, str]]:
    """Flatten a k6 MCP `list_sections` tree to ``[{slug, title}, ...]``."""
    tree = payload.get("tree") if isinstance(payload, dict) else payload
    out: list[dict[str, str]] = []

    def walk(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            out.append({"slug": str(node.get("slug", "")), "title": str(node.get("title", ""))})
            walk(node.get("children"))

    walk(tree)
    return out


def select_doc_slugs(payload: Any, limit: int = 4) -> list[str]:
    """Pick the doc slugs for the k6 constructs the composer emits, from a live tree."""
    nodes = flatten_sections(payload)
    picked: list[str] = []
    for term in _DOC_TERMS:
        for node in nodes:
            slug = node["slug"]
            if term in slug.lower() and slug not in picked:
                picked.append(slug)
                break
    return picked[:limit]


def _doc_excerpt(content: Any, limit: int = 200) -> str:
    if not isinstance(content, str):
        return ""
    text = content
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :]
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:limit]
    return ""


def parse_documentation(slug: str, payload: Any) -> dict[str, str]:
    """Turn a k6 MCP `get_documentation` payload into a compact citation."""
    section = payload.get("section", {}) if isinstance(payload, dict) else {}
    content = payload.get("content", "") if isinstance(payload, dict) else ""
    return {
        "slug": slug,
        "title": str(section.get("title") or slug) if isinstance(section, dict) else slug,
        "excerpt": _doc_excerpt(content),
    }


def parse_service_facts(service_name: str, services_payload: Any) -> dict[str, Any]:
    """Extract service facts from ``signoz_list_services``."""
    parsed = parse_mcp_json(services_payload)
    services = []
    if isinstance(parsed, dict):
        services = parsed.get("data") or []
    names = {s.get("serviceName") or s.get("name") for s in services if isinstance(s, dict)}
    match = next((s for s in services if isinstance(s, dict) and (s.get("serviceName") == service_name or s.get("name") == service_name)), None)
    return {
        "service": service_name,
        "exists": service_name in names or bool(match),
        "span_count": _to_int((match or {}).get("callCount") or (match or {}).get("numCalls")),
        "p99_ms": nano_to_ms((match or {}).get("p99")) if match else None,
    }


def parse_signoz_services(services_payload: Any) -> list[dict[str, Any]]:
    parsed = parse_mcp_json(services_payload)
    services = parsed.get("data") if isinstance(parsed, dict) else []
    return [
        {
            "service": s.get("serviceName") or s.get("name"),
            "call_count": _to_int(s.get("callCount") or s.get("numCalls")),
        }
        for s in services
        if isinstance(s, dict)
    ]


def _metric_value(metrics: dict, name: str, *keys: str) -> float | None:
    """Pull one number from a k6 metric, tolerating flat / count / values shapes."""
    v = metrics.get(name)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        nest = v.get("values") if isinstance(v.get("values"), dict) else v
        for k in keys:
            got = nest.get(k)
            if isinstance(got, (int, float)):
                return float(got)
    return None


def _k6_stderr_errors(stderr: Any) -> str:
    """Pull the human-meaningful error lines out of k6's JSON-lines stderr."""
    if not isinstance(stderr, str):
        return ""
    msgs: list[str] = []
    for line in stderr.strip().splitlines()[-15:]:
        try:
            obj = json.loads(line.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if obj.get("level") == "error" and obj.get("msg"):
            msgs.append(str(obj["msg"]).splitlines()[0][:200])
    return " | ".join(dict.fromkeys(msgs))[:600]


def parse_validation(payload: Any) -> str | None:
    """Return an actionable error when the k6 MCP `validate_script` payload is a failure,
    surfacing the real k6 stderr plus the server's structured issues and suggestions, so
    the fix loop has something to act on instead of a bare exit code."""
    if not isinstance(payload, dict):
        return f"unexpected validate_script response: {payload!r:.200}"
    # exit 99 means k6 ran the script but crossed a threshold: a runtime SLO breach (exactly what we
    # load-test to surface), not an invalid script. Accept it so it is measured by run_test, rather
    # than routed to the fix loop, which can only repair script syntax and would needlessly give up.
    if payload.get("exit_code") == 99:
        return None
    if payload.get("valid") is True and payload.get("exit_code") in (0, None):
        return None

    parts = [f"k6 validation failed (exit {payload.get('exit_code')})."]
    if err := _k6_stderr_errors(payload.get("stderr")):
        parts.append("k6 error: " + err)
    issues = payload.get("issues")
    if isinstance(issues, list):
        lines = [
            f"- {i['message']}" + (f" (fix: {i['suggestion']})" if i.get("suggestion") else "")
            for i in issues[:3]
            if isinstance(i, dict) and i.get("message")
        ]
        if lines:
            parts.append("issues:\n" + "\n".join(lines))
    if not err and len(parts) == 1:
        parts.append(str(payload.get("error") or payload.get("stderr") or payload.get("stdout") or "")[:400])
    return "\n".join(parts)[:1200]


_K6_REQS = re.compile(r"http_reqs[.\s]*:\s*([\d,]+)")
_K6_P95 = re.compile(r"http_req_duration[^\n]*?p\(95\)=([\d.]+)\s*(us|µs|ms|s)\b")
_K6_FAILED = re.compile(r"http_req_failed[.\s]*:\s*([\d.]+)%")
_K6_CHECKS = re.compile(r"✓\s*([\d,]+).*?✗\s*([\d,]+)", re.DOTALL)


def _to_ms(value: str, unit: str) -> float:
    v = float(value)
    return round(v / 1000 if unit in ("us", "µs") else v * 1000 if unit == "s" else v, 2)


def parse_run_stdout(stdout: Any) -> dict[str, Any]:
    """The k6 MCP returns the summary as stdout text, not structured metrics, so pull
    http_reqs / p95 / failure rate out of the default k6 end-of-test summary."""
    if not isinstance(stdout, str):
        return {}
    out: dict[str, Any] = {}
    if m := _K6_REQS.search(stdout):
        out["http_reqs"] = int(m.group(1).replace(",", ""))
    if m := _K6_P95.search(stdout):
        out["p95_ms"] = _to_ms(m.group(1), m.group(2))
    if m := _K6_FAILED.search(stdout):
        out["failed_rate"] = round(float(m.group(1)) / 100, 4)
    return out


def parse_run(payload: Any) -> RunResult:
    """Turn a k6 MCP `run_script` payload into a typed RunResult."""
    if not isinstance(payload, dict):
        return RunResult(
            success=False, exit_code=-1, detail=f"unexpected run_script response: {payload!r:.200}"
        )

    metrics = payload.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}

    p95 = _metric_value(metrics, "http_req_duration", "p(95)", "p95")
    failed = _metric_value(metrics, "http_req_failed", "rate", "value")
    reqs = _metric_value(metrics, "http_reqs", "count", "value", "rate")
    passed = _metric_value(metrics, "checks", "passes")
    failed_checks = _metric_value(metrics, "checks", "fails")

    if reqs is None or p95 is None:
        summary = parse_run_stdout(payload.get("stdout"))
        reqs = reqs if reqs is not None else summary.get("http_reqs")
        p95 = p95 if p95 is not None else summary.get("p95_ms")
        failed = failed if failed is not None else summary.get("failed_rate")

    return RunResult(
        success=bool(payload.get("success", payload.get("exit_code") == 0)),
        exit_code=int(payload.get("exit_code", -1)),
        http_reqs=int(reqs or 0),
        http_req_duration_p95_ms=p95,
        http_req_failed_rate=failed,
        checks_passed=int(passed or 0),
        checks_failed=int(failed_checks or 0),
        summary_text=str(payload.get("summary") or payload.get("stdout") or "")[:600],
        detail=str(payload.get("error") or "")[:400],
        raw_metrics=metrics,
    )
