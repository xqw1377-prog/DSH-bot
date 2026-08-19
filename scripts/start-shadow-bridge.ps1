# 只读 Shadow：导出双量化快照并启动 Gateway/Projection/Bots。
# 不启用 Paper，不打开 live，不读取交易密钥。
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$EnvFile = Join-Path $Root ".env.shadow"
if (-not (Test-Path $EnvFile)) {
    if (Test-Path (Join-Path $Root ".env.shadow.example")) {
        Copy-Item (Join-Path $Root ".env.shadow.example") $EnvFile
        Write-Host "created .env.shadow from example; fill DSH_CRYPTO_STATE_JSON first"
        exit 1
    }
    throw "missing .env.shadow"
}

Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#") -or $line -notmatch "=") { return }
    $parts = $line.Split("=", 2)
    $name = $parts[0].Trim()
    $value = $parts[1].Trim().Trim("'").Trim('"')
    Set-Item -Path "Env:$name" -Value $value
}

if ($env:DSH_LOCAL_PAPER -eq "1") {
    throw "DSH_LOCAL_PAPER must be 0 for shadow snapshot bridge"
}
if ($env:QUANT_GATEWAY_READ_ONLY -ne "1") {
    throw "QUANT_GATEWAY_READ_ONLY must be 1"
}
if (-not $env:QUANT_GATEWAY_SNAPSHOT_DIR) {
    throw "QUANT_GATEWAY_SNAPSHOT_DIR is required"
}
if (-not $env:DSH_CRYPTO_STATE_JSON) {
    throw "DSH_CRYPTO_STATE_JSON is required"
}

$env:DSH_CRYPTO_MODE = "shadow"
$env:DSH_A_SHARE_MODE = "shadow"
$env:DSH_LOCAL_PAPER = "0"

if ($env:QUANT_GATEWAY_SNAPSHOT_DIR -notmatch '^[A-Za-z]:\\' -and -not $env:QUANT_GATEWAY_SNAPSHOT_DIR.StartsWith("/")) {
    $env:QUANT_GATEWAY_SNAPSHOT_DIR = Join-Path $Root $env:QUANT_GATEWAY_SNAPSHOT_DIR
}
New-Item -ItemType Directory -Force -Path $env:QUANT_GATEWAY_SNAPSHOT_DIR | Out-Null

$python = if (Test-Path (Join-Path $Root ".venv\Scripts\python.exe")) {
    Join-Path $Root ".venv\Scripts\python.exe"
} else { "python" }

& $python (Join-Path $Root "scripts\export_dual_quant_snapshots.py")
if ($LASTEXITCODE -ne 0) { throw "initial snapshot export failed" }

$env:PYTHONPATH = @(
    (Join-Path $Root "packages\domain-contracts\src"),
    (Join-Path $Root "packages\dsh-runtime\src"),
    (Join-Path $Root "packages\dsh-snapshot-bridge\src"),
    (Join-Path $Root "services\quant-gateway\src"),
    (Join-Path $Root "services\projection-api\src"),
    (Join-Path $Root "services\risk-policy\src"),
    (Join-Path $Root "plugins\dsh-quant-gateway\src"),
    (Join-Path $Root "plugins\dsh-trade-approval\src"),
    (Join-Path $Root "plugins\dsh-crypto-agent\src"),
    (Join-Path $Root "plugins\dsh-a-stock-agent\src")
) -join ";"

Write-Host "starting read-only gateway on :8001 (snapshot + READ_ONLY=1)"
$gw = Start-Process $python -ArgumentList "-m", "uvicorn", "quant_gateway.main:app", "--host", "127.0.0.1", "--port", "8001" -PassThru -NoNewWindow
$proj = Start-Process $python -ArgumentList "-m", "uvicorn", "projection_api.main:app", "--host", "127.0.0.1", "--port", "8004" -PassThru -NoNewWindow
$risk = Start-Process $python -ArgumentList "-m", "uvicorn", "risk_policy.main:app", "--host", "127.0.0.1", "--port", "8003" -PassThru -NoNewWindow

try {
    Write-Host "gateway pid=$($gw.Id) projection pid=$($proj.Id)"
    Write-Host "run bots: python scripts/run_crypto_bot.py --mode shadow"
    Write-Host "          python scripts/run_a_stock_bot.py --mode shadow"
    $every = if ($env:DSH_SNAPSHOT_EXPORT_EVERY_SEC) { [int]$env:DSH_SNAPSHOT_EXPORT_EVERY_SEC } else { 15 }
    while ($true) {
        Start-Sleep -Seconds $every
        & $python (Join-Path $Root "scripts\export_dual_quant_snapshots.py")
    }
} finally {
    foreach ($proc in @($gw, $proj, $risk)) {
        if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    }
}
