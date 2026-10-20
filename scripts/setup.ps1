$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Path .secrets -Force | Out-Null
foreach ($name in @('database_password', 'app_key', 'manager_key')) {
    $path = Join-Path '.secrets' $name
    if (-not (Test-Path -LiteralPath $path)) {
        $bytes = New-Object byte[] 48
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        $rng.GetBytes($bytes)
        $rng.Dispose()
        [IO.File]::WriteAllText((Join-Path (Get-Location) $path), [Convert]::ToBase64String($bytes))
    }
}
if (-not (Test-Path .secrets/provider_api_key)) {
    [IO.File]::WriteAllText((Join-Path (Get-Location) '.secrets/provider_api_key'), '')
}
Write-Host 'Secrets ready. Set .secrets/provider_api_key to enable model calls; existing values preserved.'
