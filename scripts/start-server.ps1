$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonPath)) { throw "Virtual environment not found. Complete installation first." }
Set-Location $ProjectRoot
$EnvironmentName = [Environment]::GetEnvironmentVariable("KFB_ENV")
if ([string]::IsNullOrWhiteSpace($EnvironmentName)) { $EnvironmentName = "demo" }

if ($EnvironmentName.ToLowerInvariant() -ne "demo") {
    if ([Environment]::GetEnvironmentVariable("KFB_SECURE_SSL_REDIRECT") -ne "1") {
        throw "Production startup refused: KFB_SECURE_SSL_REDIRECT must be 1 and TLS must terminate at Caddy."
    }
    & $PythonPath -m waitress --listen=127.0.0.1:8000 --trusted-proxy=127.0.0.1 --trusted-proxy-headers=x-forwarded-proto kfb_hms.wsgi:application
} else {
    & $PythonPath -m waitress --listen=127.0.0.1:8000 kfb_hms.wsgi:application
}
