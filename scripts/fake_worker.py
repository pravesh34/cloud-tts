#!/usr/bin/env python3
"""Local fake worker — exercises the whole pipeline without a GPU.

It speaks the same protocol as worker/worker.py but synthesizes audio with the
macOS `say` command. Perfect for testing the gateway, queue, auth, and PWA on
this laptop before touching Colab.

    GATEWAY_URL=http://127.0.0.1:8000 WORKER_TOKEN=dev-worker python3 scripts/fake_worker.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
CATALOG = json.loads((ROOT / "voices.json").read_text(encoding="utf-8"))
VOICES = {v["id"]: v for v in CATALOG["voices"]}

GATEWAY = (os.environ.get("GATEWAY_URL") or "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("WORKER_TOKEN", "dev-worker")
WORKER_ID = "fake-" + uuid.uuid4().hex[:6]
HEARTBEAT_INTERVAL = 5
POLL_WAIT = 10

SAY_VOICES = {"male": "Alex", "female": "Samantha"}
state = {"worker_state": "idle", "model_state": "ready", "engine": "chatterbox"}
stop = threading.Event()
session = requests.Session()
session.headers.update({"Authorization": f"Bearer {TOKEN}"})


def log(*args) -> None:
    print(time.strftime("[%H:%M:%S]"), *args, flush=True)


def synth_with_say(text: str, voice: dict, speed: float, fmt: str) -> tuple[bytes, str]:
    speaker = SAY_VOICES.get(voice.get("gender", "female"), "Samantha")
    rate = max(90, min(320, int(180 * speed)))
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / "speech.aiff"
        wav = Path(tmp) / "speech.wav"
        subprocess.run(["say", "-v", speaker, "-r", str(rate), "-o", str(aiff), text], check=True)
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@24000", str(aiff), str(wav)],
            check=True,
        )
        if fmt == "mp3" and shutil.which("ffmpeg"):
            mp3 = Path(tmp) / "speech.mp3"
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
                 "-codec:a", "libmp3lame", "-b:a", "64k", str(mp3)],
                check=True,
            )
            return mp3.read_bytes(), "audio/mpeg"
        return wav.read_bytes(), "audio/wav"


def heartbeat_loop() -> None:
    while not stop.wait(HEARTBEAT_INTERVAL):
        try:
            session.post(f"{GATEWAY}/internal/worker/heartbeat", json={"worker_id": WORKER_ID, **state}, timeout=10)
        except Exception as exc:
            log("heartbeat failed:", exc)


def handle_job(job: dict) -> None:
    voice = VOICES.get(job.get("voice") or "", CATALOG["voices"][0])
    log(f"job {job['id'][:8]}: {voice['id']} ({len(job['text'])} chars)")
    state["worker_state"] = "busy"
    try:
        data, mime = synth_with_say(job["text"], voice, float(job.get("speed", 1.0)), job.get("format", "mp3"))
        session.put(
            f"{GATEWAY}/internal/worker/jobs/{job['id']}/result",
            data=data,
            headers={"Content-Type": mime},
            timeout=60,
        ).raise_for_status()
        log(f"job {job['id'][:8]}: done ({len(data) // 1024} KB {mime})")
    except Exception as exc:
        log(f"job {job['id'][:8]} failed: {exc}")
        try:
            session.post(f"{GATEWAY}/internal/worker/jobs/{job['id']}/error", json={"error": str(exc)}, timeout=10)
        except Exception:
            pass
    finally:
        state["worker_state"] = "idle"


def main() -> None:
    try:
        requests.get(f"{GATEWAY}/health", timeout=20).raise_for_status()
    except Exception as exc:
        sys.exit(f"Gateway not reachable at {GATEWAY}: {exc}")

    log(f"fake worker {WORKER_ID} polling {GATEWAY}")
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    backoff = 2
    while not stop.is_set():
        try:
            data = session.post(
                f"{GATEWAY}/internal/worker/poll",
                json={"worker_id": WORKER_ID, "wait": POLL_WAIT, **state},
                timeout=POLL_WAIT + 15,
            )
            if data.status_code == 401:
                sys.exit("Gateway rejected WORKER_TOKEN (401)")
            data.raise_for_status()
            payload = data.json()
            backoff = 2
            if payload.get("wake"):
                state["engine"] = payload.get("engine") or state["engine"]
                state["model_state"] = "ready"
                log("wake requested -> ready")
            if payload.get("job"):
                handle_job(payload["job"])
        except KeyboardInterrupt:
            break
        except Exception as exc:
            log(f"connection issue ({exc}); retry in {backoff}s")
            stop.wait(backoff)
            backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
