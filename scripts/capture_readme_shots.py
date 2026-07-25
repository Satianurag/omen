"""Capture omen CLI output and render README screenshot PNGs."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "docs" / "assets"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in ("Cascadia Mono", "Consolas", "Courier New"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def run_cmd(argv: list[str]) -> str:
    proc = subprocess.run(
        argv,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**dict(**__import__("os").environ), "PYTHONIOENCODING": "utf-8"},
    )
    return strip_ansi(proc.stdout + proc.stderr)


def filter_correlate(text: str) -> str:
    lines: list[str] = []
    for raw in strip_ansi(text).splitlines():
        line = raw.rstrip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if line.startswith('time="') or line.startswith("uv :") or line.startswith("At line:"):
            continue
        if line.strip().startswith("+ CategoryInfo") or line.strip().startswith("+ FullyQualifiedErrorId"):
            continue
        if line.strip().startswith("+") and "capture_run" in line:
            continue
        if "level=info msg=" in line:
            continue
        lines.append(line)
    return "\n".join(lines).strip("\n")


def _colorize_line(line: str) -> tuple[str, tuple[int, int, int]]:
    if line.startswith("[PASS]"):
        return line, (120, 220, 140)
    if line.startswith("[WARN]"):
        return line, (240, 200, 100)
    if line.startswith("[INFO]"):
        return line, (140, 180, 240)
    if "omen" in line.lower() and ("·" in line or ":" in line):
        return line, (220, 140, 220)
    if line.startswith("▶") or line.startswith("■") or "→" in line:
        return line, (220, 140, 220)
    if line.startswith("🂠") or "The " in line:
        return line, (235, 235, 235)
    if line.startswith("Doctor:") or line.startswith("verdict:"):
        return line, (235, 235, 235)
    if line.startswith("--- ") or line.startswith("+++ ") or line.startswith("@@") or line.startswith("+"):
        return line, (120, 220, 140)
    if line.startswith("═══") or line.startswith("the reading"):
        return line, (220, 140, 220)
    return line, (210, 210, 210)


def render_shot(text: str, out: Path, *, width: int = 1400, pad: int = 28, font_size: int = 18) -> None:
    font = _load_font(font_size)
    lines = text.splitlines() or [""]

    dummy = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(dummy)
    line_height = font_size + 6
    max_width = 0
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        max_width = max(max_width, bbox[2] - bbox[0])

    img_w = min(max(width, max_width + pad * 2), 2200)
    img_h = pad * 2 + line_height * len(lines) + 18
    img = Image.new("RGB", (img_w, img_h), (12, 12, 12))
    draw = ImageDraw.Draw(img)

    for i, color in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        draw.ellipse((pad + i * 22, pad - 8, pad + 12 + i * 22, pad + 4), fill=color)

    y = pad + 10
    for line in lines:
        colored, rgb = _colorize_line(line)
        draw.text((pad, y), colored, font=font, fill=rgb)
        y += line_height

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, format="PNG", optimize=True)


def main() -> int:
    shots = [
        ("shot-render.png", ["uv", "run", "omen", "render"], None, 1200),
        ("shot-arcana.png", ["uv", "run", "omen", "arcana"], None, 1500),
        ("shot-doctor.png", ["uv", "run", "omen", "doctor", "--runtime"], None, 1200),
        (
            "shot-correlate.png",
            ["uv", "run", "python", "scripts/capture_run.py"],
            filter_correlate,
            1500,
        ),
    ]

    for name, cmd, post, width in shots:
        print(f"Capturing {name}...", flush=True)
        text = run_cmd(cmd)
        if post:
            text = post(text)
        render_shot(text, ASSETS / name, width=width)
        if "omen" not in text.lower() and "kassi" in text.lower():
            print(f"ERROR: {name} still contains kassi branding", file=sys.stderr)
            return 1
        print(f"  wrote {ASSETS / name}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
