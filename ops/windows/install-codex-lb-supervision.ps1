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
    [string]$OutboundProxy = "http://127.0.0.1:8800",
    [ValidateRange(1, 134217728)]
    [int]$Codex56ProxyMaxBodyBytes = 128 * 1024 * 1024,
    [string]$ProxyUnauthenticatedClientCidrs = "",
    [switch]$LogProxyRequestShape,
    [string]$TaskNamePrefix = "codex-lb",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$resolvedRepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
$resolvedWorkspaceRoot = [System.IO.Path]::GetFullPath($WorkspaceRoot)
$resolvedDataRoot = [System.IO.Path]::GetFullPath($DataRoot)
$resolvedCodexHome = [System.IO.Path]::GetFullPath($CodexHome)
$resolvedEncryptionKeyFile = [System.IO.Path]::GetFullPath($EncryptionKeyFile)
$opsRoot = $PSScriptRoot

function Quote-Argument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Join-ActionArguments {
    param([string[]]$Values)
    return ($Values | ForEach-Object {
        if ($_ -eq "" -or $_ -match "[\s`"]") { Quote-Argument -Value $_ } else { $_ }
    }) -join " "
}

$mainTaskName = "$TaskNamePrefix-main"
$shimTaskName = "$TaskNamePrefix-shim"
$mainWatchdogTaskName = "$TaskNamePrefix-main-watchdog"
$shimWatchdogTaskName = "$TaskNamePrefix-shim-watchdog"
$recoveryScript = Join-Path $opsRoot "codex-lb-recover-task.ps1"
$watchdogScript = Join-Path $opsRoot "codex-lb-request-watchdog.ps1"
$supervisedLauncher = Join-Path $opsRoot "codex_lb_supervised_launcher.py"
$watchdogLauncher = Join-Path $opsRoot "codex_lb_no_window_powershell.py"
$pythonwExe = Join-Path $resolvedRepoRoot ".venv\Scripts\pythonw.exe"
$identityHelper = Join-Path $opsRoot "codex-lb-process-identity.ps1"
$safeTaskNamePrefix = $TaskNamePrefix -replace "[^A-Za-z0-9_.-]", "_"
$stateNamespace = "$safeTaskNamePrefix-$MainPort-$ShimPort"
$stateRoot = Join-Path (Join-Path $resolvedDataRoot "watchdog") $stateNamespace

$mainArgumentValues = @(
    $supervisedLauncher, "main",
    "--repo-root", $resolvedRepoRoot,
    "--data-root", $resolvedDataRoot,
    "--encryption-key-file", $resolvedEncryptionKeyFile,
    "--bind-host", $BindHost,
    "--bind-port", [string]$MainPort,
    "--outbound-proxy", $OutboundProxy,
    "--proxy-unauthenticated-client-cidrs", $ProxyUnauthenticatedClientCidrs
)
if ($LogProxyRequestShape) {
    $mainArgumentValues += "--log-proxy-request-shape"
}
$mainArguments = Join-ActionArguments $mainArgumentValues
$shimArguments = Join-ActionArguments @(
    $supervisedLauncher, "shim",
    "--repo-root", $resolvedRepoRoot,
    "--workspace-root", $resolvedWorkspaceRoot,
    "--data-root", $resolvedDataRoot,
    "--codex-home", $resolvedCodexHome,
    "--listen-port", [string]$ShimPort,
    "--upstream-port", [string]$MainPort,
    "--max-body-bytes", [string]$Codex56ProxyMaxBodyBytes
)
$mainWatchdogArguments = Join-ActionArguments @(
    $watchdogLauncher, $watchdogScript,
    "-ProbeUri", "http://127.0.0.1:$MainPort/v1/models",
    "-TaskName", $mainTaskName,
    "-ServiceKind", "main",
    "-ListenerPort", [string]$MainPort,
    "-ExpectedRepoRoot", $resolvedRepoRoot,
    "-ExpectedCodexHome", $resolvedCodexHome,
    "-ExpectedEncryptionKeyFile", $resolvedEncryptionKeyFile,
    "-StatePath", (Join-Path $stateRoot "main.json"),
    "-RecoveryScript", $recoveryScript
)
$shimWatchdogArguments = Join-ActionArguments @(
    $watchdogLauncher, $watchdogScript,
    "-ProbeUri", "http://127.0.0.1:$ShimPort/v1/models",
    "-DependencyProbeUri", "http://127.0.0.1:$MainPort/v1/models",
    "-TaskName", $shimTaskName,
    "-ServiceKind", "shim",
    "-ListenerPort", [string]$ShimPort,
    "-ExpectedRepoRoot", $resolvedRepoRoot,
    "-ExpectedCodexHome", $resolvedCodexHome,
    "-ExpectedEncryptionKeyFile", $resolvedEncryptionKeyFile,
    "-StatePath", (Join-Path $stateRoot "shim.json"),
    "-RecoveryScript", $recoveryScript
)

$tasks = @(
    [pscustomobject]@{ name = $mainTaskName; kind = "service"; execute = $pythonwExe; arguments = $mainArguments },
    [pscustomobject]@{ name = $shimTaskName; kind = "service"; execute = $pythonwExe; arguments = $shimArguments },
    [pscustomobject]@{
        name = $mainWatchdogTaskName
        kind = "watchdog"
        execute = $pythonwExe
        arguments = $mainWatchdogArguments
    },
    [pscustomobject]@{
        name = $shimWatchdogTaskName
        kind = "watchdog"
        execute = $pythonwExe
        arguments = $shimWatchdogArguments
    }
)
$plan = [pscustomobject]@{
    repo_root = $resolvedRepoRoot
    workspace_root = $resolvedWorkspaceRoot
    data_root = $resolvedDataRoot
    codex_home = $resolvedCodexHome
    encryption_key_file = $resolvedEncryptionKeyFile
    bind_host = $BindHost
    outbound_proxy = $OutboundProxy
    codex56_proxy_max_body_bytes = $Codex56ProxyMaxBodyBytes
    proxy_unauthenticated_client_cidrs = $ProxyUnauthenticatedClientCidrs
    log_proxy_request_shape = [bool]$LogProxyRequestShape
    main_port = $MainPort
    shim_port = $ShimPort
    tasks = $tasks
}

if ($DryRun) {
    $plan | ConvertTo-Json -Depth 6
    exit 0
}

foreach ($path in @(
    $resolvedRepoRoot,
    $resolvedWorkspaceRoot,
    $resolvedCodexHome,
    $resolvedEncryptionKeyFile,
    $pythonwExe,
    $supervisedLauncher,
    $watchdogLauncher,
    $watchdogScript,
    $recoveryScript,
    $identityHelper
)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Required supervision path not found: $path"
    }
}
New-Item -ItemType Directory -Path $stateRoot -Force | Out-Null

$userId = if ([string]::IsNullOrWhiteSpace($env:USERDOMAIN)) {
    $env:USERNAME
}
else {
    "$env:USERDOMAIN\$env:USERNAME"
}
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
$serviceSettings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable
$watchdogSettings = New-ScheduledTaskSettingsSet `
    -Hidden `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable
$serviceTrigger = New-ScheduledTaskTrigger -AtLogOn -User $userId
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

foreach ($taskPlan in $tasks) {
    $action = New-ScheduledTaskAction `
        -Execute $taskPlan.execute `
        -Argument $taskPlan.arguments `
        -WorkingDirectory $resolvedRepoRoot
    $settings = if ($taskPlan.kind -eq "service") { $serviceSettings } else { $watchdogSettings }
    $trigger = if ($taskPlan.kind -eq "service") { $serviceTrigger } else { $watchdogTrigger }
    Register-ScheduledTask `
        -TaskName $taskPlan.name `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Force | Out-Null
}

Write-Output "registered supervision tasks: $($tasks.name -join ', ')"
