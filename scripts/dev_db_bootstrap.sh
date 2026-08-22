#!/usr/bin/env bash
# Bring up the local development database and build its schema from the migrations.
#
# The schema is built by running alembic, not by restoring a production dump: it is the
# authoritative definition, it needs no production credentials, and it keeps member PII out
# of a development machine. Use --seed to add a handful of synthetic members to click around
# with; the test suite does not need them (it rolls back every transaction).
#
#   ./scripts/dev_db_bootstrap.sh
#   ./scripts/dev_db_bootstrap.sh --seed
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE_FILE=docker-compose.dev.yml
SEED=0
[[ "${1:-}" == "--seed" ]] && SEED=1

echo "==> Levantando Postgres local"
docker compose -f "$COMPOSE_FILE" up -d

echo "==> Esperando a que acepte conexiones"
for _ in $(seq 1 60); do
    if docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U fitpilot -d fitpilot >/dev/null 2>&1; then
        echo "    listo"
        break
    fi
    sleep 2
done

if ! docker compose -f "$COMPOSE_FILE" exec -T db pg_isready -U fitpilot -d fitpilot >/dev/null 2>&1; then
    echo "ERROR: la base no respondio a tiempo" >&2
    exit 1
fi

echo "==> Aplicando migraciones"
alembic upgrade head

echo "==> Verificando el esquema de campanas"
docker compose -f "$COMPOSE_FILE" exec -T db psql -U fitpilot -d fitpilot -v ON_ERROR_STOP=1 <<'SQL'
\pset pager off
SELECT 'tablas de campanas' AS check, count(*) AS n
FROM information_schema.tables
WHERE table_schema = 'app'
  AND table_name IN ('campaigns', 'campaign_variants', 'campaign_recipients');

SELECT 'columnas nuevas' AS check, string_agg(column_name, ', ' ORDER BY column_name) AS cols
FROM information_schema.columns
WHERE table_schema = 'app'
  AND (
    (table_name = 'campaigns' AND column_name = 'heartbeat_at')
    OR (table_name = 'campaign_recipients'
        AND column_name IN ('send_after', 'favorite_class_type_id', 'favorite_class_template_id'))
  );

SELECT 'indices de despacho' AS check, string_agg(indexname, ', ' ORDER BY indexname) AS idx
FROM pg_indexes
WHERE schemaname = 'app'
  AND indexname IN ('idx_campaign_recipient_dispatch',
                    'idx_campaign_recipient_conversion',
                    'idx_campaign_recipient_favorite_class');

SELECT 'revision alembic' AS check, version_num FROM app.alembic_version;
SQL

if [[ "$SEED" == "1" ]]; then
    echo "==> Sembrando datos sinteticos"
    python scripts/dev_seed.py
fi

echo
echo "Listo. Base local en postgresql://fitpilot:fitpilot_local@127.0.0.1:5433/fitpilot"
echo "Los tests ya pueden correr:  python -m pytest tests/ -q"
