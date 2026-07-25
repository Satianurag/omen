#!/usr/bin/env bash
# Terminal-native demo recording (asciinema → agg GIF). Preferred for README embed.
#
#   ./scripts/record_demo_cast.sh
#
# Produces:
#   docs/assets/omen-run.cast
#   docs/assets/omen-run.gif   (animated, ~750KB)

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
CAST="$ROOT/docs/assets/omen-run.cast"
GIF="$ROOT/docs/assets/omen-run.gif"
AGG="${AGG:-/tmp/agg}"

mkdir -p "$(dirname "$CAST")"

if ! uv run python -c "import asciinema" 2>/dev/null; then
  uv pip install asciinema
fi

if ! command -v "$AGG" >/dev/null 2>&1; then
  echo "Downloading agg 1.9.0..."
  curl -fsSL -o "$AGG" "https://github.com/asciinema/agg/releases/download/v1.9.0/agg-x86_64-unknown-linux-gnu"
  chmod +x "$AGG"
fi

echo "Recording terminal cast (~90s live omen diff-mode run)..."
uv run asciinema rec -q -c "uv run python scripts/capture_run.py" "$CAST" --idle-time-limit 2

echo "Converting cast → GIF..."
"$AGG" "$CAST" "$GIF"

echo "Done:"
echo "  $CAST"
echo "  $GIF"
