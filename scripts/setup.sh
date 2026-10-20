#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
umask 077
mkdir -p .secrets
for name in database_password app_key manager_key; do
    if [ ! -f ".secrets/$name" ]; then
        openssl rand -base64 48 > ".secrets/$name"
    fi
done
if [ ! -f .secrets/provider_api_key ]; then : > .secrets/provider_api_key; fi
echo 'Secrets ready; existing values preserved.'
