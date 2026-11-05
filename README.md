# Cloud TTS

Private text-to-speech that runs on a **free Colab GPU**, orchestrated by a **free Render web service**. The client is an installable PWA served by the gateway itself.

No paid APIs. No API keys from a TTS vendor. Your text goes to your own worker.

```
Phone / desktop PWA  ──USER_API_KEY──▶  Render (FastAPI gateway, SQLite queue)
                                              ▲
                                              │ outbound only
                                              │ heartbeat · long-poll · upload
                                        Colab T4 GPU
                                        Chatterbox (natural) + Kokoro (fast)
```

## Why this stack

| Piece | Choice | Reason |
|---|---|---|
| Natural engine | **Chatterbox 500M** (MIT) | clones a real male/female voice from ~10 s of audio; ~3.5 GB VRAM; ~0.4× realtime on a mid GPU |
| Fast engine | **Kokoro-82M** (Apache-2.0) | instant, runs on CPU, good fallback when Colab has no GPU |
| Orchestration | **Render free web service** | one URL serves the API *and* the PWA; simple wake-up |
| Worker | **Colab free T4** | the GPU you already have |

Reality check: Colab has no inbound ports and cannot be started remotely. "Start session" wakes Render and loads the model, but the notebook must already be running.

## Quick start (no GPU, no Colab)

```bash
make setup
cp .env.example .env
make dev            # terminal 1 → http://127.0.0.1:8000
make fake-worker    # terminal 2 → uses macOS `say` as the TTS engine
```

Open <http://127.0.0.1:8000/app/>, then:

1. Gateway URL: `http://127.0.0.1:8000`
2. User API key: `dev-user-key` (from `.env`)
3. **Save** → **Start session** → type text → **Generate speech**

The fake worker speaks the real worker protocol, so this exercises auth, queueing, long-polling, uploads, retention, and the PWA end to end. `make test` runs 17 gateway tests.

## Deploy the gateway to Render

1. Push this repository to GitHub.
2. Render → **New → Blueprint**, pick the repo. `render.yaml` creates the service.
   - If you prefer manual setup: Root Directory `gateway`, Build `pip install -r requirements.txt`, Start `uvicorn app:app --host 0.0.0.0 --port $PORT`, health check `/health`.
3. Copy the generated `USER_API_KEY` and `WORKER_TOKEN` from the Render dashboard (Environment tab).
4. Open `https://<your-service>.onrender.com/app/` — the PWA is served from the same service.

Free-tier behavior to expect:

- Spins down after **15 idle minutes**; the next request takes ~60 s (the PWA shows a cold-start counter).
- 512 MB RAM / 0.1 CPU — fine, because the model never runs on Render.
- Ephemeral disk: SQLite state and finished audio are lost on restart/spin-down. The worker re-registers automatically.
- 750 instance hours/month. One service running 24/7 uses ~744, so **stop Colab when you're done**.
- ~5 GB bandwidth/month (Hobby). MP3 at 64 kbps is ~0.48 MB/min; WAV is ~2.9 MB/min — MP3 is the default for a reason.

## Start the Colab worker

1. Open `colab/CloudTTS_Worker.ipynb` in Google Colab.
2. Set **Runtime → Change runtime type → T4 GPU**.
3. Add two Colab **Secrets** (key icon in the sidebar):
   - `GATEWAY_URL` — e.g. `https://your-service.onrender.com`
   - `WORKER_TOKEN` — from Render (not the user key)
4. In the notebook, set `REPO_URL` to this GitHub repo and run all cells.
5. Optional: put `male.wav` and `female.wav` in `MyDrive/cloud_tts_voices/`; the notebook copies them into `worker/voices/`.

The notebook caches Hugging Face downloads in Google Drive, so later sessions load faster. Keep the tab open; closing it kills the worker.

## Voice clips

Chatterbox clones whatever is in:

- `worker/voices/male.wav`
- `worker/voices/female.wav`

10–15 s, one speaker, clean, 24 kHz+, no music. Use your own voice or public-domain audio you have rights to. Without the files the worker uses Chatterbox's built-in voice, so everything still runs.

Kokoro voices (`male_fast`, `male_warm`, `female_fast`, `female_warm`) are presets and need no clips.

Edit `voices.json` to add profiles: `chatterbox` profiles use `"ref"`, `kokoro` profiles use `"voice"`. The gateway reads the same file (it has an embedded fallback if the file is missing).

## API

All `/api/*` routes need `Authorization: Bearer $USER_API_KEY`.
All `/internal/*` routes need `Authorization: Bearer $WORKER_TOKEN`.

```bash
export BARK_URL="https://your-service.onrender.com"
export BARK_API_KEY="your-user-key"

curl "$BARK_URL/api/session" -H "Authorization: Bearer $BARK_API_KEY"

curl -X POST "$BARK_URL/api/wake" \
  -H "Authorization: Bearer $BARK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"engine":"chatterbox"}'

curl -X POST "$BARK_URL/api/jobs" \
  -H "Authorization: Bearer $BARK_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text":"Hello from Cloud TTS","voice":"male_natural","format":"mp3"}'

curl "$BARK_URL/api/jobs/JOB_ID" -H "Authorization: Bearer $BARK_API_KEY"
curl -L "$BARK_URL/api/jobs/JOB_ID/audio" -H "Authorization: Bearer $BARK_API_KEY" -o speech.mp3
```

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | public liveness probe (also wakes Render) |
| GET | `/api/session` | gateway/worker/model/queue in one call |
| GET | `/api/voices` | voice catalog |
| POST | `/api/wake` | load or swap engine on the worker |
| POST | `/api/jobs` | queue speech (`text`, `voice`, `speed`, `exaggeration`, `format`) |
| GET | `/api/jobs/{id}` | status |
| GET | `/api/jobs/{id}/audio` | WAV/MP3 bytes |
| DELETE | `/api/jobs/{id}` | drop a job |
| POST | `/internal/worker/poll` | long-poll for wake/jobs |
| POST | `/internal/worker/heartbeat` | independent liveness signal |
| PUT | `/internal/worker/jobs/{id}/result` | raw audio upload |
| POST | `/internal/worker/jobs/{id}/error` | report a failure |

## Reliability details

- **Independent heartbeat thread** — a long render no longer makes the worker look offline (the old code used one loop for both).
- **Atomic job claim** — `BEGIN IMMEDIATE` + conditional `UPDATE`, so two workers cannot take the same job.
- **Long-poll** — the worker holds a request for up to 25 s instead of hammering Render.
- **Sentence chunking** — Chatterbox drops or repeats words on long inputs; the worker renders sentence by sentence and joins with a short silence.
- **Requeue** — a job stuck in `processing` for 5 minutes goes back to `queued`.
- **Cleanup** — finished jobs and audio are deleted after 45 minutes; the table is capped at 200 rows.

## Limits and caveats

- Chatterbox output is **slower** than Kokoro (~20–40 s for a paragraph) and carries Resemble's inaudible **Perth watermark**.
- Colab free runtimes expire (~12 h) and can be preempted. Restart the notebook and tap Refresh.
- Render may restart the service at any time; queued jobs can be lost. Treat it as best-effort.
- Only clone voices you have permission to use. Do not use this to impersonate people.
- Kokoro is Apache-2.0; Chatterbox is MIT. Reference clips are your responsibility.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot reach the gateway` | Render is asleep or the URL is wrong; tap Start session and wait ~60 s |
| `Worker offline` | Colab tab closed or runtime died; run all cells again |
| `Gateway rejected WORKER_TOKEN` | Colab secret does not match Render's `WORKER_TOKEN` |
| Model load stuck | First Chatterbox run downloads ~3 GB; check Colab output |
| Generation fails with OOM | Switch the voice to a Kokoro profile, or restart the Colab runtime |
| Audio missing after a while | Retention deletes it after 45 min; regenerate |
| `ffmpeg` missing | `apt-get install -y ffmpeg` on the worker; WAV still works without it |

## Project layout

```
gateway/     FastAPI app, SQLite queue, voice catalog loader
worker/      supervisor + Chatterbox/Kokoro engines + reference clips
colab/       ready-to-run Colab notebook
web/         installable PWA (served at /app/)
scripts/     fake worker + icon generator
tests/       gateway test suite
voices.json  shared voice catalog
render.yaml  Blueprint
```
