[CmdletBinding()]
param(
    [switch]$DownloadModel,
    [switch]$InitConfig,
    [switch]$SkipInstall,
    [switch]$LoadNer,
    [switch]$WithPdf,
    [string]$PythonBin,
    [string]$NodeBin,
    [string]$VenvDir = ".venv"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = Split-Path -Parent $PSScriptRoot
$LockFile = Join-Path $RootDir "requirements-windows.lock"
$ExpectedPythonVersion = "3.12.10"
$ModelId = "uer/roberta-base-finetuned-cluener2020-chinese"
$ModelRevision = "cddd8fc233e373855a8c0a7f4b7eb83acb686a2b"
$ModelFiles = @(
    "config.json",
    "pytorch_model.bin",
    "special_tokens_map.json",
    "tokenizer_config.json",
    "vocab.txt"
)

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath"
    }
}

if (-not [System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform(
    [System.Runtime.InteropServices.OSPlatform]::Windows
)) {
    throw "Tuomin Windows bootstrap requires native Windows."
}

Set-Location -LiteralPath $RootDir

if (-not $PythonBin) {
    $UvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if ($UvCommand) {
        $PythonBin = (& $UvCommand.Source @("python", "find", $ExpectedPythonVersion)).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $PythonBin) {
            throw "Unable to locate Python $ExpectedPythonVersion through uv."
        }
    }
    else {
        $PythonCommand = Get-Command python -ErrorAction Stop
        $PythonBin = $PythonCommand.Source
    }
}

$ActualPython = & $PythonBin -X utf8 -c "import platform; print(platform.python_version())"
if ($LASTEXITCODE -ne 0) {
    throw "Unable to run Python at $PythonBin"
}
if ($ActualPython -ne $ExpectedPythonVersion) {
    throw "Expected Python $ExpectedPythonVersion, found $ActualPython at $PythonBin."
}

$VenvPath = Join-Path $RootDir $VenvDir
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    Invoke-Checked -FilePath $PythonBin -Arguments @("-m", "venv", $VenvPath)
}
$VenvPythonVersion = & $VenvPython -X utf8 -c "import platform; print(platform.python_version())"
if ($LASTEXITCODE -ne 0 -or $VenvPythonVersion -ne $ExpectedPythonVersion) {
    throw "Expected $VenvPath to use Python $ExpectedPythonVersion; found $VenvPythonVersion."
}

if (-not $SkipInstall) {
    $Extras = if ($WithPdf) { ".[dev,ner,document,pdf,shell]" } else { ".[dev,ner,document,shell]" }
    Invoke-Checked -FilePath $VenvPython -Arguments @(
        "-m", "pip", "install", "--constraint", $LockFile, "-e", $Extras
    )
    Invoke-Checked -FilePath $VenvPython -Arguments @("-m", "pip", "check")
}

if ($DownloadModel) {
    $HfCommand = Join-Path $VenvPath "Scripts\hf.exe"
    $DownloadArgs = @("download", $ModelId) + $ModelFiles + @("--revision", $ModelRevision, "--max-workers", "4")
    Invoke-Checked -FilePath $HfCommand -Arguments $DownloadArgs
}

$ConfigPath = Join-Path $RootDir "tuomin_apps.json"
if ($InitConfig -and -not (Test-Path -LiteralPath $ConfigPath)) {
    Copy-Item -LiteralPath (Join-Path $RootDir "tuomin_apps.example.json") -Destination $ConfigPath
}

if ($NodeBin) {
    $env:TUOMIN_NODE_BIN = (Resolve-Path -LiteralPath $NodeBin).Path
}
$env:TUOMIN_VENV_DIR = $VenvDir


$DoctorArgs = @("-X", "utf8", (Join-Path $RootDir "scripts\doctor_windows.py"))
if ($LoadNer -or $DownloadModel) {
    $DoctorArgs += "--load-ner"
}
Invoke-Checked -FilePath $VenvPython -Arguments $DoctorArgs
