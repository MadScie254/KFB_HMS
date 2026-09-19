#!/usr/bin/env bash
# The exact checks CI runs, runnable on any machine.
#
# The hosted runner has never once executed them: every GitHub Actions run in
# this repository has failed in seconds without a runner because the account
# is billing-locked. Until that is cleared, this script is the only way anyone
# can verify a change, so it is kept identical to .github/workflows/quality.yml
# and a test pins the two together.
set -euo pipefail

PYTHON="${PYTHON:-python3}"
cd "$(dirname "$0")/.."

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
run "deployment check" env KFB_SECRET_KEY=ci-only-key KFB_ALLOWED_HOSTS=localhost "$PYTHON" manage.py check --deploy

printf '\n'
if [ "$failures" -ne 0 ]; then
    printf '\033[31m%s check(s) failed.\033[0m\n' "$failures"
    exit 1
fi
printf '\033[32mAll checks passed.\033[0m\n'
