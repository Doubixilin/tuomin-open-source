[CmdletBinding()]
param(
    [string]$VenvDir = ".venv",
    [string]$NodeBin,
    [string]$RuntimeDir
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path (Join-Path $RootDir $VenvDir) "Scripts\python.exe"
if (-not $NodeBin) { $NodeBin = (Get-Command node -ErrorAction Stop).Source }
$ValidationArgs = @("-X", "utf8", (Join-Path $PSScriptRoot "windows_validation.py"), "suite", "--python", $Python, "--node", $NodeBin)
if ($RuntimeDir) { $ValidationArgs += @("--runtime-root", $RuntimeDir) }
& $Python @ValidationArgs
if ($LASTEXITCODE -ne 0) { throw "Windows validation failed; see its isolated runtime report." }
