[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProbeUri,
    [string]$DependencyProbeUri = "",
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
    [string]$StatePath,
    [string]$RecoveryScript = $(Join-Path $PSScriptRoot "codex-lb-recover-task.ps1"),
    [ValidateRange(1, 10)]
    [int]$FailureThreshold = 2,
    [ValidateRange(1, 30)]
    [int]$ProbeTimeoutSeconds = 5,
    [ValidateRange(10, 300)]
    [int]$RecoveryTimeoutSeconds = 180,
    [string]$ExpectedStatusCodes = "200,401,403"
)

$ErrorActionPreference = "Stop"

function Test-RequestPath {
    param([Parameter(Mandatory = $true)][string]$Uri)

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
            --url $Uri 2>$null
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

function Remove-FailureState {
    if (Test-Path -LiteralPath $StatePath) {
        Remove-Item -LiteralPath $StatePath -Force
    }
}

$mutexName = "Local\codex-lb-watchdog-" + ($TaskName -replace "[^A-Za-z0-9_.-]", "_")
$mutex = [System.Threading.Mutex]::new($false, $mutexName)
$hasMutex = $false
try {
    try {
        $hasMutex = $mutex.WaitOne(0)
    }
    catch [System.Threading.AbandonedMutexException] {
        $hasMutex = $true
    }
    if (-not $hasMutex) {
        Write-Output "watchdog skipped: another instance is active for task '$TaskName'"
        exit 0
    }

    if (-not [string]::IsNullOrWhiteSpace($DependencyProbeUri) -and -not (Test-RequestPath -Uri $DependencyProbeUri)) {
        Remove-FailureState
        Write-Output "watchdog deferred: dependency request path is unavailable for task '$TaskName'"
        return
    }

    if (Test-RequestPath -Uri $ProbeUri) {
        Remove-FailureState
        Write-Output "watchdog healthy: task='$TaskName' uri='$ProbeUri'"
        return
    }

    $failureCount = 0
    if (Test-Path -LiteralPath $StatePath) {
        try {
            $state = Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json
            $failureCount = [int]$state.consecutive_failures
        }
        catch {
            $failureCount = 0
        }
    }
    $failureCount += 1

    if ($failureCount -lt $FailureThreshold) {
        $stateDirectory = Split-Path -Parent $StatePath
        if (-not [string]::IsNullOrWhiteSpace($stateDirectory) -and -not (Test-Path -LiteralPath $stateDirectory)) {
            New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
        }
        [pscustomobject]@{
            consecutive_failures = $failureCount
            last_failure_at = (Get-Date).ToUniversalTime().ToString("o")
            probe_uri = $ProbeUri
            task_name = $TaskName
        } | ConvertTo-Json | Set-Content -LiteralPath $StatePath -Encoding UTF8
        Write-Output "watchdog failure recorded: task='$TaskName' count=$failureCount threshold=$FailureThreshold"
        return
    }

    if (-not (Test-Path -LiteralPath $RecoveryScript)) {
        throw "Recovery script not found: $RecoveryScript"
    }

    $recoveryArguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", $RecoveryScript,
        "-TaskName", $TaskName,
        "-ServiceKind", $ServiceKind,
        "-ListenerPort", [string]$ListenerPort,
        "-ExpectedRepoRoot", $ExpectedRepoRoot,
        "-ExpectedCodexHome", $ExpectedCodexHome,
        "-ExpectedEncryptionKeyFile", $ExpectedEncryptionKeyFile,
        "-ProbeUri", $ProbeUri,
        "-ExpectedStatusCodes", $ExpectedStatusCodes,
        "-ProbeTimeoutSeconds", [string]$ProbeTimeoutSeconds,
        "-RecoveryTimeoutSeconds", [string]$RecoveryTimeoutSeconds
    )
    & powershell.exe @recoveryArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Recovery failed for task '$TaskName' with exit code $LASTEXITCODE."
    }

    Remove-FailureState
    Write-Output "watchdog recovery completed: task='$TaskName'"
}
finally {
    if ($hasMutex) {
        $mutex.ReleaseMutex()
    }
    $mutex.Dispose()
}
