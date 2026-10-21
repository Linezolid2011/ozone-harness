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

REM -- 1. Already running? Then just open the UI. ------------------
REM Double-clicking twice is the most likely user action, and a
REM second server would refuse to bind the port and print an error
REM that looks like a failure. Check the port first (plain
REM PowerShell, no extra tools), and if something is listening,
REM open the browser and leave that instance alone.
powershell -NoProfile -Command "try { $c = New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1', 8787); $c.Close(); exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 (
    echo [info] The web UI is already running on port 8787.
    echo [info] Opening the browser - the existing window keeps serving.
    start "" "http://127.0.0.1:8787/"
    timeout /t 2 >nul
    exit /b 0
)

REM -- 2. Start the server. ---------------------------------------
REM The project-local venv wins when present: web search and image
REM attachments need httpx / trafilatura / lxml / Pillow / OCR, and
REM those should never be installed into the system Python.
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -X utf8 -m harness.cli --web
    goto done
)

echo [warn] .venv not found - web search and image attachments will be unavailable.
echo [warn] Run "run.cmd --setup" once to install the dependencies.
echo.

REM Prefer the official py launcher: it avoids the fake python.exe
REM app-execution alias inside WindowsApps.
py -3 -X utf8 -m harness.cli --web
if errorlevel 9009 (
    python -X utf8 -m harness.cli --web
    if errorlevel 9009 (
        echo.
        echo [ERROR] No usable Python found. Install Python 3.10+ first,
        echo         then run "run.cmd --setup" once.
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
