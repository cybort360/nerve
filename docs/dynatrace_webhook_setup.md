# Dynatrace Webhook Setup

Wire Dynatrace problem notifications to NERVE so incidents are detected in real
time (no polling). On a `PROBLEM_OPEN`, NERVE opens an `INCIDENT_RESPONSE`
mission and starts the orchestration loop; on `PROBLEM_RESOLVED`, it records a
resolution event on that mission.

**Endpoint:** `POST /webhooks/dynatrace`
**Auth:** a shared secret sent in the `X-Dynatrace-Signature` request header,
compared in constant time against `DYNATRACE_WEBHOOK_SECRET`.

---

## 1. Generate and set the shared secret

```bash
openssl rand -hex 32
```

Put the value in your environment (locally in `.env`, in production via Secret
Manager / `--set-env-vars`):

```
DYNATRACE_WEBHOOK_SECRET=<the generated value>
```

You'll paste the **same** value into Dynatrace as a custom header (step 3).
If this is blank, the receiver rejects every call (`401`).

---

## 2. Configure the webhook in Dynatrace

Dynatrace → **Settings → Integration → Problem notifications → Add notification
→ Custom integration**.

- **Name:** `NERVE Incident Autopilot`
- **Webhook URL:** `https://<your-nerve-host>/webhooks/dynatrace`
  (local testing via a tunnel, e.g. `https://<id>.ngrok.io/webhooks/dynatrace`)
- **Custom payload:** paste the template in step 3.
- **Custom HTTP headers:** add one header
  - Name: `X-Dynatrace-Signature`
  - Value: `<your DYNATRACE_WEBHOOK_SECRET>`
- Leave "Call webhook if new events merge into existing problems" as you prefer;
  NERVE keys on the problem id.

> Dynatrace does not natively HMAC-sign the request body, so authentication uses
> a fixed shared-secret header. Keep the secret confidential and serve the
> endpoint over HTTPS.

---

## 3. Payload template

Paste this exact JSON into Dynatrace's **Custom payload** field. The
placeholders (`{...}`) are substituted by Dynatrace at send time and map to
NERVE's `DynatraceWebhookPayload` schema.

```json
{
  "problem_id": "{ProblemID}",
  "state": "{State}",
  "title": "{ProblemTitle}",
  "severity": "{ProblemSeverity}",
  "impact": "{ProblemImpact}",
  "url": "{ProblemURL}",
  "impacted_entities": {ImpactedEntities}
}
```

- `state` is Dynatrace's `{State}` — `OPEN` or `RESOLVED`.
- `problem_id` and `state` are required; the rest are optional.
- `{ImpactedEntities}` expands to a JSON array (no surrounding quotes).

---

## 4. Test it locally with curl

With the server running and `DYNATRACE_WEBHOOK_SECRET=devsecret` set:

```bash
# PROBLEM_OPEN -> creates an INCIDENT_RESPONSE mission and starts the loop
curl -s -X POST http://localhost:8000/webhooks/dynatrace \
  -H "Content-Type: application/json" \
  -H "X-Dynatrace-Signature: devsecret" \
  -d '{
    "problem_id": "P-12345",
    "state": "OPEN",
    "title": "Elevated error rate on checkout",
    "severity": "AVAILABILITY",
    "impact": "SERVICE",
    "url": "https://xxx.live.dynatrace.com/#problems/problemdetails;pid=P-12345",
    "impacted_entities": [{"type": "SERVICE", "name": "checkout"}]
  }'
# -> {"status":"mission_created","mission_id":"..."}

# PROBLEM_RESOLVED -> records a DYNATRACE_RESOLVED event on that mission
curl -s -X POST http://localhost:8000/webhooks/dynatrace \
  -H "Content-Type: application/json" \
  -H "X-Dynatrace-Signature: devsecret" \
  -d '{"problem_id": "P-12345", "state": "RESOLVED", "title": "Elevated error rate on checkout"}'
# -> {"status":"resolution_logged","mission_id":"..."}
```

A wrong/missing header returns `401`; a malformed body returns `422`.

### No-Dynatrace demo shortcut

When `DEMO_MODE=true`, fire a seeded `PROBLEM_OPEN` through the same handler
without a signature (great for demos):

```bash
curl -s -X POST http://localhost:8000/webhooks/dynatrace/test
# -> {"status":"mission_created","mission_id":"..."}
```

This endpoint returns `403` when `DEMO_MODE` is not enabled.
