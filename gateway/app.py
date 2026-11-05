"""Cloud TTS gateway.

A tiny FastAPI service that queues speech jobs for a Colab GPU worker.
It is intentionally stateless apart from SQLite: a free Render instance can
restart or spin down at any time and the worker simply re-registers.
"""

from __future__ import annotations

import io
import logging
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:  # package import (tests, uvicorn gateway.app:app)
    from . import voices as catalog
except ImportError:  # top-level import (Render: uvicorn app:app from gateway/)
    import voices as catalog  # type: ignore[no-redef]

log = logging.getLogger("cloudtts")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))


def _load_dotenv() -> None:
    """Tiny .env loader so local development needs no extra dependency."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

USER_API_KEY = os.environ.get("USER_API_KEY", "")
WORKER_TOKEN = os.environ.get("WORKER_TOKEN", "")
DB = os.environ.get("DATABASE_PATH", str(Path(__file__).resolve().parent.parent / ".data" / "gateway.sqlite3"))
HEARTBEAT_TTL = int(os.environ.get("HEARTBEAT_TTL", "60"))
JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", "2700"))
STALE_PROCESSING_SECONDS = int(os.environ.get("STALE_PROCESSING_SECONDS", "300"))
MAX_AUDIO_BYTES = int(os.environ.get("MAX_AUDIO_MB", "25")) * 1024 * 1024
MAX_TEXT = int(os.environ.get("MAX_TEXT", "5000"))
POLL_HOLD_MAX = int(os.environ.get("POLL_HOLD_MAX", "25"))
MAX_JOBS = int(os.environ.get("MAX_JOBS", "200"))
CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


# --------------------------------------------------------------------------- db

def _connect() -> sqlite3.Connection:
    Path(DB).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS state (
                id INTEGER PRIMARY KEY CHECK(id=1),
                worker_id TEXT,
                last_seen REAL,
                worker_state TEXT DEFAULT 'offline',
                engine TEXT,
                model_state TEXT DEFAULT 'unloaded',
                wake_requested INTEGER DEFAULT 0,
                wake_engine TEXT
            );
            INSERT OR IGNORE INTO state(id) VALUES(1);

            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
                voice TEXT,
                engine TEXT,
                speed REAL DEFAULT 1.0,
                exaggeration REAL DEFAULT 0.5,
                format TEXT DEFAULT 'mp3',
                status TEXT NOT NULL,
                created REAL NOT NULL,
                updated REAL NOT NULL,
                error TEXT,
                audio BLOB,
                audio_mime TEXT,
                worker_id TEXT
            );
            CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created);
            """
        )


def _cleanup(c: sqlite3.Connection) -> None:
    now = time.time()
    c.execute(
        "DELETE FROM jobs WHERE status IN ('completed','failed') AND updated < ?",
        (now - JOB_TTL_SECONDS,),
    )
    # A worker that died mid-render should not strand a job forever.
    c.execute(
        "UPDATE jobs SET status='queued', updated=?, worker_id=NULL "
        "WHERE status='processing' AND updated < ?",
        (now, now - STALE_PROCESSING_SECONDS),
    )
    c.execute(
        """DELETE FROM jobs WHERE id IN (
               SELECT id FROM jobs ORDER BY created DESC LIMIT -1 OFFSET ?
           ) AND status IN ('completed','failed')""",
        (MAX_JOBS,),
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    log.info("cloudtts gateway ready (db=%s, voices=%s)", DB, ",".join(catalog.ENGINES))
    yield


app = FastAPI(title="Cloud TTS Gateway", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ------------------------------------------------------------------------ auth

def bearer(value: Optional[str]) -> str:
    if not value or not value.startswith("Bearer "):
        return ""
    return value[7:].strip()


def require_user(authorization: Optional[str]) -> None:
    if not USER_API_KEY or not secrets.compare_digest(bearer(authorization), USER_API_KEY):
        raise HTTPException(401, "Invalid or missing user API key")


def require_worker(authorization: Optional[str]) -> None:
    if not WORKER_TOKEN or not secrets.compare_digest(bearer(authorization), WORKER_TOKEN):
        raise HTTPException(401, "Invalid or missing worker token")


# ----------------------------------------------------------------------- models

class JobRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    voice: Optional[str] = None
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    exaggeration: float = Field(default=0.5, ge=0.0, le=1.5)
    format: Literal["mp3", "wav"] = "mp3"


class WorkerPoll(BaseModel):
    worker_id: str
    engine: Optional[str] = None
    model_state: str = "unloaded"
    worker_state: str = "idle"
    wait: int = 0


class WorkerHeartbeat(BaseModel):
    worker_id: str
    worker_state: str = "idle"
    model_state: str = "unloaded"
    engine: Optional[str] = None


class WakeRequest(BaseModel):
    engine: Optional[str] = None


# -------------------------------------------------------------------- lifecycle

@app.get("/health")
def health():
    return {"ok": True, "service": "cloud-tts-gateway", "version": "2.0.0"}


@app.get("/")
def root():
    if WEB_DIR.exists():
        return RedirectResponse(url="/app/")
    return HTMLResponse(
        "<!doctype html><html><body style='font-family:system-ui;padding:2rem'>"
        "<h2>Cloud TTS Gateway</h2><p>Gateway is running.</p>"
        "<p><a href='/docs'>API documentation</a></p></body></html>"
    )


# ------------------------------------------------------------------ user: info

@app.get("/api/voices")
def list_voices(authorization: Optional[str] = Header(None)):
    require_user(authorization)
    return {"default_voice": catalog.DEFAULT_VOICE, "voices": catalog.public_voices()}


@app.get("/api/session")
def session(authorization: Optional[str] = Header(None)):
    require_user(authorization)
    with db() as c:
        row = c.execute("SELECT * FROM state WHERE id=1").fetchone()
        now = time.time()
        online = bool(row["last_seen"] and now - row["last_seen"] <= HEARTBEAT_TTL)
        counts = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}
        oldest = c.execute("SELECT MIN(created) AS t FROM jobs WHERE status='queued'").fetchone()["t"]
        ready = bool(online and row["model_state"] == "ready")
        return {
            "gateway": "online",
            "worker": "online" if online else "offline",
            "worker_state": row["worker_state"] if online else "offline",
            "engine": row["engine"] if online else None,
            "model": row["model_state"] if online else "unavailable",
            "ready": ready,
            "queue": {
                "queued": counts.get("queued", 0),
                "processing": counts.get("processing", 0),
                "completed": counts.get("completed", 0),
                "failed": counts.get("failed", 0),
                "oldest_queued_seconds": round(now - oldest, 1) if oldest else None,
            },
            "last_heartbeat_age_seconds": round(now - row["last_seen"], 1) if row["last_seen"] else None,
            "heartbeat_ttl_seconds": HEARTBEAT_TTL,
            "engines": catalog.ENGINES,
            "workers": "single" if online else "none",
        }


@app.get("/api/ready")
def ready(authorization: Optional[str] = Header(None)):
    """Backwards-compatible readiness probe."""
    require_user(authorization)
    with db() as c:
        row = c.execute("SELECT * FROM state WHERE id=1").fetchone()
    online = bool(row["last_seen"] and time.time() - row["last_seen"] <= HEARTBEAT_TTL)
    return {
        "gateway": "online",
        "worker": "online" if online else "offline",
        "model": row["model_state"] if online else "unavailable",
        "ready": bool(online and row["model_state"] == "ready"),
    }


@app.post("/api/wake")
def wake(body: Optional[WakeRequest] = None, authorization: Optional[str] = Header(None)):
    require_user(authorization)
    with db() as c:
        row = c.execute("SELECT * FROM state WHERE id=1").fetchone()
        online = bool(row["last_seen"] and time.time() - row["last_seen"] <= HEARTBEAT_TTL)
        if not online:
            raise HTTPException(
                503,
                detail={
                    "code": "WORKER_OFFLINE",
                    "message": "The Colab worker is not running. Open the notebook and run all cells, then retry.",
                },
            )
        engine = (body.engine if body and body.engine else None) or row["engine"] or catalog.ENGINES[0]
        if engine not in catalog.ENGINES:
            raise HTTPException(400, f"Unknown engine {engine!r}; choose one of {catalog.ENGINES}")
        c.execute(
            "UPDATE state SET wake_requested=1, wake_engine=? WHERE id=1",
            (engine,),
        )
        return {"accepted": True, "engine": engine, "message": "Wake request queued for the active worker."}


@app.post("/api/jobs")
def create_job(body: JobRequest, authorization: Optional[str] = Header(None)):
    require_user(authorization)
    voice = catalog.get_voice(body.voice)
    if not voice:
        raise HTTPException(400, f"Unknown voice {body.voice!r}. GET /api/voices for the catalog.")
    if not body.text.strip():
        raise HTTPException(422, "text must not be blank")

    now = time.time()
    job_id = str(uuid.uuid4())
    with db() as c:
        _cleanup(c)
        c.execute(
            "INSERT INTO jobs(id,text,voice,engine,speed,exaggeration,format,status,created,updated) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                body.text,
                voice["id"],
                voice["engine"],
                body.speed,
                body.exaggeration,
                body.format,
                "queued",
                now,
                now,
            ),
        )
        row = c.execute("SELECT * FROM state WHERE id=1").fetchone()
        online = bool(row["last_seen"] and now - row["last_seen"] <= HEARTBEAT_TTL)
        # Ask an active worker to load/swap to the right engine.
        if online and (row["model_state"] != "ready" or row["engine"] != voice["engine"]):
            c.execute("UPDATE state SET wake_requested=1, wake_engine=? WHERE id=1", (voice["engine"],))
    return {
        "job_id": job_id,
        "status": "queued",
        "voice": voice["id"],
        "engine": voice["engine"],
        "format": body.format,
        "poll": f"/api/jobs/{job_id}",
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, authorization: Optional[str] = Header(None)):
    require_user(authorization)
    with db() as c:
        row = c.execute(
            "SELECT id,status,voice,engine,format,created,updated,error FROM jobs WHERE id=?",
            (job_id,),
        ).fetchone()
    if not row:
        raise HTTPException(404, "Job not found")
    data = dict(row)
    data["age_seconds"] = round(time.time() - row["created"], 1)
    return data


@app.get("/api/jobs/{job_id}/audio")
def get_audio(job_id: str, authorization: Optional[str] = Header(None)):
    require_user(authorization)
    with db() as c:
        row = c.execute("SELECT status,audio,audio_mime,format,voice FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Job not found")
    if row["status"] != "completed" or not row["audio"]:
        raise HTTPException(409, "Audio is not ready")
    ext = "mp3" if (row["audio_mime"] or "").endswith("mpeg") else (row["format"] or "wav")
    return StreamingResponse(
        io.BytesIO(bytes(row["audio"])),
        media_type=row["audio_mime"] or "audio/wav",
        headers={
            "Content-Disposition": f'attachment; filename="cloudtts-{job_id[:8]}.{ext}"',
            "Cache-Control": "no-store",
        },
    )


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, authorization: Optional[str] = Header(None)):
    require_user(authorization)
    with db() as c:
        cur = c.execute("DELETE FROM jobs WHERE id=?", (job_id,))
    if cur.rowcount == 0:
        raise HTTPException(404, "Job not found")
    return {"deleted": job_id}


# ---------------------------------------------------------------- worker: state

def _touch(c: sqlite3.Connection, body: WorkerHeartbeat) -> None:
    c.execute(
        "UPDATE state SET worker_id=?, last_seen=?, worker_state=?, model_state=?, engine=? WHERE id=1",
        (body.worker_id, time.time(), body.worker_state, body.model_state, body.engine),
    )


@app.post("/internal/worker/heartbeat")
def heartbeat(body: WorkerHeartbeat, authorization: Optional[str] = Header(None)):
    require_worker(authorization)
    with db() as c:
        _touch(c, body)
    return {"ok": True, "server_time": time.time()}


@app.post("/internal/worker/status")
def worker_status(body: WorkerHeartbeat, authorization: Optional[str] = Header(None)):
    require_worker(authorization)
    with db() as c:
        _touch(c, body)
    return {"ok": True}


@app.post("/internal/worker/poll")
def poll(body: WorkerPoll, authorization: Optional[str] = Header(None)):
    """Long-poll for wake requests and queued jobs.

    The worker calls this repeatedly. We hold the connection briefly so an
    idle worker does not hammer the gateway, and claim jobs inside an
    ``IMMEDIATE`` transaction so two workers can never take the same job.
    """
    require_worker(authorization)
    deadline = time.time() + max(0, min(body.wait, POLL_HOLD_MAX))
    while True:
        with db() as c:
            c.execute(
                "UPDATE state SET worker_id=?, last_seen=?, worker_state=?, model_state=?, engine=? WHERE id=1",
                (body.worker_id, time.time(), body.worker_state, body.model_state, body.engine),
            )
            _cleanup(c)
            row = c.execute("SELECT * FROM state WHERE id=1").fetchone()
            wake_requested = bool(row["wake_requested"])
            wake_engine = row["wake_engine"]

            c.execute("BEGIN IMMEDIATE")
            try:
                if wake_requested:
                    c.execute("UPDATE state SET wake_requested=0, wake_engine=NULL WHERE id=1")
                    c.execute("COMMIT")
                    return {"wake": True, "engine": wake_engine or body.engine, "job": None, "heartbeat_interval": 15}

                job = c.execute(
                    "SELECT id,text,voice,engine,speed,exaggeration,format FROM jobs "
                    "WHERE status='queued' ORDER BY created LIMIT 1"
                ).fetchone()

                if job and row["model_state"] != "ready":
                    c.execute("UPDATE state SET wake_requested=1, wake_engine=? WHERE id=1", (job["engine"],))
                    c.execute("COMMIT")
                    return {"wake": True, "engine": job["engine"], "job": None, "heartbeat_interval": 15}

                if job and row["engine"] and job["engine"] != row["engine"]:
                    # Loaded engine is wrong for the oldest job: swap first.
                    c.execute("UPDATE state SET wake_requested=1, wake_engine=? WHERE id=1", (job["engine"],))
                    c.execute("COMMIT")
                    return {"wake": True, "engine": job["engine"], "job": None, "heartbeat_interval": 15}

                if job:
                    c.execute(
                        "UPDATE jobs SET status='processing', updated=?, worker_id=? WHERE id=? AND status='queued'",
                        (time.time(), body.worker_id, job["id"]),
                    )
                    c.execute("COMMIT")
                    return {
                        "wake": False,
                        "engine": job["engine"],
                        "job": dict(job),
                        "heartbeat_interval": 15,
                    }
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

        if time.time() >= deadline:
            return {"wake": False, "engine": None, "job": None, "heartbeat_interval": 15}
        time.sleep(0.4)


@app.put("/internal/worker/jobs/{job_id}/result")
async def job_result(job_id: str, request: Request, authorization: Optional[str] = Header(None)):
    require_worker(authorization)
    body = await request.body()
    if not body or len(body) < 128:
        raise HTTPException(400, "Missing or invalid audio body")
    if len(body) > MAX_AUDIO_BYTES:
        raise HTTPException(413, f"Audio exceeds {MAX_AUDIO_BYTES // (1024 * 1024)} MB")
    mime = (request.headers.get("content-type") or "audio/wav").split(";")[0].strip().lower()
    head = body[:4]
    if head[:4] == b"RIFF":
        mime = "audio/wav"
    elif head[:3] == b"ID3" or head[:2] == b"\xff\xfb" or head[:2] == b"\xff\xf3":
        mime = "audio/mpeg"
    with db() as c:
        cur = c.execute(
            "UPDATE jobs SET status='completed', audio=?, audio_mime=?, updated=?, error=NULL "
            "WHERE id=? AND status IN ('processing','queued')",
            (sqlite3.Binary(body), mime, time.time(), job_id),
        )
    if cur.rowcount == 0:
        raise HTTPException(404, "Job not found or already finished")
    return {"ok": True}


@app.post("/internal/worker/jobs/{job_id}/error")
def job_error(job_id: str, payload: dict, authorization: Optional[str] = Header(None)):
    require_worker(authorization)
    message = str(payload.get("error", "Worker error"))[:1000]
    with db() as c:
        cur = c.execute(
            "UPDATE jobs SET status='failed', error=?, updated=? WHERE id=?",
            (message, time.time(), job_id),
        )
    if cur.rowcount == 0:
        raise HTTPException(404, "Job not found")
    return {"ok": True}


# ------------------------------------------------------------------- static web
if WEB_DIR.exists():
    app.mount("/app", StaticFiles(directory=WEB_DIR, html=True), name="web")
