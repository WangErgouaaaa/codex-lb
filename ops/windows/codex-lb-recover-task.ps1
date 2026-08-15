[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TaskName,
    [Parameter(Mandatory = $true)]
    [ValidateSet("main", "shim")]
    [string]$ServiceKind,
    [Parameter(Mandatory = $true)]
    [int]$ListenerPort,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedRepoRoot,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedCodexHome,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedEncryptionKeyFile,
    [Parameter(Mandatory = $true)]
    [string]$ProbeUri,
    [string]$ExpectedStatusCodes = "200,401,403",
    [ValidateRange(1, 30)]
    [int]$ProbeTimeoutSeconds = 5,
    [ValidateRange(10, 300)]
    [int]$RecoveryTimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$identityHelper = Join-Path $PSScriptRoot "codex-lb-process-identity.ps1"
if (-not (Test-Path -LiteralPath $identityHelper)) {
    throw "Process identity helper not found: $identityHelper"
}
. $identityHelper

function Test-RequestPath {
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $statusCode = & curl.exe `
            --silent `
            --show-error `
            --noproxy "*" `
            --connect-timeout $ProbeTimeoutSeconds `
            --max-time $ProbeTimeoutSeconds `
            --output NUL `
            --write-out "%{http_code}" `
            --url $ProbeUri 2>$null
        $curlExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($curlExitCode -ne 0) {
        return $false
    }
    $allowed = @(
        $ExpectedStatusCodes.Split(",") |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -ne "" }
    )
    return $allowed -contains ([string]$statusCode).Trim()
}

function Get-PortListeners {
    return @(
        Get-NetTCPConnection -State Listen -LocalPort $ListenerPort -ErrorAction SilentlyContinue
    )
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -eq $task) {
    throw "Scheduled task '$TaskName' does not exist."
}

$normalizedRepoRoot = [System.IO.Path]::GetFullPath($ExpectedRepoRoot).TrimEnd("\", "/")
if (
    -not (Test-CodexLbSupervisedTaskAction `
        -Task $task `
        -ExpectedRepoRoot $normalizedRepoRoot `
        -ListenerPort $ListenerPort `
        -ServiceKind $ServiceKind `
        -ExpectedCodexHome $ExpectedCodexHome `
        -ExpectedEncryptionKeyFile $ExpectedEncryptionKeyFile)
) {
    throw "Scheduled task '$TaskName' does not match the expected supervised $ServiceKind identity."
}

if (Test-RequestPath) {
    Write-Output "recovery skipped: task='$TaskName' request path is already healthy"
    exit 0
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$releaseDeadline = [DateTime]::UtcNow.AddSeconds(10)
while ([DateTime]::UtcNow -lt $releaseDeadline) {
    $currentTaskState = (Get-ScheduledTask -TaskName $TaskName).State
    if ($currentTaskState -eq "Ready" -and (Get-PortListeners).Count -eq 0) {
        break
    }
    Start-Sleep -Milliseconds 200
}

foreach ($listener in (Get-PortListeners)) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
    if (
        $null -eq $process -or
        -not (Test-CodexLbSupervisedListenerProcess `
            -Process $process `
            -ExpectedRepoRoot $normalizedRepoRoot `
            -ListenerPort $ListenerPort `
            -ServiceKind $ServiceKind `
            -ExpectedCodexHome $ExpectedCodexHome `
            -ExpectedEncryptionKeyFile $ExpectedEncryptionKeyFile)
    ) {
        throw "Port $ListenerPort is owned by unexpected PID $($listener.OwningProcess); refusing to stop it."
    }
    Stop-Process -Id $listener.OwningProcess -Force
}

if ((Get-ScheduledTask -TaskName $TaskName).State -ne "Ready") {
    throw "Scheduled task '$TaskName' did not reach Ready state before recovery start."
}

Start-ScheduledTask -TaskName $TaskName
$deadline = [DateTime]::UtcNow.AddSeconds($RecoveryTimeoutSeconds)
while ([DateTime]::UtcNow -lt $deadline) {
    if (Test-RequestPath) {
        Write-Output "recovered task='$TaskName' port=$ListenerPort"
        exit 0
    }
    Start-Sleep -Milliseconds 500
}

throw "Task '$TaskName' did not recover request path '$ProbeUri' within $RecoveryTimeoutSeconds seconds."
