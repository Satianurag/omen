"""Tests for k6 scaffold composition and options preservation."""

from __future__ import annotations

from omen.codegen.compose import compose
from omen.codegen.slots import DEFAULT_PLAN
from omen.parse import ensure_scaffold_options, finalize_script
from omen.state import Endpoint


def test_load_scaffold_uses_constant_arrival_rate():
    spec = {
        "paths": {
            "/api/visits": {
                "post": {
                    "requestBody": {
                        "content": {"application/json": {"schema": {"type": "object", "properties": {}}}}
                    }
                }
            }
        }
    }
    script = compose(
        plan=DEFAULT_PLAN,
        openapi_spec=spec,
        endpoints=[Endpoint(method="POST", path="/api/visits")],
        base_url="http://127.0.0.1:8400",
    )
    assert "constant-arrival-rate" in script
    assert "rate: 120" in script
    assert "sleep(0)" in script


def test_finalize_script_keeps_scaffold_load_profile():
    scaffold = (
        "import http from 'k6/http';\n"
        "export const options = {\n"
        "  scenarios: { stress: { executor: 'constant-arrival-rate', rate: 80, timeUnit: '1s', duration: '45s' } },\n"
        "};\n"
        "const BASE_URL = 'http://127.0.0.1:8400';\n"
        "export default function () { http.post(BASE_URL + '/api/visits'); sleep(0); }\n"
    )
    authored = (
        "import http from 'k6/http';\n"
        "export const options = { vus: 1, duration: '30s' };\n"
        "export default function () { http.post('http://example.test/x'); sleep(0.3); }\n"
    )
    merged = finalize_script(authored, scaffold)
    assert "rate: 80" in merged
    assert "duration: '45s'" in merged
    assert "vus: 1" not in merged
    assert "http://example.test/x" in merged


def test_ensure_scaffold_options_replaces_weak_author_options():
    scaffold = (
        "import http from 'k6/http';\n"
        "export const options = {\n"
        "  scenarios: { stress: { executor: 'constant-arrival-rate', rate: 80, timeUnit: '1s', duration: '45s' } },\n"
        "};\n"
        "export default function () { http.get('http://example.test'); }\n"
    )
    authored = (
        "import http from 'k6/http';\n"
        "export const options = { vus: 1, duration: '1s' };\n"
        "export default function () { http.get('http://example.test'); sleep(0.3); }\n"
    )
    merged = ensure_scaffold_options(authored, scaffold)
    assert "constant-arrival-rate" in merged
    assert "rate: 80" in merged
    assert "vus: 1" not in merged


def test_finalize_script_inserts_scaffold_head_when_author_omits_options():
    scaffold = (
        "import http from 'k6/http';\n"
        "export const options = {\n"
        "  scenarios: { stress: { executor: 'constant-arrival-rate', rate: 80 } },\n"
        "};\n"
        "const BASE_URL = 'http://example.test';\n"
        "export default function () {}\n"
    )
    authored = "import http from 'k6/http';\nexport default function () { http.get('http://example.test'); }\n"
    merged = finalize_script(authored, scaffold)
    assert "rate: 80" in merged
    assert "http.get('http://example.test')" in merged


def test_run_script_args_maps_load_taxonomy_to_vus():
    from omen.app import _run_script_args

    assert _run_script_args("script", {"test_taxonomy": "load"}) == {
        "script": "script",
        "vus": 50,
        "duration": "30s",
    }
    smoke = _run_script_args("script", {"test_taxonomy": "smoke"})
    assert smoke["vus"] == 1 and smoke["iterations"] == 5
