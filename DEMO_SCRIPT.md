# NERVE — 3-Minute Demo Script

This is the run-of-show for the Incident Autopilot demo. In demo mode NERVE runs a
**seeded** incident (no live Dynatrace/GitLab/Gemini required): a checkout service
with a ~340% error spike correlated to a `payment_processor.py` deployment 31 minutes
earlier. The system files an incident issue, proposes a rollback, weathers an injected
"contradictory metrics" failure, waits for **your** approval, executes the rollback,
and resolves once the seeded service recovers.

---

## 0. One-time setup (before recording)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set **both** demo flags (plus any MongoDB URI — Atlas free tier or local):

```
DEMO_MODE=true
FAILURE_ENGINE_ENABLED=true
MONGODB_URI=mongodb://localhost:27017     # or your Atlas string
```

> The seeded demo does not call Gemini or live MCP servers, but `config.py` still
> requires the other env vars to be present (any placeholder value is fine for the
> seeded run): `GOOGLE_CLOUD_PROJECT`, `DYNATRACE_ENVIRONMENT_URL`,
> `DYNATRACE_API_TOKEN`, `GITLAB_TOKEN`, `GITLAB_PROJECT_ID`.

## 1. Start the system

```bash
uvicorn main:app --port 8000
```

Open **http://localhost:8000/** in a browser (full-screen for recording).

---

## The 3-minute arc

### [0:00–0:20] Frame the problem — *what to say*
> "This is NERVE — an autonomous operations system. It's 2 a.m., checkout error rates
> just spiked 340%. Normally an on-call engineer gets paged and starts digging. Watch
> NERVE do the investigation itself — and notice it still asks a human before doing
> anything destructive."

**Do:** In a second terminal, kick off the demo:
```bash
curl http://localhost:8000/demo/start
# {"status":"demo_started"}
```

### [0:20–0:50] Detection, correlation, and the incident issue — *what to say*
> "NERVE pulled the Dynatrace problem, fetched recent deployments, and correlated the
> spike to a deployment of `payment_processor.py` 31 minutes earlier. It's already
> filed a GitLab incident issue with its reasoning."

**Show on the dashboard:**
- **Status badge** flips `PLANNING → EXECUTING` (indigo).
- **Agent Activity** feed streams: `INCIDENT_DETECTED → CONTEXT_ASSEMBLED →
  REASONING_COMPLETE → ACTION_CREATED → ACTION_EXECUTED` (the issue) →
  `RESOLUTION_MONITORING_STARTED`.
- **Pending Approvals** shows a `gitlab_rollback` card with **Approve / Reject** buttons.

### [0:50–1:30] The twist — contradictory metrics + risk spike — *what to say*
> "Now the hard part. Real incidents are noisy. NERVE's failure engine is injecting
> contradictory metric readings — the kind of conflicting signal that makes humans
> hesitate. Watch the risk score climb and the system flag uncertainty instead of
> blindly acting."

**Show:**
- **⚠ Active Failures** panel appears: `CONTRADICTORY_METRICS on get_metrics`.
- **Risk gauge** jumps from ~0 into the amber band; feed shows `RISK_SCORE_UPDATED`
  and `REPLAN_TRIGGERED`.
- A few seconds later the failure clears (panel disappears, risk settles).

### [1:30–2:10] Human-in-the-loop approval — *what to say*
> "Here's the key safety property: NERVE will **not** execute the rollback on its own.
> Every state-changing action needs human sign-off. I'm the on-call engineer — I've
> reviewed its reasoning, and I approve."

**Do:** Click **Approve** on the rollback card (approver: `demo-operator`).
*(CLI fallback below.)*

**Show:** feed streams `ACTION_APPROVED → MCP_TOOL_CALLED (trigger_pipeline) →
ACTION_EXECUTED`. The rollback card clears.

### [2:10–2:50] Resolution — *what to say*
> "The rollback pipeline ran, the service is recovering, and NERVE is watching the
> error rate. Once it's back within 10% of baseline for three consecutive checks, it
> closes the incident on its own."

**Show:**
- Feed: repeated `RESOLUTION_CHECK`, then `ISSUE_CLOSED` and `MISSION_STATUS_CHANGED`.
- **Status badge** flips to **RESOLVED** (green).

### [2:50–3:00] Close — *what to say*
> "From detection to correlation to a safe, human-approved fix and verified recovery —
> autonomously, with a full audit trail of every event. That's NERVE."

---

## CLI fallbacks (keep this terminal ready)

```bash
# Find the demo mission id (most recent) and watch its state:
curl -s http://localhost:8000/missions/<MISSION_ID> | python -m json.tool

# The mission id is also printed in the server logs as `mission_id=...` on DEMO_STARTED.

# Approve the pending rollback (if the dashboard button misbehaves):
curl -X POST http://localhost:8000/actions/<ACTION_ID>/approve \
  -H 'Content-Type: application/json' -d '{"approved_by":"demo-operator"}'

# Full event log (audit trail) for the screen:
curl -s "http://localhost:8000/missions/<MISSION_ID>/events?limit=200" | python -m json.tool
```

Get `<ACTION_ID>` from the `pending_actions` array in the `GET /missions/<id>` response.

---

## If something breaks mid-demo — fallback plan

| Symptom | Fast recovery |
|---|---|
| Dashboard shows `ERR 404` / blank | You opened it before `/demo/start`. Re-run the curl, then add `?mission=<MISSION_ID>` to the URL (id is in the server log). |
| **Approve** button does nothing | Use the CLI approve fallback above — narrate "approving from the command line." |
| Risk gauge / failures panel don't update | The dashboard polls every 5s — pause and let one cycle pass; or refresh the page (state is server-side, nothing is lost). |
| Mission never reaches RESOLVED | Resolution only happens **after** you approve. Make sure you clicked Approve; the seeded service recovers only once the rollback pipeline runs. |
| Server failed to start (`ValidationError` at boot) | A required env var is unset — fill the placeholders listed in step 0 and restart. |
| Mongo connection error at startup | Point `MONGODB_URI` at a reachable instance (`mongodb://localhost:27017` with a local `mongod`, or your Atlas string). |
| Total failure | Have a **pre-recorded clip** of this exact arc ready to cut to. The demo is deterministic and seeded, so a recorded run matches a live run step-for-step. |

**Reset between takes:** stop the server (`Ctrl-C`) and restart it; `/demo/start`
creates a brand-new mission each time, so prior runs don't interfere. (If you want a
clean event log, also drop the `nerve` MongoDB database between takes.)

---

## Notes for the presenter

- **What's real vs seeded:** the orchestration, agents, approval gate, failure engine,
  resolver, event sourcing, and dashboard are all the real system. Only the Dynatrace/
  GitLab *responses* and the Gemini *correlation* are seeded fixtures (so the demo is
  deterministic and needs no external accounts).
- **The Task Graph panel** stays empty during the incident — Incident Autopilot is an
  action-centric workflow (issue + rollback actions), not a task-DAG mission. It's the
  **Pending Approvals**, **Risk**, **Active Failures**, and **Activity** panels that
  carry this demo. (The task graph lights up for `GENERAL` missions created via
  `POST /missions`.)
- **Timing:** the scripted beats run on a real-seconds timeline (~45s inject, ~90s
  clear, ~120s approval window). If you want a faster rehearsal, the timeline is driven
  by `DemoScenario(time_scale=...)`.
