$ErrorActionPreference = "Stop"

Push-Location $PSScriptRoot
try {
    if (Test-Path ".venv\Scripts\python.exe") {
        & ".venv\Scripts\python.exe" -m pytest -vv --tb=short
    } else {
        python -m pytest -vv --tb=short
    }
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
} finally {
    Pop-Location
}
