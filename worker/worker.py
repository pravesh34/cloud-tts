#!/usr/bin/env python3
"""Cloud TTS worker.

Runs on a free Colab GPU (or any machine with Python) and polls the Render
gateway for work. The Colab runtime has no inbound ports, so every connection
here is outbound: heartbeat, long-poll, upload.

Environment:
    GATEWAY_URL     e.g. https://cloud-tts.onrender.com
    WORKER_TOKEN    the secret shared with the gateway
    ENGINE          chatterbox (default) or kokoro
    WORKER_ID       optional stable id
"""

from __future__ import annotations

import gc
import json
import os
import signal
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

import requests

from engines import ENGINES, encode_audio

ROOT = Path(__file__).resolve().parent
CATALOG = json.loads((ROOT.parent / "voices.json").read_text(encoding="utf-8"))
VOICES = {v["id"]: v for v in CATALOG["voices"]}
VOICES_DIR = ROOT / "voices"

GATEWAY = (os.environ.get("GATEWAY_URL") or os.environ.get("BARK_GATEWAY") or "").rstrip("/")
TOKEN = os.environ.get("WORKER_TOKEN") or os.environ.get("BARK_WORKER_TOKEN") or ""
WORKER_ID = os.environ.get("WORKER_ID") or uuid.uuid4().hex[:12]
DEFAULT_ENGINE = os.environ.get("ENGINE") or CATALOG.get("default_voice", "male_natural")
DEFAULT_VOICE = next((v for v in VOICES.values() if v["id"] == CATALOG.get("default_voice")), None)
DEFAULT_ENGINE = DEFAULT_VOICE["engine"] if DEFAULT_VOICE else "chatterbox"
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "15"))
POLL_WAIT = int(os.environ.get("POLL_WAIT", "25"))


def log(*args) -> None:
    print(time.strftime("[%H:%M:%S]"), *args, flush=True)


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.worker_state = "idle"
        self.model_state = "unloaded"
        self.engine: str | None = None

    def set(self, **kwargs) -> None:
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "worker_state": self.worker_state,
                "model_state": self.model_state,
                "engine": self.engine,
            }


class EngineManager:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.engine = None
        self.name: str | None = None

    def ensure(self, name: str):
        with self.lock:
            if self.engine is not None and self.name == name:
                return self.engine
            self._unload_locked()
            cls = ENGINES.get(name)
            if cls is None:
                raise ValueError(f"unknown engine {name!r}; available: {sorted(ENGINES)}")
            log(f"loading engine: {name}")
            engine = cls(voices_dir=VOICES_DIR)
            engine.load()
            self.engine, self.name = engine, name
            return engine

    def _unload_locked(self) -> None:
        if self.engine is not None:
            log(f"unloading engine: {self.name}")
        self.engine = None
        self.name = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    def unload(self) -> None:
        with self.lock:
            self._unload_locked()


class Worker:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {TOKEN}"})
        self.state = State()
        self.manager = EngineManager()
        self.stop = threading.Event()

    # ---------------------------------------------------------------- transport
    def _request(self, method: str, path: str, *, timeout: float = 40, **kwargs):
        response = self.session.request(method, f"{GATEWAY}{path}", timeout=timeout, **kwargs)
        if response.status_code == 401:
            raise SystemExit("Gateway rejected WORKER_TOKEN (401). Check the Colab secret.")
        response.raise_for_status()
        if response.headers.get("content-type", "").startswith("application/json"):
            return response.json()
        return response

    def _push_status(self) -> None:
        try:
            self._request("POST", "/internal/worker/status", json={"worker_id": WORKER_ID, **self.state.snapshot()}, timeout=15)
        except Exception as exc:  # the heartbeat thread will retry
            log("status push failed:", exc)

    def heartbeat_loop(self) -> None:
        while not self.stop.wait(0):
            try:
                self._request(
                    "POST",
                    "/internal/worker/heartbeat",
                    json={"worker_id": WORKER_ID, **self.state.snapshot()},
                    timeout=15,
                )
            except SystemExit:
                self.stop.set()
                return
            except Exception as exc:
                log("heartbeat failed:", exc)
            self.stop.wait(HEARTBEAT_INTERVAL)

    # ------------------------------------------------------------------ handlers
    def handle_wake(self, engine: str | None) -> None:
        if not engine:
            return
        if self.manager.name == engine and self.state.model_state == "ready":
            return
        self.state.set(model_state="loading", engine=engine, worker_state="loading")
        self._push_status()
        try:
            self.manager.ensure(engine)
            self.state.set(model_state="ready", engine=engine, worker_state="idle")
            log(f"engine ready: {engine}")
        except Exception:
            self.state.set(model_state="error", worker_state="error")
            self.manager.unload()
            log("engine load failed:")
            traceback.print_exc()
        self._push_status()

    def handle_job(self, job: dict) -> None:
        voice = VOICES.get(job.get("voice") or "")
        if voice is None:
            self._fail(job["id"], f"unknown voice {job.get('voice')!r}")
            return
        engine_name = voice["engine"]
        log(f"job {job['id'][:8]}: {engine_name}/{voice['id']} ({len(job['text'])} chars, {job.get('format', 'mp3')})")
        self.state.set(worker_state="busy")
        self._push_status()
        started = time.time()
        try:
            engine = self.manager.ensure(engine_name)
            self.state.set(model_state="ready", engine=engine_name)
            samples = engine.synth(job["text"], voice, exaggeration=float(job.get("exaggeration", 0.5)))
            data, mime = encode_audio(
                samples,
                engine.sample_rate,
                fmt=job.get("format", "mp3"),
                speed=float(job.get("speed", 1.0)),
            )
            self._request(
                "PUT",
                f"/internal/worker/jobs/{job['id']}/result",
                data=data,
                headers={"Content-Type": mime},
                timeout=180,
            )
            log(f"job {job['id'][:8]}: done in {time.time() - started:.1f}s ({len(data) // 1024} KB)")
        except Exception as exc:
            log(f"job {job['id'][:8]} failed: {exc}")
            traceback.print_exc()
            self._fail(job["id"], str(exc))
            if "out of memory" in str(exc).lower():
                self.manager.unload()
                self.state.set(model_state="unloaded")
        finally:
            self.state.set(worker_state="idle")
            self._push_status()

    def _fail(self, job_id: str, message: str) -> None:
        try:
            self._request("POST", f"/internal/worker/jobs/{job_id}/error", json={"error": message[:900]}, timeout=20)
        except Exception:
            log("could not report failure:", message[:200])

    # ---------------------------------------------------------------------- loop
    def run(self) -> None:
        log(f"worker {WORKER_ID} polling {GATEWAY}")
        threading.Thread(target=self.heartbeat_loop, name="heartbeat", daemon=True).start()
        backoff = 2
        while not self.stop.is_set():
            try:
                payload = {"worker_id": WORKER_ID, "wait": POLL_WAIT, **self.state.snapshot()}
                data = self._request("POST", "/internal/worker/poll", json=payload, timeout=POLL_WAIT + 20)
                backoff = 2
                if data.get("wake"):
                    self.handle_wake(data.get("engine"))
                job = data.get("job")
                if job:
                    self.handle_job(job)
            except SystemExit:
                break
            except Exception as exc:
                log(f"connection issue ({exc}); retrying in {backoff}s")
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)


def main() -> None:
    if not GATEWAY:
        sys.exit("GATEWAY_URL is not set. Export it or add it to Colab Secrets.")
    if not TOKEN:
        sys.exit("WORKER_TOKEN is not set. Export it or add it to Colab Secrets.")

    worker = Worker()

    def _shutdown(signum, frame):
        log("shutting down")
        worker.stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)
    worker.run()


if __name__ == "__main__":
    main()
