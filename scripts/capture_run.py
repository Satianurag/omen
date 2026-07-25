"""Stream a real omen diff-mode run, phase by phase, for a terminal recording.

    uv run python scripts/capture_run.py

Drives a real petclinic diff-mode run (k6 MCP + live SigNoz + configured model),
printing each Major Arcana phase as the Burr executor walks it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import structlog
from burr.lifecycle import PostRunStepHook
from dotenv import load_dotenv
from theodosia import UpstreamManager, bind_upstream
from theodosia.upstream import reset_upstream

from omen import arcana
from omen.app import build_application
from omen.cli import (
    _BOLD,
    _CYAN,
    _DIM,
    _MAGENTA,
    _RESET,
    _color_diff,
    _outcome_color,
    _phase_detail,
)
from omen.upstream import k6_upstream_config, signoz_configured, signoz_upstream_config

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.ERROR))

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "examples" / "petclinic"
APP_URL = "http://127.0.0.1:8400"


def _wait_for_app(timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{APP_URL}/healthz", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _baseline_app() -> str:
    """Petclinic without the flawed POST /api/visits endpoint (diff baseline)."""
    full = (APP_DIR / "app.py").read_text()
    start = full.find('@app.post("/api/visits")')
    end = full.find("\ndef main()", start)
    if start < 0 or end < 0:
        raise RuntimeError("could not strip /api/visits from petclinic app.py for demo baseline")
    baseline = full[:start] + full[end:]
    return baseline.replace("    _init_db()\n", "")


def _build_diff_repo() -> Path:
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="omen-capture-"))
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.email", "omen@local"], cwd=tmp, check=True)
    subprocess.run(["git", "config", "user.name", "omen"], cwd=tmp, check=True)
    shutil.copy(APP_DIR / "openapi.json", tmp / "openapi.json")
    (tmp / "app.py").write_text(_baseline_app())
    subprocess.run(["git", "add", "."], cwd=tmp, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "baseline: owners and vets"], cwd=tmp, check=True)
    shutil.copy(APP_DIR / "app.py", tmp / "app.py")
    subprocess.run(["git", "add", "app.py"], cwd=tmp, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat: add visit recording endpoint"],
        cwd=tmp,
        check=True,
    )
    return tmp


def _status(action: str, st: dict) -> str:
    if st.get("error"):
        return "refused"
    if action == "screen":
        g = st.get("groundedness") or {}
        return "grounded" if g.get("grounded") else ("ungrounded" if g.get("available") else "screened")
    if action == "report":
        v = (st.get("verdict") or "").lower()
        if "regression" in v:
            return "regression"
        if "degradation" in v:
            return "degrading"
        return "failed" if v.startswith(("failed", "no run")) else "passed"
    return st.get("stage") or "ok"


class _Narrator(PostRunStepHook):
    def post_run_step(self, *, state, action, **_kw) -> None:
        name = action.name
        num, card, _ = arcana.ARCANA.get(name, ("", name, ""))
        st = dict(state.get_all()) if hasattr(state, "get_all") else dict(state)
        status = _status(name, st)
        col = _outcome_color(status)
        tools, facts = _phase_detail(name, st)
        if tools and facts:
            detail = f"{_CYAN}{tools}{_RESET}  {_DIM}·  {facts}{_RESET}"
        elif tools:
            detail = f"{_CYAN}{tools}{_RESET}"
        else:
            detail = f"{_DIM}{facts}{_RESET}"
        print(
            f"{_DIM}{arcana.SIGIL}{_RESET} {_DIM}{num:>4}{_RESET}  "
            f"{_BOLD}{_MAGENTA}{card:<19}{_RESET}{_DIM}{name:<18}{_RESET}"
            f"{_DIM}→{_RESET} {col}{status:<11}{_RESET} {detail}",
            flush=True,
        )


async def main() -> None:
    load_dotenv(ROOT / ".env")
    if not signoz_configured():
        print("SigNoz is not configured in .env (OMEN_SIGNOZ_API_KEY). Aborting.")
        return

    app_env = {
        **os.environ,
        "OTEL_SERVICE_NAME": "petclinic",
        "OTEL_EXPORTER_OTLP_ENDPOINT": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"),
        "OTEL_EXPORTER_OTLP_PROTOCOL": os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc"),
        "OTEL_TRACES_EXPORTER": "otlp",
    }
    app = subprocess.Popen(
        ["uv", "run", "opentelemetry-instrument", "python", str(APP_DIR / "app.py"), "serve"],
        cwd=str(ROOT),
        env=app_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if not _wait_for_app():
        app.terminate()
        print("petclinic app did not come up. Aborting.")
        return
    diff_repo = _build_diff_repo()
    print(
        f"\n{_BOLD}{_MAGENTA}{arcana.SIGIL}  omen is running.{_RESET} "
        f"{_CYAN}{arcana.TAGLINE}{_RESET}  {_DIM}(diff mode: POST /api/visits){_RESET}\n"
    )

    upstream = UpstreamManager({"k6": k6_upstream_config(), "signoz": signoz_upstream_config()})
    token = bind_upstream(upstream)
    try:
        application = build_application(hooks=[_Narrator()])
        _, _, state = await application.arun(
            halt_after=["report"],
            inputs={
                "repo_path": str(diff_repo),
                "ref": "HEAD~1",
                "target_base_url": APP_URL,
                "signoz_service": "petclinic",
            },
        )
    finally:
        await upstream.aclose()
        reset_upstream(token)
        app.terminate()
        shutil.rmtree(diff_repo, ignore_errors=True)

    report = state["report"]
    verdict = report.get("verdict")
    print(f"\n{arcana.SIGIL}  {_BOLD}verdict:{_RESET} {_outcome_color(verdict)}{verdict}{_RESET}")
    remediation = report.get("remediation")
    if remediation:
        print(
            f"\n{_BOLD}{arcana.SIGIL}  proposed fix{_RESET} "
            f"{_DIM}(validated diff: applies cleanly and still parses){_RESET}"
        )
        print(_color_diff(remediation))


if __name__ == "__main__":
    asyncio.run(main())
