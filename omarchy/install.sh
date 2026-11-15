#!/usr/bin/env bash
# Links this folder into the Omarchy shell's plugins and puts the widget in the bar: in the stock
# Agents widget's place when that is in the bar, else at the default spot on the right.
# Safe to run again. Pass --remove to take the widget out and delete the link.
#   bash omarchy/install.sh
set -euo pipefail

id=tim.claude-usage
here=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
link="$HOME/.config/omarchy/plugins/$id"
config="$HOME/.config/omarchy/shell.json"

in_bar() {
  jq -e --arg id "$1" '[.bar.layout[][]? | .id] | index($id)' "$config" >/dev/null 2>&1
}

if [[ ${1:-} == --remove ]]; then
  omarchy plugin disable "$id" || true
  rm -f "$link"
  omarchy-shell shell rescanPlugins >/dev/null
  echo "Removed $link"
  exit 0
fi

for tool in python3 jq notify-send wl-copy hyprctl; do
  command -v "$tool" >/dev/null || echo "Warning: $tool is not installed (README, Omarchy)" >&2
done

mkdir -p "${link%/*}"
ln -sfn "$here" "$link"
omarchy-shell shell rescanPlugins >/dev/null
# The rescan runs in the shell and takes a moment; enabling before it lands fails as "not known".
for _ in $(seq 20); do
  omarchy plugin list --json 2>/dev/null | grep -q "\"$id\"" && break
  sleep 0.5
done
if ! in_bar "$id"; then
  if in_bar omarchy.agents; then
    omarchy plugin enable "$id" --section right --before omarchy.agents
  else
    omarchy plugin enable "$id" --section right
  fi
fi
echo "Linked $link -> $here"
if in_bar omarchy.agents; then
  echo "The stock Agents widget is still in the bar. To drop it: omarchy plugin disable omarchy.agents"
fi
