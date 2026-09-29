#!/usr/bin/env bash
# Bring up the fictional demonstration environment in one command.
#
# Creates a virtual environment if there is not one, installs the locked
# dependencies, applies migrations, seeds the fictional demo data and starts
# the server. Safe to re-run: seeding is idempotent.
#
# This is the demo environment only. It refuses to touch a production
# configuration, because it seeds fictional patients and prices.
set -euo pipefail

cd "$(dirname "$0")/.."
HOST="${KFB_HOST:-127.0.0.1}"
PORT="${KFB_PORT:-8000}"

if [ "${KFB_ENV:-demo}" != "demo" ]; then
    echo "Refusing to run: KFB_ENV is '${KFB_ENV}'." >&2
    echo "This script seeds fictional demonstration data and must not touch production." >&2
    exit 1
fi
export KFB_ENV=demo
export KFB_ALLOWED_HOSTS="${KFB_ALLOWED_HOSTS:-$HOST,localhost,127.0.0.1}"

if [ ! -d .venv ]; then
    echo "==> Creating the virtual environment"
    "${PYTHON:-python3}" -m venv .venv
fi
PY=".venv/bin/python"

echo "==> Installing locked dependencies"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.lock

echo "==> Applying migrations"
"$PY" manage.py migrate --noinput

echo "==> Seeding fictional demonstration data"
"$PY" manage.py seed_demo

cat <<BANNER

  Kingdom Faith Based Hospital — demonstration environment

  Open   http://${HOST}:${PORT}

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

BANNER

exec "$PY" manage.py runserver "${HOST}:${PORT}"
