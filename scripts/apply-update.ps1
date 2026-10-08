# Runs outside the application so Windows releases the old executable/DLLs first.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    [Parameter(Mandatory = $true)][string]$StageRoot,
    [Parameter(Mandatory = $true)][int]$ParentProcessId,
    [Parameter(Mandatory = $true)][string]$Version,
    [Parameter(Mandatory = $true)][string]$ResultPath,
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
$install = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$stage = [IO.Path]::GetFullPath($StageRoot).TrimEnd('\')
$installParent = [IO.Path]::GetDirectoryName($install)
$stageParent = [IO.Path]::GetDirectoryName($stage)
if (-not $installParent -or $installParent -ine $stageParent -or
    [IO.Path]::GetFileName($stage) -notmatch '^\.FenghuoClip-update-[0-9a-f]{32}$') {
    throw 'The staging directory must be an update-specific sibling of the installation.'
}
if ($Version -notmatch '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$') {
    throw 'Invalid update version.'
}
$payload = Join-Path $stage 'FenghuoClip'
$backup = Join-Path $stage 'previous'
$failed = Join-Path $stage 'failed'
$marker = Join-Path $stage 'health-ok'
$log = Join-Path $stage 'update.log'
$result = [IO.Path]::GetFullPath($ResultPath)
if ([IO.Path]::GetFileName($result) -ne 'result.json' -or
    [IO.Path]::GetFileName([IO.Path]::GetDirectoryName($result)) -ne 'updates') {
    throw 'Invalid update result path.'
}
foreach ($directory in @($install, $stage, $payload)) {
    $item = Get-Item -LiteralPath $directory -Force
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Update directories must be ordinary directories, not links.'
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $install 'FenghuoClip.exe') -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $payload 'FenghuoClip.exe') -PathType Leaf) -or
    -not (Test-Path -LiteralPath (Join-Path $payload '_internal') -PathType Container) -or
    (Test-Path -LiteralPath $backup) -or (Test-Path -LiteralPath $failed)) {
    throw 'The installation or prepared update is incomplete.'
}
if ($ValidateOnly) {
    Write-Output 'Update paths validated; no files changed.'
    exit 0
}

function Write-UpdateLog([string]$Message) {
    try { Add-Content -LiteralPath $log -Encoding UTF8 -Value ((Get-Date -Format o) + ' ' + $Message) } catch { }
}

function Write-Result([string]$State, [string]$Message) {
    try {
        $record = @{ state = $State; message = $Message; version = $Version; at = (Get-Date -Format o) }
        $resultDirectory = [IO.Path]::GetDirectoryName($result)
        New-Item -ItemType Directory -Path $resultDirectory -Force | Out-Null
        $temporaryResult = Join-Path $resultDirectory ('result-' + [guid]::NewGuid().ToString('N') + '.tmp')
        $record | ConvertTo-Json -Compress | Set-Content -LiteralPath $temporaryResult -Encoding UTF8
        Move-Item -LiteralPath $temporaryResult -Destination $result -Force
    } catch {
        Write-UpdateLog ('Could not save update result: ' + $_.Exception.Message)
    }
}

function Move-WithRetry([string]$Source, [string]$Destination) {
    # File scanners may briefly retain handles even after the old process exits.
    for ($attempt = 0; $attempt -lt 20; $attempt++) {
        try {
            Move-Item -LiteralPath $Source -Destination $Destination -ErrorAction Stop
            return
        } catch {
            if ($attempt -eq 19) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
}

function Remove-StageChild([string]$Path) {
    $absolute = [IO.Path]::GetFullPath($Path)
    if ([IO.Path]::GetDirectoryName($absolute) -ine $stage -or
        [IO.Path]::GetFileName($absolute) -notin @('previous', 'failed')) {
        throw 'Refusing to remove a path outside the updater staging directory.'
    }
    $item = Get-Item -LiteralPath $absolute -Force -ErrorAction SilentlyContinue
    if ($item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Refusing to remove a linked directory.'
    }
    if ($item) { Remove-Item -LiteralPath $absolute -Recurse -Force }
}

$movedOriginal = $false
$movedPayload = $false
$newProcess = $null
try {
    Set-Content -LiteralPath (Join-Path $stage 'helper-ready') -Value 'ready' -Encoding ASCII
    Write-UpdateLog ('Waiting for process ' + $ParentProcessId)
    $oldProcess = Get-Process -Id $ParentProcessId -ErrorAction SilentlyContinue
    if ($oldProcess) {
        if (-not $oldProcess.WaitForExit(120000)) {
            throw 'The application did not close within two minutes. Close it and retry the update.'
        }
    }
    if (Get-ChildItem -LiteralPath $install -Force | Where-Object { $_.Name -notin @('FenghuoClip.exe', '_internal') }) {
        throw 'The installation contains personal files. Move them outside the application directory before updating.'
    }
    if (Test-Path -LiteralPath $marker) { Remove-Item -LiteralPath $marker -Force }
    Move-WithRetry $install $backup
    $movedOriginal = $true
    Move-WithRetry $payload $install
    $movedPayload = $true
    Write-UpdateLog 'Starting the updated application.'
    $newProcess = Start-Process -FilePath (Join-Path $install 'FenghuoClip.exe') `
        -ArgumentList ('--update-health "{0}"' -f $marker) -WorkingDirectory $install `
        -WindowStyle Hidden -PassThru
    $deadline = (Get-Date).AddSeconds(90)
    $healthy = $false
    while ((Get-Date) -lt $deadline) {
        if (Test-Path -LiteralPath $marker -PathType Leaf) {
            if ((Get-Content -LiteralPath $marker -Raw).Trim() -eq $Version) {
                $healthy = $true
                break
            }
        }
        $newProcess.Refresh()
        if ($newProcess.HasExited) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not $healthy) { throw 'The new version did not become ready; restoring the previous version.' }
    Write-Result 'success' ('Updated to ' + $Version)
    Write-UpdateLog 'The new version is healthy. Update complete.'
    # A cleanup failure is harmless; never roll back a healthy running application.
    try { Remove-StageChild $backup } catch { Write-UpdateLog ('Backup cleanup failed: ' + $_.Exception.Message) }
    exit 0
} catch {
    $failure = $_.Exception.Message
    Write-UpdateLog ('Update failed: ' + $failure)
    if ($newProcess) {
        try {
            $newProcess.Refresh()
            if (-not $newProcess.HasExited) {
                Stop-Process -Id $newProcess.Id -Force
                $newProcess.WaitForExit(15000) | Out-Null
            }
        } catch { Write-UpdateLog ('Could not stop the failed version: ' + $_.Exception.Message) }
    }
    try {
        if ($movedPayload) { Move-WithRetry $install $failed }
        if ($movedOriginal) { Move-WithRetry $backup $install }
        Write-Result 'error' ($failure + ' Previous version retained.')
        if ($movedOriginal) {
            Start-Process -FilePath (Join-Path $install 'FenghuoClip.exe') -WorkingDirectory $install -WindowStyle Hidden
        }
        try { Remove-StageChild $failed } catch { Write-UpdateLog ('Failed package cleanup: ' + $_.Exception.Message) }
    } catch {
        $rollbackError = $_.Exception.Message
        Write-UpdateLog ('Rollback requires recovery: ' + $rollbackError)
        Write-Result 'error' ($failure + ' Automatic restore failed: ' + $rollbackError + '. Original files: ' + $backup)
    }
    exit 1
}
