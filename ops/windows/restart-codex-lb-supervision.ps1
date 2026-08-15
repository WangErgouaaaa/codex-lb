[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot,
    [Parameter(Mandatory = $true)]
    [string]$WorkspaceRoot,
    [Parameter(Mandatory = $true)]
    [string]$DataRoot,
    [Parameter(Mandatory = $true)]
    [string]$CodexHome,
    [Parameter(Mandatory = $true)]
    [string]$EncryptionKeyFile,
    [Parameter(Mandatory = $true)]
    [int]$MainPort,
    [Parameter(Mandatory = $true)]
    [int]$ShimPort,
    [ValidateSet("0.0.0.0", "127.0.0.1")]
    [string]$BindHost = "0.0.0.0",
    [string]$TaskNamePrefix = "codex-lb",
    [string[]]$LegacyTaskNames = @(),
    [ValidateRange(10, 300)]
    [int]$StartupTimeoutSeconds = 180,
    [ValidateRange(1, 30)]
    [int]$ProbeTimeoutSeconds = 5,
    [string]$ExpectedStatusCodes = "200,401,403",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$resolvedRepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
$resolvedWorkspaceRoot = [System.IO.Path]::GetFullPath($WorkspaceRoot)
$resolvedDataRoot = [System.IO.Path]::GetFullPath($DataRoot)
$resolvedCodexHome = [System.IO.Path]::GetFullPath($CodexHome)
$resolvedEncryptionKeyFile = [System.IO.Path]::GetFullPath($EncryptionKeyFile)
$installer = Join-Path $PSScriptRoot "install-codex-lb-supervision.ps1"
$identityHelper = Join-Path $PSScriptRoot "codex-lb-process-identity.ps1"
$mainTaskName = "$TaskNamePrefix-main"
$shimTaskName = "$TaskNamePrefix-shim"
$mainWatchdogTaskName = "$TaskNamePrefix-main-watchdog"
$shimWatchdogTaskName = "$TaskNamePrefix-shim-watchdog"
$mainProbe = "http://127.0.0.1:$MainPort/v1/models"
$shimProbe = "http://127.0.0.1:$ShimPort/v1/models"

$plan = [pscustomobject]@{
    repo_root = $resolvedRepoRoot
    workspace_root = $resolvedWorkspaceRoot
    data_root = $resolvedDataRoot
    codex_home = $resolvedCodexHome
    encryption_key_file = $resolvedEncryptionKeyFile
    bind_host = $BindHost
    main_port = $MainPort
    shim_port = $ShimPort
    main_task = $mainTaskName
    shim_task = $shimTaskName
    main_watchdog_task = $mainWatchdogTaskName
    shim_watchdog_task = $shimWatchdogTaskName
    main_probe = $mainProbe
    shim_probe = $shimProbe
    legacy_tasks = $LegacyTaskNames
}
if ($DryRun) {
    $plan | ConvertTo-Json -Depth 4
    exit 0
}

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

function Get-PortListeners {
    param([Parameter(Mandatory = $true)][int]$Port)

    return @(
        Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    )
}

function Stop-ExpectedListeners {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port,
        [Parameter(Mandatory = $true)]
        [ValidateSet("main", "shim")]
        [string]$ServiceKind
    )

    $releaseDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while ([DateTime]::UtcNow -lt $releaseDeadline) {
        if ((Get-PortListeners -Port $Port).Count -eq 0) {
            return
        }
        Start-Sleep -Milliseconds 250
    }

    foreach ($listener in (Get-PortListeners -Port $Port)) {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)" -ErrorAction SilentlyContinue
        $isSupervised = (
            $null -ne $process -and
            (Test-CodexLbSupervisedListenerProcess `
                -Process $process `
                -ExpectedRepoRoot $resolvedRepoRoot `
                -ListenerPort $Port `
                -ServiceKind $ServiceKind `
                -ExpectedCodexHome $resolvedCodexHome `
                -ExpectedEncryptionKeyFile $resolvedEncryptionKeyFile)
        )
        $isLegacy = (
            $null -ne $process -and
            (Test-CodexLbLegacyListenerProcess `
                -Process $process `
                -ExpectedRepoRoot $resolvedRepoRoot `
                -ExpectedWorkspaceRoot $resolvedWorkspaceRoot `
                -ListenerPort $Port `
                -ServiceKind $ServiceKind)
        )
        if (-not $isSupervised -and -not $isLegacy) {
            throw "Port $Port is owned by unexpected PID $($listener.OwningProcess); refusing to stop it."
        }
        Stop-Process -Id $listener.OwningProcess -Force
    }
}

function Wait-RequestPath {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$TaskName
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-RequestPath -Uri $Uri) {
            return
        }
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -eq $task) {
            throw "Scheduled task '$TaskName' disappeared during startup."
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Task '$TaskName' did not pass request probe '$Uri' within $StartupTimeoutSeconds seconds."
}

function Wait-Watchdog {
    param(
        [Parameter(Mandatory = $true)][string]$TaskName,
        [Parameter(Mandatory = $true)][DateTime]$PreviousRunTime
    )

    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    while ([DateTime]::UtcNow -lt $deadline) {
        $task = Get-ScheduledTask -TaskName $TaskName
        $info = Get-ScheduledTaskInfo -TaskName $TaskName
        if ($task.State -eq "Ready" -and $info.LastRunTime -gt $PreviousRunTime) {
            if ($info.LastTaskResult -ne 0) {
                throw "Watchdog task '$TaskName' failed with result $($info.LastTaskResult)."
            }
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Watchdog task '$TaskName' did not finish within 30 seconds."
}

function Restore-Watchdogs {
    foreach ($watchdogTaskName in @($mainWatchdogTaskName, $shimWatchdogTaskName)) {
        try {
            Enable-ScheduledTask -TaskName $watchdogTaskName -ErrorAction Stop | Out-Null
        }
        catch {
            Write-Warning "Could not re-enable watchdog task '$watchdogTaskName': $($_.Exception.Message)"
        }
    }
}

function Restore-LegacyTasks {
    foreach ($legacyTaskName in $existingLegacyTaskNames) {
        Enable-ScheduledTask -TaskName $legacyTaskName -ErrorAction Stop | Out-Null
        Start-ScheduledTask -TaskName $legacyTaskName -ErrorAction Stop
    }
}

if (-not (Test-Path -LiteralPath $installer)) {
    throw "Supervision installer not found: $installer"
}
if (-not (Test-Path -LiteralPath $identityHelper)) {
    throw "Process identity helper not found: $identityHelper"
}
. $identityHelper

$managedTaskNames = @(
    $mainTaskName,
    $shimTaskName,
    $mainWatchdogTaskName,
    $shimWatchdogTaskName
)
$existingLegacyTaskNames = @(
    foreach ($legacyTaskName in $LegacyTaskNames) {
        if ($legacyTaskName -in $managedTaskNames) {
            continue
        }
        if ($null -ne (Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue)) {
            $legacyTaskName
        }
    }
)

& $installer `
    -RepoRoot $resolvedRepoRoot `
    -WorkspaceRoot $resolvedWorkspaceRoot `
    -DataRoot $resolvedDataRoot `
    -CodexHome $resolvedCodexHome `
    -EncryptionKeyFile $resolvedEncryptionKeyFile `
    -MainPort $MainPort `
    -ShimPort $ShimPort `
    -BindHost $BindHost `
    -TaskNamePrefix $TaskNamePrefix

try {
    Disable-ScheduledTask -TaskName $mainWatchdogTaskName | Out-Null
    Disable-ScheduledTask -TaskName $shimWatchdogTaskName | Out-Null

    foreach ($legacyTaskName in $LegacyTaskNames) {
        if ($legacyTaskName -in @($mainTaskName, $shimTaskName, $mainWatchdogTaskName, $shimWatchdogTaskName)) {
            continue
        }
        $legacyTask = Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue
        if ($null -ne $legacyTask) {
            Disable-ScheduledTask -TaskName $legacyTaskName | Out-Null
            Stop-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue
        }
    }

    Stop-ScheduledTask -TaskName $shimTaskName -ErrorAction SilentlyContinue
    Stop-ScheduledTask -TaskName $mainTaskName -ErrorAction SilentlyContinue
    Stop-ExpectedListeners -Port $ShimPort -ServiceKind "shim"
    Stop-ExpectedListeners -Port $MainPort -ServiceKind "main"

    Start-ScheduledTask -TaskName $mainTaskName
    Wait-RequestPath -Uri $mainProbe -TaskName $mainTaskName
    Start-ScheduledTask -TaskName $shimTaskName
    Wait-RequestPath -Uri $shimProbe -TaskName $shimTaskName

    Enable-ScheduledTask -TaskName $mainWatchdogTaskName | Out-Null
    Enable-ScheduledTask -TaskName $shimWatchdogTaskName | Out-Null
    $mainWatchdogPreviousRunTime = (Get-ScheduledTaskInfo -TaskName $mainWatchdogTaskName).LastRunTime
    $shimWatchdogPreviousRunTime = (Get-ScheduledTaskInfo -TaskName $shimWatchdogTaskName).LastRunTime
    Start-ScheduledTask -TaskName $mainWatchdogTaskName
    Start-ScheduledTask -TaskName $shimWatchdogTaskName
    Wait-Watchdog -TaskName $mainWatchdogTaskName -PreviousRunTime $mainWatchdogPreviousRunTime
    Wait-Watchdog -TaskName $shimWatchdogTaskName -PreviousRunTime $shimWatchdogPreviousRunTime
}
catch {
    $cutoverError = $_
    if ($existingLegacyTaskNames.Count -gt 0) {
        try {
            Disable-ScheduledTask -TaskName $mainWatchdogTaskName -ErrorAction SilentlyContinue | Out-Null
            Disable-ScheduledTask -TaskName $shimWatchdogTaskName -ErrorAction SilentlyContinue | Out-Null
            Stop-ScheduledTask -TaskName $shimTaskName -ErrorAction SilentlyContinue
            Stop-ScheduledTask -TaskName $mainTaskName -ErrorAction SilentlyContinue
            Stop-ExpectedListeners -Port $ShimPort -ServiceKind "shim"
            Stop-ExpectedListeners -Port $MainPort -ServiceKind "main"
            Restore-LegacyTasks
            $legacyProbeTaskName = $existingLegacyTaskNames[0]
            Wait-RequestPath -Uri $mainProbe -TaskName $legacyProbeTaskName
            Wait-RequestPath -Uri $shimProbe -TaskName $legacyProbeTaskName
        }
        catch {
            throw (
                "Cutover failed: $($cutoverError.Exception.Message) " +
                "Legacy rollback also failed: $($_.Exception.Message)"
            )
        }
    }
    else {
        Restore-Watchdogs
    }
    throw $cutoverError
}

$mainListeners = @(Get-PortListeners -Port $MainPort)
$shimListeners = @(Get-PortListeners -Port $ShimPort)
if ($mainListeners.Count -ne 1 -or $shimListeners.Count -ne 1) {
    throw "Expected exactly one supervised listener on each managed port."
}
$mainListener = $mainListeners[0]
$shimListener = $shimListeners[0]
if ([string]$mainListener.LocalAddress -ne $BindHost) {
    throw "Main listener address '$($mainListener.LocalAddress)' does not match expected '$BindHost'."
}
if ([string]$shimListener.LocalAddress -ne "127.0.0.1") {
    throw "Shim listener address '$($shimListener.LocalAddress)' does not match expected '127.0.0.1'."
}

$mainTask = Get-ScheduledTask -TaskName $mainTaskName
$shimTask = Get-ScheduledTask -TaskName $shimTaskName
if (
    -not (Test-CodexLbSupervisedTaskAction `
        -Task $mainTask `
        -ExpectedRepoRoot $resolvedRepoRoot `
        -ListenerPort $MainPort `
        -ServiceKind "main" `
        -ExpectedCodexHome $resolvedCodexHome `
        -ExpectedEncryptionKeyFile $resolvedEncryptionKeyFile) -or
    -not (Test-CodexLbSupervisedTaskAction `
        -Task $shimTask `
        -ExpectedRepoRoot $resolvedRepoRoot `
        -ListenerPort $ShimPort `
        -ServiceKind "shim" `
        -ExpectedCodexHome $resolvedCodexHome `
        -ExpectedEncryptionKeyFile $resolvedEncryptionKeyFile)
) {
    throw "One or more service tasks do not match the expected supervised identity."
}

$mainProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($mainListener.OwningProcess)"
$shimProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($shimListener.OwningProcess)"
if (
    -not (Test-CodexLbSupervisedListenerProcess `
        -Process $mainProcess `
        -ExpectedRepoRoot $resolvedRepoRoot `
        -ListenerPort $MainPort `
        -ServiceKind "main" `
        -ExpectedCodexHome $resolvedCodexHome `
        -ExpectedEncryptionKeyFile $resolvedEncryptionKeyFile) -or
    -not (Test-CodexLbSupervisedListenerProcess `
        -Process $shimProcess `
        -ExpectedRepoRoot $resolvedRepoRoot `
        -ListenerPort $ShimPort `
        -ServiceKind "shim" `
        -ExpectedCodexHome $resolvedCodexHome `
        -ExpectedEncryptionKeyFile $resolvedEncryptionKeyFile)
) {
    throw "One or more listeners are not owned by the expected hidden supervised launcher."
}

[pscustomobject]@{
    status = "ok"
    main_port = $MainPort
    main_pid = $mainListener.OwningProcess
    main_address = $mainListener.LocalAddress
    shim_port = $ShimPort
    shim_pid = $shimListener.OwningProcess
    shim_address = $shimListener.LocalAddress
    main_task = $mainTaskName
    shim_task = $shimTaskName
    main_watchdog_task = $mainWatchdogTaskName
    shim_watchdog_task = $shimWatchdogTaskName
} | ConvertTo-Json
