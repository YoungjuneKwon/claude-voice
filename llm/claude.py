#!/usr/bin/env python3
"""Summarizer driver: Claude Code CLI.

Driver contract (shared by everything under llm/):
    stdin  = the full prompt
    stdout = the summary sentences, plain text

Rides the machine's existing Claude subscription, so it needs no API key of its
own. The catch is that `claude -p` is an agent loop, not a one-shot completion:
left alone it reads the prompt as a task and starts working. Disabling every
tool and forcing a JSON schema is what pins it to summarizing.
"""

import json
import os
import subprocess
import sys

DISALLOWED_TOOLS = (
    "Bash Read Write Edit Glob Grep WebFetch WebSearch Task TodoWrite NotebookEdit Skill"
)
SCHEMA = json.dumps({
    "type": "object",
    "properties": {"speech": {"type": "string"}},
    "required": ["speech"],
})


def main():
    prompt = sys.stdin.read()

    env = dict(os.environ)
    env["CLAUDE_VOICE_ACTIVE"] = "1"  # stops this child's own Stop hook

    proc = subprocess.run(
        [
            "claude", "-p",
            "--model", os.environ.get("CLAUDE_VOICE_MODEL", "haiku"),
            "--output-format", "json",
            "--disable-slash-commands",
            "--disallowedTools", DISALLOWED_TOOLS,
            "--json-schema", SCHEMA,
            prompt,
        ],
        capture_output=True, text=True, env=env, timeout=180,
        # Pin the cwd. The hook fires from whatever project Claude was working
        # in, and `claude -p` would otherwise load that project's CLAUDE.md —
        # context that cannot improve a summary, and that lets one project's
        # instructions colour another project's readout.
        cwd=os.environ.get("CLAUDE_VOICE_ROOT", "."),
    )
    if proc.returncode != 0:
        sys.exit(f"claude -p exited {proc.returncode}: {proc.stderr[:400]}")

    outer = json.loads(proc.stdout)
    usd = outer.get("total_cost_usd") or 0.0
    print(f"cost=${usd:.4f} duration={outer.get('duration_ms')}ms", file=sys.stderr)
    sys.stdout.write(json.loads(outer["result"])["speech"].strip())


if __name__ == "__main__":
    main()
