@echo off
REM ============================================================
REM  O3zone - double-click launcher for the web UI
REM
REM  Why this file exists: run.cmd is the power-user entry point
REM  (it forwards flags to the CLI). A student who just wants to
REM  chat should not have to remember "--web" - this one takes no
REM  arguments: it starts the local server and opens the browser.
REM  Closing this window stops the server.
REM
REM  run.cmd itself is unchanged; everything here is additive.
REM
REM  ASCII only: cmd.exe reads .cmd files in the OEM codepage,
REM  so CJK text in here would break on some machines. All the
REM  user-facing Chinese comes from the Python side instead.
REM ============================================================
setlocal
set PYTHONUTF8=1
title O3zone
cd /d "%~dp0"

REM -- 1. Start the guided server on a free port. ------------------
REM Port zero asks the OS to bind an available port atomically. The
REM Python server reads the real port and opens that URL in the browser.
REM Never probe port 8787 and assume a listener there is O3zone.

REM Always use the base interpreter for this guided entry point. It can
REM then check or repair .venv without trying to replace the interpreter
REM that is currently executing it. A complete venv is activated before
REM the chat APIs unlock; declining setup still opens the normal Web UI.

REM Prefer the official py launcher: it avoids the fake python.exe
REM app-execution alias inside WindowsApps.
py -3 -X utf8 -m harness.cli --web --guided-venv --port 0
if errorlevel 9009 (
    python -X utf8 -m harness.cli --web --guided-venv --port 0
    if errorlevel 9009 (
        echo.
        echo [ERROR] No usable Python found. Install Python 3.10+ first.
        pause
        exit /b 2
    )
)

:done
if errorlevel 1 (
    echo.
    echo [!] The server exited with an error - read the message above.
)
echo.
echo This window can be closed now; closing it stops the server.
pause >nul
exit /b 0
