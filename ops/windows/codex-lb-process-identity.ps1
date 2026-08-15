function Test-CodexLbPathEquals {
    param(
        [AllowNull()]
        [string]$Actual,
        [Parameter(Mandatory = $true)]
        [string]$Expected
    )

    if ([string]::IsNullOrWhiteSpace($Actual)) {
        return $false
    }
    try {
        $actualPath = [System.IO.Path]::GetFullPath($Actual).TrimEnd("\", "/")
        $expectedPath = [System.IO.Path]::GetFullPath($Expected).TrimEnd("\", "/")
    }
    catch {
        return $false
    }
    return $actualPath.Equals($expectedPath, [System.StringComparison]::OrdinalIgnoreCase)
}

function Test-CodexLbCommandLineToken {
    param(
        [AllowNull()]
        [string]$CommandLine,
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }
    $quotedValue = [regex]::Escape('"' + $Value + '"')
    $plainValue = [regex]::Escape($Value)
    $pattern = "(?:^|\s)(?:$quotedValue|$plainValue)(?=\s|$)"
    return [regex]::IsMatch(
        $CommandLine,
        $pattern,
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
}

function Test-CodexLbCommandLineOption {
    param(
        [AllowNull()]
        [string]$CommandLine,
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }
    $optionName = [regex]::Escape($Name)
    $quotedValue = [regex]::Escape('"' + $Value + '"')
    $plainValue = [regex]::Escape($Value)
    $pattern = "(?:^|\s)$optionName\s+(?:$quotedValue|$plainValue)(?=\s|$)"
    return [regex]::IsMatch(
        $CommandLine,
        $pattern,
        [System.Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
}

function Test-CodexLbSupervisedTaskAction {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Task,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedRepoRoot,
        [Parameter(Mandatory = $true)]
        [int]$ListenerPort,
        [Parameter(Mandatory = $true)]
        [ValidateSet("main", "shim")]
        [string]$ServiceKind,
        [string]$ExpectedCodexHome = "",
        [string]$ExpectedEncryptionKeyFile = ""
    )

    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) {
        return $false
    }

    $action = $actions[0]
    $repoRoot = [System.IO.Path]::GetFullPath($ExpectedRepoRoot).TrimEnd("\", "/")
    $pythonw = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
    $launcher = Join-Path $repoRoot "ops\windows\codex_lb_supervised_launcher.py"
    $arguments = [string]$action.Arguments
    $portOption = if ($ServiceKind -eq "main") { "--bind-port" } else { "--listen-port" }

    $matchesBaseIdentity = (
        (Test-CodexLbPathEquals -Actual ([string]$action.Execute) -Expected $pythonw) -and
        (
            [string]::IsNullOrWhiteSpace([string]$action.WorkingDirectory) -or
            (Test-CodexLbPathEquals -Actual ([string]$action.WorkingDirectory) -Expected $repoRoot)
        ) -and
        (Test-CodexLbCommandLineToken -CommandLine $arguments -Value $launcher) -and
        (Test-CodexLbCommandLineToken -CommandLine $arguments -Value $ServiceKind) -and
        (Test-CodexLbCommandLineOption -CommandLine $arguments -Name "--repo-root" -Value $repoRoot) -and
        (Test-CodexLbCommandLineOption -CommandLine $arguments -Name $portOption -Value ([string]$ListenerPort))
    )
    if (-not $matchesBaseIdentity) {
        return $false
    }
    if ($ServiceKind -eq "main") {
        if ([string]::IsNullOrWhiteSpace($ExpectedEncryptionKeyFile)) {
            return $false
        }
        $encryptionKeyFile = [System.IO.Path]::GetFullPath($ExpectedEncryptionKeyFile)
        return Test-CodexLbCommandLineOption `
            -CommandLine $arguments `
            -Name "--encryption-key-file" `
            -Value $encryptionKeyFile
    }
    if ([string]::IsNullOrWhiteSpace($ExpectedCodexHome)) {
        return $false
    }
    $codexHome = [System.IO.Path]::GetFullPath($ExpectedCodexHome).TrimEnd("\", "/")
    return Test-CodexLbCommandLineOption -CommandLine $arguments -Name "--codex-home" -Value $codexHome
}

function Test-CodexLbSupervisedListenerProcess {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Process,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedRepoRoot,
        [Parameter(Mandatory = $true)]
        [int]$ListenerPort,
        [Parameter(Mandatory = $true)]
        [ValidateSet("main", "shim")]
        [string]$ServiceKind,
        [string]$ExpectedCodexHome = "",
        [string]$ExpectedEncryptionKeyFile = ""
    )

    $repoRoot = [System.IO.Path]::GetFullPath($ExpectedRepoRoot).TrimEnd("\", "/")
    $pythonw = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
    $launcher = Join-Path $repoRoot "ops\windows\codex_lb_supervised_launcher.py"
    $commandLine = [string]$Process.CommandLine
    $portOption = if ($ServiceKind -eq "main") { "--bind-port" } else { "--listen-port" }

    $matchesBaseIdentity = (
        ([string]$Process.Name).Equals("pythonw.exe", [System.StringComparison]::OrdinalIgnoreCase) -and
        (Test-CodexLbCommandLineToken -CommandLine $commandLine -Value $pythonw) -and
        (Test-CodexLbCommandLineToken -CommandLine $commandLine -Value $launcher) -and
        (Test-CodexLbCommandLineToken -CommandLine $commandLine -Value $ServiceKind) -and
        (Test-CodexLbCommandLineOption -CommandLine $commandLine -Name "--repo-root" -Value $repoRoot) -and
        (Test-CodexLbCommandLineOption -CommandLine $commandLine -Name $portOption -Value ([string]$ListenerPort))
    )
    if (-not $matchesBaseIdentity) {
        return $false
    }
    if ($ServiceKind -eq "main") {
        if ([string]::IsNullOrWhiteSpace($ExpectedEncryptionKeyFile)) {
            return $false
        }
        $encryptionKeyFile = [System.IO.Path]::GetFullPath($ExpectedEncryptionKeyFile)
        return Test-CodexLbCommandLineOption `
            -CommandLine $commandLine `
            -Name "--encryption-key-file" `
            -Value $encryptionKeyFile
    }
    if ([string]::IsNullOrWhiteSpace($ExpectedCodexHome)) {
        return $false
    }
    $codexHome = [System.IO.Path]::GetFullPath($ExpectedCodexHome).TrimEnd("\", "/")
    return Test-CodexLbCommandLineOption -CommandLine $commandLine -Name "--codex-home" -Value $codexHome
}

function Test-CodexLbLegacyListenerProcess {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Process,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedRepoRoot,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedWorkspaceRoot,
        [Parameter(Mandatory = $true)]
        [int]$ListenerPort,
        [Parameter(Mandatory = $true)]
        [ValidateSet("main", "shim")]
        [string]$ServiceKind
    )

    $repoRoot = [System.IO.Path]::GetFullPath($ExpectedRepoRoot).TrimEnd("\", "/")
    $workspaceRoot = [System.IO.Path]::GetFullPath($ExpectedWorkspaceRoot).TrimEnd("\", "/")
    $python = Join-Path $repoRoot ".venv\Scripts\python.exe"
    $commandLine = [string]$Process.CommandLine
    if (
        -not ([string]$Process.Name).Equals("python.exe", [System.StringComparison]::OrdinalIgnoreCase) -or
        -not (Test-CodexLbCommandLineToken -CommandLine $commandLine -Value $python)
    ) {
        return $false
    }

    if ($ServiceKind -eq "main") {
        $legacyLauncher = Join-Path $repoRoot ".venv\Scripts\codex-lb.exe"
        return (
            (Test-CodexLbCommandLineToken -CommandLine $commandLine -Value $legacyLauncher) -and
            (Test-CodexLbCommandLineOption -CommandLine $commandLine -Name "--port" -Value ([string]$ListenerPort))
        )
    }

    $legacyShim = Join-Path $workspaceRoot "GPT5.6-Win11-local-runtime\codex-56-tools-proxy.py"
    return Test-CodexLbCommandLineToken -CommandLine $commandLine -Value $legacyShim
}
