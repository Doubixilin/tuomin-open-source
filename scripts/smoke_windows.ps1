[CmdletBinding()]
param(
    [int]$Port = 0,
    [string]$VenvDir = ".venv",
    [string]$RuntimeDir,
    [switch]$NoWarmup
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $PSScriptRoot
$Python = Join-Path (Join-Path $RootDir $VenvDir) "Scripts\python.exe"
# The helper always generates a synthetic registry; root tuomin_apps.json is not read.
$ValidationArgs = @("-X", "utf8", (Join-Path $PSScriptRoot "windows_validation.py"), "smoke", "--python", $Python, "--port", "$Port")
if ($RuntimeDir) { $ValidationArgs += @("--runtime-root", $RuntimeDir) }
if ($NoWarmup) { $ValidationArgs += "--no-warmup" }
& $Python @ValidationArgs
if ($LASTEXITCODE -ne 0) { throw "Windows smoke failed; see its isolated runtime report." }
