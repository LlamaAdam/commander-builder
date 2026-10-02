@echo off
rem Local one-command runner for the full walkthrough (2026-10-02).
rem
rem Runs tests\e2e\full-walkthrough.spec.js in the INSTALLED Chrome against
rem an app that is ALREADY running (the desktop launcher or
rem `python -m commander_builder.web`), then commits e2e-results\ and
rem pushes it on the current branch so the checklist + screenshots travel
rem back to whoever is reviewing. Forge rows run for real here because the
rem local vendor\forge exists.
rem
rem   E2E_BASE_URL     where the app is (default http://127.0.0.1:5000, the
rem                    `python -m commander_builder.web` default; the desktop
rem                    window picks a free port - read it from its address bar)
rem   E2E_KEEP_DECKS=1 keep the decks the run creates instead of deleting them
setlocal
cd /d "%~dp0\.."

if "%E2E_BASE_URL%"=="" set "E2E_BASE_URL=http://127.0.0.1:5000"
set "E2E_CHROME=1"

node -e "fetch(process.env.E2E_BASE_URL + '/api/health').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"
if errorlevel 1 (
  echo App not reachable at %E2E_BASE_URL% - start Commander Builder first ^(or set E2E_BASE_URL to its address^).
  exit /b 1
)

if not exist node_modules (
  call npm ci --no-audit --no-fund
  if errorlevel 1 exit /b 1
)

rem The spec never fails on a row; a non-zero exit here means the browser
rem or the harness itself broke. Keep going so the partial checklist is
rem still committed.
call npx playwright test --config playwright.full.config.js tests/e2e/full-walkthrough.spec.js
set "rc=%errorlevel%"

if not exist e2e-results\CHECKLIST.md (
  echo no e2e-results\CHECKLIST.md was written ^(playwright exit %rc%^)
  exit /b 1
)

git add e2e-results
git commit -q -m "chore: local walkthrough results %date% %time:~0,5%"
if errorlevel 1 (
  echo nothing new to commit in e2e-results\
) else (
  git push
)
echo.
type e2e-results\CHECKLIST.md
exit /b 0
