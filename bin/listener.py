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
        # Values may be quoted so the file stays safe to `source` from shell.
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


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


def resolve_by_topic(message):
    """Resolve the session from the forum topic the message was posted in.

    This is the good path: a topic belongs to exactly one session, so anything
    said inside it is unambiguous — no reply-to needed, which matters because
    a dictated reply rarely targets a specific message.
    """
    thread_id = message.get("message_thread_id")
    if thread_id is None:
        return None
    topics_file = state_dir() / "topics.json"
    if not topics_file.exists():
        return None
    try:
        topics = json.loads(topics_file.read_text())
    except json.JSONDecodeError:
        return None
    for session_id, entry in topics.items():
        if entry.get("thread_id") == thread_id:
            return {**entry, "session_id": session_id}
    log(f"message in unknown topic {thread_id}")
    return None


def resolve_session(message):
    """Find the session a reply belongs to.

    Prefer the forum topic. Falling back: replying to a specific message names
    a session too, and failing both, the newest session is the best guess.
    """
    by_topic = resolve_by_topic(message)
    if by_topic:
        return by_topic

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


def resolve_project_dir(route):
    """Find the directory `claude --resume` must run from.

    Sessions are stored per project directory, and --resume only looks inside
    the one derived from the current cwd. The cwd recorded with the route is
    whatever directory the session was in when the turn ended, which drifts as
    work moves around — resume it from there and Claude reports "No conversation
    found" even though the session exists.

    The transcript's first entry carries the cwd the session was registered
    under, which is the one that actually resolves.
    """
    transcript = route.get("transcript_path")
    if transcript and Path(transcript).exists():
        try:
            with open(transcript) as handle:
                for line in handle:
                    entry = json.loads(line)
                    if entry.get("cwd"):
                        return entry["cwd"]
        except (json.JSONDecodeError, OSError) as exc:
            log(f"could not read cwd from transcript: {exc}")
    return route.get("cwd")


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

    cwd = resolve_project_dir(route) or str(ROOT)
    if not Path(cwd).is_dir():
        log(f"project dir {cwd} is gone; running from {ROOT}")
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

    # Answer inside the topic the question was asked in, or the thread falls
    # out of its session and lands in General.
    thread = message.get("message_thread_id")

    def say(text):
        params = {"chat_id": chat_id, "text": text}
        if thread is not None:
            params["message_thread_id"] = thread
        api("sendMessage", **params)

    if "voice" in message:
        audio = download(message["voice"]["file_id"])
        text = transcribe(audio)
        if not text:
            say("못 알아들었어요. 다시 말씀해 주세요.")
            return
        # Echo the transcript back. STT gets things wrong, and seeing what was
        # heard is the only way to know whether an odd answer came from a
        # misheard instruction.
        say(f"🎙 {text}")
    elif "text" in message:
        text = message["text"].strip()
        if text.startswith("/"):
            return  # bot commands are not conversation
    else:
        return

    route = resolve_session(message)
    if not route:
        say("이어갈 세션이 없어요. 먼저 클로드에서 작업을 한 번 돌려주세요.")
        return

    if not resume(route, text):
        say("세션을 이어받지 못했어요.")


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
