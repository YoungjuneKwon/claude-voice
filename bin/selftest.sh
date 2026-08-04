#!/usr/bin/env bash
# End-to-end smoke test: run the worker against a fake Stop-hook payload.
# Use this before wiring the hook up, and whenever a stage changes.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TEXT="${1:-Stop 훅은 Claude Code가 한 턴의 응답을 마쳤을 때 실행됩니다. \
stdin으로 session_id, transcript_path, cwd, last_assistant_message 등이 담긴 JSON이 들어오고, \
last_assistant_message 필드에 이번 턴 응답 본문이 통째로 들어있습니다. \
훅이 즉시 종료하고 TTS 작업은 백그라운드로 던져야 매 턴이 멈추지 않습니다.}"

python3 -c '
import json, sys
print(json.dumps({
    "session_id": "selftest-0000-0000",
    "cwd": sys.argv[1],
    "hook_event_name": "Stop",
    "last_assistant_message": sys.argv[2],
    "stop_hook_active": False,
}))
' "$ROOT" "$TEXT" | "$ROOT/bin/voice-worker.py"
