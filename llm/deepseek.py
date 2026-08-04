#!/usr/bin/env python3
"""Summarizer driver: DeepSeek chat completions.

Driver contract:
    stdin  = the full prompt
    stdout = the summary sentences, plain text

Unlike the claude driver this is a plain one-shot completion — there is no
agent loop to talk out of doing the task, so no tool-disabling or forced schema
is needed. It does need an API key.

The key can be given directly, or read from a file that already holds it so it
is not duplicated into this project's config. Both shapes are accepted:

    DEEPSEEK_API_KEY=sk-...
    DEEPSEEK_API_KEY=deepseek|deepseek-chat|sk-...

The second is the `provider|model|key` triple some deployments store, and the
model name in it is used unless overridden.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://api.deepseek.com/chat/completions"

# DeepSeek sheds load with 503 fairly readily, and a summary is worth a couple
# of seconds of patience before giving up on the turn.
RETRY_ON = {429, 500, 502, 503, 504}
ATTEMPTS = 3


def credentials():
    """Return (model, api_key), preferring an explicit key over the scraped one."""
    key = os.environ.get("CLAUDE_VOICE_DEEPSEEK_KEY")
    model = os.environ.get("CLAUDE_VOICE_DEEPSEEK_MODEL")
    if key:
        return model or "deepseek-chat", key

    source = os.environ.get("CLAUDE_VOICE_DEEPSEEK_ENV")
    if not source:
        sys.exit("deepseek: set CLAUDE_VOICE_DEEPSEEK_KEY or CLAUDE_VOICE_DEEPSEEK_ENV")
    var = os.environ.get("CLAUDE_VOICE_DEEPSEEK_VAR", "DEEPSEEK_API_KEY")

    path = Path(source)
    if not path.exists():
        sys.exit(f"deepseek: no such file: {path}")

    # Matches both `VAR=value` and a docker-compose `- VAR=value` list entry.
    match = re.search(rf"^\s*-?\s*{re.escape(var)}=(\S+)\s*$", path.read_text(), re.M)
    if not match:
        sys.exit(f"deepseek: no {var} in {path}")

    value = match.group(1)
    if "|" in value:
        parts = value.split("|")
        if len(parts) != 3:
            sys.exit(f"deepseek: expected provider|model|key in {var}, got {len(parts)} fields")
        _provider, found_model, found_key = parts
        return model or found_model, found_key
    return model or "deepseek-chat", value


def main():
    prompt = sys.stdin.read().strip()
    if not prompt:
        sys.exit("deepseek: empty prompt")

    model, api_key = credentials()

    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 512,
        "stream": False,
    }).encode()

    payload = None
    for attempt in range(1, ATTEMPTS + 1):
        req = urllib.request.Request(ENDPOINT, data=body, headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.load(resp)
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:200]
            if exc.code in RETRY_ON and attempt < ATTEMPTS:
                delay = 2 ** attempt
                print(f"HTTP {exc.code}, retrying in {delay}s", file=sys.stderr)
                time.sleep(delay)
                continue
            sys.exit(f"deepseek: HTTP {exc.code}: {detail}")

    usage = payload.get("usage", {})
    served = payload.get("model", model)
    message = payload["choices"][0]["message"]
    content = (message.get("content") or "").strip()

    if not content:
        # `deepseek-chat` is an alias that resolves to a non-reasoning config.
        # Naming a reasoning model directly turns thinking on, and the whole
        # max_tokens budget goes to reasoning_content with nothing left over.
        hint = " (spent the budget on reasoning_content — use the deepseek-chat alias)" \
            if message.get("reasoning_content") else ""
        sys.exit(f"deepseek: {served} returned no content{hint}")

    print(
        f"model={served} in={usage.get('prompt_tokens')} out={usage.get('completion_tokens')}",
        file=sys.stderr,
    )
    sys.stdout.write(content)


if __name__ == "__main__":
    main()
