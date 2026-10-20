$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
docker compose exec -T platform python -m service.cli drain
if ($LASTEXITCODE -ne 0) { throw 'Runtime drain failed; platform kept running for recovery.' }
docker compose down
