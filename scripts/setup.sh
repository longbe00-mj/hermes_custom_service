#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
umask 077
mkdir -p .secrets
chmod 700 .secrets
for name in database_password app_key manager_key; do
    if [ ! -f ".secrets/$name" ]; then
        openssl rand -base64 48 > ".secrets/$name"
    fi
done
if [ ! -f .secrets/provider_api_key ]; then : > .secrets/provider_api_key; fi
# Compose local secrets are read-only bind mounts: host UID does not become container UID.
# Files are readable in explicitly granted containers; the 0700 host directory protects traversal.
chmod 644 .secrets/database_password .secrets/app_key .secrets/manager_key .secrets/provider_api_key
echo 'Secrets ready; existing values preserved.'
