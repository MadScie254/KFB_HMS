param([string]$OutputDirectory = "", [int]$RetentionDays = 365)
$ErrorActionPreference = "Stop"
if ($env:KFB_ENV -ne "production") { throw "KFB_ENV=production is required for login evidence retention." }
if (-not $OutputDirectory) { $OutputDirectory = $env:KFB_BACKUP_DIRECTORY }
if (-not $OutputDirectory) { throw "Set OutputDirectory or KFB_BACKUP_DIRECTORY for encrypted evidence exports." }
$PythonPath = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonPath)) { throw "Virtual environment not found. Complete installation first." }
& $PythonPath (Join-Path $PSScriptRoot "..\manage.py") prune_login_attempts --output $OutputDirectory --retention-days $RetentionDays
if ($LASTEXITCODE -ne 0) { throw "Login evidence export/prune failed." }
