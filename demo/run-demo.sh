#!/usr/bin/env bash
# Paced live demo for screen recording. Every command is real.
#
#   ./demo/run-demo.sh          press Enter to advance (default)
#   ./demo/run-demo.sh --auto   advance on a timer, hands-free
#
# Read the matching scene from demo/narration.txt while you record.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STAGE="${AGIT_SCREENCAST_DIR:-$HOME/agit-screencast}"
WORK="$STAGE/agit-demo"
MODE="pause"
[[ "${1:-}" == "--auto" ]] && MODE="auto"

export PYTHONPATH="$REPO"
export AGIT_FORCE_COLOR=1
AGIT="python3 -m agit"

dim=$'\033[2m'; off=$'\033[0m'; bold=$'\033[1m'
saffron=$'\033[38;5;208m'; green=$'\033[38;5;42m'

wait_for_cue() {
  if [[ "$MODE" == "auto" ]]; then
    sleep "${1:-4}"
  else
    printf '\n%s' "${dim}— Enter —${off}"
    read -r _ || true
    printf '\033[1A\033[2K'
  fi
}

scene() {           # scene <number> <title>
  clear
  printf '%s%sagit%s %s%s%s\n' "$bold" "$saffron" "$off" "$dim" "$2" "$off"
  printf '%s%s%s\n\n' "$dim" "scene $1 of 9 · see demo/narration.txt" "$off"
}

type_out() {        # type_out <command>
  printf '%s~/agit-demo%s %s$%s ' "$green" "$off" "$saffron" "$off"
  local i
  for ((i = 0; i < ${#1}; i++)); do
    printf '%s' "${1:$i:1}"
    sleep 0.018
  done
  printf '\n'
}

step() {            # step <command> [cwd]
  local cmd="$1" cwd="${2:-$WORK}"
  type_out "$cmd"
  wait_for_cue 1
  ( cd "$cwd" && eval "${cmd/#agit/$AGIT}" ) || true
  wait_for_cue 8
}

# ---------------------------------------------------------------- setup
rm -rf "$STAGE"
mkdir -p "$STAGE"
clear
printf '\n  %sready.%s workspace: %s\n' "$bold" "$off" "$STAGE"
printf '  start your screen recording now, then press Enter.\n'
wait_for_cue 3

scene 1 "a real bug, fixed by an agent — offline, no API key"
step "agit demo agit-demo --force" "$STAGE"

scene 2 "the run, step by step, with the reason for each step"
step "agit log"

scene 3 "which lines did the agent write?"
step "agit blame csvkit.py -L 1,22"

scene 4 "and why this exact line?"
step "agit why csvkit.py 24"

scene 5 "rewind to step 4 and try a different approach"
step "agit fork 4 \"use the stdlib csv module instead\" --provider script --script $REPO/agit/demo/script-stdlib.json --yes --verify 'python3 -m unittest discover -q -p \"test_*.py\" .'"

scene 6 "two verified solutions to the same task"
step "agit runs"

scene 7 "re-drive the whole run with no network calls"
step "agit replay \$(cd '$WORK' && $AGIT runs | awk '/demo/{print \$1; exit}')"

scene 8 "it really is git"
step "git -C .agit/ledger log --graph --all --oneline | head -24"

scene 9 "the dashboard: every run in one static HTML file"
step "agit dashboard -o dashboard.html"
( cd "$WORK" && open "dashboard.html" 2>/dev/null || true )

printf '\n  %sdone.%s click the blame tab, then click line 24 on camera.\n' "$bold" "$off"
printf '  %sworkspace kept at %s%s\n' "$dim" "$WORK" "$off"
