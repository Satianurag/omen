"""The omen workflow as a Burr state machine, served over MCP by Theodosia.

An agent drives it one ``step`` at a time. The graph's edges are the only legal
moves; illegal steps are refused with ``valid_next_actions`` and recorded. k6 work
is delegated to the Grafana k6 MCP server and the post-run correlation to the
SigNoz MCP Server, both via ``call_upstream``. The driving agent never sees those
servers, only omen's single ``step`` tool.

Flow:
    select_mode ─diff──→ read_diff → extract_endpoints ┐
                └intent─→ parse_intent ────────────────┴→ doc_lookup → scaffold → generate_script
    generate_script → validate_script ─needs_fix─→ fix_script → validate_script  (bounded loop)
    validate_script → run_test ─signoz?─→ signoz_preflight → correlate → detect_anomalies ┐
                              └─else───────────────────────────────────────────────────┐ │
    (also on give-up) ───────────────────────────────────────────────────────────────→ analyze → screen → report

``scaffold`` composes a deterministic k6 baseline from the OpenAPI spec; ``generate_script``
then has the model author the final script on top of it, guided by k6's own
``generate_script`` MCP prompt. The ``validate_script → fix_script → validate_script`` loop is
an explicit gate: on a validation failure ``fix_script`` repairs the script from the k6 error
(real stderr + issues), bounded by ``MAX_FIX_ATTEMPTS``, then falls back to the deterministic
scaffold. So the model never produces an unvalidated script that reaches ``run_test``.
``doc_lookup`` (k6 MCP docs), ``signoz_preflight`` (SigNoz ``signoz_list_services``), and
``detect_anomalies`` (``signoz_aggregate_traces`` p95 time_series over the test window) are MCP-native phases: all degrade
gracefully via ``safe_upstream`` and record every upstream tool call to ``mcp_calls`` for the
report's provenance. Every path then converges on three model phases: ``analyze`` (the writer:
Granite 4.1 composes the cited analysis and the remediation diff), ``screen`` (the auditor: a
separate Granite Guardian model judges whether that analysis is grounded in its evidence), and
``report`` (narrates the run as a tarot reading and seals the verdict to the ledger).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import structlog
from burr.core import ApplicationBuilder, ApplicationContext, Condition, State, action
from theodosia import ERROR, OK, SourceResult, UpstreamError, call_upstream, mount, safe_upstream, tracker

from omen import analysis, arcana, codegen, guardian, parse, publish, remediate
from omen.githost import get_diff
from omen.k6gen import fetch_k6_generation_guidance
from omen.llm import LLMError, make_llm
from omen.state import MAX_FIX_ATTEMPTS, Endpoint
from omen.upstream import K6_SERVER, SIGNOZ_SERVER, signoz_configured, upstream

log = structlog.get_logger()


def _record(
    calls: list[dict[str, str]], phase: str, server: str, tool: str, status: str
) -> list[dict[str, str]]:
    """Append one upstream tool call to the provenance log (immutably), tagged with the phase
    that made it so the run's step trace can attribute each call to its state-machine phase."""
    return [*calls, {"phase": phase, "server": server, "tool": tool, "status": status}]


async def _signoz_json_tool(
    name: str, tool: str, args: dict[str, Any]
) -> SourceResult:
    """Call a SigNoz MCP tool whose payload is often a raw JSON string.

    ``theodosia.safe_upstream`` classifies any string containing ``error`` as
    failed; trace search responses include ``has_error`` and status messages, so
    we parse via ``parse_mcp_json`` instead.
    """
    try:
        raw = await call_upstream(SIGNOZ_SERVER, tool, args)
    except UpstreamError as exc:
        return SourceResult(name, ERROR, detail=f"upstream unavailable: {exc}"[:300])
    except Exception as exc:
        return SourceResult(name, ERROR, detail=f"{type(exc).__name__}: {exc}"[:300])
    parsed = parse.parse_mcp_json(raw)
    if isinstance(parsed, dict) and parsed.get("error"):
        return SourceResult(name, ERROR, detail=str(parsed["error"])[:300])
    return SourceResult(name, OK, data=parsed)


def _scenario_duration(plan: dict | None) -> str:
    """Wall-clock duration of the scaffold's scenario executor (for run timeouts only)."""
    tax = (plan or {}).get("test_taxonomy", "load")
    if tax == "smoke":
        return "5s"
    if tax == "regression_comparison":
        return "30s"
    return "35s"


def _load_spec(repo_path: str | None) -> dict[str, Any] | None:
    if not repo_path:
        return None
    spec_path = Path(repo_path) / "openapi.json"
    if not spec_path.exists():
        return None
    try:
        return json.loads(spec_path.read_text())
    except json.JSONDecodeError as exc:
        log.warning("openapi_parse_failed", error=str(exc))
        return None


@action(
    reads=[],
    writes=[
        "mode",
        "repo_path",
        "ref",
        "target_base_url",
        "user_intent",
        "signoz_service",
        "signoz_enabled",
        "stage",
        "error",
    ],
)
async def select_mode(
    state: State,
    repo_path: str = "",
    ref: str = "HEAD~1",
    target_base_url: str = "http://localhost:8000",
    intent: str = "",
    signoz_service: str = "petclinic",
) -> State:
    """Start a run. Pass `intent` for natural-language mode, or just `repo_path`/`ref` for diff mode. `signoz_service` is the target's ``OTEL_SERVICE_NAME`` in SigNoz."""
    intent = (intent or "").strip()
    return state.update(
        mode="intent" if intent else "diff",
        repo_path=repo_path or None,
        ref=ref,
        target_base_url=target_base_url,
        user_intent=intent or None,
        signoz_service=signoz_service or "petclinic",
        signoz_enabled=signoz_configured(),
        stage="selected",
        error=None,
    )


@action(reads=["repo_path", "ref"], writes=["diff_text", "stage", "error"])
async def read_diff(state: State) -> State:
    """Read `git diff <ref>..HEAD` from the repo."""
    try:
        diff = await asyncio.to_thread(get_diff, Path(state["repo_path"]), state["ref"])
    except Exception as exc:
        log.error("read_diff_failed", error=str(exc))
        return state.update(diff_text=None, stage="failed", error=f"read_diff: {exc}")
    return state.update(diff_text=diff, stage="diffed", error=None)


@action(reads=["diff_text", "repo_path"], writes=["endpoints", "openapi_spec", "graphrag_context", "stage"])
async def extract_endpoints(state: State) -> State:
    """Pull changed routes from the diff and load the sibling openapi.json."""
    endpoints = parse.extract_endpoints_from_diff(state["diff_text"] or "")
    spec = _load_spec(state["repo_path"])
    graphrag_context = parse.retrieve_context(spec, endpoints)
    log.info(
        "extract_endpoints_ok",
        count=len(endpoints),
        has_spec=spec is not None,
        has_context=graphrag_context is not None,
    )
    return state.update(
        endpoints=[ep.model_dump() for ep in endpoints],
        openapi_spec=spec,
        graphrag_context=graphrag_context,
        stage="scoped",
    )


@action(
    reads=["user_intent", "repo_path"],
    writes=["endpoints", "openapi_spec", "graphrag_context", "stage", "error"],
)
async def parse_intent(state: State) -> State:
    """Score OpenAPI operations against the natural-language intent and pick the top matches."""
    spec = _load_spec(state["repo_path"])
    if spec is None:
        return state.update(
            endpoints=[],
            openapi_spec=None,
            graphrag_context=None,
            stage="failed",
            error=f"parse_intent: no readable openapi.json under {state['repo_path']!r}",
        )
    endpoints = parse.score_intent(spec, state["user_intent"] or "")
    graphrag_context = parse.retrieve_context(spec, endpoints)
    log.info("parse_intent_ok", matched=len(endpoints), has_context=graphrag_context is not None)
    return state.update(
        endpoints=[ep.model_dump() for ep in endpoints],
        openapi_spec=spec,
        graphrag_context=graphrag_context,
        stage="scoped",
        error=None,
    )


@action(reads=["endpoints", "mcp_calls"], writes=["doc_refs", "mcp_calls", "stage"])
async def doc_lookup(state: State) -> State:
    """Consult the k6 MCP documentation for the constructs omen emits (HTTP requests, thresholds, checks, scenarios) and record version-grounded citations. Non-blocking: degrades to no references when the docs are unavailable."""
    calls = state["mcp_calls"]
    if not state["endpoints"]:
        return state.update(doc_refs=[], mcp_calls=calls, stage="documented")

    refs: list[dict[str, str]] = []
    sections = await safe_upstream(
        "k6_docs", K6_SERVER, "list_sections", {"root_slug": "using-k6", "depth": 2}, expect="dict"
    )
    calls = _record(calls, "doc_lookup", K6_SERVER, "list_sections", sections.status)
    if sections.usable:
        for slug in parse.select_doc_slugs(sections.data):
            doc = await safe_upstream(
                "k6_docs", K6_SERVER, "get_documentation", {"slug": slug}, expect="dict"
            )
            calls = _record(calls, "doc_lookup", K6_SERVER, "get_documentation", doc.status)
            if doc.usable:
                refs.append(parse.parse_documentation(slug, doc.data))
    log.info("doc_lookup_done", refs=len(refs))
    return state.update(doc_refs=refs, mcp_calls=calls, stage="documented")


@action(
    reads=["endpoints", "openapi_spec", "target_base_url"],
    writes=["plan", "scaffold_script", "stage", "error"],
)
async def scaffold(state: State) -> State:
    """Compose a deterministic, self-contained k6 scaffold from the OpenAPI spec (no model): per-endpoint requests with sample bodies, the baked base URL, and load options. This is the runnable baseline the next step builds on."""
    endpoints = [Endpoint(**e) for e in state["endpoints"]]
    if not endpoints:
        return state.update(stage="failed", error="scaffold: no endpoints to test")

    plan = codegen.default_plan(endpoints)
    script = codegen.compose(
        plan=plan,
        openapi_spec=state["openapi_spec"],
        endpoints=endpoints,
        base_url=state["target_base_url"],
    )
    log.info("scaffold_done", endpoints=len(endpoints))
    return state.update(plan=plan.model_dump(), scaffold_script=script, stage="scaffolded", error=None)


@action(
    reads=["scaffold_script", "endpoints", "user_intent", "mcp_calls", "graphrag_context"],
    writes=["generated_script", "stage", "mcp_calls"],
)
async def generate_script(state: State) -> State:
    """Author the final k6 script on top of the scaffold, using k6's own `generate_script` MCP prompt and best-practices to guide the model. Falls back to the scaffold when the model or guidance is unavailable; validation failures are repaired by the fix_script phase."""
    scaffold_script = state["scaffold_script"]
    endpoints = [Endpoint(**e) for e in state["endpoints"]]
    description = parse.build_generation_description(
        endpoints, state["user_intent"], scaffold_script, schema_context=state.get("graphrag_context")
    )
    calls = state["mcp_calls"]

    guidance = await fetch_k6_generation_guidance(description)
    calls = _record(
        calls, "generate_script", K6_SERVER, "generate_script(prompt)", "ok" if guidance else "error"
    )
    if guidance:
        system = guidance + "\n\nReturn ONLY the k6 script. No prose, no markdown fences."
    else:
        system = "You are a k6 expert. Return ONLY a single self-contained k6 script. No prose, no markdown fences."

    script = ""
    try:
        raw = await asyncio.to_thread(make_llm().generate, system=system, user=description)
        script = parse.extract_script(raw)
    except LLMError as exc:
        log.warning("generate_script_llm_failed", error=str(exc))

    if script:
        script = parse.finalize_script(script, scaffold_script)
    if not script:
        log.info("generate_script_used_scaffold")
        script = scaffold_script
    return state.update(generated_script=script, stage="generated", mcp_calls=calls)


@action(
    reads=["generated_script", "scaffold_script", "validation_error", "endpoints", "fix_attempts"],
    writes=["generated_script", "stage", "fix_attempts"],
)
async def fix_script(state: State) -> State:
    """Repair the k6 script using the error the k6 MCP `validate_script` tool returned (real stderr + issues + suggestions), then loop back to validation. The correction loop is an explicit edge in the state machine; on a model failure it falls back to the scaffold."""
    endpoints = [Endpoint(**e) for e in state["endpoints"]]
    description = parse.build_fix_description(state["generated_script"], state["validation_error"], endpoints)
    system = (
        "You are a k6 expert. Fix the script so it passes validation. Return ONLY the corrected "
        "k6 script. No prose, no markdown fences."
    )

    script = ""
    try:
        raw = await asyncio.to_thread(make_llm().generate, system=system, user=description)
        script = parse.extract_script(raw)
    except LLMError as exc:
        log.warning("fix_script_llm_failed", error=str(exc))

    if script:
        script = parse.finalize_script(script, state["scaffold_script"])
    if not script:
        script = state["scaffold_script"]
    log.info("fix_script_done", attempt=state["fix_attempts"] + 1)
    return state.update(generated_script=script, stage="generated", fix_attempts=state["fix_attempts"] + 1)


@action(
    reads=["generated_script", "scaffold_script", "fix_attempts", "mcp_calls"],
    writes=["validation_error", "generated_script", "stage", "mcp_calls"],
)
async def validate_script(state: State) -> State:
    """Validate the script via the k6 MCP `validate_script` tool (1 VU, 1 iteration)."""
    calls = state["mcp_calls"]
    try:
        payload = await call_upstream(K6_SERVER, "validate_script", {"script": state["generated_script"]})
    except Exception as exc:
        return state.update(
            validation_error=f"k6 MCP unavailable: {exc}",
            stage="failed_validation",
            mcp_calls=_record(calls, "validate_script", K6_SERVER, "validate_script", "error"),
        )

    calls = _record(calls, "validate_script", K6_SERVER, "validate_script", "ok")
    err = parse.parse_validation(payload)
    if err is None:
        return state.update(validation_error=None, stage="validated", mcp_calls=calls)
    if state["fix_attempts"] < MAX_FIX_ATTEMPTS:
        log.warning("validation_failed_retrying", attempts=state["fix_attempts"], error=err)
        return state.update(validation_error=err, stage="needs_fix", mcp_calls=calls)
    # Gave up fixing the authored script; run the deterministic scaffold if it is untried.
    if state["generated_script"] != state["scaffold_script"]:
        log.warning("validation_gave_up_using_scaffold", error=err)
        return state.update(
            validation_error=err,
            generated_script=state["scaffold_script"],
            stage="validated",
            mcp_calls=calls,
        )
    return state.update(validation_error=err, stage="failed_validation", mcp_calls=calls)


def _run_timeout(duration: str) -> float:
    """A generous ceiling for the k6 MCP run_script call: the test duration plus headroom for
    provisioning, teardown, and result transfer. A run that exceeds this is treated as hung
    (an authored script can wedge k6 so it never exits, and the MCP call would otherwise block
    the whole pipeline forever)."""
    d = duration.strip().lower()
    try:
        if d.endswith("ms"):
            secs = float(d[:-2]) / 1000
        elif d.endswith("m"):
            secs = float(d[:-1]) * 60
        elif d.endswith("s"):
            secs = float(d[:-1])
        else:
            secs = float(d)
    except ValueError:
        secs = 30.0
    return secs + 120.0


def _run_script_args(script: str, plan: dict | None) -> dict[str, Any]:
    """Map the plan taxonomy to mcp-k6 ``run_script`` overrides.

    grafana/mcp-k6 (May 2026) ignores ``options.scenarios`` in the script and runs a
    1-VU/30s default unless ``vus`` and ``duration`` are passed explicitly."""
    tax = (plan or {}).get("test_taxonomy", "load")
    args: dict[str, Any] = {"script": script}
    if tax == "smoke":
        args["vus"] = 1
        args["iterations"] = 5
    elif tax == "regression_comparison":
        args["vus"] = 20
        args["duration"] = "30s"
    else:
        args["vus"] = 50
        args["duration"] = "30s"
    return args


@action(
    reads=["generated_script", "scaffold_script", "plan", "mcp_calls"],
    writes=["run_result", "run_started_at", "run_ended_at", "stage", "error", "mcp_calls"],
)
async def run_test(state: State) -> State:
    """Execute the load test via the k6 MCP ``run_script`` tool.

    mcp-k6 ignores script ``options.scenarios`` unless ``vus``/``duration`` are passed as
    tool parameters, so the plan taxonomy is mapped to those overrides here. Bounded by a
    timeout: if the authored script wedges k6 so the call never returns, fall back to the
    deterministic scaffold once."""
    started = time.time()
    timeout = _run_timeout(_scenario_duration(state["plan"]))
    script, scaffold = state["generated_script"], state["scaffold_script"]
    calls = state["mcp_calls"]

    async def _run(src: str):
        return await asyncio.wait_for(
            call_upstream(K6_SERVER, "run_script", _run_script_args(src, state["plan"])),
            timeout=timeout,
        )

    try:
        payload = await _run(script)
        calls = _record(calls, "run_test", K6_SERVER, "run_script", "ok")
    except TimeoutError:
        log.warning("run_test_timeout", timeout_s=round(timeout))
        calls = _record(calls, "run_test", K6_SERVER, "run_script", "timeout")
        if scaffold and script != scaffold:
            try:
                payload = await _run(scaffold)
                calls = _record(calls, "run_test", K6_SERVER, "run_script", "ok")
                log.info("run_test_scaffold_fallback")
            except Exception as exc:
                return state.update(
                    run_result=None,
                    stage="failed",
                    error=f"k6 run timed out; scaffold fallback failed: {exc}",
                    mcp_calls=_record(calls, "run_test", K6_SERVER, "run_script", "error"),
                )
        else:
            return state.update(
                run_result=None,
                stage="failed",
                error=f"k6 run timed out after {round(timeout)}s",
                mcp_calls=calls,
            )
    except Exception as exc:
        return state.update(
            run_result=None,
            stage="failed",
            error=f"k6 MCP run failed: {exc}",
            mcp_calls=_record(calls, "run_test", K6_SERVER, "run_script", "error"),
        )

    ended = time.time()
    result = parse.parse_run(payload)
    log.info("run_test_ok", reqs=result.http_reqs, exit_code=result.exit_code)
    return state.update(
        run_result=result.model_dump(),
        run_started_at=started,
        run_ended_at=ended,
        stage="ran",
        error=None,
        mcp_calls=calls,
    )


@action(
    reads=["signoz_service", "run_started_at", "run_ended_at", "mcp_calls"],
    writes=["signoz_preflight", "mcp_calls", "stage", "run_ended_at"],
)
async def signoz_preflight(state: State) -> State:
    """Verify the target OTel service exists in SigNoz via ``signoz_list_services``."""
    service = state["signoz_service"]
    earliest = state["run_started_at"] or (time.time() - 300)
    flush_sec = float(os.environ.get("OMEN_SIGNOZ_FLUSH_SEC", "5"))
    if flush_sec > 0:
        await asyncio.sleep(flush_sec)
    latest = max(state["run_ended_at"] or time.time(), time.time())
    calls = state["mcp_calls"]

    listed = await safe_upstream(
        "signoz_services",
        SIGNOZ_SERVER,
        "signoz_list_services",
        {"start": int(earliest * 1000), "end": int(latest * 1000), "searchContext": "omen preflight"},
        expect="dict",
    )
    calls = _record(calls, "signoz_preflight", SIGNOZ_SERVER, "signoz_list_services", listed.status)
    facts = parse.parse_service_facts(service, listed.data if listed.usable else None)
    preflight = {
        **facts,
        "services": parse.parse_signoz_services(listed.data) if listed.usable else [],
    }
    log.info(
        "signoz_preflight_done",
        service=service,
        exists=facts["exists"],
        services=len(preflight["services"]),
    )
    return state.update(signoz_preflight=preflight, mcp_calls=calls, stage="preflighted", run_ended_at=latest)


@action(
    reads=["signoz_service", "run_started_at", "run_ended_at", "mcp_calls"],
    writes=["correlation", "mcp_calls", "stage"],
)
async def correlate(state: State) -> State:
    """Read server-side traces over the test window via SigNoz MCP aggregate/search tools."""
    earliest = state["run_started_at"] or (time.time() - 300)
    flush_sec = float(os.environ.get("OMEN_SIGNOZ_FLUSH_SEC", "5"))
    latest = (state["run_ended_at"] or time.time()) + flush_sec + 2.0
    service = state["signoz_service"]
    mcp_calls = parse.build_correlation_mcp_calls(service, earliest, latest)

    calls = state["mcp_calls"]
    raw: dict[str, Any] = {}
    available = False

    async def _search_root_cause(args: dict[str, Any]) -> SourceResult:
        nonlocal calls, available
        result = await _signoz_json_tool("signoz_root", "signoz_search_traces", args)
        calls = _record(calls, "correlate", SIGNOZ_SERVER, "signoz_search_traces", result.status)
        available = available or result.usable
        return result

    async def agg(name: str, args: dict[str, Any]) -> Any:
        nonlocal calls, available
        result = await safe_upstream(
            f"signoz_{name}", SIGNOZ_SERVER, "signoz_aggregate_traces", args, expect="dict"
        )
        calls = _record(calls, "correlate", SIGNOZ_SERVER, "signoz_aggregate_traces", result.status)
        available = available or result.usable
        raw[name] = {"tool": "signoz_aggregate_traces", "args": args, "data": result.data}
        return result.data

    total = await agg("rollup_total", mcp_calls["rollup_total"])
    errs = await agg("rollup_5xx", mcp_calls["rollup_5xx"])
    client_errs = await agg("rollup_4xx", mcp_calls["rollup_4xx"])
    p95 = await agg("rollup_p95", mcp_calls["rollup_p95"])
    tl_err = await agg("timeline_errors", mcp_calls["timeline_errors"])
    tl_p95 = await agg("timeline_p95", mcp_calls["timeline_p95"])
    by_count = await agg("by_path", mcp_calls["by_path"])
    by_err = await agg("by_path_errors", mcp_calls["by_path_errors"])
    by_p95 = await agg("by_path_p95", mcp_calls["by_path_p95"])

    search = await _search_root_cause(mcp_calls["root_cause"])
    raw["root_cause"] = {
        "tool": "signoz_search_traces",
        "args": mcp_calls["root_cause"],
        "data": search.data,
    }
    root_cause = parse.summarize_root_causes(search.data)
    if not root_cause or str(root_cause[0].get("error_message", "")).startswith("HTTP "):
        await asyncio.sleep(max(2.0, flush_sec / 2))
        retry = await _search_root_cause(mcp_calls["root_cause"])
        raw["root_cause_retry"] = {
            "tool": "signoz_search_traces",
            "args": mcp_calls["root_cause"],
            "data": retry.data,
        }
        retry_causes = parse.summarize_root_causes(retry.data)
        if retry_causes and not str(retry_causes[0].get("error_message", "")).startswith("HTTP "):
            root_cause = retry_causes
            search = retry
    if not root_cause or str(root_cause[0].get("error_message", "")).startswith("HTTP "):
        fallback = await _search_root_cause(mcp_calls["root_cause_fallback"])
        if fallback.usable:
            raw["root_cause_fallback"] = {
                "tool": "signoz_search_traces",
                "args": mcp_calls["root_cause_fallback"],
                "data": fallback.data,
            }
            fb = parse.summarize_root_causes(fallback.data)
            if (fb and not str(fb[0].get("error_message", "")).startswith("HTTP ")) or not root_cause:
                root_cause = fb

    trace_ids = [
        r.get("trace_id")
        for r in parse.summarize_correlation(search.data)
        if isinstance(r.get("trace_id"), str) and r.get("trace_id")
    ][:3]
    detail_payloads: list[Any] = []
    win = mcp_calls["root_cause"]
    for tid in trace_ids:
        detail = await _signoz_json_tool(
            f"signoz_trace_{tid[:8]}",
            "signoz_get_trace_details",
            {
                "traceId": tid,
                "start": str(win["start"]),
                "end": str(win["end"]),
                "includeSpans": True,
                "searchContext": "omen root cause trace details",
            },
        )
        calls = _record(calls, "correlate", SIGNOZ_SERVER, "signoz_get_trace_details", detail.status)
        if detail.usable and detail.data:
            detail_payloads.append(detail.data)

    rollup_row = {
        "total_events": parse.scalar_value(total),
        "server_errors": parse.scalar_value(errs),
        "client_errors": parse.scalar_value(client_errs),
        "p95_ms": parse.nano_to_ms(parse.scalar_value(p95)),
    }
    timeline = parse.merge_timeline(tl_err, tl_p95)
    by_path = parse.merge_by_path(by_count, by_err, by_p95)
    root_cause = parse.enrich_root_causes(root_cause, detail_payloads)

    rows_by_query = {
        "rollup": [rollup_row],
        "timeline": timeline,
        "by_path": by_path,
        "root_cause": root_cause,
    }
    findings = parse.summarize_findings(rows_by_query)
    out = {k: {"rows": v} for k, v in rows_by_query.items()}
    correlation = {"available": available, "queries": out, "findings": findings, "mcp": raw}
    log.info(
        "correlate_done",
        available=available,
        worst_path=(findings["worst_path"] or {}).get("path"),
        top_error=(findings["top_error"] or {}).get("error_message"),
    )
    return state.update(correlation=correlation, mcp_calls=calls, stage="correlated")


@action(
    reads=["signoz_service", "run_started_at", "run_ended_at", "correlation", "mcp_calls"],
    writes=["anomalies", "correlation", "mcp_calls", "stage"],
)
async def detect_anomalies(state: State) -> State:
    """P95 latency time-series over the test window via ``signoz_aggregate_traces``."""
    earliest = state["run_started_at"] or (time.time() - 300)
    latest = state["run_ended_at"] or time.time()
    service = state["signoz_service"]

    calls = state["mcp_calls"]
    queries = parse.build_anomaly_mcp_calls(service, earliest, latest)
    out: dict[str, Any] = {}
    available = False

    result = await safe_upstream(
        "signoz_forecast",
        SIGNOZ_SERVER,
        "signoz_aggregate_traces",
        queries["forecast"],
        expect="dict",
    )
    calls = _record(calls, "detect_anomalies", SIGNOZ_SERVER, "signoz_aggregate_traces", result.status)
    available = result.usable
    rows = []
    for row in parse.summarize_correlation(result.data):
        t = row.get("timestamp") or row.get("time") or row.get("_time")
        p95_ms = parse.nano_to_ms(row.get("value") or row.get("p95(duration_nano)"))
        rows.append({"_time": t, "p95_ms": p95_ms})
    out["forecast"] = {"tool": "signoz_aggregate_traces", "args": queries["forecast"], "rows": rows}

    summary = parse.summarize_anomalies(
        rows, [], method="signoz aggregate_traces p95 time_series", floor_ms=_LATENCY_FLOOR_MS
    )
    anomalies = {"available": available, "forecaster": "signoz", "queries": out, **summary}

    correlation = dict(state["correlation"] or {})
    findings = dict(correlation.get("findings") or {})
    findings["anomaly"] = summary
    correlation["findings"] = findings
    log.info(
        "detect_anomalies_done",
        available=available,
        breaches=summary["forecast_breaches"],
        buckets=summary["anomalous_buckets"],
    )
    return state.update(anomalies=anomalies, correlation=correlation, mcp_calls=calls, stage="detected")


@action(
    reads=[
        "endpoints",
        "run_result",
        "correlation",
        "anomalies",
        "diff_text",
        "repo_path",
        "validation_error",
        "error",
        "mode",
        "signoz_preflight",
    ],
    writes=["analysis", "recommendation", "remediation", "analysis_context", "verdict", "stage"],
)
async def analyze(state: State) -> State:
    """The writer phase (Granite 4.1): turn the correlated facts into a cited analysis (cause,
    evidence, recommendation) and, in diff mode, a validated remediation diff. The evidence the
    analysis is grounded on is captured verbatim so the next phase can screen the analysis against
    it. Deterministic fallbacks keep this phase working when no model is configured."""
    verdict = _verdict(state)
    findings = (state["correlation"] or {}).get("findings") or {}
    evidence = analysis.gather_evidence(
        run_result=state["run_result"],
        findings=findings,
        anomalies=state["anomalies"],
        preflight=state["signoz_preflight"],
    )
    analysis_text = await _analyze(state, verdict, evidence)
    remediation = await _remediate(state, verdict)
    context = "\n".join(f"[{source}] {claim}" for claim, source in evidence)
    return state.update(
        analysis=analysis_text,
        recommendation=analysis.recommend(findings),
        remediation=remediation,
        analysis_context=context,
        verdict=verdict,
        stage="analyzed",
    )


@action(reads=["analysis", "analysis_context"], writes=["groundedness", "stage"])
async def screen(state: State) -> State:
    """The auditor phase (Granite Guardian): an independent model judges whether the analysis is
    grounded in the evidence it cites, before the verdict is published. The pass/fail verdict is
    sealed to the report ledger. Non-blocking: degrades to unavailable when Guardian is off or
    unreachable, so a run never fails for lack of the screen."""
    text, context = state["analysis"], state["analysis_context"]
    if not (guardian.guardian_configured() and text and context):
        return state.update(groundedness={"available": False, "grounded": None}, stage="screened")
    verdict = await asyncio.to_thread(guardian.make_guardian().groundedness, context=context, response=text)
    log.info("screen_done", available=verdict.get("available"), grounded=verdict.get("grounded"))
    return state.update(groundedness=verdict, stage="screened")


@action(
    reads=[
        "endpoints",
        "plan",
        "run_result",
        "correlation",
        "anomalies",
        "validation_error",
        "error",
        "mode",
        "signoz_enabled",
        "doc_refs",
        "signoz_preflight",
        "mcp_calls",
        "fix_attempts",
        "analysis",
        "recommendation",
        "remediation",
        "groundedness",
        "verdict",
    ],
    writes=["report", "stage"],
)
async def report(state: State) -> State:
    """Assemble the final report from the analyzed and screened state and have the model narrate
    the run as a tarot reading. Terminal action."""
    verdict = state["verdict"] or _verdict(state)
    narration = await _narrate(state, verdict)
    summary = {
        "session": _session(),
        "mode": state["mode"],
        "endpoints_tested": state["endpoints"],
        "plan": state["plan"],
        "validation_error": state["validation_error"],
        "error": state["error"],
        "run_result": state["run_result"],
        "signoz_enabled": state["signoz_enabled"],
        "correlation": state["correlation"],
        "anomalies": state["anomalies"],
        "steps": _trace(state, verdict),
        "mcp_provenance": {
            "tool_calls": state["mcp_calls"],
            "k6_doc_refs": state["doc_refs"],
            "signoz_preflight": state["signoz_preflight"],
        },
        "narration": narration,
        "analysis": state["analysis"],
        "recommendation": state["recommendation"],
        "remediation": state["remediation"],
        "groundedness": state["groundedness"],
        "verdict": verdict,
    }
    if publish.publish_configured():
        summary["published"] = await asyncio.to_thread(publish.publish_run, summary)
        log.info("report_published", ok=summary["published"])
    return state.update(report=summary, stage="done")


def _session() -> dict[str, Any]:
    """Burr's own run identifiers for the current execution, so the run and its step trace land
    in SigNoz under the same ``app_id`` that ``omen sessions show`` and Burr's tracker use, rather
    than a omen-invented id."""
    ctx = ApplicationContext.get()
    if ctx is None:
        return {}
    return {
        "app_id": getattr(ctx, "app_id", None),
        "partition_key": getattr(ctx, "partition_key", None),
        "sequence_id": getattr(ctx, "sequence_id", None),
    }


_TERMINAL_PHASES = ("read_diff", "parse_intent", "extract_endpoints")


def _trace(state: State, verdict: str) -> list[dict[str, Any]]:
    """The agent's walk through the state machine as an ordered list of one record per executed
    phase: its Major Arcana card, an outcome status, and the upstream tool calls it made (from the
    phase-tagged provenance). This is what makes the FSM run itself observable in SigNoz, keyed to
    Burr's `app_id`."""
    by_phase: dict[str, list[dict]] = {}
    for c in state["mcp_calls"] or []:
        by_phase.setdefault(c.get("phase", ""), []).append(c)

    phases = ["select_mode", "read_diff" if state["mode"] == "diff" else "parse_intent"]
    if state["mode"] == "diff" and state["diff_text"]:
        phases.append("extract_endpoints")
    phases += ["doc_lookup", "scaffold", "generate_script", "validate_script"]
    if state["fix_attempts"]:
        phases.append("fix_script")
    if state["run_result"] is not None:
        phases.append("run_test")
        if state["signoz_enabled"]:
            phases += ["signoz_preflight", "correlate", "detect_anomalies"]
    phases += ["analyze"]
    if (state["groundedness"] or {}).get("available"):
        phases.append("screen")
    phases.append("report")

    steps = []
    for seq, phase in enumerate(phases):
        ptools = by_phase.get(phase, [])
        num, card, _ = arcana.ARCANA.get(phase, ("", phase, ""))
        steps.append(
            {
                "seq": seq,
                "phase": phase,
                "card_num": num,
                "card": card,
                "status": _step_status(state, phase, ptools, verdict),
                "tool_calls": len(ptools),
                "tools": [f"{t['server']}.{t['tool']}={t['status']}" for t in ptools],
            }
        )
    return steps


def _step_status(state: State, phase: str, ptools: list[dict], verdict: str) -> str:
    statuses = {t["status"] for t in ptools}
    if "timeout" in statuses:
        return "timeout (scaffold fallback)"
    if "error" in statuses:
        return "degraded"
    if state["fix_attempts"] and phase in ("validate_script", "fix_script"):
        return f"repaired x{state['fix_attempts']}"
    if phase == "screen":
        return "grounded" if (state["groundedness"] or {}).get("grounded") else "ungrounded"
    if phase == "report":
        v = (verdict or "").lower()
        if "regression" in v:
            return "regression"
        if "throttling" in v:
            return "throttled"
        if "degradation" in v:
            return "degrading"
        return "failed" if v.startswith(("failed", "no run")) else "ok"
    return "ok"


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_LATENCY_FLOOR_MS = 40.0  # below this p95/forecast, a flagged bucket is jitter, not a degradation
_HIGH_LATENCY_MS = 200.0  # rollup p95 above this with zero errors is a latency regression under load


def _verdict(state: State) -> str:
    if state["error"]:
        return f"failed: {state['error']}"
    if state["run_result"] is None:
        return f"no run: {state['validation_error'] or 'validation gave up'}"
    findings = (state["correlation"] or {}).get("findings") or {}
    wp, te = findings.get("worst_path"), findings.get("top_error")
    server_errors = findings.get("server_errors") or 0
    if wp and server_errors > 0:
        cause = (te or {}).get("error_message") or "server errors under load"
        p95_wp = wp.get("p95_ms")
        p95_part = f"p95 {p95_wp}ms, " if p95_wp is not None else ""
        return (
            f"server-side regression: {wp['path']} {p95_part}"
            f"{wp.get('err_pct', 0)}% 5xx, cause: {cause}"
        )
    # Client-side throttling: 4xx dominate with no server errors. The service is healthy; offered
    # load simply exceeded a rate limit, so the failures are the client's to back off on, not a
    # server regression. Checked before the latency branch, since p95 time-series can fire on the
    # near-zero p95 of fast-rejected requests and otherwise mislabel a throttle as degradation.
    total = findings.get("total_events") or 0
    ce = findings.get("client_errors") or 0
    rr = state["run_result"] or {}
    k6_failed = rr.get("http_req_failed_rate") or 0
    if (findings.get("server_errors") or 0) == 0 and k6_failed >= 0.2:
        path = (wp or {}).get("path") or "the changed endpoint"
        return (
            f"client-side throttling: {path} {round(100 * k6_failed)}% failed at k6, "
            "no server errors (rate-limited, not broken)"
        )
    if (findings.get("server_errors") or 0) == 0 and total and ce / total >= 0.2:
        path = (wp or {}).get("path") or "the changed endpoint"
        return (
            f"client-side throttling: {path} {round(100 * ce / total)}% 4xx, "
            "no server errors (rate-limited, not broken)"
        )
    p95 = _as_float(findings.get("p95_ms"))
    if server_errors == 0 and p95 is not None and p95 >= _HIGH_LATENCY_MS:
        path = (wp or {}).get("path") or "the changed endpoint"
        return (
            f"latency degradation: {path} p95 {p95}ms with no errors; "
            "server stayed reachable but response time regressed under load"
        )
    # No errors, but the latency trend flags a regression the error rate misses.
    # Compare first vs second half of p95 buckets (SigNoz time_series) or any forecast band breach.
    an = state["anomalies"] or {}
    fp, p95 = _as_float(an.get("forecast_p95_ms")), _as_float(findings.get("p95_ms"))
    rising = fp is not None and p95 is not None and fp > 1.15 * p95
    flagged = (an.get("anomalous_buckets") or 0) or (an.get("forecast_breaches") or 0)
    # ...but only when latency is actually meaningful. A single bucket near the floor
    # otherwise cries wolf. Count the forecast too: a trend heading above the floor is real
    # degradation even if the window's measured p95 has not crossed it yet.
    slow = (p95 is not None and p95 >= _LATENCY_FLOOR_MS) or (fp is not None and fp >= _LATENCY_FLOOR_MS)
    if an.get("available") and slow and (rising or flagged):
        path = (wp or {}).get("path") or "the changed endpoint"
        return (
            f"latency degradation: {path} p95 {findings.get('p95_ms')}ms with no errors; "
            f"SigNoz forecast p95 {an.get('forecast_p95_ms')}ms, {an.get('anomalous_buckets')} anomalous bucket(s)"
        )
    rr = state["run_result"]
    return "passed" if rr and rr.get("success") else f"ran with failures (exit {(rr or {}).get('exit_code')})"


_NARRATION_SYSTEM = (
    "You are narrating a load-test run, themed as a tarot reading. For each phase line you are "
    "given, write exactly one short, concrete sentence prefixed with its card name (for example "
    "'The Fool: ...'). Use the numbers from the facts. Keep each line under 18 words. No preamble, "
    "no markdown, one line per phase."
)


def _phase_facts(state: State, verdict: str) -> str:
    lines = [f"- The Fool (select_mode): {state['mode']} mode, {len(state['endpoints'])} endpoint(s)"]
    if refs := state["doc_refs"]:
        names = ", ".join(r.get("slug", "").split("/")[-1] for r in refs)
        lines.append(f"- The Hierophant (doc_lookup): consulted k6 docs ({names})")
    if plan := state["plan"]:
        lines.append(f"- The Chariot (scaffold): deterministic {plan.get('test_taxonomy', 'load')} scaffold")
    fixes = state["fix_attempts"]
    if state["validation_error"]:
        lines.append(
            f"- The Magician/Justice/Temperance: validation failed after {fixes} fix attempt(s), ran the scaffold"
        )
    elif fixes:
        lines.append(
            f"- The Magician/Justice/Temperance: authored the script, repaired it in {fixes} round(s), validated"
        )
    else:
        lines.append("- The Magician/Justice: authored the script on the scaffold; it passed validation")
    if rr := state["run_result"]:
        lines.append(
            f"- The Tower (run_test): {rr.get('http_reqs')} requests, p95 {rr.get('http_req_duration_p95_ms')} ms, "
            f"failed rate {rr.get('http_req_failed_rate')}"
        )
    if state["signoz_enabled"]:
        pf = state["signoz_preflight"] or {}
        lines.append(
            f"- The Hermit (signoz_preflight): service {pf.get('service')}, exists={pf.get('exists')}, "
            f"spans={pf.get('span_count')}"
        )
        f = (state["correlation"] or {}).get("findings") or {}
        wp, te = f.get("worst_path"), f.get("top_error")
        detail = f"server 5xx={f.get('server_errors')} 4xx={f.get('client_errors')} p95={f.get('p95_ms')}ms"
        if wp:
            detail += (
                f"; worst endpoint {wp.get('path')} at {wp.get('err_pct')}% errors, p95 {wp.get('p95_ms')}ms"
            )
        if te:
            detail += f"; dominant server error '{te.get('error_message')}' ({te.get('count')}x)"
        lines.append(f"- The Lovers (correlate): {detail}")
        if an := (state["anomalies"] or {}):
            lines.append(
                f"- The Star (detect_anomalies): {an.get('method', 'forecast')}, forecast p95 "
                f"{an.get('forecast_p95_ms')}ms (peak {an.get('peak_p95_ms')}ms over "
                f"{an.get('buckets_analyzed', 0)} buckets); {an.get('anomalous_buckets', 0)} "
                f"anomalous bucket(s), {an.get('forecast_breaches', 0)} band breach(es)"
            )
    if state["analysis"]:
        cure = " and a remediation diff" if state["remediation"] else ""
        lines.append(f"- The Sun (analyze): wrote the cited analysis{cure}")
    if (g := state["groundedness"]) and g.get("available"):
        lines.append(
            f"- The Hanged Man (screen): Guardian groundedness check = "
            f"{'grounded' if g.get('grounded') else 'UNGROUNDED'}"
        )
    lines.append(f"- Judgement (report): verdict = {verdict}")
    return "\n".join(lines)


def _omen_fallback(state: State, verdict: str) -> str:
    ran = ["select_mode", "doc_lookup", "scaffold", "generate_script", "validate_script"]
    if state["run_result"]:
        ran.append("run_test")
    if state["signoz_enabled"]:
        ran += ["signoz_preflight", "correlate", "detect_anomalies"]
    ran.append("analyze")
    if (state["groundedness"] or {}).get("available"):
        ran.append("screen")
    ran.append("report")
    lines = []
    for phase in ran:
        card = arcana.ARCANA.get(phase)
        if card:
            _, name, omen = card
            lines.append(f"{name}: {omen}")
    lines.append(arcana.reading(verdict))
    return "\n".join(lines)


async def _analyze(state: State, verdict: str, evidence: list[tuple[str, str]]) -> str:
    """The practical, cited writeup: cause, endpoints, evidence, recommendation. Model-written
    when one is configured, deterministic otherwise, so it always exists."""
    findings = (state["correlation"] or {}).get("findings") or {}
    fallback = analysis.compose_analysis(
        verdict,
        mode=state["mode"],
        endpoints=state["endpoints"],
        findings=findings,
        evidence=evidence,
    )
    documents = [(source, claim) for claim, source in evidence]
    instruction = (
        f"Write the post-run analysis for this load test. Verdict: {verdict}. Ground every fact in "
        "the provided documents and cite each one's source in the Evidence section."
    )
    try:
        text = await asyncio.to_thread(
            make_llm().generate,
            system=analysis.ANALYSIS_SYSTEM,
            user=instruction,
            documents=documents,
        )
        if text and text.strip():
            # Some models add markdown emphasis / trailing line-break spaces; normalize for
            # clean terminal and dashboard display.
            return "\n".join(line.rstrip().replace("**", "") for line in text.strip().splitlines())
    except LLMError as exc:
        log.warning("analysis_llm_failed", error=str(exc))
    return fallback


async def _remediate(state: State, verdict: str) -> str | None:
    """Propose a remediation: a validated unified diff that fixes the correlated root cause.
    The model emits SEARCH/REPLACE edits (reliable, unlike model-authored unified diffs); omen
    applies them to the real file, validates the result still parses (AST), and renders a real
    diff with difflib. Only in diff mode with a server-side root cause and the file on hand;
    otherwise None, and the analysis carries the prose recommendation. A small ensemble: the
    first candidate that applies cleanly and parses wins."""
    diff_text, repo = state["diff_text"], state["repo_path"]
    findings = (state["correlation"] or {}).get("findings") or {}
    if not (diff_text and repo and (findings.get("top_error") or findings.get("worst_path"))):
        return None
    path = remediate.changed_file(diff_text)
    src_file = Path(repo) / path if path else None
    if not (src_file and src_file.exists()):
        return None
    source = src_file.read_text()
    documents = remediate.documents(source, findings, analysis.recommend(findings))
    instruction = f"Propose the minimal code fix for the root cause that resolves this finding: {verdict}."

    for _ in range(3):  # ensemble: first edit that applies cleanly and still parses wins
        try:
            text = await asyncio.to_thread(
                make_llm().generate,
                system=remediate.SEARCH_REPLACE_SYSTEM,
                user=instruction,
                documents=documents,
            )
        except LLMError as exc:
            log.warning("remediation_llm_failed", error=str(exc))
            continue  # transient (e.g. a rate-limited model call); try the next ensemble attempt
        patched = remediate.apply_blocks(source, remediate.parse_blocks(text))
        if patched and remediate.valid_python(patched):
            log.info("remediation_ok", file=path)
            return remediate.unified(source, patched, path)
    return None


async def _narrate(state: State, verdict: str) -> str:
    facts = _phase_facts(state, verdict)
    try:
        text = await asyncio.to_thread(make_llm().generate, system=_NARRATION_SYSTEM, user=facts)
        if text and text.strip():
            return arcana.adorn(text.strip())
    except LLMError as exc:
        log.warning("narration_llm_failed", error=str(exc))
    return arcana.adorn(_omen_fallback(state, verdict))


def build_application(hooks=None):
    builder = (
        ApplicationBuilder()
        .with_actions(
            select_mode=select_mode,
            read_diff=read_diff,
            extract_endpoints=extract_endpoints,
            parse_intent=parse_intent,
            doc_lookup=doc_lookup,
            scaffold=scaffold,
            generate_script=generate_script,
            validate_script=validate_script,
            fix_script=fix_script,
            run_test=run_test,
            signoz_preflight=signoz_preflight,
            correlate=correlate,
            detect_anomalies=detect_anomalies,
            analyze=analyze,
            screen=screen,
            report=report,
        )
        .with_transitions(
            ("select_mode", "read_diff", Condition.expr("stage == 'selected' and mode == 'diff'")),
            ("select_mode", "parse_intent", Condition.expr("stage == 'selected' and mode == 'intent'")),
            ("read_diff", "analyze", Condition.expr("stage == 'failed'")),
            ("read_diff", "extract_endpoints", Condition.expr("stage == 'diffed'")),
            ("extract_endpoints", "doc_lookup", Condition.expr("stage == 'scoped'")),
            ("parse_intent", "analyze", Condition.expr("stage == 'failed'")),
            ("parse_intent", "doc_lookup", Condition.expr("stage == 'scoped'")),
            ("doc_lookup", "scaffold", Condition.expr("stage == 'documented'")),
            ("scaffold", "analyze", Condition.expr("stage == 'failed'")),
            ("scaffold", "generate_script", Condition.expr("stage == 'scaffolded'")),
            ("generate_script", "validate_script", Condition.expr("stage == 'generated'")),
            ("validate_script", "fix_script", Condition.expr("stage == 'needs_fix'")),
            ("fix_script", "validate_script", Condition.expr("stage == 'generated'")),
            ("validate_script", "run_test", Condition.expr("stage == 'validated'")),
            ("validate_script", "analyze", Condition.expr("stage == 'failed_validation'")),
            ("run_test", "signoz_preflight", Condition.expr("stage == 'ran' and signoz_enabled")),
            ("run_test", "analyze", Condition.expr("stage == 'ran' and not signoz_enabled")),
            ("run_test", "analyze", Condition.expr("stage == 'failed'")),
            ("signoz_preflight", "correlate", Condition.expr("stage == 'preflighted'")),
            ("correlate", "detect_anomalies", Condition.expr("stage == 'correlated'")),
            ("detect_anomalies", "analyze", Condition.expr("stage == 'detected'")),
            ("analyze", "screen", Condition.expr("stage == 'analyzed'")),
            ("screen", "report", Condition.expr("stage == 'screened'")),
        )
        .with_state(
            stage="new",
            mode=None,
            repo_path=None,
            ref="HEAD~1",
            target_base_url="http://localhost:8000",
            user_intent=None,
            signoz_service="petclinic",
            signoz_enabled=False,
            diff_text=None,
            endpoints=[],
            openapi_spec=None,
            graphrag_context=None,
            doc_refs=[],
            plan=None,
            scaffold_script=None,
            generated_script=None,
            validation_error=None,
            fix_attempts=0,
            run_result=None,
            run_started_at=None,
            run_ended_at=None,
            signoz_preflight=None,
            correlation=None,
            anomalies=None,
            analysis=None,
            recommendation=None,
            remediation=None,
            analysis_context=None,
            verdict=None,
            groundedness=None,
            mcp_calls=[],
            report=None,
            error=None,
        )
        .with_entrypoint("select_mode")
        .with_tracker(tracker(project="omen"))
    )
    if hooks:
        builder = builder.with_hooks(*hooks)
    return builder.build()


if __name__ == "__main__":
    mount(build_application, name="omen", upstream=upstream()).run()
