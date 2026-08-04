#!/usr/bin/env python3
"""Speech-to-text driver: Google Cloud Speech-to-Text.

Driver contract (shared by everything under stt/):
    stdin  = OGG/Opus audio bytes (what Telegram hands out for a voice note)
    stdout = the transcript, plain text

Auth mirrors tts/gcp.py: the service-account key from config, with the gcloud
credential cache pinned to our own state dir so activating the account here
never disturbs the user's normal gcloud config. The auth block is duplicated
rather than shared because a driver is meant to be a self-contained executable
you can copy as a starting point for another provider.
"""

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://speech.googleapis.com/v1/speech:recognize"

# The synchronous endpoint tops out around a minute of audio. A spoken reply
# that long is a different feature; fail loudly rather than silently truncate.
MAX_BYTES = 8 * 1024 * 1024


def need(key):
    value = os.environ.get(key)
    if not value:
        sys.exit(f"gcp stt: {key} is not set")
    return value


def access_token(key_file, config_dir):
    env = dict(os.environ, CLOUDSDK_CONFIG=str(config_dir))
    config_dir.mkdir(parents=True, exist_ok=True)

    def token():
        return subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True, text=True, env=env,
        )

    got = token()
    if got.returncode != 0:
        subprocess.run(
            ["gcloud", "auth", "activate-service-account",
             "--key-file", key_file, "--quiet"],
            capture_output=True, env=env, check=True,
        )
        got = token()
    if got.returncode != 0:
        sys.exit(f"gcp stt: could not get an access token: {got.stderr[:300]}")
    return got.stdout.strip()


def main():
    audio = sys.stdin.buffer.read()
    if not audio:
        sys.exit("gcp stt: empty input")
    if len(audio) > MAX_BYTES:
        sys.exit(f"gcp stt: {len(audio)} bytes is too long for sync recognize")

    key_file = need("CLAUDE_VOICE_GCP_KEY")
    project = need("CLAUDE_VOICE_GCP_PROJECT")
    language = os.environ.get("CLAUDE_VOICE_STT_LANGUAGE", "ko-KR")
    state = Path(os.environ.get("CLAUDE_VOICE_STATE", "state"))

    token = access_token(key_file, state / "gcloud")

    body = json.dumps({
        "config": {
            "encoding": "OGG_OPUS",
            "sampleRateHertz": 48000,
            "languageCode": language,
            "enableAutomaticPunctuation": True,
        },
        "audio": {"content": base64.b64encode(audio).decode()},
    }).encode()

    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "x-goog-user-project": project,
    })
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        sys.exit(f"gcp stt: HTTP {exc.code}: {exc.read().decode()[:400]}")

    # No results means the audio carried no recognizable speech — a pocket
    # bump, or a language mismatch. Either way there is nothing to act on.
    transcript = " ".join(
        r["alternatives"][0]["transcript"].strip()
        for r in payload.get("results", [])
        if r.get("alternatives")
    ).strip()
    if not transcript:
        sys.exit("gcp stt: no speech recognized")

    sys.stdout.write(transcript)


if __name__ == "__main__":
    main()
