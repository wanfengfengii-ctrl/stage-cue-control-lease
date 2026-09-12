#!/usr/bin/env bash
# One-shot acceptance: backend race/boundary tests, then dual-browser E2E.
set -euo pipefail

echo "==> waiting for API / web ..."
for i in $(seq 1 60); do
  if curl -fsS http://web/health >/dev/null 2>&1; then break; fi
  sleep 1
done
curl -fsS http://web/health
echo

echo "==> [1/3] pytest: transaction contention + expiry boundary (real PostgreSQL)"
cd /accept/backend
/opt/venv/bin/python -m pytest -q

echo "==> [2/3] Vitest: countdown boundary + takeover/old-token UI logic"
cd /accept/frontend
npm run test:unit

echo "==> [3/3] Playwright: dual-browser handover (real FastAPI + PostgreSQL)"
npx playwright test

echo
echo "ALL ACCEPTANCE CHECKS PASSED"
