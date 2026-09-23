#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
for size in 56 80; do
  bundle="$ROOT/configs/airootfs/usr/share/omarchy-iso/install-snake.frames"
  (( size == 80 )) && bundle="$ROOT/configs/airootfs/usr/share/omarchy-iso/install-snake-large.frames"
  [[ $(wc -l <"$bundle") == 102 ]] || { echo 'Expected 0–100% plus completion frame' >&2; exit 1; }

for index in 1 20 49 83 101 102; do
  frame=$(sed -n "${index}p" "$bundle" | base64 -d | gzip -dc)
  [[ $(printf '%s\n' "$frame" | wc -l) == $((size / 2)) ]] || { echo "Bad frame height: $index" >&2; exit 1; }
  [[ $(printf '%s' "$frame" | grep -o '▀' | wc -l) == $((size * size / 2)) ]] || {
    echo "Bad frame width: $index" >&2; exit 1;
  }
done

start=$(sed -n '1p' "$bundle")
middle=$(sed -n '49p' "$bundle")
finish=$(sed -n '102p' "$bundle")
[[ $start != "$middle" && $middle != "$finish" ]] || {
  echo 'Frame progression is static' >&2; exit 1;
}
done
echo 'ok - source video frames cover live percentages and the completed logo'
