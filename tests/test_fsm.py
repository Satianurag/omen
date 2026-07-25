from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from burr.core import State
from fastmcp import Client
from theodosia import bind_upstream, mount
from theodosia.testing import FakeUpstream
from theodosia.upstream import reset_upstream

from omen import app as omen_app
from omen.app import build_application

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "petstore"

K6_RESPONSES = {
    "k6": {
        "validate_script": {"valid": True, "exit_code": 0, "stdout": "", "stderr": ""},
        "run_script": {
            "success": True,
            "exit_code": 0,
            "summary": "omen summary",
            "metrics": {
                "http_reqs": {"count": 120},
                "http_req_duration": {"p(95)": 14.2, "avg": 6.1},
                "http_req_failed": {"rate": 0.0},
                "checks": {"passes": 120, "fails": 0, "rate": 1.0},
            },
        },
        "list_sections": {
            "tree": [
                {"slug": "using-k6/http-requests", "title": "HTTP Requests", "child_count": 0},
                {"slug": "using-k6/thresholds", "title": "Thresholds", "child_count": 0},
                {"slug": "using-k6/checks", "title": "Checks", "child_count": 0},
                {"slug": "using-k6/scenarios", "title": "Scenarios", "child_count": 0},
            ]
        },
        "get_documentation": {
            "section": {"slug": "using-k6/thresholds", "title": "Thresholds"},
            "content": "---\ntitle: 'Thresholds'\n---\n\n# Thresholds\n\nThresholds are pass/fail criteria for the system under test.",
        },
    }
}

K6_AND_SIGNOZ_RESPONSES = {
    **K6_RESPONSES,
    "signoz": {
        "signoz_list_services": {
            "content": [
                {
                    "type": "text",
                    "text": '{"data":[{"serviceName":"petclinic","callCount":120}]}',
                }
            ]
        },
        "signoz_aggregate_traces": {
            "content": [
                {
                    "type": "text",
                    "text": '{"data":{"value":120}}',
                }
            ]
        },
        "signoz_search_traces": {
            "content": [
                {
                    "type": "text",
                    "text": '{"data":[{"statusMessage":"database is locked","http.route":"/api/visits"}]}',
                }
            ]
        },
    },
}


_K6_SCRIPT = (
    "import http from 'k6/http';\n"
    "export const options = { vus: 5, duration: '10s' };\n"
    "export default function () {\n"
    "  http.get('http://localhost:8000/api/pets');\n"
    "}\n"
)


class _FakeLLM:
    def generate(self, *, system: str, user: str, stop=None, format=None, documents=None) -> str:
        s = system.lower()
        if "narrat" in s or "tarot" in s:
            return "The Fool: the run begins.\nThe Tower: load applied.\nJudgement: passed."
        if "site-reliability" in s or "analysis" in s:
            return "Summary\nA load-only regression.\n\nEvidence\n- 5xx observed [SigNoz correlate]\n\nRecommendation\nAdd pooling."
        return _K6_SCRIPT


async def _no_guidance(description: str) -> None:
    return None


@pytest.fixture(autouse=True)
def _offline_llm(monkeypatch):
    monkeypatch.setattr(omen_app, "make_llm", lambda *a, **k: _FakeLLM())
    monkeypatch.setattr(omen_app, "fetch_k6_generation_guidance", _no_guidance)
    # Keep the Guardian screen hermetic; full-run tests exercise the phase with it disabled.
    monkeypatch.setenv("OMEN_GUARDIAN", "0")


async def test_full_run_intent_mode(monkeypatch):
    monkeypatch.setattr(omen_app, "signoz_configured", lambda: False)
    fake = FakeUpstream(K6_RESPONSES)
    token = bind_upstream(fake)
    try:
        application = build_application()
        _, _, state = await application.arun(
            halt_after=["report"],
            inputs={"repo_path": str(EXAMPLE), "intent": "load test listing the pets"},
        )
    finally:
        reset_upstream(token)

    report = state["report"]
    assert report["verdict"] == "passed"
    assert report["run_result"]["http_reqs"] == 120
    assert report["run_result"]["http_req_duration_p95_ms"] == 14.2

    # the analyze and screen phases ran: a cited analysis exists and the screen verdict is recorded
    # (unavailable here because Guardian is disabled for the test).
    assert isinstance(report["analysis"], str) and report["analysis"].strip()
    assert report["groundedness"] == {"available": False, "grounded": None}

    # the run is keyed by Burr's own app_id (not a omen-minted id), and the state-machine walk is
    # captured as an ordered step trace for the SigNoz dashboard.
    assert report["session"]["app_id"]
    steps = report["steps"]
    phases = [s["phase"] for s in steps]
    assert phases[0] == "select_mode" and phases[-1] == "report"
    assert phases[-2] == "analyze"  # screen is skipped here (Guardian disabled)
    assert [s["seq"] for s in steps] == list(range(len(steps)))
    run_test_step = next(s for s in steps if s["phase"] == "run_test")
    assert "k6.run_script=ok" in run_test_step["tools"]

    # scaffold built a deterministic load plan; the model authored the script on top of it.
    assert report["plan"]["test_taxonomy"] == "load"
    validated = fake.calls_to("k6", "validate_script")
    assert len(validated) == 1
    script = validated[0].args["script"]
    assert "import http from 'k6/http'" in script
    assert "import" in script and "openapi-to-k6" not in script
    assert fake.calls_to("k6", "run_script")

    # the report narrates the run (model in tests, omens otherwise).
    assert isinstance(report["narration"], str) and report["narration"].strip()

    # doc_lookup consulted the k6 MCP docs and recorded version-grounded citations.
    assert fake.calls_to("k6", "list_sections")
    provenance = report["mcp_provenance"]
    assert provenance["k6_doc_refs"], "expected k6 doc references"
    assert {r["slug"] for r in provenance["k6_doc_refs"]} & {"using-k6/thresholds", "using-k6/checks"}
    tools_called = {(c["server"], c["tool"]) for c in provenance["tool_calls"]}
    assert ("k6", "list_sections") in tools_called
    assert ("k6", "generate_script(prompt)") in tools_called
    assert ("k6", "validate_script") in tools_called
    assert ("k6", "run_script") in tools_called
    # SigNoz was not configured, so no preflight ran.
    assert provenance["signoz_preflight"] is None


async def test_signoz_correlation_when_configured(monkeypatch):
    monkeypatch.setenv("OMEN_SIGNOZ_API_KEY", "test-key")

    fake = FakeUpstream(K6_AND_SIGNOZ_RESPONSES)
    token = bind_upstream(fake)
    try:
        application = build_application()
        _, _, state = await application.arun(
            halt_after=["report"],
            inputs={"repo_path": str(EXAMPLE), "intent": "load test listing the pets", "signoz_service": "petclinic"},
        )
    finally:
        reset_upstream(token)

    report = state["report"]
    assert report["signoz_enabled"] is True
    correlation = report["correlation"]
    assert correlation["available"] is True

    assert fake.calls_to("signoz", "signoz_list_services")
    assert fake.calls_to("signoz", "signoz_aggregate_traces")
    assert fake.calls_to("signoz", "signoz_search_traces")

    preflight = report["mcp_provenance"]["signoz_preflight"]
    assert preflight["service"] == "petclinic"
    assert preflight["exists"] is True

    tools_called = {(c["server"], c["tool"]) for c in report["mcp_provenance"]["tool_calls"]}
    assert ("signoz", "signoz_list_services") in tools_called
    assert ("signoz", "signoz_aggregate_traces") in tools_called


async def test_enforcement_refuses_illegal_first_step():
    server = mount(build_application, name="omen", upstream=FakeUpstream(K6_RESPONSES))
    async with Client(server) as client:
        result = await client.call_tool("step", {"action": "run_test", "inputs": {}})
        payload = result.structured_content
    assert payload.get("error") == "invalid_transition"
    assert "select_mode" in payload.get("valid_next_actions", [])


async def test_run_test_timeout_falls_back_to_scaffold(monkeypatch):
    """A hung authored-script run must not block the pipeline: run_test times out and
    re-runs the deterministic scaffold once, recording timeout then ok."""

    async def fake_call_upstream(server, tool, args):
        if args["script"] == "AUTHORED":
            await asyncio.sleep(5)  # never returns within the timeout
        return {
            "success": True,
            "exit_code": 0,
            "metrics": {"http_reqs": {"count": 42}, "http_req_duration": {"p(95)": 10.0}},
        }

    monkeypatch.setattr(omen_app, "call_upstream", fake_call_upstream)
    monkeypatch.setattr(omen_app, "_run_timeout", lambda _d: 0.05)

    state = State(
        {
            "generated_script": "AUTHORED",
            "scaffold_script": "SCAFFOLD",
            "plan": {"test_taxonomy": "load"},
            "mcp_calls": [],
        }
    )
    result = await omen_app.run_test(state)
    assert result["stage"] == "ran"
    assert result["run_result"]["http_reqs"] == 42  # the scaffold run's payload
    statuses = [c["status"] for c in result["mcp_calls"] if c["tool"] == "run_script"]
    assert statuses == ["timeout", "ok"]


async def test_screen_records_guardian_groundedness(monkeypatch):
    """The screen phase passes the evidence as context and the analysis as the judged response to
    a separate Guardian model, and seals the grounded verdict to the report state."""
    monkeypatch.setenv("OMEN_GUARDIAN", "1")
    seen = {}

    class _FakeGuardian:
        def groundedness(self, *, context, response):
            seen["context"], seen["response"] = context, response
            return {"available": True, "grounded": True, "label": "No", "model": "granite3-guardian:8b"}

    monkeypatch.setattr(omen_app.guardian, "make_guardian", lambda: _FakeGuardian())
    state = State(
        {"analysis": "Summary\nA load-only regression.", "analysis_context": "[SigNoz correlate] 7 5xx"}
    )
    result = await omen_app.screen(state)
    assert result["stage"] == "screened"
    assert result["groundedness"]["grounded"] is True
    assert result["groundedness"]["available"] is True
    assert seen["context"] == "[SigNoz correlate] 7 5xx"
    assert seen["response"] == "Summary\nA load-only regression."


async def test_screen_skips_when_guardian_disabled(monkeypatch):
    monkeypatch.setenv("OMEN_GUARDIAN", "0")
    result = await omen_app.screen(State({"analysis": "x", "analysis_context": "y"}))
    assert result["stage"] == "screened"
    assert result["groundedness"] == {"available": False, "grounded": None}


def test_publish_builds_run_and_step_events_keyed_by_app_id():
    """The run event and the per-phase step events both carry Burr's app_id, so the dashboard
    correlates the verdict with the agent's state-machine walk."""
    from omen import publish

    report = {
        "session": {"app_id": "abc123", "partition_key": None, "sequence_id": 14},
        "verdict": "server-side regression: /api/visits ... cause: database is locked",
        "recommendation": "enable WAL",
        "groundedness": {"grounded": True},
        "steps": [
            {
                "seq": 0,
                "phase": "select_mode",
                "card": "The Fool",
                "card_num": "0",
                "status": "ok",
                "tool_calls": 0,
                "tools": [],
            },
            {
                "seq": 1,
                "phase": "run_test",
                "card": "The Tower",
                "card_num": "XVI",
                "status": "ok",
                "tool_calls": 1,
                "tools": ["k6.run_script=ok"],
            },
        ],
    }
    run_event = publish.build_event(report)
    assert run_event["app_id"] == "abc123"
    assert run_event["steps_total"] == 2

    steps = publish.build_step_events(report)
    assert [s["app_id"] for s in steps] == ["abc123", "abc123"]
    assert [s["phase"] for s in steps] == ["select_mode", "run_test"]
    assert steps[1]["tools"] == ["k6.run_script=ok"]


def test_remediate_applies_validates_and_diffs():
    """The model proposes SEARCH/REPLACE; omen applies it, validates the AST, and renders a
    real unified diff. A non-matching block or invalid result is rejected, never returned."""
    from omen import remediate

    source = "import time\n\n\ndef f():\n    x = 1\n    time.sleep(0.015)\n    return x\n"
    text = (
        "<<<<<<< SEARCH\n    time.sleep(0.015)\n=======\n"
        "    # removed sleep held inside the critical section\n>>>>>>> REPLACE"
    )
    blocks = remediate.parse_blocks(text)
    assert blocks == [("    time.sleep(0.015)", "    # removed sleep held inside the critical section")]

    patched = remediate.apply_blocks(source, blocks)
    assert patched is not None and "time.sleep" not in patched
    assert remediate.valid_python(patched)

    diff = remediate.unified(source, patched, "app.py")
    assert "--- a/app.py" in diff and "+++ b/app.py" in diff and "@@" in diff

    # a search block that does not match exactly is rejected (no partial/hallucinated edit)
    assert remediate.apply_blocks(source, [("does not exist", "x")]) is None
    # a syntactically invalid result is caught
    assert not remediate.valid_python("def (:\n")
    assert remediate.changed_file("+++ b/examples/petclinic/app.py") == "examples/petclinic/app.py"
