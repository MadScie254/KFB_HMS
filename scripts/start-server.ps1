$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$EnvironmentName = [Environment]::GetEnvironmentVariable("KFB_ENV")
if ([string]::IsNullOrWhiteSpace($EnvironmentName)) {
    throw "Production startup refused: KFB_ENV must be set explicitly to production."
}
if ($EnvironmentName.Trim().ToLowerInvariant() -ne "production") {
    throw "Production startup refused: KFB_ENV must be production. Use run-demo.ps1 for the demonstration environment."
}
$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonPath)) { throw "Virtual environment not found. Complete installation first." }
Set-Location $ProjectRoot
if ([Environment]::GetEnvironmentVariable("KFB_SECURE_SSL_REDIRECT") -ne "1") {
    throw "Production startup refused: KFB_SECURE_SSL_REDIRECT must be 1 and TLS must terminate at Caddy."
}
& $PythonPath -m waitress --listen=127.0.0.1:8000 --trusted-proxy=127.0.0.1 --trusted-proxy-headers=x-forwarded-proto kfb_hms.wsgi:application
