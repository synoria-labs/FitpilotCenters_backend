#!/usr/bin/env bash
# Run the test suite against the local development database.
#
# The JWT secrets live here rather than in .env because tests/test_security_hardening.py
# asserts that app.security.jwt refuses to import without them, and .env is read regardless
# of the process environment — putting them there would disable that check. Passing them as
# process environment keeps both things true: the modules that need secrets can import, and
# the fail-fast test (which spawns a subprocess with a cleared environment) still bites.
#
#   ./scripts/dev_test.sh                     # everything
#   ./scripts/dev_test.sh tests/test_campaigns.py -x
set -euo pipefail

cd "$(dirname "$0")/.."

export SECRET_KEY_ACCESS_TOKEN="${SECRET_KEY_ACCESS_TOKEN:-local-development-access-secret-not-for-production-use}"
export SECRET_KEY_REFRESH_TOKEN="${SECRET_KEY_REFRESH_TOKEN:-local-development-refresh-secret-not-for-production-use}"

if [[ $# -gt 0 ]]; then
    exec python -m pytest "$@"
fi
exec python -m pytest tests/ -q
