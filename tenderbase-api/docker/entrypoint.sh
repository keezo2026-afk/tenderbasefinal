#!/usr/bin/env sh
# ---------------------------------------------------------------------------
# Container entrypoint. Chooses a role: api | worker | scheduler | migrate.
# Configuration comes exclusively from environment variables — no secrets are
# baked into the image.
# ---------------------------------------------------------------------------
set -eu

ROLE="${1:-api}"
shift 2>/dev/null || true

WORKERS="${UVICORN_WORKERS:-2}"
HOST="${UVICORN_HOST:-0.0.0.0}"
PORT="${UVICORN_PORT:-8000}"

run_migrations() {
    echo "[entrypoint] applying database migrations"
    alembic upgrade head
}

case "$ROLE" in
    api)
        if [ "${RUN_MIGRATIONS_ON_START:-false}" = "true" ]; then
            run_migrations
        fi
        exec uvicorn app.main:app --host "$HOST" --port "$PORT" --workers "$WORKERS" \
            --proxy-headers --forwarded-allow-ips="*"
        ;;
    worker)
        exec arq app.workers.scheduler.WorkerSettings
        ;;
    scheduler)
        # The ARQ worker also runs the cron schedule; kept as an explicit role
        # for deployments that separate the two.
        exec arq app.workers.scheduler.WorkerSettings
        ;;
    migrate)
        run_migrations
        ;;
    shell)
        exec python "$@"
        ;;
    *)
        exec "$ROLE" "$@"
        ;;
esac
