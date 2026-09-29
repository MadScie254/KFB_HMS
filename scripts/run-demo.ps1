# Bring up the fictional demonstration environment in one command.
#
# Creates a virtual environment if there is not one, installs the locked
# dependencies, applies migrations, seeds the fictional demo data and starts
# the server. Safe to re-run: seeding is idempotent.
#
# This is the demo environment only. It refuses to touch a production
# configuration, because it seeds fictional patients and prices.
$ErrorActionPreference = "Stop"
$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $ProjectRoot

$HostName = if ($env:KFB_HOST) { $env:KFB_HOST } else { "127.0.0.1" }
$Port = if ($env:KFB_PORT) { $env:KFB_PORT } else { "8000" }

if ($env:KFB_ENV -and $env:KFB_ENV -ne "demo") {
    throw "Refusing to run: KFB_ENV is '$($env:KFB_ENV)'. This script seeds fictional demonstration data and must not touch production."
}
$env:KFB_ENV = "demo"
if (-not $env:KFB_ALLOWED_HOSTS) { $env:KFB_ALLOWED_HOSTS = "$HostName,localhost,127.0.0.1" }

$PythonPath = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $PythonPath)) {
    Write-Host "==> Creating the virtual environment"
    $Launcher = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
    & $Launcher -m venv .venv
}

Write-Host "==> Installing locked dependencies"
& $PythonPath -m pip install --quiet --upgrade pip
& $PythonPath -m pip install --quiet -r requirements.lock

Write-Host "==> Applying migrations"
& $PythonPath manage.py migrate --noinput

Write-Host "==> Seeding fictional demonstration data"
& $PythonPath manage.py seed_demo

Write-Host @"

  Kingdom Faith Based Hospital - demonstration environment

  Open   http://${HostName}:${Port}

  Sign in with any of these; the password is the same for all of them.
  Password: Demo-Only-2026!

    owner.demo        the brief, reports, stock intelligence, everything read-only
    pharmacy.demo     receive a delivery, issue stock to a ward, propose a write-off
    procurement.demo  purchase requests and deliveries
    nurse.demo        account for ward stock that was issued
    reviewer.demo     approve stock counts, write-offs and credit notes
    reception.demo    register patients, take payment
    clinician.demo    the clinical queue and notes

  Worth trying, in this order:
    1. procurement.demo  Deliveries      receive the waiting order, attach any photo
    2. pharmacy.demo     Deliveries      check that delivery; you cannot check your own
    3. pharmacy.demo     Ward custody    issue stock to a ward
    4. nurse.demo        Ward custody    say what happened to it
    5. reviewer.demo     Write-offs      approve what pharmacy proposed
    6. owner.demo        Today's brief   see what all of that surfaced

  All data is fictional. Stop the server with Ctrl+C.

"@

& $PythonPath manage.py runserver "${HostName}:${Port}"
