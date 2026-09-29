#!/usr/bin/env bash
# The exact checks CI runs, runnable on any machine.
#
# The hosted runner has never once executed them: every GitHub Actions run in
# this repository has failed in seconds without a runner because the account
# is billing-locked. Until that is cleared, this script is the only way anyone
# can verify a change, so it is kept identical to .github/workflows/quality.yml
# and a test pins the two together.
set -euo pipefail

cd "$(dirname "$0")/.."

# Prefer the project virtualenv that scripts/run-demo.sh creates, so these
# checks run against the locked dependencies rather than whatever the system
# interpreter happens to have. Override with PYTHON=... to use another one.
if [ -n "${PYTHON:-}" ]; then
    :
elif [ -x .venv/bin/python ]; then
    PYTHON=".venv/bin/python"
else
    PYTHON="python3"
fi
printf 'Using interpreter: %s\n' "$PYTHON"

failures=0
run() {
    local name="$1"; shift
    printf '\n\033[1m==> %s\033[0m\n' "$name"
    if "$@"; then
        printf '\033[32mPASS\033[0m %s\n' "$name"
    else
        printf '\033[31mFAIL\033[0m %s\n' "$name"
        failures=$((failures + 1))
    fi
}

run "ruff" "$PYTHON" -m ruff check hospital kfb_hms
run "tests" env KFB_ENV=demo "$PYTHON" manage.py test
run "migrations are in sync with the models" env KFB_ENV=demo "$PYTHON" manage.py makemigrations --check --dry-run
# Checked against a PRODUCTION configuration, not a demo one. Run in demo mode
# this step validates settings no hospital will ever use, so it could not fail
# on a production misconfiguration - which is the only thing it exists to catch.
run "deployment check (production configuration)" env \
    KFB_ENV=production \
    KFB_SECRET_KEY=ci-only-not-a-real-secret-0123456789abcdefghijklmnopqrstuvwxyz \
    KFB_DATABASE_URL=postgresql://ci:ci@127.0.0.1:5432/ci \
    KFB_ALLOWED_HOSTS=hospital.example.test \
    "$PYTHON" manage.py check --deploy --fail-level WARNING

printf '\n'
if [ "$failures" -ne 0 ]; then
    printf '\033[31m%s check(s) failed.\033[0m\n' "$failures"
    exit 1
fi
printf '\033[32mAll checks passed.\033[0m\n'
