# Local Chat Harness launcher (PowerShell flavour, same as run.cmd).
#
# Usage:
#   .\run.ps1 --setup               create .venv and install deps
#   .\run.ps1 --web                 open the web UI
#   .\run.ps1                       interactive chat
#   .\run.ps1 --once "hello"        run a single turn
#   .\run.ps1 --memories            show the memory store
#   .\run.ps1 --sessions            list sessions
#
# This file must stay PURE ASCII, for the same reason run.cmd must:
# Windows PowerShell 5.1 reads a .ps1 without a BOM as ANSI, so non-ASCII
# comments get mis-decoded and can break the parser (hit this for real).
#
# There is no hard-coded path here: the project's own .venv wins,
# otherwise we fall back to the py launcher / python on PATH.

# Functions must be defined BEFORE use - PowerShell executes statements
# top-down, so a function defined further down is not callable yet.
function Resolve-BasePython {
    # Prefer the py launcher: it points at the real official install and
    # avoids the fake python.exe app-execution alias inside WindowsApps.
    foreach ($name in @("py", "python", "python3")) {
        $found = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($found) { return $found.Source }
    }
    return $null
}

$ErrorActionPreference = "Stop"

$repo = $PSScriptRoot
Push-Location $repo
try {
    $env:PYTHONUTF8 = "1"
    $venvPython = Join-Path $repo ".venv\Scripts\python.exe"

    # --setup must be handled before the venv shortcut below, otherwise it
    # would be forwarded to the CLI as an unknown flag.
    if ($args.Count -gt 0 -and $args[0] -eq "--setup") {
        Write-Host "[setup] Creating .venv ..."
        $base = Resolve-BasePython
        if (-not $base) {
            Write-Host "[ERROR] No Python found. Install Python 3.10+ and add it to PATH." -ForegroundColor Red
            exit 2
        }
        & $base -m venv .venv
        if (-not (Test-Path $venvPython)) {
            Write-Host "[ERROR] Could not create .venv." -ForegroundColor Red
            exit 2
        }
        Write-Host "[setup] Installing requirements.txt ..."
        & $venvPython -m pip install --disable-pip-version-check -r requirements.txt
        if ($LASTEXITCODE -ne 0) {
            Write-Host "[ERROR] Dependency install failed. Check your network / proxy." -ForegroundColor Red
            exit 1
        }
        Write-Host ""
        Write-Host "[setup] Done. Now run:  .\run.ps1 --web"
        exit 0
    }

    # The project-local venv wins when present: the web tools need
    # httpx/trafilatura/lxml, and we do not want to install those into the
    # user's global Python.
    if (Test-Path $venvPython) {
        & $venvPython -X utf8 -m harness.cli @args
        exit $LASTEXITCODE
    }

    Write-Host "[warn] .venv not found - web_search / web_open_url will be unavailable." -ForegroundColor Yellow
    Write-Host "[warn] Run '.\run.ps1 --setup' once to install dependencies." -ForegroundColor Yellow

    $python = Resolve-BasePython
    if (-not $python) {
        Write-Host "[ERROR] No usable Python found. Install Python 3.10+ first." -ForegroundColor Red
        exit 2
    }
    & $python -X utf8 -m harness.cli @args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
