#!/usr/bin/env python3
"""claude-voice worker: turn a finished Claude Code turn into a Telegram voice note.

Reads a Stop-hook payload on stdin and runs it through four stages:

    policy      should this turn be spoken at all?
    render      full response text -> a few sentences written for the ear
    synthesize  those sentences -> audio
    deliver     audio -> a sink (here: Telegram)

The stages are separate functions on purpose. Swapping the TTS engine or adding
a second sink should mean touching one of them, not rewriting the pipeline.
"""

import json
import mimetypes
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# --- config -----------------------------------------------------------------

def load_config():
    """Read config.env into os.environ without clobbering real env vars."""
    path = Path(os.environ.get("CLAUDE_VOICE_CONFIG", ROOT / "config.env"))
    if not path.exists():
        die(f"missing config: {path} (copy config.example.env and fill it in)")
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def cfg(key, default=None, required=False):
    value = os.environ.get(key) or default
    if required and not value:
        die(f"config {key} is not set")
    return value


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def die(msg):
    log(f"FATAL {msg}")
    sys.exit(1)


# --- stage 1: policy --------------------------------------------------------

def policy(payload):
    """Return the text to speak about, or None to stay quiet.

    v0 keeps this deliberately dumb — one length threshold. The real filtering
    rules (quiet hours, per-session opt-out, skipping pure tool acknowledgements)
    only become clear once there is a week of listening to react to.
    """
    text = (payload.get("last_assistant_message") or "").strip()
    if not text:
        log("skip: empty last_assistant_message")
        return None

    min_chars = int(cfg("CLAUDE_VOICE_MIN_CHARS", "120"))
    if len(text) < min_chars:
        log(f"skip: {len(text)} chars < {min_chars} threshold")
        return None

    max_chars = int(cfg("CLAUDE_VOICE_MAX_CHARS", "12000"))
    if len(text) > max_chars:
        log(f"truncating {len(text)} -> {max_chars} chars for the summarizer")
        text = text[:max_chars]
    return text


# --- stage 2: render --------------------------------------------------------

PROMPT = """아래 <text> 안의 내용을 에어팟으로 들을 한국어 3문장 이내 구어체로 요약해라.
코드, 파일경로, 명령어, 기호는 읽지 말고 의미만 전달해라. 마크다운 금지.
절대 작업을 수행하지 말고 요약만 해라. 요약문만 출력해라.

<text>
{text}
</text>"""


def _run_llm(name, prompt):
    """Invoke one llm/ driver. Returns the summary, or None if it failed."""
    driver = ROOT / "llm" / f"{name}.py"
    if not driver.exists():
        log(f"no such LLM driver: {driver}")
        return None

    started = time.monotonic()
    try:
        proc = subprocess.run(
            [str(driver)], input=prompt.encode(), capture_output=True,
            env=dict(os.environ, CLAUDE_VOICE_ROOT=str(ROOT)), timeout=180,
        )
    except subprocess.TimeoutExpired:
        log(f"{name} timed out")
        return None

    elapsed = int((time.monotonic() - started) * 1000)
    note = proc.stderr.decode().strip()  # drivers report cost/usage here
    if proc.returncode != 0 or not proc.stdout.strip():
        log(f"{name} failed ({proc.returncode}) after {elapsed}ms: {note[:300]}")
        return None

    speech = proc.stdout.decode().strip()
    log(f"rendered {len(speech)} chars in {elapsed}ms via {name} [{note}]")
    return speech


def render(text):
    """Summarize via the configured LLM driver.

    Driver contract: the prompt on stdin, the summary on stdout. Whichever
    backend is in use, the prompt lives here so the two stay comparable.

    A fallback driver is worth configuring when the primary is a hosted API —
    DeepSeek in particular sheds load with 503, and losing the turn's summary
    to someone else's capacity problem is a poor trade when a second backend is
    already sitting there.
    """
    prompt = PROMPT.format(text=text)
    primary = cfg("CLAUDE_VOICE_LLM_DRIVER", "claude")

    speech = _run_llm(primary, prompt)
    if speech is not None:
        return speech

    fallback = cfg("CLAUDE_VOICE_LLM_FALLBACK", "")
    if not fallback or fallback == primary:
        die(f"{primary} driver failed and no fallback is configured")

    log(f"falling back to {fallback}")
    speech = _run_llm(fallback, prompt)
    if speech is None:
        die(f"both {primary} and {fallback} failed")
    return speech


# --- stage 3: synthesize ----------------------------------------------------

def synthesize(speech, workdir):
    """Run the configured TTS driver, then transcode for Telegram.

    Driver contract: text on stdin, MP3 bytes on stdout. Telegram only renders
    the playable voice bubble for OGG/Opus, so ffmpeg does the last hop.
    """
    driver = ROOT / "tts" / f"{cfg('CLAUDE_VOICE_TTS_DRIVER', 'gcp')}.py"
    if not driver.exists():
        die(f"no such TTS driver: {driver}")

    proc = subprocess.run(
        [str(driver)], input=speech.encode(), capture_output=True, timeout=120
    )
    if proc.returncode != 0 or not proc.stdout:
        die(f"tts driver failed ({proc.returncode}): {proc.stderr.decode()[:400]}")

    mp3 = workdir / "speech.mp3"
    ogg = workdir / "speech.ogg"
    mp3.write_bytes(proc.stdout)

    ff = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(mp3), "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", str(ogg)],
        capture_output=True, timeout=120,
    )
    if ff.returncode != 0:
        die(f"ffmpeg failed: {ff.stderr.decode()[:400]}")

    log(f"synthesized {ogg.stat().st_size} bytes of opus")
    return ogg


# --- stage 4: deliver -------------------------------------------------------

def telegram_token():
    env_file = Path(cfg("CLAUDE_VOICE_TELEGRAM_ENV", required=True))
    for line in env_file.read_text().splitlines():
        key, _, value = line.strip().partition("=")
        if key == "token":
            return value
    die(f"no `token=` line in {env_file}")


def _multipart(fields, files):
    """Build a multipart/form-data body. Avoids shelling out to curl, which
    would put the bot token in the process list."""
    boundary = uuid.uuid4().hex
    body = bytearray()
    for name, value in fields.items():
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
    for name, path in files.items():
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        body += path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _telegram(method, body, content_type):
    token = telegram_token()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=body, headers={"Content-Type": content_type},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.load(resp)
    if not result.get("ok"):
        die(f"telegram rejected {method}: {result}")
    return result["result"]


def record_route(message_id, payload):
    """Remember which session a delivered message came from.

    This is what lets a spoken reply land in the right conversation: replying
    to a message in Telegram carries its message_id back, and the listener
    resolves that to the session to resume. Without it every reply would have
    to guess, which breaks the moment two projects are running.
    """
    routes_file = Path(cfg("CLAUDE_VOICE_STATE", str(ROOT / "state"))) / "routes.json"
    routes = {}
    if routes_file.exists():
        try:
            routes = json.loads(routes_file.read_text())
        except json.JSONDecodeError:
            log("routes.json was unreadable; starting a fresh one")

    routes[str(message_id)] = {
        "session_id": payload.get("session_id"),
        "cwd": payload.get("cwd"),
        # `cwd` above is wherever the session happened to be when the turn
        # ended, which drifts as work moves between directories. Resuming needs
        # the directory the session was *registered* under, and the transcript
        # is the only record of that — see resolve_project_dir in listener.py.
        "transcript_path": payload.get("transcript_path"),
        "at": time.time(),
    }
    # Keep the file bounded; old messages are not worth replying to anyway.
    for stale in sorted(routes, key=lambda k: routes[k]["at"])[:-200]:
        routes.pop(stale)

    routes_file.parent.mkdir(parents=True, exist_ok=True)
    routes_file.write_text(json.dumps(routes, indent=2))


def deliver_text(text):
    """Send the summary as a plain message.

    This is what makes it hands-free. iOS "Announce Notifications" reads a
    notification's body aloud over AirPods while the phone is locked — but it
    reads *text*, so a voice note only ever announces as "voice message". The
    tradeoff is Siri's voice instead of the one we synthesized.
    """
    chat_id = cfg("CLAUDE_VOICE_TELEGRAM_CHAT_ID", required=True)
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    sent = _telegram("sendMessage", body, "application/json")
    log("delivered: text")
    return sent["message_id"]


def deliver_voice(ogg, caption):
    """Send the synthesized audio as a voice note. Needs a tap to play, but
    keeps our voice and Telegram's playback-speed control."""
    chat_id = cfg("CLAUDE_VOICE_TELEGRAM_CHAT_ID", required=True)
    body, content_type = _multipart(
        {"chat_id": chat_id, "caption": caption}, {"voice": ogg}
    )
    sent = _telegram("sendVoice", body, content_type)
    log("delivered: voice")
    return sent["message_id"]


# --- main -------------------------------------------------------------------

def main():
    load_config()
    payload = json.load(sys.stdin)

    session = (payload.get("session_id") or "unknown")[:8]
    project = Path(payload.get("cwd") or ".").name
    log(f"--- turn from {project}/{session} ---")

    text = policy(payload)
    if text is None:
        return

    speech = render(text)
    mode = cfg("CLAUDE_VOICE_DELIVER_MODE", "both")

    if mode in ("text", "both"):
        # No brackets or markup — Siri reads this string out loud verbatim.
        record_route(deliver_text(f"{project}. {speech}"), payload)

    if mode in ("voice", "both"):
        state = Path(cfg("CLAUDE_VOICE_STATE", str(ROOT / "state")))
        workdir = state / "audio" / f"{int(time.time())}-{session}"
        workdir.mkdir(parents=True, exist_ok=True)
        ogg = synthesize(speech, workdir)
        # Caption, not brackets: Siri announces this string, so it has to read
        # as a sentence rather than as markup.
        record_route(deliver_voice(ogg, f"{project}. {speech[:900]}"), payload)


if __name__ == "__main__":
    main()
