#!/usr/bin/env bash
# Stop hook entrypoint.
#
# This runs on every turn, so it must do as close to nothing as possible:
# hand the payload to a detached worker and return immediately. Summarizing and
# synthesizing take ~15s; doing that here would stall every turn by that much.
set -u

# Recursion guard. The worker shells out to `claude -p`, and that child process
# inherits this same user-level hook config — without the guard its Stop hook
# fires too and we summarize our own summaries, forever.
if [ -n "${CLAUDE_VOICE_ACTIVE:-}" ]; then
  exit 0
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Read the payload here rather than in the worker: stdin belongs to the hook,
# and it closes as soon as we exit.
payload="$(cat)"

state="$ROOT/state"
mkdir -p "$state"

printf '%s' "$payload" | setsid "$ROOT/bin/voice-worker.py" >>"$state/worker.log" 2>&1 &

exit 0
