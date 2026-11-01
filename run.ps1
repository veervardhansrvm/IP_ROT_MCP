# Usage:
#   .\run.ps1          start the MCP server on http://localhost:8080/mcp
#   .\run.ps1 smoke    live check: IP rotation + each search tool
#   .\run.ps1 test     unit tests (no network, no secrets)
# First run creates .venv; settings come from .env in this folder.
param([string]$Mode = "serve")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating .venv ..."
    py -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -q -r requirements-dev.txt
}
if ($Mode -ne "test" -and -not (Test-Path ".env")) {
    Write-Host "No .env found. Copy .env.example to .env and fill it in." -ForegroundColor Yellow
    exit 1
}

switch ($Mode) {
    "serve" { .\.venv\Scripts\python.exe -m bd_search_mcp.server }
    "smoke" { .\.venv\Scripts\python.exe smoke.py }
    "test"  { .\.venv\Scripts\python.exe -m pytest -q }
    default { Write-Host "Unknown mode '$Mode' (serve | smoke | test)"; exit 2 }
}
exit $LASTEXITCODE
