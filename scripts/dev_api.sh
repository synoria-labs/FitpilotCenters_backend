#!/usr/bin/env bash
# Run the API against the local development database.
#
# The JWT secrets are exported here rather than written to .env on purpose:
# `tests/test_security_hardening.py` asserts that app.security.jwt refuses to import when no
# secrets are configured, and load_environment() reads .env regardless of the process
# environment — so a .env carrying secrets would quietly disable that check for everyone who
# runs the suite. Keeping them in the launcher means the guarantee still holds.
set -euo pipefail

cd "$(dirname "$0")/.."

# Obviously fake, so they can never be mistaken for real credentials, and long enough to
# satisfy the length validation at import time.
export SECRET_KEY_ACCESS_TOKEN="${SECRET_KEY_ACCESS_TOKEN:-local-development-access-secret-not-for-production-use}"
export SECRET_KEY_REFRESH_TOKEN="${SECRET_KEY_REFRESH_TOKEN:-local-development-refresh-secret-not-for-production-use}"

if ! python -c "
import os, sys
from app.core.env import load_environment
load_environment()
url = os.getenv('DATABASE_URL')
sys.exit(0 if url else 1)
" 2>/dev/null; then
    echo "ERROR: falta DATABASE_URL. Corre primero scripts/dev_db_bootstrap.sh" >&2
    exit 1
fi

echo "API en http://127.0.0.1:8000  ·  GraphQL en /graphql"
exec python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
