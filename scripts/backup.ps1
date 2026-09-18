param([string]$OutputDirectory = "")
$ErrorActionPreference = "Stop"
$PythonPath = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonPath)) { throw "Virtual environment not found. Complete installation first." }
if ($OutputDirectory) {
    & $PythonPath (Join-Path $PSScriptRoot "..\manage.py") backup_kfb --output $OutputDirectory
} else {
    & $PythonPath (Join-Path $PSScriptRoot "..\manage.py") backup_kfb
}
if ($LASTEXITCODE -ne 0) { throw "Backup failed." }

