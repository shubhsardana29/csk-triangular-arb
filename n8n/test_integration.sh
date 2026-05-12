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
#!/usr/bin/env bash
# test_integration.sh — end-to-end local test for the n8n integration

set -e

WEBHOOK_URL="${N8N_WEBHOOK_URL:-http://localhost:43821/webhook/arb-opportunity}"
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

ORIG_THRESHOLD=$(echo "$STATUS" | python3 -c "import sys,json; print(json.load(sys.stdin)['min_spread_pct'])" 2>/dev/null || echo "0.015")

# ── 2. Auth rejection test ────────────────────────────────────────────────────

sep
echo "3. Auth rejection (wrong secret)"

STATUS_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    -H "X-Control-Secret: wrong" \
    "$CONTROL_URL/status")

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

# ── 4. n8n webhook → control API ──────────────────────────────────────────────

sep
echo "5. n8n webhook pipeline test"

BEFORE=$(curl -sf -H "X-Control-Secret: $SECRET" "$CONTROL_URL/status" | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['min_spread_pct'])")

echo "Threshold before: $BEFORE"

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

echo "Waiting 5s for n8n..."
sleep 5

AFTER=$(curl -sf -H "X-Control-Secret: $SECRET" "$CONTROL_URL/status" | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['min_spread_pct'])")

echo "Threshold after: $AFTER"

sep
echo "Final bot config"

curl -sf -H "X-Control-Secret: $SECRET" "$CONTROL_URL/status" | \
    python3 -m json.tool 2>/dev/null

echo ""
echo "n8n UI:"
echo "http://localhost:43821"
echo ""