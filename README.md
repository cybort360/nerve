# NERVE — Autonomous Operations Control System

NERVE is a persistent multi-agent execution system that turns high-level goals into
continuously evolving operational plans. The reference mission type is **Incident
Autopilot**: it correlates a Dynatrace anomaly with recent GitLab deployments, files
an incident issue, proposes a human-approved rollback, and monitors the service until
its error rate returns to baseline.

- **API + orchestration:** FastAPI app with a per-mission async orchestration loop
- **Agents:** Planner, Execution, Risk, Auditor (Gemini-backed reasoning)
- **Integrations:** Dynatrace MCP + GitLab MCP
- **State:** MongoDB Atlas (via `motor`)
- **Dashboard:** single-page mission view at `/`

See `CLAUDE.md` and `ARCHITECTURE.md` for the full design.

---

## Prerequisites

- **Python 3.11+**
- **Docker** (for building the Cloud Run image)
- **Google Cloud SDK** (`gcloud`) — authenticated, with a project selected
- A **MongoDB Atlas** cluster (or local MongoDB) and **Dynatrace** / **GitLab** tokens

---

## Local Development

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
#    then edit .env and fill in: GOOGLE_CLOUD_PROJECT, MONGODB_URI,
#    DYNATRACE_ENVIRONMENT_URL, DYNATRACE_API_TOKEN, GITLAB_TOKEN, GITLAB_PROJECT_ID

# 4. Run the API (auto-reload for development)
uvicorn main:app --reload
```

Then open **http://localhost:8000/** for the dashboard (the `--reload` dev server
defaults to port 8000; the container serves on 8080).

Run the test suite with:

```bash
pytest                 # unit + integration (live MCP tests skip without creds)
```

---

## Cloud Run Deployment

Secrets live in **Google Cloud Secret Manager** and are injected as environment
variables by Cloud Run — they are never baked into the image.

### 1. Create the secrets (once per project)

Edit the placeholders in `deploy/secrets_setup.sh`, then:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable secretmanager.googleapis.com run.googleapis.com \
                       cloudbuild.googleapis.com artifactregistry.googleapis.com

bash deploy/secrets_setup.sh
```

### 2. Create the Artifact Registry repo (once)

```bash
gcloud artifacts repositories create nerve \
  --repository-format=docker --location=us-central1
```

### 3. Build, push, and deploy

```bash
gcloud builds submit --config cloudbuild.yaml .
```

This runs the three `cloudbuild.yaml` steps: **build → push to Artifact Registry →
deploy to Cloud Run** with the `--set-secrets` bindings for `MONGODB_URI`,
`DYNATRACE_API_TOKEN`, and `GITLAB_TOKEN`.

> **Note — non-secret config:** the app also needs non-secret env vars to boot
> (`GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `DYNATRACE_ENVIRONMENT_URL`,
> `GITLAB_URL`, `GITLAB_PROJECT_ID`, `GEMINI_MODEL`). Add them via the commented
> `--set-env-vars` line in `cloudbuild.yaml`, or:
> ```bash
> gcloud run services update nerve --region us-central1 \
>   --set-env-vars=GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID,GOOGLE_CLOUD_LOCATION=us-central1,DYNATRACE_ENVIRONMENT_URL=https://xxxx.live.dynatrace.com,GITLAB_URL=https://gitlab.com,GITLAB_PROJECT_ID=123,GEMINI_MODEL=gemini-2.5-flash
> ```

> **Note — background loop:** the orchestration loop runs as a background asyncio
> task, so the service needs CPU even when idle. Deploy with
> `--no-cpu-throttling --min-instances=1` (and grant the Cloud Run service account
> `roles/secretmanager.secretAccessor` and `roles/aiplatform.user`).

---

## Triggering the Demo Scenario

The scripted demo (seeded checkout incident → contradictory metrics → replan →
approval → resolution) requires **both** feature flags:

```
DEMO_MODE=true
FAILURE_ENGINE_ENABLED=true
```

Set them locally in `.env` (or via `--set-env-vars` on Cloud Run), restart the app,
then start the demo:

```bash
curl http://localhost:8000/demo/start
# → {"status": "demo_started"}
```

Watch it unfold on the dashboard at `/`, or poll the API directly:

```bash
# Create a mission manually
curl -X POST http://localhost:8000/missions \
  -H 'Content-Type: application/json' \
  -d '{"goal": "resolve checkout error spike", "mission_type": "INCIDENT_RESPONSE"}'

# Inspect mission state (tasks, events, risk, pending actions)
curl http://localhost:8000/missions/<MISSION_ID>

# Approve a pending rollback action
curl -X POST http://localhost:8000/actions/<ACTION_ID>/approve \
  -H 'Content-Type: application/json' -d '{"approved_by": "demo-operator"}'
```
