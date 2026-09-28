#!/usr/bin/env bash
# Start AgentGuard's own local development PostgreSQL: a container named
# agentguard-postgres on 127.0.0.1:5434, with the databases named in .env
# (DATABASE_URL and TEST_DATABASE_URL). Idempotent.
#
# It never touches any other container or database (e.g. another project's
# postgres on :5432). User, password, port and database names are read from
# .env, so nothing is hardcoded here.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
NAME=agentguard-postgres
IMAGE=${AGENTGUARD_PG_IMAGE:-postgres:17-alpine}
[ -f "$ROOT/.env" ] || { echo "no .env in $ROOT (copy .env.example)"; exit 1; }
url=$(grep -E '^DATABASE_URL=' "$ROOT/.env" | tail -1 | cut -d= -f2-)
test_url=$(grep -E '^TEST_DATABASE_URL=' "$ROOT/.env" | tail -1 | cut -d= -f2- || true)
# postgresql+psycopg2://USER:PASS@HOST:PORT/DB
re='^[a-z+0-9]+://([^:]+):([^@]+)@([^:/]+):([0-9]+)/([^?]+)'
[[ $url =~ $re ]] || { echo "DATABASE_URL in .env is not user:pass@host:port/db"; exit 1; }
PGU=${BASH_REMATCH[1]} PGP=${BASH_REMATCH[2]} PGH=${BASH_REMATCH[3]} PGPORT=${BASH_REMATCH[4]} DB=${BASH_REMATCH[5]}
case "$PGH" in 127.0.0.1|localhost) ;; *) echo "DATABASE_URL host is $PGH, not local; nothing to start"; exit 0;; esac
TESTDB=""
if [[ $test_url =~ $re ]]; then TESTDB=${BASH_REMATCH[5]}; fi

if docker inspect "$NAME" >/dev/null 2>&1; then
  state=$(docker inspect -f '{{.State.Running}}' "$NAME")
else
  state=missing
fi
if [ "$state" = missing ]; then
  if lsof -nP -iTCP:"$PGPORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "port $PGPORT is already in use by another process; choose another port in .env"; exit 1
  fi
  docker run -d --name "$NAME" --label agentguard.dev=1 -p "127.0.0.1:$PGPORT:5432" \
    -e POSTGRES_USER="$PGU" -e POSTGRES_PASSWORD="$PGP" -e POSTGRES_DB="$DB" \
    -v agentguard-postgres-data:/var/lib/postgresql/data "$IMAGE" >/dev/null
  echo "created container $NAME ($IMAGE) on 127.0.0.1:$PGPORT"
elif [ "$state" = false ]; then
  docker start "$NAME" >/dev/null; echo "started container $NAME"
else
  echo "container $NAME already running"
fi
for _ in $(seq 1 60); do docker exec "$NAME" pg_isready -U "$PGU" -d "$DB" >/dev/null 2>&1 && break; sleep 1; done
docker exec "$NAME" pg_isready -U "$PGU" -d "$DB" >/dev/null || { echo "$NAME did not become ready"; exit 1; }
for d in "$DB" $TESTDB; do
  if [ "$(docker exec "$NAME" psql -U "$PGU" -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$d'")" != 1 ]; then
    docker exec "$NAME" createdb -U "$PGU" "$d"; echo "created database $d"
  fi
done
echo "AgentGuard PostgreSQL ready: $PGH:$PGPORT databases: $DB ${TESTDB}"
