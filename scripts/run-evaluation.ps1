param([string]$Python = "", [int]$Port = 8090)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not $Python) {
    $bundled = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
    if ((Test-Path -LiteralPath '.runtime') -and (Test-Path -LiteralPath $bundled)) { $Python = $bundled }
    else { $Python = 'python' }
}
if (Test-Path -LiteralPath '.runtime') { $env:PYTHONPATH = (Join-Path (Get-Location) '.runtime') + [IO.Path]::PathSeparator + $env:PYTHONPATH }
$env:PYTHONIOENCODING = 'utf-8'
& $Python -m uvicorn evaluation.web:app --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
