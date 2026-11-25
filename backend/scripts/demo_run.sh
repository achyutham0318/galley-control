#!/bin/sh
set -e

command -v docker >/dev/null 2>&1 || { echo "docker is required"; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "curl is required"; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 is required"; exit 1; }

DEMO_MODE_ENABLED=true docker compose up -d --build

attempt=0
until curl -sf http://localhost:8000/api/v1/health >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "API failed to become healthy"
    exit 1
  fi
  sleep 2
done

curl -sf http://localhost:8000/docs >/dev/null
curl -sf http://localhost:8000/openapi.json >/dev/null

docker compose exec -T api python -m scripts.seed
docker compose exec -T api python -m scripts.seed

TOKEN=$(curl -sf -X POST http://localhost:8000/api/v1/auth/staff/pin-login \
  -H "Content-Type: application/json" \
  -d '{"staff_id":"MGR01","pin":"112233"}' \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['access_token'])")

echo "SEEDED_PINS: MGR01=112233 COUNTER01=445566 KITCHEN01=778899 STUDENT=Student@123"
printf "%-20s %-12s %-12s %-18s\n" "SCENARIO" "TOTAL_MS" "RISK_SCORE" "DECISION"
for item in \
  "normal_pickup|{}" \
  "qr_replay|{}" \
  "pre_prep_cancel|{}" \
  "serial_abuse|{\"surge_override\":true}" \
  "serial_abuse|{\"surge_override\":false}"
do
  scenario=${item%%|*}
  body=${item#*|}
  resp=$(curl -sf -X POST "http://localhost:8000/api/v1/demo/simulate/${scenario}" \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    -d "${body}")
  python3 -c "
import json, sys
d = json.loads(sys.argv[1])
fr = d['final_result']
dec = fr.get('decision') or fr.get('flash') or ''
risk = fr.get('risk_score')
risk_s = f'{risk:.2f}' if risk is not None else '-'
print(f\"{d['scenario']:<20} {d['total_latency_ms']:<12.2f} {risk_s:<12} {dec:<18}\")
" "$resp"
done
