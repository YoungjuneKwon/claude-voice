#!/usr/bin/env python3
"""claude-voice bridge: a Telegram topic that talks to opencode instead of Claude.

The listener resolves most topics to a Claude session and continues it with
`claude -p --resume`. A *bridge* topic is resolved here instead: it is bound to
one long-lived opencode session, and anything said in it is forwarded there and
answered in the same topic.

This is what lets the control room split the work — Claude turns the scenario
into code, opencode prepares the resources (review, images, TTS) — while the
person sees both halves in one Telegram group, one topic each.

Bindings live in `state/bridges.json`, keyed by Telegram thread id:

    {"6686": {"kind": "opencode", "session_id": "ses_…",
              "directory": "/contents/workspace/ai-trpg",
              "provider": "openai", "model": "gpt-6.1-sol", "agent": "build"}}

Run as a script, it reads one job on stdin and posts the answer back:

    echo '{"thread_id": 6686, "text": "…"}' | bin/opencode-bridge.py

The listener spawns it detached, for the same reason it detaches `claude -p`:
a turn can run for minutes and the poll loop has to stay responsive.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# opencode serves two APIs. The `/api/*` one is event-sourced and never runs the
# turn — it stops at `admitted`. This older path is synchronous: the answer is
# in the response body. Measured 2026-10-19; do not "modernize" it without
# re-testing the whole round trip.
OPENCODE_BASE = "http://127.0.0.1:4097"

# A resource job (an image set, a batch of lines) is not a chat reply.
ANSWER_TIMEOUT = 900

# Telegram rejects anything longer; split rather than lose the tail.
TELEGRAM_LIMIT = 3900


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] bridge: {msg}", flush=True)


def load_config():
    path = Path(os.environ.get("CLAUDE_VOICE_CONFIG", ROOT / "config.env"))
    if not path.exists():
        sys.exit(f"missing config: {path}")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def cfg(key, default=None, required=False):
    value = os.environ.get(key) or default
    if required and not value:
        sys.exit(f"config {key} is not set")
    return value


def state_dir():
    path = Path(cfg("CLAUDE_VOICE_STATE", str(ROOT / "state")))
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- bindings ---------------------------------------------------------------

def resolve(thread_id):
    """Return the bridge bound to this topic, or None if it is a Claude topic."""
    if thread_id is None:
        return None
    path = state_dir() / "bridges.json"
    if not path.exists():
        return None
    try:
        bridges = json.loads(path.read_text())
    except json.JSONDecodeError:
        log("bridges.json was unreadable; treating every topic as Claude's")
        return None
    entry = bridges.get(str(thread_id))
    if entry:
        return {**entry, "thread_id": thread_id}
    return None


# --- opencode ---------------------------------------------------------------

def ask(entry, text):
    """Send one message to the bound opencode session and return its answer."""
    url = (f"{OPENCODE_BASE}/session/{entry['session_id']}/message"
           f"?directory={urllib.parse.quote(entry['directory'])}")
    body = json.dumps({
        "model": {"providerID": entry.get("provider", "openai"),
                  "modelID": entry.get("model", "gpt-6.1-sol")},
        "agent": entry.get("agent", "build"),
        "parts": [{"type": "text", "text": text}],
    }).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=ANSWER_TIMEOUT) as resp:
        payload = json.load(resp)

    payload = payload.get("data", payload)
    parts = [p.get("text", "") for p in payload.get("parts", [])
             if p.get("type") == "text"]
    answer = "\n".join(p for p in parts if p.strip()).strip()
    return answer or None


# --- telegram ---------------------------------------------------------------

def telegram_token():
    env_file = Path(cfg("CLAUDE_VOICE_TELEGRAM_ENV", required=True))
    for line in env_file.read_text().splitlines():
        key, _, value = line.strip().partition("=")
        if key == "token":
            return value
    sys.exit(f"no `token=` line in {env_file}")


def say(thread_id, text):
    token = telegram_token()
    chat_id = cfg("CLAUDE_VOICE_TELEGRAM_CHAT_ID", required=True)
    for start in range(0, len(text), TELEGRAM_LIMIT):
        params = {"chat_id": chat_id, "text": text[start:start + TELEGRAM_LIMIT],
                  "message_thread_id": thread_id}
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.load(resp)
        if not result.get("ok"):
            log(f"telegram rejected sendMessage: {result}")
            return


# --- main -------------------------------------------------------------------

def main():
    load_config()
    job = json.load(sys.stdin)
    thread_id = job["thread_id"]
    entry = resolve(thread_id)
    if not entry:
        sys.exit(f"no bridge bound to topic {thread_id}")

    label = cfg("CLAUDE_VOICE_OPENCODE_LABEL", "[오픈코드]")
    log(f"-> {entry['session_id'][:12]}: {job['text'][:60]}")
    try:
        answer = ask(entry, job["text"])
    except Exception as exc:  # the person is waiting; tell them, don't vanish
        log(f"opencode call failed: {exc}")
        say(thread_id, f"{label} 응답을 받지 못했습니다 — {exc}")
        return

    if not answer:
        say(thread_id, f"{label} 빈 응답이 왔습니다.")
        return
    log(f"<- {len(answer)} chars")
    say(thread_id, f"{label} {answer}")


if __name__ == "__main__":
    main()
