import pytest
import time
from fastapi.testclient import TestClient

from gateway import app as appmod

USER = {"Authorization": "Bearer user-secret"}
WORKER = {"Authorization": "Bearer worker-secret"}
FAKE_WAV = b"RIFF" + b"\x00" * 400


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "DB", str(tmp_path / "gateway.sqlite3"))
    monkeypatch.setattr(appmod, "USER_API_KEY", "user-secret")
    monkeypatch.setattr(appmod, "WORKER_TOKEN", "worker-secret")
    monkeypatch.setattr(appmod, "HEARTBEAT_TTL", 60)
    monkeypatch.setattr(appmod, "JOB_TTL_SECONDS", 2700)
    monkeypatch.setattr(appmod, "STALE_PROCESSING_SECONDS", 300)
    appmod.init_db()
    with TestClient(appmod.app) as c:
        yield c


def heartbeat(client, state="idle", model="ready", engine="chatterbox"):
    response = client.post(
        "/internal/worker/heartbeat",
        headers=WORKER,
        json={"worker_id": "w1", "worker_state": state, "model_state": model, "engine": engine},
    )
    assert response.status_code == 200


def poll(client, wait=0):
    return client.post(
        "/internal/worker/poll",
        headers=WORKER,
        json={"worker_id": "w1", "wait": wait, "worker_state": "idle", "model_state": "ready", "engine": "chatterbox"},
    )


# --------------------------------------------------------------------- basics

def test_health_is_public(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_api_requires_user_key(client):
    assert client.get("/api/voices").status_code == 401
    assert client.get("/api/voices", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/api/voices", headers=USER).status_code == 200


def test_worker_token_is_separate(client):
    assert client.post("/internal/worker/heartbeat", json={"worker_id": "w1"}).status_code == 401
    assert client.post("/internal/worker/heartbeat", headers=USER, json={"worker_id": "w1"}).status_code == 401


def test_voice_catalog(client):
    data = client.get("/api/voices", headers=USER).json()
    assert data["default_voice"]
    ids = {v["id"] for v in data["voices"]}
    assert {"male_natural", "female_natural"} <= ids
    assert all("ref" not in v for v in data["voices"])


# ------------------------------------------------------------------- session

def test_session_reports_offline(client):
    data = client.get("/api/session", headers=USER).json()
    assert data["worker"] == "offline"
    assert data["ready"] is False


def test_wake_rejects_when_worker_offline(client):
    response = client.post("/api/wake", headers=USER, json={"engine": "chatterbox"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "WORKER_OFFLINE"


def test_heartbeat_marks_worker_online(client):
    heartbeat(client)
    data = client.get("/api/session", headers=USER).json()
    assert data["worker"] == "online"
    assert data["ready"] is True
    assert data["engine"] == "chatterbox"


def test_wake_sets_flag_for_next_poll(client):
    heartbeat(client, model="unloaded")
    assert client.post("/api/wake", headers=USER, json={"engine": "kokoro"}).status_code == 200
    payload = poll(client).json()
    assert payload["wake"] is True
    assert payload["engine"] == "kokoro"


# ---------------------------------------------------------------------- jobs

def test_full_job_lifecycle(client):
    heartbeat(client)
    created = client.post(
        "/api/jobs",
        headers=USER,
        json={"text": "Hello from the test suite.", "voice": "male_natural", "format": "wav"},
    )
    assert created.status_code == 200
    job_id = created.json()["job_id"]
    assert created.json()["engine"] == "chatterbox"

    claimed = poll(client).json()
    assert claimed["job"]["id"] == job_id
    assert claimed["job"]["text"].startswith("Hello")

    uploaded = client.put(
        f"/internal/worker/jobs/{job_id}/result",
        headers={**WORKER, "Content-Type": "audio/wav"},
        content=FAKE_WAV,
    )
    assert uploaded.status_code == 200

    job = client.get(f"/api/jobs/{job_id}", headers=USER).json()
    assert job["status"] == "completed"

    audio = client.get(f"/api/jobs/{job_id}/audio", headers=USER)
    assert audio.status_code == 200
    assert audio.content == FAKE_WAV
    assert audio.headers["content-type"].startswith("audio/wav")


def test_unknown_voice_is_rejected(client):
    heartbeat(client)
    response = client.post("/api/jobs", headers=USER, json={"text": "hi", "voice": "nope"})
    assert response.status_code == 400
    assert "Unknown voice" in response.json()["detail"]


def test_bad_format_is_rejected(client):
    heartbeat(client)
    response = client.post("/api/jobs", headers=USER, json={"text": "hi", "format": "flac"})
    assert response.status_code == 422


def test_audio_not_ready(client):
    heartbeat(client)
    job_id = client.post("/api/jobs", headers=USER, json={"text": "hi"}).json()["job_id"]
    response = client.get(f"/api/jobs/{job_id}/audio", headers=USER)
    assert response.status_code == 409


def test_result_for_unknown_job(client):
    response = client.put(
        "/internal/worker/jobs/does-not-exist/result",
        headers={**WORKER, "Content-Type": "audio/wav"},
        content=FAKE_WAV,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------- concurrency

def test_two_workers_cannot_claim_same_job(client):
    heartbeat(client)
    job_id = client.post("/api/jobs", headers=USER, json={"text": "only once"}).json()["job_id"]

    first = poll(client).json().get("job")
    second = poll(client).json().get("job")
    claimed = [j["id"] for j in (first, second) if j]
    assert claimed == [job_id]


def test_job_for_other_engine_triggers_swap(client):
    heartbeat(client, engine="chatterbox")
    client.post("/api/jobs", headers=USER, json={"text": "fast", "voice": "female_fast"})
    payload = poll(client).json()
    assert payload["wake"] is True
    assert payload["engine"] == "kokoro"
    assert payload["job"] is None


# ------------------------------------------------------------------ retention

def test_completed_jobs_are_cleaned_up(client, monkeypatch):
    heartbeat(client)
    job_id = client.post("/api/jobs", headers=USER, json={"text": "bye"}).json()["job_id"]
    poll(client)
    client.put(
        f"/internal/worker/jobs/{job_id}/result",
        headers={**WORKER, "Content-Type": "audio/wav"},
        content=FAKE_WAV,
    )
    assert client.get(f"/api/jobs/{job_id}", headers=USER).status_code == 200

    monkeypatch.setattr(appmod, "JOB_TTL_SECONDS", 0)
    time.sleep(0.01)
    poll(client)
    assert client.get(f"/api/jobs/{job_id}", headers=USER).status_code == 404


def test_stale_processing_job_is_requeued(client, monkeypatch):
    heartbeat(client)
    job_id = client.post("/api/jobs", headers=USER, json={"text": "again"}).json()["job_id"]
    assert poll(client).json()["job"]["id"] == job_id

    with appmod.db() as conn:
        conn.execute("UPDATE jobs SET updated=? WHERE id=?", (time.time() - 1000, job_id))

    monkeypatch.setattr(appmod, "STALE_PROCESSING_SECONDS", 300)
    payload = poll(client).json()
    assert payload["job"]["id"] == job_id
