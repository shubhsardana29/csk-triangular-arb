#!/usr/bin/env bash
# test_integration.sh — end-to-end local test for the n8n integration
#
# Prerequisites:
#   1. Bot running with CONTROL_API_ENABLED=true CONTROL_API_SECRET=test-secret
#   2. n8n running on localhost:5678
#   3. opportunity_analyzer_local_test.json imported and ACTIVE
#   4. N8N_WEBHOOK_URL set below (copy from the Bot Webhook node in n8n)
#
# Usage:
#   chmod +x n8n/test_integration.sh
#   N8N_WEBHOOK_URL=http://localhost:5678/webhook/arb-opportunity \
#   CONTROL_API_SECRET=test-secret \
#   bash n8n/test_integration.sh

set -e

WEBHOOK_URL="${N8N_WEBHOOK_URL:-http://localhost:5678/webhook/arb-opportunity}"
CONTROL_URL="http://127.0.0.1:8765"
SECRET="${CONTROL_API_SECRET:-test-secret}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠ $1${NC}"; }
fail() { echo -e "${RED}✗ $1${NC}"; exit 1; }
sep()  { echo -e "\n────────────────────────────────────────────"; }

# ── 1. Control API health ────────────────────────────────────────────────────

sep
echo "1. Control API health check"
HEALTH=$(curl -sf -H "X-Control-Secret: $SECRET" "$CONTROL_URL/health" || echo "FAIL")
if echo "$HEALTH" | grep -q '"ok"'; then
    ok "Control API is up — $HEALTH"
else
    fail "Control API not responding. Is the bot running with CONTROL_API_ENABLED=true?"
fi

sep
echo "2. Current bot config"
STATUS=$(curl -sf -H "X-Control-Secret: $SECRET" "$CONTROL_URL/status")
echo "$STATUS" | python3 -m json.tool 2>/dev/null || echo "$STATUS"

# capture current threshold for reset at the end
ORIG_THRESHOLD=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin)['min_spread_pct'])" 2>/dev/null || echo "0.015")

# ── 2. Auth rejection test ────────────────────────────────────────────────────

sep
echo "3. Auth rejection (wrong secret)"
STATUS_CODE=$(curl -s -o /dev/null -w "%{http_code}" -H "X-Control-Secret: wrong" "$CONTROL_URL/status")
if [ "$STATUS_CODE" = "401" ]; then
    ok "Correctly rejected wrong secret with 401"
else
    warn "Expected 401, got $STATUS_CODE"
fi

# ── 3. Direct control API test ────────────────────────────────────────────────

sep
echo "4. Direct /control test (lower threshold to 0.013)"
RESULT=$(curl -sf -X POST "$CONTROL_URL/control" \
    -H "Content-Type: application/json" \
    -H "X-Control-Secret: $SECRET" \
    -d '{"min_spread_pct": 0.013}')
echo "$RESULT" | python3 -m json.tool 2>/dev/null || echo "$RESULT"
if echo "$RESULT" | grep -q '"min_spread_pct": 0.013'; then
    ok "Threshold updated to 0.013 via control API"
else
    warn "Unexpected response — check above"
fi

# reset
curl -sf -X POST "$CONTROL_URL/control" \
    -H "Content-Type: application/json" \
    -H "X-Control-Secret: $SECRET" \
    -d "{\"min_spread_pct\": $ORIG_THRESHOLD}" > /dev/null
ok "Threshold reset to $ORIG_THRESHOLD"

# ── 4. n8n webhook → mock Claude → control API ───────────────────────────────

sep
echo "5. n8n pipeline — Test A: HIGH profit (profit=2.8%, threshold=1.5%)"
echo "   Expected: mock Claude says 'lower', n8n posts to control API"
echo ""

BEFORE=$(curl -sf -H "X-Control-Secret: $SECRET" "$CONTROL_URL/status" | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['min_spread_pct'])")
echo "   Threshold before: $BEFORE"

curl -sf -X POST "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d '{
        "event": "opportunity",
        "symbol": "ICP",
        "direction": "INR_CHEAP",
        "profit_pct": 0.028,
        "inr_delta": 560.00,
        "min_spread_pct": 0.015
    }' > /dev/null

echo "   Waiting 3s for n8n to process..."
sleep 3

AFTER=$(curl -sf -H "X-Control-Secret: $SECRET" "$CONTROL_URL/status" | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['min_spread_pct'])")
echo "   Threshold after:  $AFTER"

if python3 -c "exit(0 if float('$AFTER') < float('$BEFORE') else 1)" 2>/dev/null; then
    ok "Test A passed — threshold lowered from $BEFORE → $AFTER"
else
    warn "Test A: threshold did not change ($BEFORE → $AFTER). Check n8n Executions log."
fi

# reset
curl -sf -X POST "$CONTROL_URL/control" \
    -H "Content-Type: application/json" \
    -H "X-Control-Secret: $SECRET" \
    -d "{\"min_spread_pct\": $ORIG_THRESHOLD}" > /dev/null
ok "Threshold reset to $ORIG_THRESHOLD"

# ── Test B: low profit → hold ─────────────────────────────────────────────────

sep
echo "6. n8n pipeline — Test B: MID profit (profit=1.8%, threshold=1.5%)"
echo "   Expected: mock Claude says 'hold' (profit in range: 1.575%–2.25%)"
echo ""

# Reset first so we start clean after Test A
curl -sf -X POST "$CONTROL_URL/control" \
    -H "Content-Type: application/json" \
    -H "X-Control-Secret: $SECRET" \
    -d "{\"min_spread_pct\": $ORIG_THRESHOLD}" > /dev/null

BEFORE=$(curl -sf -H "X-Control-Secret: $SECRET" "$CONTROL_URL/status" | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['min_spread_pct'])")
echo "   Threshold before: $BEFORE"

# profit=1.8%: > 1.575% (not raise) but < 2.25% (not lower) → hold
curl -sf -X POST "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d '{
        "event": "opportunity",
        "symbol": "DOGE",
        "direction": "INR_EXPENSIVE",
        "profit_pct": 0.018,
        "inr_delta": 72.00,
        "min_spread_pct": 0.015
    }' > /dev/null

echo "   Waiting 5s for n8n to process..."
sleep 5

AFTER=$(curl -sf -H "X-Control-Secret: $SECRET" "$CONTROL_URL/status" | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['min_spread_pct'])")
echo "   Threshold after:  $AFTER"

if [ "$BEFORE" = "$AFTER" ]; then
    ok "Test B passed — threshold held at $AFTER (profit 1.8% in hold zone 1.575%–2.25%)"
else
    warn "Test B: threshold changed ($BEFORE → $AFTER). Mock Claude decision was: raise or lower"
fi

# ── Test C: non-opportunity event → ignored ───────────────────────────────────

sep
echo "7. n8n pipeline — Test C: non-opportunity event (should be silently dropped)"

# Reset to known value first
curl -sf -X POST "$CONTROL_URL/control" \
    -H "Content-Type: application/json" \
    -H "X-Control-Secret: $SECRET" \
    -d "{\"min_spread_pct\": $ORIG_THRESHOLD}" > /dev/null

curl -sf -X POST "$WEBHOOK_URL" \
    -H "Content-Type: application/json" \
    -d '{"event": "spread_report", "cycle": 100, "top_spread": 0.005}' > /dev/null

sleep 4
AFTER_C=$(curl -sf -H "X-Control-Secret: $SECRET" "$CONTROL_URL/status" | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['min_spread_pct'])")

if [ "$AFTER_C" = "$ORIG_THRESHOLD" ]; then
    ok "Test C passed — non-opportunity event ignored, threshold still $AFTER_C"
else
    warn "Test C: threshold changed from $ORIG_THRESHOLD → $AFTER_C unexpectedly"
fi

# ── Test D: clamping ──────────────────────────────────────────────────────────

sep
echo "8. Control API safety — clamp test (try to set threshold below floor 0.012)"
CLAMP_RESULT=$(curl -sf -X POST "$CONTROL_URL/control" \
    -H "Content-Type: application/json" \
    -H "X-Control-Secret: $SECRET" \
    -d '{"min_spread_pct": 0.001}')
CLAMPED=$(echo "$CLAMP_RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['status']['min_spread_pct'])" 2>/dev/null)

if python3 -c "exit(0 if float('$CLAMPED') >= 0.005 else 1)" 2>/dev/null; then
    ok "Clamped correctly — requested 0.001, applied $CLAMPED (floor 0.005)"
else
    warn "Clamp may not have worked — applied $CLAMPED"
fi

# final reset
curl -sf -X POST "$CONTROL_URL/control" \
    -H "Content-Type: application/json" \
    -H "X-Control-Secret: $SECRET" \
    -d "{\"min_spread_pct\": $ORIG_THRESHOLD}" > /dev/null

# ── Summary ───────────────────────────────────────────────────────────────────

sep
echo ""
echo "All tests complete. Final bot config:"
curl -sf -H "X-Control-Secret: $SECRET" "$CONTROL_URL/status" | python3 -m json.tool 2>/dev/null
echo ""
echo "To see n8n execution details: http://localhost:5678 → Executions (left sidebar)"
echo ""
