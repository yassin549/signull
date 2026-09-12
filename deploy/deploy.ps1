param(
  [Parameter(Mandatory = $true)][string]$Server,
  [string]$User = "root",
  [string]$Key = (Join-Path $env:USERPROFILE ".ssh\signull_hetzner"),
  [switch]$SkipProvision
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$archive = Join-Path $env:TEMP "signull-deploy.tgz"

Write-Host "==> Building $archive"
Push-Location $root
try {
  tar -czf $archive `
    --exclude=.git `
    --exclude=__pycache__ `
    --exclude=.venv `
    --exclude=venv `
    --exclude=.pytest_cache `
    --exclude=scratch `
    --exclude=terminals `
    --exclude=mcps `
    --exclude=agent-tools `
    --exclude=data/backtest_cache `
    --exclude=data/backtest_cache_predict `
    --exclude=data/btc_klines `
    --exclude=data/btc_1s `
    --exclude=data/sessions `
    --exclude=.env `
    --exclude=*.pyc `
    .
  if ($LASTEXITCODE -ne 0) { throw "tar failed" }
} finally {
  Pop-Location
}

$target = "${User}@${Server}"

Write-Host "==> Uploading to $target"
& scp -i $Key -o StrictHostKeyChecking=accept-new $archive "${target}:/tmp/signull-deploy.tgz"
if ($LASTEXITCODE -ne 0) { throw "scp failed" }

$remote = "mkdir -p /opt/signull && tar -xzf /tmp/signull-deploy.tgz -C /opt/signull && sed -i 's/\r$//' /opt/signull/deploy/*.sh /opt/signull/deploy/*.service"
if (-not $SkipProvision) {
  $remote += " && bash /opt/signull/deploy/provision.sh"
}

Write-Host "==> Extracting and provisioning on server"
& ssh -i $Key -o StrictHostKeyChecking=accept-new $target $remote
if ($LASTEXITCODE -ne 0) { throw "remote command failed" }

Write-Host ""
Write-Host "Done. Open a tunnel from this machine with:"
Write-Host "  ssh -i `"$Key`" -N -L 8080:127.0.0.1:8080 ${target}"
Write-Host "Then browse http://127.0.0.1:8080"
