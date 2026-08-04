#!/usr/bin/env bash
# Register (or remove) the Stop hook in ~/.claude/settings.json.
#
#   ./install.sh            add the hook
#   ./install.sh --remove   take it back out
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="${CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
MODE="${1:-install}"

python3 - "$SETTINGS" "$ROOT/hooks/stop.sh" "$MODE" <<'PY'
import json, pathlib, sys

settings_path, hook_cmd, mode = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]

settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}

# Back up before touching a file we did not write.
if settings_path.exists():
    backup = settings_path.with_suffix(".json.bak")
    backup.write_text(settings_path.read_text())
    print(f"backed up existing settings to {backup}")

hooks = settings.setdefault("hooks", {})
# Stop does not support matchers — it always fires on every turn.
groups = [g for g in hooks.get("Stop", [])
          if not any(h.get("command") == hook_cmd for h in g.get("hooks", []))]

if mode == "--remove":
    if groups:
        hooks["Stop"] = groups
    else:
        hooks.pop("Stop", None)
    action = "removed"
else:
    groups.append({"hooks": [{"type": "command", "command": hook_cmd, "timeout": 30}]})
    hooks["Stop"] = groups
    action = "installed"

if not hooks:
    settings.pop("hooks", None)

settings_path.parent.mkdir(parents=True, exist_ok=True)
settings_path.write_text(json.dumps(settings, indent=2) + "\n")
print(f"{action}: Stop -> {hook_cmd}")
print(f"wrote {settings_path}")
PY

echo "Start a new Claude Code session to pick this up."
