#!/usr/bin/env bash
# Convert the Playwright demo recording into docs/demo.gif (needs ffmpeg).
set -euo pipefail
cd "$(dirname "$0")/.."
video="$(find test-results/demo -name '*.webm' | head -1)"
if [ -z "$video" ]; then
  echo "no demo recording found; run: npm run demo" >&2
  exit 1
fi
mkdir -p ../docs
ffmpeg -y -loglevel error -i "$video" \
  -vf "fps=8,scale=1100:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse" \
  ../docs/demo.gif
echo "wrote docs/demo.gif from $video"
