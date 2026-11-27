param(
    [string]$PythonBin = "python",
    [string]$VenvDir = ".venv-packaging-win312",
    [string]$Customer = "community",
    [string]$ReleaseDir = "release",
    [string]$BuildOutputDir = "dist",
    [string]$BuildWorkDir = "build",
    [string]$ExtractDir = "local_private/windows-package-check",
    [switch]$SkipInstall,
    [switch]$SkipNativeBuild,
    [switch]$AllowModelDownload,
    [switch]$Lite,
    [switch]$PublicBuild = ($env:TUOMIN_PUBLIC_BUILD -eq "1")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RootDir = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$RootPrefix = $RootDir.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar

function Resolve-ProjectPath {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ([System.IO.Path]::IsPathRooted($Value)) {
        return [System.IO.Path]::GetFullPath($Value)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $RootDir $Value))
}

function Assert-ProjectChild {
    param([Parameter(Mandatory = $true)][string]$Value)
    $FullPath = [System.IO.Path]::GetFullPath($Value)
    if (-not $FullPath.StartsWith($RootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing path outside repository: $FullPath"
    }
    return $FullPath
}

function Remove-GeneratedPath {
    param([Parameter(Mandatory = $true)][string]$Value)
    $FullPath = Assert-ProjectChild $Value
    if (Test-Path -LiteralPath $FullPath) {
        Remove-Item -LiteralPath $FullPath -Recurse -Force
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $Executable $($Arguments -join ' ')"
    }
}

$VenvPath = Assert-ProjectChild (Resolve-ProjectPath $VenvDir)
$ReleasePath = Assert-ProjectChild (Resolve-ProjectPath $ReleaseDir)
$BuildOutputPath = Assert-ProjectChild (Resolve-ProjectPath $BuildOutputDir)
$BuildWorkPath = Assert-ProjectChild (Resolve-ProjectPath $BuildWorkDir)
$ExtractRoot = Assert-ProjectChild (Resolve-ProjectPath $ExtractDir)
$PackagingPython = Join-Path $VenvPath "Scripts\python.exe"

Push-Location $RootDir
try {
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONNOUSERSITE = "1"
    $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"
    $env:HF_HUB_OFFLINE = "1"
    $env:TRANSFORMERS_OFFLINE = "1"
    $env:TUOMIN_LITE = if ($Lite) { "1" } else { "0" }
    $env:TUOMIN_PUBLIC_BUILD = if ($PublicBuild) { "1" } else { "0" }
    $env:TUOMIN_BUNDLE_MODEL_DIR = Join-Path $RootDir "packaging\ner-model"
    $BundleName = if ($Lite) { "Tuomin-Lite" } else { "Tuomin" }

    if (-not (Test-Path -LiteralPath $PackagingPython -PathType Leaf)) {
        Invoke-Checked $PythonBin @("-m", "venv", $VenvPath)
    }

    $PythonVersion = (& $PackagingPython -c "import platform; print(platform.python_version())").Trim()
    if ($LASTEXITCODE -ne 0 -or $PythonVersion -ne "3.12.10") {
        throw "Packaging environment must use Python 3.12.10; found '$PythonVersion'"
    }

    if (-not $SkipInstall) {
        Invoke-Checked $PackagingPython @("-m", "ensurepip", "--upgrade")
        if ($PublicBuild) {
            $Extras = if ($Lite) { "serve,document,shell,pdf-text" } else { "serve,document,shell,pdf-text,ner" }
            Invoke-Checked $PackagingPython @(
                "-m", "pip", "install", "--constraint", "requirements-windows.lock", ".[$Extras]", "pyinstaller==6.22.2"
            )
        } else {
            Invoke-Checked $PackagingPython @(
                "-m", "pip", "install", "--requirement", "requirements-windows.lock", "pyinstaller==6.22.2", "nuitka==4.1.3"
            )
        }
    }
    Invoke-Checked $PackagingPython @("-m", "pip", "check")

    $ToolVersions = & $PackagingPython -c "from importlib.metadata import version; import os; print(version('pyinstaller')); print('unused' if os.environ['TUOMIN_PUBLIC_BUILD'] == '1' else version('nuitka'))"
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller or Nuitka is unavailable in packaging environment"
    }
    $ToolVersionLines = @($ToolVersions)
    if ($ToolVersionLines[0].Trim() -ne "6.22.2" -or (-not $PublicBuild -and $ToolVersionLines[1].Trim() -ne "4.1.3")) {
        throw "Packaging tool version mismatch: $($ToolVersionLines -join ', ')"
    }

    if (-not $Lite -and -not $PublicBuild) {
        $ModelArguments = @("scripts/prepare_ner_model.py")
        if ($AllowModelDownload) {
            $ModelArguments += "--allow-download"
        }
        Invoke-Checked $PackagingPython $ModelArguments
    }
    if ($PublicBuild) {
        $Customer = "community"
    } else {
        Invoke-Checked $PackagingPython @(
            "scripts/write_delivery_metadata.py",
            "--output", "packaging/delivery.json",
            "--customer", $Customer
        )
    }

    Remove-GeneratedPath (Join-Path $BuildOutputPath $BundleName)
    Remove-GeneratedPath (Join-Path $BuildWorkPath "tuomin_gateway")

    if (-not $PublicBuild) {
        if (-not $SkipNativeBuild) {
            Invoke-Checked $PackagingPython @("scripts/build_native_modules.py")
        }
        Invoke-Checked $PackagingPython @("scripts/build_native_modules.py", "--check")
    }
    Invoke-Checked $PackagingPython @(
        "-m", "PyInstaller", "--clean", "--noconfirm",
        "--distpath", $BuildOutputPath, "--workpath", $BuildWorkPath,
        "packaging/tuomin_gateway.spec"
    )

    $BundlePath = Join-Path $BuildOutputPath $BundleName
    if ($PublicBuild) {
        Copy-Item -LiteralPath (Join-Path $RootDir "packaging/windows-public") -Destination (Join-Path $BundlePath "Windows-Notices") -Recurse
    }
    Invoke-Checked $PackagingPython @(
        "scripts/build_manual.py", (Join-Path $BundlePath "Tuomin-Windows使用手册.html"), "--windows"
    )
    $ExecutablePath = Join-Path $BundlePath "Tuomin.exe"
    if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
        throw "Build failed: expected executable is unavailable: $ExecutablePath"
    }
    Invoke-Checked $PackagingPython @(
        "scripts/verify_build_manifest.py", "--bundle", $BundlePath
    )

    $PackageArguments = @(
        "scripts/package_release.py",
        "--bundle", $BundlePath,
        "--release-dir", $ReleasePath,
        "--customer", $Customer
    )
    if ($Lite) { $PackageArguments += "--lite" }
    Invoke-Checked $PackagingPython $PackageArguments

    $ProjectVersion = (& $PackagingPython -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])").Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to read project version"
    }
    $CustomerSlug = ($Customer.Trim() -replace "[^A-Za-z0-9._-]+", "_").Trim([char[]]"._-")
    if (-not $CustomerSlug) {
        $CustomerSlug = "customer"
    }
    $VariantSuffix = if ($Lite) { "-lite" } else { "" }
    $ReleaseBase = "Tuomin-windows-x64$VariantSuffix-$ProjectVersion-$CustomerSlug"
    if ($PublicBuild) {
        $PublicVariant = if ($Lite) { "Lite" } else { "Runtime" }
        $ReleaseBase = "Tuomin-$ProjectVersion-Windows-x64-$PublicVariant"
    }
    $ArchivePath = Join-Path $ReleasePath "$ReleaseBase.zip"
    $ManifestPath = Join-Path $ReleasePath "$ReleaseBase.manifest.json"
    $ChecksumPath = Join-Path $ReleasePath "$ReleaseBase.sha256"
    Invoke-Checked $PackagingPython @(
        "scripts/verify_build_manifest.py",
        "--bundle", $BundlePath,
        "--manifest", $ManifestPath,
        "--archive", $ArchivePath,
        "--checksum", $ChecksumPath
    )

    Remove-GeneratedPath $ExtractRoot
    New-Item -ItemType Directory -Path $ExtractRoot | Out-Null
    Expand-Archive -LiteralPath $ArchivePath -DestinationPath $ExtractRoot
    $ExtractedBundle = Join-Path $ExtractRoot $BundleName
    Invoke-Checked $PackagingPython @(
        "scripts/verify_build_manifest.py",
        "--bundle", $ExtractedBundle,
        "--manifest", $ManifestPath,
        "--archive", $ArchivePath,
        "--checksum", $ChecksumPath
    )

    Write-Host "Build complete: $ExecutablePath"
    Write-Host "Release archive: $ArchivePath"
    Write-Host "Extracted verification copy: $ExtractedBundle"
}
finally {
    Pop-Location
}
