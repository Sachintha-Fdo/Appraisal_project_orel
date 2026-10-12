# Builds the orel-server image on this machine and pushes it to Docker Hub.
# The server then runs: docker compose pull; docker compose up -d
#
# One-time: docker login   (Docker Hub username + access token)
#
# Usage (from this folder):
#   powershell -ExecutionPolicy Bypass -File build-and-push.ps1 -Namespace <dockerhub-user> -Version 1.0.0
#
# Pushes <namespace>/orel-server:<version> and <namespace>/orel-server:latest.

param(
    [Parameter(Mandatory = $true)][string]$Namespace,
    [Parameter(Mandatory = $true)][string]$Version,
    # Oracle Linux servers are usually x86_64; use linux/arm64 for Ampere (aarch64) hosts.
    [string]$Platform = 'linux/amd64'
)

$ErrorActionPreference = 'Stop'
$image = "$Namespace/orel-server"

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "docker $($args -join ' ') failed (exit $LASTEXITCODE)" }
}

Write-Host "==> Building ${image}:$Version ($Platform)"
Invoke-Docker build --platform $Platform -t "${image}:$Version" -t "${image}:latest" $PSScriptRoot

Write-Host "==> Pushing to Docker Hub"
Invoke-Docker push "${image}:$Version"
Invoke-Docker push "${image}:latest"

Write-Host ""
Write-Host "Pushed ${image}:$Version. On the server: set IMAGE_TAG=$Version in .env, then"
Write-Host "  docker compose pull; docker compose up -d"
