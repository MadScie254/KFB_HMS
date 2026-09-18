$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonPath)) { throw "Virtual environment not found. Complete installation first." }
Set-Location $ProjectRoot
& $PythonPath -m waitress --listen=0.0.0.0:8000 kfb_hms.wsgi:application

