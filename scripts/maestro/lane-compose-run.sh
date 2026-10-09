#!/bin/sh
# Run a command in the `api` container of this lane's compose project, against the
# lane's own database on the main checkout's Postgres.
#
# Why: the compose Postgres enforces a password for host connections, and maestro
# connects from the host without one. Inside the compose network the password is
# in the container's environment, so running the DB-touching steps there needs no
# credential on the host.
#
# Usage (from a lane, with the env maestro sets): scripts/maestro/lane-compose-run.sh <cmd...>
set -eu

: "${DATABASE_URL:?DATABASE_URL must be the host-style connection string of the lane}"

# postgres://user@localhost:5432/<db>?application_name=... -> <db>
db_name=${DATABASE_URL##*/}
db_name=${db_name%%\?*}
export LANE_DB_NAME="$db_name"

here=$(cd "$(dirname "$0")" && pwd)
# COMPOSE_FILE is a path list; maestro sets it to base file + its generated override.
sep=:
export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml}${sep}${here}/lane-network.override.yml"

exec docker compose run --rm --no-deps -T api "$@"
