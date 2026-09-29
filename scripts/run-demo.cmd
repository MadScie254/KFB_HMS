@echo off
REM Bring up the fictional demonstration environment from a Command Prompt.
REM
REM PowerShell users can run run-demo.ps1 directly; this wrapper exists because
REM cmd.exe cannot execute a .ps1 file, and Command Prompt is what the Start
REM menu still opens by default on many machines.
setlocal

cd /d "%~dp0.."

if defined KFB_ENV if /I not "%KFB_ENV%"=="demo" (
    echo Refusing to run: KFB_ENV is "%KFB_ENV%".
    echo This script seeds fictional demonstration data and must not touch production.
    exit /b 1
)
set KFB_ENV=demo

if not defined KFB_HOST set KFB_HOST=127.0.0.1
if not defined KFB_PORT set KFB_PORT=8000
if not defined KFB_ALLOWED_HOSTS set KFB_ALLOWED_HOSTS=%KFB_HOST%,localhost,127.0.0.1

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo ==^> Creating the virtual environment
    py -m venv .venv || python -m venv .venv || goto :failed
)

echo ==^> Installing locked dependencies
"%PY%" -m pip install --quiet --upgrade pip || goto :failed
"%PY%" -m pip install --quiet -r requirements.lock || goto :failed

echo ==^> Applying migrations
"%PY%" manage.py migrate --noinput || goto :failed

echo ==^> Seeding fictional demonstration data
"%PY%" manage.py seed_demo || goto :failed

echo.
echo   Kingdom Faith Based Hospital - demonstration environment
echo.
echo   Open   http://%KFB_HOST%:%KFB_PORT%
echo.
echo   Password for every account below: Demo-Only-2026!
echo.
echo     owner.demo        the brief, reports, stock intelligence
echo     pharmacy.demo     receive a delivery, issue to a ward, propose a write-off
echo     procurement.demo  purchase requests and deliveries
echo     nurse.demo        account for ward stock that was issued
echo     reviewer.demo     approve stock counts and write-offs
echo     reception.demo    register patients, take payment
echo     clinician.demo    the clinical queue and notes
echo.
echo   Worth trying, in this order:
echo     1. procurement.demo  Deliveries      receive the waiting order, attach any photo
echo     2. pharmacy.demo     Deliveries      check it; you cannot check your own
echo     3. pharmacy.demo     Ward custody    issue stock to a ward
echo     4. nurse.demo        Ward custody    say what happened to it
echo     5. reviewer.demo     Write-offs      approve what pharmacy proposed
echo     6. owner.demo        Today's brief   see what all of that surfaced
echo.
echo   All data is fictional. Stop the server with Ctrl+C.
echo.

"%PY%" manage.py runserver %KFB_HOST%:%KFB_PORT%
goto :eof

:failed
echo.
echo Startup failed. The step above reported the reason.
exit /b 1
