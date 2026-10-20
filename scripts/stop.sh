#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
docker compose exec -T platform python -m service.cli drain
docker compose down
