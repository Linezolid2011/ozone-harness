@echo off
REM ============================================================
REM  Local Chat Harness launcher (ASCII only - cmd.exe reads .cmd
REM  files in the OEM codepage, so non-ASCII comments break it).
REM
REM  Usage:
REM    run.cmd --setup                   create .venv and install deps
REM    run.cmd --test                    run the test suite (uses .venv)
REM    run.cmd --web                     open the web UI
REM    run.cmd                           interactive chat
REM    run.cmd --once "hello"            run a single turn
REM    run.cmd --search "keywords"       try the search backend (no LLM call)
REM    run.cmd --sessions                list sessions
REM    run.cmd --memories                show the memory store
REM    run.cmd --usage                   show token usage
REM    run.cmd --replay <session-id>     replay a session (real prompts)
REM    run.cmd --vault                   inspect the dream vault (README + files)
REM    run.cmd --vault-init              create an empty vault skeleton
REM    run.cmd --dream                   consolidate chat records into the vault (one-way)
REM    run.cmd --dream-format            wipe + reinitialize ONLY the dream layer (asks twice)
REM    run.cmd --dreams                  list past consolidation runs
REM    run.cmd --dream-review <run-id>   what a run changed
REM    run.cmd --attachments-gc          report orphan images (--yes to delete them)
REM    run.cmd --show-prompt             print the full prompt each turn
REM    run.cmd --web                     UI language is in Settings -> Language (zh/en);
REM                                      it also switches the prompts the model sees.
REM                                      HARNESS_LANGUAGE only seeds a brand-new data dir.
REM ============================================================
setlocal
set PYTHONUTF8=1
cd /d "%~dp0"

REM --setup and --test must be handled BEFORE the venv shortcut below,
REM otherwise they would be forwarded to the CLI as unknown flags.
if /I "%~1"=="--setup" goto setup
if /I "%~1"=="--test" goto test

REM A project-local venv wins when present: the web tools need
REM httpx/trafilatura/lxml, and we do not want to install those
REM into the user's global Python.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -X utf8 -m harness.cli %*
    exit /b %errorlevel%
)

REM No venv: warn that the web tools will be unavailable, but still
REM start - memory-only usage works fine without any dependency.
echo [warn] .venv not found - web_search / web_open_url will be unavailable.
echo [warn] Run "run.cmd --setup" once to install dependencies.

REM Prefer the official py launcher: it avoids the fake python.exe
REM app-execution alias inside WindowsApps.
py -3 -X utf8 -m harness.cli %*
if errorlevel 9009 (
    python -X utf8 -m harness.cli %*
    if errorlevel 9009 (
        echo.
        echo [ERROR] No usable Python found. Install Python 3.10+ first.
        exit /b 2
    )
)
exit /b %errorlevel%

:setup
echo [setup] Creating .venv ...
py -3 -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Could not create .venv. Install Python 3.10+ first.
    exit /b 2
)
echo [setup] Installing dependencies from requirements.txt ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency install failed. Check your network / proxy.
    exit /b 1
)
echo.
echo [setup] Done. Now run:  run.cmd --web
exit /b 0

:test
REM Always use the interpreter that actually has the dependencies.
REM With the system Python the web-tool tests are SKIPPED and the run still
REM prints "OK" - so the summary would look like a full pass while ~30 tests
REM never ran. That silent gap is exactly what this shortcut exists to avoid.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -X utf8 -m unittest discover -s tests -t .
    exit /b %errorlevel%
)
echo [warn] .venv not found: about 30 web-tool tests will be SKIPPED, not failed.
echo [warn] The summary will still say OK - run "run.cmd --setup" for the full suite.
echo.
py -3 -X utf8 -m unittest discover -s tests -t .
if errorlevel 9009 python -X utf8 -m unittest discover -s tests -t .
exit /b %errorlevel%
