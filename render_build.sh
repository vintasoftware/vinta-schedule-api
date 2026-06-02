#!/bin/bash
set -euxo pipefail

echo "-----> Build hook"

echo "-----> uv sync"
uv sync --frozen --no-dev --no-install-project
echo "-----> uv done"

echo "-----> Running manage.py check --deploy --fail-level WARNING"
uv run manage.py check --deploy --fail-level WARNING

if [ -n "$ENABLE_DJANGO_COLLECTSTATIC" ] && [ "$ENABLE_DJANGO_COLLECTSTATIC" == 1 ]; then
    echo "-----> Running collectstatic"

    echo "-----> Collecting static files"
    uv run manage.py collectstatic --noinput  2>&1 | sed '/^Copying/d;/^$/d;/^ /d'

    echo
fi

if [ -n "$AUTO_MIGRATE" ] && [ "$AUTO_MIGRATE" == 1 ]; then
    echo "-----> Running manage.py migrate"
    uv run manage.py migrate --noinput
fi

echo "-----> Post-compile done"
