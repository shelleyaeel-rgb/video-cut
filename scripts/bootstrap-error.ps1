# Used only if Python cannot start, before the Python logger is available.
$ErrorActionPreference = 'Stop'
$bootstrapLog = Join-Path $PSScriptRoot '..\work\logs\bootstrap.log'
try {
    $bootstrapLog = [IO.Path]::GetFullPath($bootstrapLog)
    New-Item -ItemType Directory -Path (Split-Path -Parent $bootstrapLog) -Force | Out-Null
    # Two bounded files. Never touches startup.log owned by the Python logger.
    if ((Test-Path -LiteralPath $bootstrapLog) -and (Get-Item -LiteralPath $bootstrapLog).Length -gt 64KB) {
        Copy-Item -LiteralPath $bootstrapLog -Destination ($bootstrapLog + '.1') -Force
        Clear-Content -LiteralPath $bootstrapLog
    }
    $bootstrapEntry = @{time=(Get-Date -Format o); level='ERROR'; event='python_unavailable'; message='Project Python is missing or could not start. Run scripts/setup.ps1 or check the console.'} | ConvertTo-Json -Compress
    Add-Content -LiteralPath $bootstrapLog -Value $bootstrapEntry -Encoding UTF8
    Write-Output "Startup diagnostics: $bootstrapLog"
} catch {
    Write-Output 'Cannot write startup diagnostics. Check project folder permissions and free space.'
}
