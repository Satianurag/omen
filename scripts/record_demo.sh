#!/usr/bin/env bash
# Record a live omen diff-mode run for hackathon demo video.
#
#   ./scripts/record_demo.sh
#
# Produces:
#   docs/assets/omen-run.mp4   (full recording)
#   docs/assets/omen-run.gif   (README embed, ~15 fps)
#
# Requires: ffmpeg, X11 display (:1 or $DISPLAY), SigNoz + .env configured.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT_MP4="$ROOT/docs/assets/omen-run.mp4"
OUT_GIF="$ROOT/docs/assets/omen-run.gif"
DISPLAY="${DISPLAY:-:1}"
GEOM="${OMEN_RECORD_GEOM:-1280x800}"
POS="${OMEN_RECORD_POS:-80,60}"

if ! command -v ffmpeg >/dev/null; then
  echo "ffmpeg not found. Install ffmpeg and retry."
  exit 1
fi

mkdir -p "$(dirname "$OUT_MP4")"

echo "Recording ${GEOM} at +${POS} on ${DISPLAY} while capture_run executes..."
echo "Do not move the terminal window during the run."

ffmpeg -y -hide_banner -loglevel warning \
  -f x11grab -draw_mouse 0 -framerate 24 -video_size "$GEOM" -i "${DISPLAY}+${POS}" \
  -probesize 32M -analyzeduration 5M \
  -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p \
  "$OUT_MP4" &
FFPID=$!

cleanup() {
  kill "$FFPID" 2>/dev/null || true
  wait "$FFPID" 2>/dev/null || true
}
trap cleanup EXIT

sleep 2
uv run python scripts/capture_run.py
sleep 2
kill "$FFPID" 2>/dev/null || true
wait "$FFPID" 2>/dev/null || true
trap - EXIT

echo "Converting to GIF for README..."
ffmpeg -y -hide_banner -loglevel warning -i "$OUT_MP4" \
  -vf "fps=12,scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" \
  -loop 0 "$OUT_GIF"

echo "Done:"
echo "  $OUT_MP4"
echo "  $OUT_GIF"
