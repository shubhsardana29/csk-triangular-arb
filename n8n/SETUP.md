# n8n Integration — Setup & Architecture

## What this does

The bot runs a live spread-arbitrage strategy and can optionally push trade events to
an **n8n workflow** over HTTP. n8n then asks **Claude (AI)** to decide whether the
bot's spread threshold needs adjustment, applies the change via a **Control API**
running inside the bot process, and posts a summary to **Slack**.

```
Bot process
  │
  ├─ WebhookEmitter  ──POST /webhook/arb-opportunity──►  n8n
  │                                                        │
  │                                                   Filter event
  │                                                        │
  │                                                   Build Claude prompt
  │                                                        │
  │                                                   Ask Claude (HTTP)
  │                                                        │
  │                                                   Parse response
  │                                                        │
  │                  ◄──POST /control───────────────── Adjust threshold?
  │  (ControlAPI)                                          │
  │                                                   Post to Slack
  │
  └─ ControlAPI  ◄──  n8n / manual curl
      GET  /health    — liveness check
      GET  /status    — current runtime config
      POST /control   — hot-change config without restart
```

---

## Workflows

### 1. `opportunity_analyzer.json` — production

Triggered by every trade the bot executes. Claude decides whether to lower, raise, or
hold the `min_spread_pct` threshold based on how far the profit was above/below the bar.

**Nodes:**
| Step | What it does |
|------|-------------|
| Bot Webhook | Receives POST from WebhookEmitter |
| Filter and Build Prompt | Drops non-opportunity events; builds Claude prompt with trade data |
| Ask Claude | POST to Anthropic API → gets `{action, new_threshold, reason}` |
| Parse Claude Response | Validates JSON; rejects thresholds outside 1.2%–5% |
| Should Adjust? | IF node — only continues if action ≠ "hold" |
| Update Bot Config | POST to ControlAPI → updates `min_spread_pct` live |
| Slack - Threshold Changed | Slack message when threshold was adjusted |
| Slack - Trade Logged | Slack message when Claude said hold |

**Claude's decision rules:**
- `profit > threshold × 1.5` AND `profit > 2%` → **lower** (bot is too conservative)
- `profit < threshold × 1.05` → **raise** (barely cleared the bar, market thinner than expected)
- everything else → **hold**

Threshold is never set below 1.2% (cost floor) or above 5% (too restrictive).

---

### 2. `daily_digest.json` — production

Fires daily at 09:30 IST (04:00 UTC). Fetches current bot status, asks Claude for a
brief ops summary, posts to Slack.

**Nodes:** Schedule Trigger → Get Bot Status → Build Digest Prompt → Ask Claude →
Format Slack Message → Post to Slack

---

### 3. `opportunity_analyzer_local_test.json` — no Claude key required

Despite the name, **this is the workflow to run on your VPS until you have an
Anthropic API key**. Two differences from the production version:

- **Mock Claude** (Code node) — rule-based decision engine, no API call, no key needed
- **Real Slack HTTP nodes** — posts to your Slack webhook exactly like production

The mock logic is identical to what Claude would reason:
```
profit > threshold × 1.5  AND  > 2%   →  lower  (bot too conservative)
profit < threshold × 1.05 AND  > 0    →  raise   (barely cleared the bar)
everything else                        →  hold
```

When you get an Anthropic key later, swap in `opportunity_analyzer.json` — the
Parse Claude Response node and all downstream nodes are identical in both workflows.

---

## Bot-side env vars

Add these to your `.env` (or export before starting the bot):

```bash
# n8n webhook — where the bot POSTs trade events
N8N_WEBHOOK_ENABLED=true
N8N_WEBHOOK_URL=http://localhost:5678/webhook/arb-opportunity   # local
# N8N_WEBHOOK_URL=http://localhost:5678/webhook/arb-opportunity  # prod (n8n on same host)

# Control API — n8n writes back through this
CONTROL_API_ENABLED=true
CONTROL_API_HOST=127.0.0.1   # localhost only; use 0.0.0.0 only for Docker-based n8n
CONTROL_API_PORT=8765
CONTROL_API_SECRET=your-secret-here   # must match the value in n8n workflow nodes
```

---

## Control API reference

All requests require header `X-Control-Secret: <your-secret>`.

### `GET /health`
```json
{"ok": true}
```

### `GET /status`
```json
{
  "min_spread_pct": 0.015,
  "two_leg_enabled": true,
  "three_leg_enabled": false,
  "taker_fee": 0.001,
  "tds_rate": 0.01
}
```

### `POST /control`
Body (all fields optional):
```json
{
  "min_spread_pct": 0.013,
  "two_leg_enabled": true,
  "three_leg_enabled": false
}
```
`min_spread_pct` is clamped to `[0.005, 0.10]`. Changes take effect on the **next
tick** (< 100 ms). No restart needed.

---

## Local setup (WSL2 / dev machine)

### 1. Start n8n in Docker

```bash
docker run -d \
  --name n8n \
  --restart unless-stopped \
  --add-host=host.docker.internal:host-gateway \
  -p 5678:5678 \
  -v n8n_data:/home/node/.n8n \
  n8nio/n8n
```

`host.docker.internal` lets the container reach ports on your WSL2 host.

### 2. Import the test workflow

```bash
# Import
docker cp n8n/opportunity_analyzer_local_test.json n8n:/tmp/wf.json
docker exec n8n n8n import:workflow --input=/tmp/wf.json

# Activate
docker exec n8n n8n publish:workflow --id=arb-opportunity-analyzer-test
docker restart n8n
```

Open `http://localhost:5678`, find the workflow, toggle it ON if it shows inactive.

### 3. Start the bot with integration enabled

```bash
N8N_WEBHOOK_ENABLED=true \
N8N_WEBHOOK_URL=http://localhost:5678/webhook/arb-opportunity \
CONTROL_API_ENABLED=true \
CONTROL_API_HOST=0.0.0.0 \
CONTROL_API_PORT=8765 \
CONTROL_API_SECRET=test-secret \
python main.py
```

Note: use `CONTROL_API_HOST=0.0.0.0` locally so the Docker container can reach the
control API on the host. In production keep it `127.0.0.1`.

### 4. Run the integration test suite

```bash
chmod +x n8n/test_integration.sh
N8N_WEBHOOK_URL=http://localhost:5678/webhook/arb-opportunity \
CONTROL_API_SECRET=test-secret \
bash n8n/test_integration.sh
```

All 8 tests should pass (health, status, auth, direct control, n8n→lower, n8n→hold,
non-opportunity dropped, safety clamp).

---

## Production setup on VPS (no sudo, screen-based)

Your bot already runs in a `screen` session. This section adds n8n alongside it using
Docker (rootless or regular, depending on what your host provides) and the production
workflows.

### Step 1 — check Docker availability

```bash
docker info 2>/dev/null && echo "Docker OK" || echo "No Docker"
```

If Docker isn't available and you can't install it, use the **n8n npm** fallback in
Step 1b below.

### Step 1b — n8n via npm (no Docker, no sudo)

```bash
# Install n8n locally under your user
npm install -g n8n   # or: npx n8n

# Start n8n in its own screen session
screen -dmS n8n bash -c '
  N8N_USER_MANAGEMENT_DISABLED=true \
  N8N_BASIC_AUTH_ACTIVE=false \
  n8n start 2>&1 | tee ~/n8n.log
'

# Verify
sleep 8 && curl -s http://localhost:5678/healthz
```

### Step 2 — put n8n behind a local port only

n8n binds to `0.0.0.0:5678` by default. On a VPS you do **not** want it exposed
publicly. Block it at the firewall or bind to localhost only:

```bash
# If using ufw (no sudo needed to check, but setting rules needs sudo)
# Ask your host / provider to restrict port 5678 to localhost only.
# Alternatively, set N8N_HOST=127.0.0.1 before starting n8n:
N8N_HOST=127.0.0.1 n8n start
```

The Control API already binds to `127.0.0.1` in production — no action needed there.

### Step 3 — import workflows

**No Claude API key yet?** Import `opportunity_analyzer_local_test.json` (Mock Claude
+ real Slack). Import `opportunity_analyzer.json` later when you have a key.

```bash
# Copy workflow files to VPS (run from your local machine)
scp n8n/opportunity_analyzer_local_test.json n8n/daily_digest.json \
    user@your-vps:~/csk-triangular-arb/n8n/

# On VPS — import both
n8n import:workflow --input=~/csk-triangular-arb/n8n/opportunity_analyzer_local_test.json
n8n import:workflow --input=~/csk-triangular-arb/n8n/daily_digest.json
n8n publish:workflow --id=arb-opportunity-analyzer-test
n8n publish:workflow --id=arb-daily-digest
```

Or if using Docker on the VPS:
```bash
docker cp n8n/opportunity_analyzer_local_test.json n8n:/tmp/opp.json
docker cp n8n/daily_digest.json n8n:/tmp/digest.json
docker exec n8n n8n import:workflow --input=/tmp/opp.json
docker exec n8n n8n import:workflow --input=/tmp/digest.json
docker exec n8n n8n publish:workflow --id=arb-opportunity-analyzer-test
docker exec n8n n8n publish:workflow --id=arb-daily-digest
docker restart n8n
```

### Step 4 — fill in Slack URL and Control API secret in the workflow nodes

In the n8n UI (`http://your-vps:5678` via SSH tunnel — see Step 5):

**opportunity_analyzer_local_test** (no Claude key needed):
1. **Update Bot Config** node → header `X-Control-Secret` → your secret
2. **Log - Threshold Changed** node → URL → your Slack webhook URL
3. **Log - Hold** node → URL → your Slack webhook URL

**daily_digest** (needs Claude key — skip for now or comment out the Ask Claude node):
1. **Get Bot Status** node → header `X-Control-Secret` → your secret
2. **Ask Claude** node → header `x-api-key` → your Anthropic key *(skip if no key)*
3. **Post to Slack** node → URL → your Slack webhook URL

> **Tip:** to quickly set Slack URL and secret across all nodes without the UI, use
> `sed` before importing — same approach as local setup.

```bash
# Replace placeholders in the files before scp-ing to VPS
sed -i 's|YOUR_CONTROL_API_SECRET|your-strong-secret-here|g' \
    n8n/opportunity_analyzer_local_test.json n8n/daily_digest.json
sed -i 's|https://hooks.slack.com/services/YOUR/SLACK/WEBHOOK|https://hooks.slack.com/services/T.../B.../xxx|g' \
    n8n/opportunity_analyzer_local_test.json n8n/daily_digest.json
```

### Step 5 — access n8n UI on VPS via SSH tunnel

n8n is bound to localhost on the VPS, so open an SSH tunnel:

```bash
# Run this on your local machine
ssh -L 15678:localhost:5678 user@your-vps

# Then open in browser
http://localhost:15678
```

### Step 6 — start the bot with integration enabled

Add these to the env block of your `screen` session start command. The easiest way
is an `.env` file loaded by `start.sh`, or export before starting:

```bash
screen -dmS arb bash -c '
  export N8N_WEBHOOK_ENABLED=true
  export N8N_WEBHOOK_URL=http://localhost:5678/webhook/arb-opportunity
  export CONTROL_API_ENABLED=true
  export CONTROL_API_HOST=127.0.0.1
  export CONTROL_API_PORT=8765
  export CONTROL_API_SECRET=your-strong-secret-here
  cd ~/csk-triangular-arb
  source .venv/bin/activate
  python main.py 2>&1 | tee -a logs/arb.log
'
```

If your `start.sh` already launches the screen session, export the vars before calling
it, or add them directly to the script.

### Step 7 — activate workflows and verify

```bash
# From VPS
curl -s http://localhost:5678/healthz   # n8n alive
curl -sf -H "X-Control-Secret: your-strong-secret-here" http://localhost:8765/health  # bot alive

# Manual test — fire a synthetic opportunity event
curl -X POST http://localhost:5678/webhook/arb-opportunity \
  -H "Content-Type: application/json" \
  -d '{"event":"opportunity","symbol":"BTC","direction":"INR_CHEAP","profit_pct":0.028,"inr_delta":500,"min_spread_pct":0.015}'
```

Check Slack for the notification within ~5 seconds.

---

## Keeping n8n running across VPS reboots (no sudo)

Add the n8n start command to your `~/.bashrc` or `~/.profile` — but this only fires
on login shells, not reboots.

For reboots, add a crontab entry:

```bash
crontab -e
```

Add:
```
@reboot sleep 30 && screen -dmS n8n bash -c 'N8N_HOST=127.0.0.1 n8n start >> ~/n8n.log 2>&1'
@reboot sleep 45 && screen -dmS arb bash -c 'cd ~/csk-triangular-arb && export N8N_WEBHOOK_ENABLED=true && export N8N_WEBHOOK_URL=http://localhost:5678/webhook/arb-opportunity && export CONTROL_API_ENABLED=true && export CONTROL_API_HOST=127.0.0.1 && export CONTROL_API_PORT=8765 && export CONTROL_API_SECRET=your-secret && source .venv/bin/activate && python main.py >> logs/arb.log 2>&1'
```

The `sleep` values give the OS time to bring up networking before the processes start.
Start n8n first (30s) then the bot (45s) so n8n is ready to receive the first webhook.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Webhook returns 404 | Workflow not active | Open n8n UI, toggle workflow ON |
| Threshold not changing | Control API not reachable from n8n | Check `CONTROL_API_HOST`; use `0.0.0.0` if n8n is in Docker |
| No Slack messages | Expression error in Slack node | Open n8n Executions tab, find the errored run, expand the failed node |
| Claude returns bad JSON | Model occasionally wraps in markdown | Parse node strips ` ```json ` fences — should self-heal |
| n8n exits on VPS | OOM or process killed | Check `~/n8n.log`; increase VPS RAM or add swap |
| Screen session gone after reboot | No @reboot crontab | Add the crontab entries from the section above |
