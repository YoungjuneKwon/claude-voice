#!/usr/bin/env python3
"""claude-voice listener: a spoken reply in Telegram continues the session.

Long-polls Telegram for messages sent *to* the bot. A voice note is transcribed;
a typed message is used as-is. Either way the text is handed to the session the
reply belongs to via `claude -p --resume`, and that turn's own Stop hook sends
the answer back through the outbound pipeline.

The result is a closed loop that needs no laptop: listen on AirPods, reply by
voice, hear the answer.

Everything the outbound side already does is reused — this only adds the inbound
half, and deliberately owns no summarizing, synthesizing, or delivering of its own.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Long-poll window. Telegram holds the request open this long when idle, so the
# loop costs one HTTP request per interval rather than spinning.
POLL_SECONDS = 30

# A spoken instruction is transcribed by a machine and acted on with nobody
# watching. Read-only by default: a misheard word can waste a turn, but it
# cannot rewrite a file. Widen this in config once you trust the loop.
READONLY_TOOLS = "Edit Write Bash NotebookEdit"


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def load_config():
    path = Path(os.environ.get("CLAUDE_VOICE_CONFIG", ROOT / "config.env"))
    if not path.exists():
        sys.exit(f"missing config: {path}")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def cfg(key, default=None, required=False):
    value = os.environ.get(key) or default
    if required and not value:
        sys.exit(f"config {key} is not set")
    return value


def telegram_token():
    env_file = Path(cfg("CLAUDE_VOICE_TELEGRAM_ENV", required=True))
    for line in env_file.read_text().splitlines():
        key, _, value = line.strip().partition("=")
        if key == "token":
            return value
    sys.exit(f"no `token=` line in {env_file}")


TOKEN = None  # set in main once config is loaded


def api(method, **params):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    body = json.dumps(params).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=POLL_SECONDS + 30) as resp:
        payload = json.load(resp)
    if not payload.get("ok"):
        raise RuntimeError(f"telegram {method} failed: {payload}")
    return payload["result"]


def download(file_id):
    meta = api("getFile", file_id=file_id)
    url = f"https://api.telegram.org/file/bot{TOKEN}/{meta['file_path']}"
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


# --- state ------------------------------------------------------------------

def state_dir():
    path = Path(cfg("CLAUDE_VOICE_STATE", str(ROOT / "state")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_offset():
    f = state_dir() / "listener-offset"
    return int(f.read_text().strip()) if f.exists() else 0


def write_offset(value):
    (state_dir() / "listener-offset").write_text(str(value))


def resolve_session(message):
    """Find the session a reply belongs to.

    Replying to a specific message in Telegram is the natural way to say which
    conversation you mean, and it is the only thing that keeps two concurrent
    projects apart. With no reply target, the newest session is the best guess.
    """
    routes_file = state_dir() / "routes.json"
    if not routes_file.exists():
        return None
    try:
        routes = json.loads(routes_file.read_text())
    except json.JSONDecodeError:
        return None
    if not routes:
        return None

    target = (message.get("reply_to_message") or {}).get("message_id")
    if target is not None and str(target) in routes:
        return routes[str(target)]

    if target is not None:
        log(f"replied to message {target}, which is not in the route table")
    return max(routes.values(), key=lambda r: r["at"])


# --- inbound handling -------------------------------------------------------

def transcribe(audio):
    driver = ROOT / "stt" / f"{cfg('CLAUDE_VOICE_STT_DRIVER', 'gcp')}.py"
    if not driver.exists():
        log(f"no such STT driver: {driver}")
        return None
    proc = subprocess.run([str(driver)], input=audio, capture_output=True, timeout=120)
    if proc.returncode != 0 or not proc.stdout.strip():
        log(f"stt failed: {proc.stderr.decode()[:300]}")
        return None
    return proc.stdout.decode().strip()


def resume(route, text):
    """Continue the session in a detached process.

    Detached because a turn can run for minutes and the loop has to stay
    responsive. The resumed turn fires its own Stop hook, which is what sends
    the answer back — this function deliberately never talks to Telegram.
    """
    session_id = route.get("session_id")
    if not session_id:
        log("route has no session_id")
        return False

    cmd = ["claude", "-p", "--model", cfg("CLAUDE_VOICE_REPLY_MODEL", "sonnet")]
    denied = cfg("CLAUDE_VOICE_REPLY_DISALLOWED", READONLY_TOOLS)
    if denied:
        # Keep a flag after this one: --disallowedTools is variadic and would
        # otherwise swallow the prompt as another tool name.
        cmd += ["--disallowedTools", denied]
    cmd += ["--resume", session_id, text]

    cwd = route.get("cwd") or str(ROOT)
    if not Path(cwd).is_dir():
        log(f"recorded cwd {cwd} is gone; running from {ROOT}")
        cwd = str(ROOT)

    log(f"resuming {session_id[:8]} in {Path(cwd).name}: {text[:60]}")
    logfile = open(state_dir() / "resume.log", "a")
    subprocess.Popen(
        cmd, cwd=cwd, stdout=logfile, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )
    return True


def handle(message):
    chat_id = str(message.get("chat", {}).get("id"))
    if chat_id != str(cfg("CLAUDE_VOICE_TELEGRAM_CHAT_ID", required=True)):
        log(f"ignoring message from unknown chat {chat_id}")
        return

    if "voice" in message:
        audio = download(message["voice"]["file_id"])
        text = transcribe(audio)
        if not text:
            api("sendMessage", chat_id=chat_id, text="못 알아들었어요. 다시 말씀해 주세요.")
            return
        # Echo the transcript back. STT gets things wrong, and seeing what was
        # heard is the only way to know whether a odd answer came from a
        # misheard instruction.
        api("sendMessage", chat_id=chat_id, text=f"🎙 {text}")
    elif "text" in message:
        text = message["text"].strip()
        if text.startswith("/"):
            return  # bot commands are not conversation
    else:
        return

    route = resolve_session(message)
    if not route:
        api("sendMessage", chat_id=chat_id,
            text="이어갈 세션이 없어요. 먼저 클로드에서 작업을 한 번 돌려주세요.")
        return

    if not resume(route, text):
        api("sendMessage", chat_id=chat_id, text="세션을 이어받지 못했어요.")


# --- main -------------------------------------------------------------------

def main():
    global TOKEN
    load_config()
    TOKEN = telegram_token()

    me = api("getMe")
    log(f"listening as @{me['username']} (poll {POLL_SECONDS}s)")

    offset = read_offset()
    while True:
        try:
            updates = api("getUpdates", offset=offset, timeout=POLL_SECONDS,
                          allowed_updates=["message"])
        except (urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            log(f"poll failed, retrying in 5s: {exc}")
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            write_offset(offset)
            message = update.get("message")
            if not message:
                continue
            try:
                handle(message)
            except Exception as exc:  # one bad message must not kill the loop
                log(f"error handling update {update['update_id']}: {exc}")


if __name__ == "__main__":
    main()
