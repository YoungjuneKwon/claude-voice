#!/usr/bin/env python3
"""TTS driver: Google Cloud Text-to-Speech.

Driver contract (shared by everything under tts/):
    stdin  = UTF-8 text to speak
    stdout = MP3 bytes

Auth goes through the service-account key named in the config. The gcloud
credential cache is pinned to our own state dir so activating the service
account here never disturbs whatever account is active in the user's normal
gcloud config.
"""

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"


def need(key):
    value = os.environ.get(key)
    if not value:
        sys.exit(f"gcp tts: {key} is not set")
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
        sys.exit(f"gcp tts: could not get an access token: {got.stderr[:300]}")
    return got.stdout.strip()


def main():
    text = sys.stdin.read().strip()
    if not text:
        sys.exit("gcp tts: empty input")

    key_file = need("CLAUDE_VOICE_GCP_KEY")
    project = need("CLAUDE_VOICE_GCP_PROJECT")
    voice = os.environ.get("CLAUDE_VOICE_TTS_VOICE", "ko-KR-Chirp3-HD-Aoede")
    rate = float(os.environ.get("CLAUDE_VOICE_TTS_RATE", "1.0"))
    state = Path(os.environ.get("CLAUDE_VOICE_STATE", "state"))

    token = access_token(key_file, state / "gcloud")

    body = json.dumps({
        "input": {"text": text},
        # "ko-KR-Chirp3-HD-Aoede" -> language code "ko-KR"
        "voice": {"languageCode": "-".join(voice.split("-")[:2]), "name": voice},
        "audioConfig": {"audioEncoding": "MP3", "speakingRate": rate},
    }).encode()

    req = urllib.request.Request(ENDPOINT, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "x-goog-user-project": project,
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        sys.exit(f"gcp tts: HTTP {exc.code}: {exc.read().decode()[:400]}")

    sys.stdout.buffer.write(base64.b64decode(payload["audioContent"]))


if __name__ == "__main__":
    main()
