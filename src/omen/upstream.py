"""Upstream MCP servers wired into Theodosia.

omen orchestrates two MCP servers from one agent-driven state machine:

* **k6**: the official Grafana k6 MCP server. Validates and runs the generated
  load test. Always configured.
* **signoz**: the official SigNoz MCP server (``signoz/signoz-mcp-server``). After a
  run, the FSM queries SigNoz for the target service's server-side traces over the
  test window and correlates them with the client-side k6 metrics. Configured only when
  ``OMEN_SIGNOZ_API_KEY`` is set; absent that, omen degrades gracefully to k6-only.

Install / configure (see ``docs/SIGNOZ_SETUP.md``):

    foundryctl cast -f casting.yaml   # SigNoz + MCP on :8080 / :4317 / :8000

    export OMEN_SIGNOZ_URL=http://localhost:8080
    export OMEN_SIGNOZ_API_KEY=<service-account-key>

Env overrides:
    OMEN_K6_CMD / OMEN_K6_DOCKER / OMEN_K6_IMAGE   k6 MCP server
    OMEN_SIGNOZ_URL                                   SigNoz API base (default http://localhost:8080)
    OMEN_SIGNOZ_API_KEY                               SigNoz service-account API key
    OMEN_SIGNOZ_MCP_IMAGE                             MCP server image (default signoz/signoz-mcp-server:latest)
    OMEN_SIGNOZ_MCP_ENDPOINT                          HTTP MCP URL (default http://localhost:8000/mcp); used with mcp-remote
    OMEN_SIGNOZ_MCP_CMD                               stdio bridge command (default npx)
"""

from __future__ import annotations

import os
import shlex
from typing import Any

K6_SERVER = "k6"
SIGNOZ_SERVER = "signoz"

DEFAULT_K6_CMD = "k6 x mcp"
DEFAULT_SIGNOZ_MCP_IMAGE = "signoz/signoz-mcp-server:latest"


def k6_upstream_config() -> dict[str, Any]:
    if os.environ.get("OMEN_K6_DOCKER"):
        image = os.environ.get("OMEN_K6_IMAGE", "grafana/mcp-k6:latest")
        return {
            "command": "docker",
            "args": [
                "run",
                "--rm",
                "-i",
                "--network",
                "host",
                "-e",
                "K6_NO_THRESHOLDS=true",
                image,
            ],
        }
    cmd = shlex.split(os.environ.get("OMEN_K6_CMD", DEFAULT_K6_CMD))
    return {"command": cmd[0], "args": cmd[1:]}


def k6_warm_command() -> list[str]:
    cfg = k6_upstream_config()
    return [cfg["command"], *cfg["args"], "--help"]


def signoz_configured() -> bool:
    return bool(os.environ.get("OMEN_SIGNOZ_API_KEY"))


def signoz_upstream_config() -> dict[str, Any] | None:
    """Official SigNoz MCP server over stdio (Docker) or HTTP via ``mcp-remote``.

    Stdio (default): runs ``signoz/signoz-mcp-server`` with ``TRANSPORT_MODE=stdio``,
    as documented at https://signoz.io/docs/ai/signoz-mcp-server

    HTTP: set ``OMEN_SIGNOZ_MCP_TRANSPORT=http`` to bridge with ``mcp-remote``.
    """
    api_key = os.environ.get("OMEN_SIGNOZ_API_KEY")
    if not api_key:
        return None

    signoz_url = os.environ.get("OMEN_SIGNOZ_URL", "http://localhost:8080")
    transport = os.environ.get("OMEN_SIGNOZ_MCP_TRANSPORT", "stdio").strip().lower()

    if transport == "http":
        endpoint = os.environ.get("OMEN_SIGNOZ_MCP_ENDPOINT", "http://localhost:8000/mcp")
        command = os.environ.get("OMEN_SIGNOZ_MCP_CMD", "npx")
        return {
            "command": command,
            "args": [
                "-y",
                "mcp-remote@0.1.38",
                endpoint,
                "--header",
                f"SIGNOZ-API-KEY: {api_key}",
            ],
        }

    image = os.environ.get("OMEN_SIGNOZ_MCP_IMAGE", DEFAULT_SIGNOZ_MCP_IMAGE)
    return {
        "command": "docker",
        "args": [
            "run",
            "-i",
            "--rm",
            "--network",
            "host",
            "-e",
            f"SIGNOZ_URL={signoz_url}",
            "-e",
            f"SIGNOZ_API_KEY={api_key}",
            "-e",
            "TRANSPORT_MODE=stdio",
            "-e",
            "LOG_LEVEL=error",
            image,
        ],
    }


def upstream() -> dict[str, Any]:
    """The ``upstream=`` dict passed to ``mount`` / ``build_cli``."""
    servers: dict[str, Any] = {K6_SERVER: k6_upstream_config()}
    signoz = signoz_upstream_config()
    if signoz is not None:
        servers[SIGNOZ_SERVER] = signoz
    return servers
