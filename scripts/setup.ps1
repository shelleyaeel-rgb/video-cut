param([string]$Python = '', [switch]$SkipDownload, [string]$PackageMirror = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
if (-not $Python) {
    $bundled = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundled) { $Python = $bundled }
    elseif (Get-Command py -ErrorAction SilentlyContinue) { $Python = (& py -3.12 -c 'import sys;print(sys.executable)').Trim() }
    else { throw 'Please install Python 3.12 or pass -Python with its full path.' }
}
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    & $Python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the virtual environment.' }
}
if ($SkipDownload) {
    & '.\.venv\Scripts\python.exe' -m pip install --no-index --find-links work/downloads -r requirements.txt
} else {
    if ($PackageMirror) {
        & '.\.venv\Scripts\python.exe' scripts/download_tools.py --mirror $PackageMirror
    } else {
        & '.\.venv\Scripts\python.exe' scripts/download_tools.py
    }
}
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& '.\.venv\Scripts\python.exe' -c "from videocut.media import tool_path; import subprocess; print(subprocess.check_output([str(tool_path('ffmpeg')), '-version'], text=True).splitlines()[0])"
if ($LASTEXITCODE -ne 0) { throw 'Tool verification failed.' }
Write-Host 'Environment ready. Run start.cmd.'
