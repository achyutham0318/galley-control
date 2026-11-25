#!/bin/sh
set -e

docker compose up -d postgres redis
docker compose run --rm api pytest -vv --tb=short
