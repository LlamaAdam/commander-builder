#!/usr/bin/env sh
# Local one-command runner for the full walkthrough (2026-10-02).
#
# Runs tests/e2e/full-walkthrough.spec.js in the INSTALLED Chrome against
# an app that is ALREADY running (the desktop launcher or
# `python -m commander_builder.web`), then commits e2e-results/ and
# pushes it on the current branch so the checklist + screenshots travel
# back to whoever is reviewing. Forge rows run for real here because the
# local vendor/forge exists.
#
#   E2E_BASE_URL   where the app is (default http://127.0.0.1:5000, the
#                  `python -m commander_builder.web` default; the desktop
#                  window picks a free port — read it from its address bar)
#   E2E_KEEP_DECKS=1  keep the decks the run creates instead of deleting them
set -eu
cd "$(dirname "$0")/.."

: "${E2E_BASE_URL:=http://127.0.0.1:5000}"
export E2E_BASE_URL E2E_CHROME=1

if ! node -e "fetch(process.env.E2E_BASE_URL + '/api/health').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"; then
  echo "App not reachable at $E2E_BASE_URL — start Commander Builder first (or set E2E_BASE_URL to its address)."
  exit 1
fi

if [ ! -d node_modules ]; then
  npm ci --no-audit --no-fund
fi

# The spec never fails on a row; a non-zero exit here means the browser
# or the harness itself broke. Keep going so the partial checklist is
# still committed.
set +e
npx playwright test --config playwright.full.config.js tests/e2e/full-walkthrough.spec.js
rc=$?
set -e

if [ -f e2e-results/CHECKLIST.md ]; then
  git add e2e-results
  if git commit -q -m "chore: local walkthrough results $(date -u +%Y-%m-%dT%H:%MZ)"; then
    git push
  else
    echo "nothing new to commit in e2e-results/"
  fi
  echo
  cat e2e-results/CHECKLIST.md
else
  echo "no e2e-results/CHECKLIST.md was written (playwright exit $rc)"
  exit 1
fi
exit 0
