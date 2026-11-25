#!/bin/sh
set -e

alembic upgrade head

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

exec gunicorn app.main:app -k uvicorn_worker.UvicornWorker --workers 3 --bind 0.0.0.0:8000
