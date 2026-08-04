# claude-voice

Hear what Claude Code just did, and answer it out loud — without opening the laptop.

Every time a Claude Code turn ends, its response is summarized into a few sentences written for the ear and pushed to Telegram. Reply there by voice and the session picks up where it left off.

```
Claude Code turn ends
   │
   ├─ Stop hook ─> policy ─> render ─> synthesize ─> deliver ─> your phone
   │               speak?    summary    audio        Telegram
   │
   └─ you reply by voice
          │
        listener ─> transcribe ─> claude -p --resume ─> (that turn's Stop hook)
```

The loop closes on itself: a resumed turn is a turn, so its answer comes back the same way.

## Why it is built this way

**Four stages, kept apart.** The reusable part is not text-to-speech — it is the layer that catches "a turn finished" and pushes it somewhere. Swapping the speech engine, the summarizer, or the sink should mean replacing one executable, not editing a pipeline. Each stage is a separate function, and two of them are separate processes with a one-line contract.

**The hook returns immediately.** Summarizing and synthesizing take around fifteen seconds. Doing that inside the hook would stall every turn by that much, so `hooks/stop.sh` reads the payload, hands it to a detached worker, and exits in about three milliseconds.

**A recursion guard is not optional.** The worker shells out to Claude to write the summary, and that child process inherits the same user-level hook configuration. Without the `CLAUDE_VOICE_ACTIVE` guard it summarizes its own summaries forever. (`claude --bare` skips hooks entirely but never reads subscription OAuth, so it is not usable when there is no API key.)

## Requirements

| | |
|---|---|
| OS | Linux or macOS. The hook, the worker and the listener are POSIX shell + Python 3.9+, no packages to install. |
| Claude Code | Installed and authenticated, with `claude` on `PATH`. |
| Binaries | `ffmpeg` (built with libopus), `curl`, `setsid`. Add `gcloud` for the bundled Google Cloud drivers. |
| Telegram | A bot from [@BotFather](https://t.me/BotFather). Optionally a forum supergroup — see below. |
| Speech | An account with whatever service the `tts/` and `stt/` drivers you pick need. |
| Network | Outbound HTTPS to `api.telegram.org` and your speech provider. No inbound ports, no public endpoint. |

The bundled drivers use Google Cloud Text-to-Speech and Speech-to-Text, which need a service-account key and both APIs enabled on the project:

```sh
gcloud services enable texttospeech.googleapis.com speech.googleapis.com --project <your-project>
```

Nothing about the design is Google-specific — see *Swapping drivers* below.

> **The summarization prompt is written in Korean**, and the voice and speech-recognition defaults are `ko-KR`. Change the language in `config.env` and the prompt in the `PROMPT` constant at the top of `bin/voice-worker.py`.

## Install

```sh
git clone https://github.com/YoungjuneKwon/claude-voice
cd claude-voice
cp config.example.env config.env
$EDITOR config.env

# Send your bot a message first — Telegram reveals a chat id only after that.
./bin/find-chat-id.sh          # paste the printed line into config.env

./bin/selftest.sh              # exercise the pipeline before wiring the hook up
./install.sh                   # register the Stop hook in ~/.claude/settings.json
```

`./install.sh --remove` undoes it. Your settings file is backed up to `.bak` before it is touched. The hook is picked up by sessions started after installation.

To take spoken replies as well:

```sh
./bin/listen.sh start          # also: stop, status, tail
```

## Configuration

`config.env` is gitignored and holds **paths to secrets, never secrets**. Every setting is documented inline in `config.example.env`.

## Hands-free listening

Telegram will not auto-play an incoming voice note, so listening without a tap goes through iOS instead:

**Settings → Notifications → Announce Notifications** → on → enable **Telegram**

With AirPods in and the phone locked, Siri reads new notifications aloud. It reads the notification *body*, so a voice note announces only as "voice message" — `CLAUDE_VOICE_DELIVER_MODE` must include `text` for this to work.

| Mode | Tap to hear | Voice | Round trip |
|---|---|---|---|
| `text` | no | Siri | ~13s |
| `voice` | yes | your TTS engine | ~22s |
| `both` | — | both | ~22s |

Settling on `text` drops the synthesis stage entirely, and with it the speech provider dependency.

## One topic per session

Several sessions sharing one chat read as a pile of unrelated messages. If the target chat is a forum supergroup, each session gets its own topic automatically.

1. Create a group, add the bot
2. Promote the bot to **administrator** with **Manage Topics**
3. Enable **Topics** in the group settings — this converts it to a supergroup and changes the chat id to a `-100…` form
4. Re-run `bin/find-chat-id.sh` and update `config.env`

> Administrator is not optional. A bot that is only a member sees just replies and mentions under privacy mode, which silently breaks inbound replies. Being an admin makes privacy mode moot.

Forum support is detected via `getChat`, so a plain chat keeps working unchanged. The session-to-topic map lives in `state/topics.json`.

## Replying by voice

Speak into the topic and that session continues. Inside a topic no reply target is needed, which matters because a dictated message cannot point at one. Falling back, replying to a specific message names its session, and failing both the newest session is used.

Transcripts are echoed back as `🎙 …` so a strange answer can be traced to a mishearing.

### Reply permissions

Replies are **read-only by default** (`CLAUDE_VOICE_REPLY_DISALLOWED="Edit Write Bash NotebookEdit"`).

A spoken instruction is transcribed by a machine and acted on with nobody watching; a confirmation prompt would simply hang. A misheard word can waste a turn, but it cannot rewrite a file. Widen it only where git can undo the damage.

### Known limitation

The listener does not check whether the target session is currently running before resuming it. With the laptop closed — the case this is for — that is fine. Reply while an interactive session is live and two processes write to the same conversation.

## Swapping drivers

Three stages are separate executables with one-line contracts:

| Directory | stdin | stdout |
|---|---|---|
| `llm/` | the prompt | summary text |
| `tts/` | text | MP3 bytes |
| `stt/` | OGG/Opus audio | transcript text |

Drop in an executable and point the matching `*_DRIVER` setting at its name. The prompt itself stays in the worker so backends stay comparable. Drivers read configuration from the environment, so to run one by hand:

```sh
set -a; . ./config.env; set +a
echo "hello" | ./tts/gcp.py > /tmp/check.mp3
```

Measured on identical input, the two bundled summarizers differ sharply:

| | Latency | Cost per turn |
|---|---|---|
| `claude` | 15.5s | ~$0.021 equivalent, drawn from the subscription |
| `deepseek` | 2.6s | ~$0.00005 |

DeepSeek is dramatically cheaper and faster, but it is a hosted API that sheds load with 503s. The driver retries three times with backoff; past that, `CLAUDE_VOICE_LLM_FALLBACK` hands the turn to another driver so someone else's capacity problem does not cost you a summary.

## Logs

```sh
tail -f state/worker.log      # outbound: one line per stage, with timing and cost
tail -f state/listener.log    # inbound: routing decisions
```

## Status

Early. It works end to end and is in daily use, but the policy stage is deliberately naive — a single length threshold — because which turns are worth hearing only becomes clear after living with it. Quiet hours, per-session mute, and filtering out bare acknowledgements belong there once there is evidence for their shape.

## License

MIT — see [LICENSE](LICENSE).
