from __future__ import annotations

import importlib.util
import json
import os
import socket
import subprocess
import sys
import threading
import time
import types
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows supervision scripts"),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS_ROOT = REPO_ROOT / "ops" / "windows"
POWERSHELL = "powershell.exe"


def _load_supervised_launcher():
    launcher_path = OPS_ROOT / "codex_lb_supervised_launcher.py"
    spec = importlib.util.spec_from_file_location("codex_lb_supervised_launcher_test", launcher_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_powershell(
    script: Path,
    *arguments: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _run_powershell_command(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def _unused_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


class _UnauthorizedHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class _SlowHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        time.sleep(2)
        try:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, format: str, *args: object) -> None:
        return


@pytest.fixture
def unauthorized_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UnauthorizedHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def slow_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(server.server_address[1])
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _write_recovery_probe(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    recovery_script = tmp_path / "record recovery.ps1"
    recovery_marker = tmp_path / "recovery.json"
    recovery_script.write_text(
        """
param(
    [string]$TaskName,
    [string]$ServiceKind,
    [int]$ListenerPort,
    [string]$ExpectedRepoRoot,
    [string]$ExpectedCodexHome,
    [string]$ExpectedEncryptionKeyFile,
    [string]$ProbeUri,
    [string]$ExpectedStatusCodes,
    [int]$ProbeTimeoutSeconds
)
[pscustomobject]@{
    TaskName = $TaskName
    ServiceKind = $ServiceKind
    ListenerPort = $ListenerPort
    ExpectedRepoRoot = $ExpectedRepoRoot
    ExpectedCodexHome = $ExpectedCodexHome
    ExpectedEncryptionKeyFile = $ExpectedEncryptionKeyFile
    ProbeUri = $ProbeUri
    ExpectedStatusCodes = $ExpectedStatusCodes
    ProbeTimeoutSeconds = $ProbeTimeoutSeconds
} | ConvertTo-Json | Set-Content -LiteralPath $env:CODEX_LB_TEST_RECOVERY_MARKER -Encoding UTF8
""".strip(),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["CODEX_LB_TEST_RECOVERY_MARKER"] = str(recovery_marker)
    return recovery_script, recovery_marker, environment


def test_supervision_installer_quotes_paths_with_spaces(tmp_path: Path) -> None:
    installer = OPS_ROOT / "install-codex-lb-supervision.ps1"
    data_root = tmp_path / "data root"
    codex_home = REPO_ROOT.parent / "test codex home"
    encryption_key = REPO_ROOT.parent / "test encryption.key"

    result = _run_powershell(
        installer,
        "-RepoRoot",
        str(REPO_ROOT),
        "-WorkspaceRoot",
        str(REPO_ROOT.parent),
        "-DataRoot",
        str(data_root),
        "-CodexHome",
        str(codex_home),
        "-EncryptionKeyFile",
        str(encryption_key),
        "-MainPort",
        "2456",
        "-ShimPort",
        "15957",
        "-TaskNamePrefix",
        "codex-lb-staging-2456",
        "-DryRun",
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert len(plan["tasks"]) == 4
    for task in plan["tasks"]:
        assert f'"{REPO_ROOT}' in task["arguments"]
        assert "-File F:\\agent" not in task["arguments"]
    service_tasks = [task for task in plan["tasks"] if task["kind"] == "service"]
    assert service_tasks
    for task in service_tasks:
        assert Path(task["execute"]) == REPO_ROOT / ".venv" / "Scripts" / "pythonw.exe"
        assert "codex_lb_supervised_launcher.py" in task["arguments"]
        assert "powershell" not in task["execute"].lower()
    watchdog_tasks = [task for task in plan["tasks"] if task["kind"] == "watchdog"]
    assert watchdog_tasks
    for task in watchdog_tasks:
        assert Path(task["execute"]) == REPO_ROOT / ".venv" / "Scripts" / "pythonw.exe"
        assert "codex_lb_no_window_powershell.py" in task["arguments"]
        assert "codex-lb-hidden-launcher.vbs" not in task["arguments"]
        assert "powershell.exe" not in task["execute"].lower()
    assert plan["main_port"] == 2456
    assert plan["shim_port"] == 15957
    assert plan["bind_host"] == "0.0.0.0"
    assert plan["codex_home"] == str(codex_home.resolve())
    assert plan["encryption_key_file"] == str(encryption_key.resolve())


def test_supervision_installer_propagates_runtime_options(tmp_path: Path) -> None:
    installer = OPS_ROOT / "install-codex-lb-supervision.ps1"
    result = _run_powershell(
        installer,
        "-RepoRoot",
        str(REPO_ROOT),
        "-WorkspaceRoot",
        str(REPO_ROOT.parent),
        "-DataRoot",
        str(tmp_path / "data root"),
        "-CodexHome",
        str(tmp_path / "codex home"),
        "-EncryptionKeyFile",
        str(tmp_path / "encryption.key"),
        "-MainPort",
        "2456",
        "-ShimPort",
        "15957",
        "-OutboundProxy",
        "http://127.0.0.1:9999",
        "-Codex56ProxyMaxBodyBytes",
        "67108864",
        "-ProxyUnauthenticatedClientCidrs",
        "192.0.2.0/24",
        "-LogProxyRequestShape",
        "-DryRun",
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    main_task = next(task for task in plan["tasks"] if task["name"].endswith("-main"))
    shim_task = next(task for task in plan["tasks"] if task["name"].endswith("-shim"))
    assert "--outbound-proxy http://127.0.0.1:9999" in main_task["arguments"]
    assert "--proxy-unauthenticated-client-cidrs 192.0.2.0/24" in main_task["arguments"]
    assert "--log-proxy-request-shape" in main_task["arguments"]
    assert "--max-body-bytes 67108864" in shim_task["arguments"]


def test_watchdog_launcher_creates_no_console_window(tmp_path: Path) -> None:
    launcher = OPS_ROOT / "codex_lb_no_window_powershell.py"
    probe_script = tmp_path / "console probe.ps1"
    result_path = tmp_path / "console handle.txt"
    probe_script.write_text(
        """
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class NativeConsole {
    [DllImport("kernel32.dll")]
    public static extern IntPtr GetConsoleWindow();
}
"@
[NativeConsole]::GetConsoleWindow().ToInt64() |
    Set-Content -LiteralPath $env:CODEX_LB_CONSOLE_PROBE -Encoding Ascii
""".strip(),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["CODEX_LB_CONSOLE_PROBE"] = str(result_path)

    result = subprocess.run(
        [sys.executable, str(launcher), str(probe_script)],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result_path.read_text(encoding="ascii").strip() == "0"


def test_watchdog_state_is_namespaced_by_task_prefix_and_ports(tmp_path: Path) -> None:
    installer = OPS_ROOT / "install-codex-lb-supervision.ps1"
    data_root = tmp_path / "shared data root"

    def _plan(prefix: str, main_port: int, shim_port: int) -> dict[str, Any]:
        result = _run_powershell(
            installer,
            "-RepoRoot",
            str(REPO_ROOT),
            "-WorkspaceRoot",
            str(REPO_ROOT.parent),
            "-DataRoot",
            str(data_root),
            "-CodexHome",
            str(tmp_path / "codex home"),
            "-EncryptionKeyFile",
            str(tmp_path / "encryption.key"),
            "-MainPort",
            str(main_port),
            "-ShimPort",
            str(shim_port),
            "-TaskNamePrefix",
            prefix,
            "-DryRun",
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    first = _plan("codex-lb-staging-a", 2456, 15957)
    second = _plan("codex-lb-staging-b", 2458, 15958)
    first_watchdogs = [task for task in first["tasks"] if task["kind"] == "watchdog"]
    second_watchdogs = [task for task in second["tasks"] if task["kind"] == "watchdog"]
    first_arguments = "\n".join(task["arguments"] for task in first_watchdogs)
    second_arguments = "\n".join(task["arguments"] for task in second_watchdogs)

    assert "codex-lb-staging-a-2456-15957" in first_arguments
    assert "codex-lb-staging-b-2458-15958" in second_arguments
    assert first_arguments != second_arguments


def test_supervised_restart_dry_run_describes_isolated_cutover(tmp_path: Path) -> None:
    restart = OPS_ROOT / "restart-codex-lb-supervision.ps1"
    data_root = tmp_path / "restart data"

    result = _run_powershell(
        restart,
        "-RepoRoot",
        str(REPO_ROOT),
        "-WorkspaceRoot",
        str(REPO_ROOT.parent),
        "-DataRoot",
        str(data_root),
        "-CodexHome",
        str(tmp_path / "codex home"),
        "-EncryptionKeyFile",
        str(tmp_path / "encryption.key"),
        "-MainPort",
        "2456",
        "-ShimPort",
        "15957",
        "-BindHost",
        "127.0.0.1",
        "-TaskNamePrefix",
        "codex-lb-staging-2456",
        "-DryRun",
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["main_task"] == "codex-lb-staging-2456-main"
    assert plan["shim_task"] == "codex-lb-staging-2456-shim"
    assert plan["main_probe"] == "http://127.0.0.1:2456/v1/models"
    assert plan["shim_probe"] == "http://127.0.0.1:15957/v1/models"
    assert plan["main_port"] == 2456
    assert plan["shim_port"] == 15957


def test_supervised_restart_dry_run_preserves_runtime_options(tmp_path: Path) -> None:
    restart = OPS_ROOT / "restart-codex-lb-supervision.ps1"
    result = _run_powershell(
        restart,
        "-RepoRoot",
        str(REPO_ROOT),
        "-WorkspaceRoot",
        str(REPO_ROOT.parent),
        "-DataRoot",
        str(tmp_path / "data root"),
        "-CodexHome",
        str(tmp_path / "codex home"),
        "-EncryptionKeyFile",
        str(tmp_path / "encryption.key"),
        "-MainPort",
        "2456",
        "-ShimPort",
        "15957",
        "-OutboundProxy",
        "http://127.0.0.1:9999",
        "-Codex56ProxyMaxBodyBytes",
        "67108864",
        "-ProxyUnauthenticatedClientCidrs",
        "192.0.2.0/24",
        "-LogProxyRequestShape",
        "-StartupTimeoutSeconds",
        "47",
        "-StartupPollIntervalMilliseconds",
        "333",
        "-DryRun",
    )

    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["outbound_proxy"] == "http://127.0.0.1:9999"
    assert plan["codex56_proxy_max_body_bytes"] == 67108864
    assert plan["proxy_unauthenticated_client_cidrs"] == "192.0.2.0/24"
    assert plan["log_proxy_request_shape"] is True
    assert plan["startup_timeout_seconds"] == 47
    assert plan["startup_poll_interval_milliseconds"] == 333


def test_main_launcher_uses_explicit_encryption_key_file(tmp_path: Path) -> None:
    launcher = OPS_ROOT / "codex_lb_supervised_launcher.py"
    encryption_key = tmp_path / "canonical encryption.key"
    result = subprocess.run(
        [
            sys.executable,
            str(launcher),
            "main",
            "--repo-root",
            str(REPO_ROOT),
            "--data-root",
            str(tmp_path / "data root"),
            "--encryption-key-file",
            str(encryption_key),
            "--bind-host",
            "127.0.0.1",
            "--bind-port",
            "2456",
            "--dry-run",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    configuration = json.loads(result.stdout)
    assert Path(configuration["encryption_key_file"]).resolve() == encryption_key.resolve()


def test_main_launcher_maps_runtime_options_to_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = _load_supervised_launcher()
    repo_root = tmp_path / "repo"
    data_root = tmp_path / "data"
    encryption_key = tmp_path / "encryption.key"
    repo_root.mkdir()
    data_root.mkdir()
    encryption_key.write_text("test", encoding="utf-8")
    captured: dict[str, object] = {}
    fake_cli = types.ModuleType("app.cli")

    def fake_main(arguments: list[str]) -> None:
        captured["arguments"] = arguments
        captured["trace"] = os.environ.get("CODEX_LB_TRACE")
        captured["cidrs"] = os.environ.get("CODEX_LB_PROXY_UNAUTHENTICATED_CLIENT_CIDRS")
        captured["proxy_env"] = {
            name: os.environ.get(name) for name in ("all_proxy", "socks_proxy", "https_proxy", "http_proxy")
        }

    setattr(fake_cli, "main", fake_main)
    monkeypatch.setitem(sys.modules, "app.cli", fake_cli)
    monkeypatch.setattr(launcher, "_redirect_output", lambda *_args: ())

    launcher._run_main(
        Namespace(
            repo_root=repo_root,
            data_root=data_root,
            encryption_key_file=encryption_key,
            bind_host="127.0.0.1",
            bind_port=2456,
            outbound_proxy="http://127.0.0.1:9999",
            proxy_unauthenticated_client_cidrs="192.0.2.0/24",
            log_proxy_request_shape=True,
            dry_run=False,
        )
    )

    assert captured == {
        "arguments": ["--host", "127.0.0.1", "--port", "2456"],
        "trace": "shape",
        "cidrs": "192.0.2.0/24",
        "proxy_env": {
            "all_proxy": "http://127.0.0.1:9999",
            "socks_proxy": None,
            "https_proxy": "http://127.0.0.1:9999",
            "http_proxy": "http://127.0.0.1:9999",
        },
    }


def test_shim_launcher_uses_explicit_codex_home_for_tools_cache(tmp_path: Path) -> None:
    launcher = OPS_ROOT / "codex_lb_supervised_launcher.py"
    codex_home = tmp_path / "canonical codex home"
    result = subprocess.run(
        [
            sys.executable,
            str(launcher),
            "shim",
            "--repo-root",
            str(REPO_ROOT),
            "--workspace-root",
            str(REPO_ROOT.parent),
            "--data-root",
            str(tmp_path / "data root"),
            "--codex-home",
            str(codex_home),
            "--listen-port",
            "15957",
            "--upstream-port",
            "2456",
            "--dry-run",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    configuration = json.loads(result.stdout)
    assert configuration["tools_cache"] == str(codex_home.resolve() / "cache" / "codex-56-tools.json")


def test_shim_launcher_accepts_custom_max_body_bytes(tmp_path: Path) -> None:
    launcher = OPS_ROOT / "codex_lb_supervised_launcher.py"
    result = subprocess.run(
        [
            sys.executable,
            str(launcher),
            "shim",
            "--repo-root",
            str(REPO_ROOT),
            "--workspace-root",
            str(REPO_ROOT.parent),
            "--data-root",
            str(tmp_path / "data root"),
            "--codex-home",
            str(tmp_path / "codex home"),
            "--listen-port",
            "15957",
            "--upstream-port",
            "2456",
            "--max-body-bytes",
            "67108864",
            "--dry-run",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["max_body_bytes"] == 67108864


def test_main_launcher_preserves_canonical_proxy_environment_names() -> None:
    launcher = (OPS_ROOT / "codex_lb_supervised_launcher.py").read_text(encoding="utf-8")

    assert '("all_proxy", "socks_proxy", "https_proxy", "http_proxy")' in launcher


def test_supervised_restart_waits_for_new_watchdog_invocation() -> None:
    restart = (OPS_ROOT / "restart-codex-lb-supervision.ps1").read_text(encoding="utf-8")

    assert "[DateTime]$PreviousRunTime" in restart
    assert "$info.LastRunTime -gt $PreviousRunTime" in restart
    assert "Wait-Watchdog -TaskName $mainWatchdogTaskName -PreviousRunTime" in restart
    assert "Wait-Watchdog -TaskName $shimWatchdogTaskName -PreviousRunTime" in restart


def test_supervised_restart_rolls_back_legacy_or_restores_watchdogs() -> None:
    restart = (OPS_ROOT / "restart-codex-lb-supervision.ps1").read_text(encoding="utf-8")

    assert "$existingLegacyTaskNames" in restart
    assert "function Restore-LegacyTasks" in restart
    assert "catch {" in restart
    assert "Stop-ScheduledTask -TaskName $shimTaskName" in restart
    assert "Stop-ScheduledTask -TaskName $mainTaskName" in restart
    assert "Disable-ScheduledTask -TaskName $mainWatchdogTaskName" in restart
    assert "Enable-ScheduledTask -TaskName $legacyTaskName" in restart
    assert "Start-ScheduledTask -TaskName $legacyTaskName" in restart
    assert "if ($existingLegacyTaskNames.Count -gt 0)" in restart
    assert "Restore-Watchdogs" in restart
    assert "$mainListener.LocalAddress" in restart
    assert "$BindHost" in restart
    assert "$shimListener.LocalAddress" in restart
    assert '"127.0.0.1"' in restart


def test_supervised_restart_normalizes_comma_delimited_legacy_tasks(tmp_path: Path) -> None:
    restart = OPS_ROOT / "restart-codex-lb-supervision.ps1"
    result = _run_powershell(
        restart,
        "-RepoRoot",
        str(REPO_ROOT),
        "-WorkspaceRoot",
        str(REPO_ROOT.parent),
        "-DataRoot",
        str(tmp_path / "data root"),
        "-CodexHome",
        str(tmp_path / "codex home"),
        "-EncryptionKeyFile",
        str(tmp_path / "encryption.key"),
        "-MainPort",
        "2456",
        "-ShimPort",
        "15957",
        "-LegacyTaskNames",
        "legacy-a,legacy-b",
        "-DryRun",
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["legacy_tasks"] == ["legacy-a", "legacy-b"]


def test_recovery_budget_covers_production_sized_database_startup() -> None:
    installer = (OPS_ROOT / "install-codex-lb-supervision.ps1").read_text(encoding="utf-8")
    restart = (OPS_ROOT / "restart-codex-lb-supervision.ps1").read_text(encoding="utf-8")
    watchdog = (OPS_ROOT / "codex-lb-request-watchdog.ps1").read_text(encoding="utf-8")
    recovery = (OPS_ROOT / "codex-lb-recover-task.ps1").read_text(encoding="utf-8")

    assert "[int]$StartupTimeoutSeconds = 180" in restart
    assert "[int]$RecoveryTimeoutSeconds = 180" in watchdog
    assert "[int]$RecoveryTimeoutSeconds = 180" in recovery
    assert '"-RecoveryTimeoutSeconds", [string]$RecoveryTimeoutSeconds' in watchdog
    assert "-ExecutionTimeLimit (New-TimeSpan -Minutes 5)" in installer


def test_exact_supervised_process_identity_rejects_same_repo_wrong_launcher() -> None:
    identity_helper = OPS_ROOT / "codex-lb-process-identity.ps1"
    repo_root = str(REPO_ROOT)
    pythonw = REPO_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    launcher = OPS_ROOT / "codex_lb_supervised_launcher.py"
    valid_command = (
        f'"{pythonw}" "{launcher}" main --repo-root "{repo_root}" '
        '--data-root C:\\staging --encryption-key-file "C:\\canonical\\encryption.key" '
        "--bind-host 127.0.0.1 --bind-port 2456"
    )
    wrong_command = (
        f'"{pythonw}" "{OPS_ROOT / "other_launcher.py"}" main --repo-root "{repo_root}" '
        '--data-root C:\\staging --encryption-key-file "C:\\canonical\\encryption.key" '
        "--bind-host 127.0.0.1 --bind-port 2456"
    )

    def _identity_result(command_line: str) -> subprocess.CompletedProcess[str]:
        escaped_helper = str(identity_helper).replace("'", "''")
        escaped_repo = repo_root.replace("'", "''")
        escaped_executable = str(pythonw).replace("'", "''")
        escaped_command = command_line.replace("'", "''")
        command = (
            f". '{escaped_helper}'; "
            "$process = [pscustomobject]@{ "
            "Name = 'pythonw.exe'; "
            f"ExecutablePath = '{escaped_executable}'; "
            f"CommandLine = '{escaped_command}' "
            "}; "
            f"if (Test-CodexLbSupervisedListenerProcess -Process $process "
            f"-ExpectedRepoRoot '{escaped_repo}' -ListenerPort 2456 -ServiceKind main "
            "-ExpectedEncryptionKeyFile 'C:\\canonical\\encryption.key') "
            "{ exit 0 } else { exit 42 }"
        )
        return _run_powershell_command(command)

    valid = _identity_result(valid_command)
    wrong = _identity_result(wrong_command)

    assert valid.returncode == 0, valid.stderr
    assert wrong.returncode == 42, wrong.stderr


def test_exact_shim_identity_requires_expected_codex_home() -> None:
    identity_helper = OPS_ROOT / "codex-lb-process-identity.ps1"
    pythonw = REPO_ROOT / ".venv" / "Scripts" / "pythonw.exe"
    launcher = OPS_ROOT / "codex_lb_supervised_launcher.py"
    expected_codex_home = REPO_ROOT.parent / "canonical-codex-home"
    command_line = (
        f'"{pythonw}" "{launcher}" shim --repo-root "{REPO_ROOT}" '
        f'--workspace-root "{REPO_ROOT.parent}" --data-root C:\\staging '
        f'--codex-home "{expected_codex_home}" --listen-port 15957 --upstream-port 2456'
    )

    def _result(codex_home: Path) -> subprocess.CompletedProcess[str]:
        command = (
            f". '{identity_helper}'; "
            "$process = [pscustomobject]@{ "
            "Name = 'pythonw.exe'; "
            f"CommandLine = '{command_line}' "
            "}; "
            "if (Test-CodexLbSupervisedListenerProcess -Process $process "
            f"-ExpectedRepoRoot '{REPO_ROOT}' -ListenerPort 15957 -ServiceKind shim "
            f"-ExpectedCodexHome '{codex_home}') "
            "{ exit 0 } else { exit 42 }"
        )
        return _run_powershell_command(command)

    valid = _result(expected_codex_home)
    wrong = _result(REPO_ROOT.parent / "wrong-codex-home")

    assert valid.returncode == 0, valid.stderr
    assert wrong.returncode == 42, wrong.stderr


def test_recovery_requires_service_kind_and_exact_identity_contract() -> None:
    recovery = (OPS_ROOT / "codex-lb-recover-task.ps1").read_text(encoding="utf-8")
    watchdog = (OPS_ROOT / "codex-lb-request-watchdog.ps1").read_text(encoding="utf-8")

    assert '[ValidateSet("main", "shim")]' in recovery
    assert "Test-CodexLbSupervisedTaskAction" in recovery
    assert "Test-CodexLbSupervisedListenerProcess" in recovery
    assert '"-ServiceKind", $ServiceKind' in watchdog


def test_watchdog_recovers_only_after_second_failure(tmp_path: Path) -> None:
    watchdog = OPS_ROOT / "codex-lb-request-watchdog.ps1"
    recovery_script, recovery_marker, environment = _write_recovery_probe(tmp_path)
    state_path = tmp_path / "watchdog state.json"
    port = _unused_port()
    arguments = (
        "-ProbeUri",
        f"http://127.0.0.1:{port}/v1/models",
        "-TaskName",
        "codex-lb-staging-main",
        "-ServiceKind",
        "main",
        "-ListenerPort",
        str(port),
        "-ExpectedRepoRoot",
        str(REPO_ROOT),
        "-ExpectedCodexHome",
        str(REPO_ROOT.parent),
        "-ExpectedEncryptionKeyFile",
        str(tmp_path / "encryption.key"),
        "-StatePath",
        str(state_path),
        "-RecoveryScript",
        str(recovery_script),
        "-FailureThreshold",
        "2",
        "-ProbeTimeoutSeconds",
        "1",
    )

    first = _run_powershell(watchdog, *arguments, env=environment)
    assert first.returncode == 0, first.stderr
    assert json.loads(state_path.read_text(encoding="utf-8-sig"))["consecutive_failures"] == 1
    assert not recovery_marker.exists()

    second = _run_powershell(watchdog, *arguments, env=environment)
    assert second.returncode == 0, second.stderr
    recovery = json.loads(recovery_marker.read_text(encoding="utf-8-sig"))
    assert recovery["TaskName"] == "codex-lb-staging-main"
    assert recovery["ListenerPort"] == port
    assert not state_path.exists()


def test_watchdog_treats_hung_request_path_as_failure(tmp_path: Path, slow_server: int) -> None:
    watchdog = OPS_ROOT / "codex-lb-request-watchdog.ps1"
    recovery_script, recovery_marker, environment = _write_recovery_probe(tmp_path)
    state_path = tmp_path / "hung-request-state.json"
    arguments = (
        "-ProbeUri",
        f"http://127.0.0.1:{slow_server}/v1/models",
        "-TaskName",
        f"codex-lb-hung-{os.getpid()}-{slow_server}",
        "-ServiceKind",
        "main",
        "-ListenerPort",
        str(slow_server),
        "-ExpectedRepoRoot",
        str(REPO_ROOT),
        "-ExpectedCodexHome",
        str(REPO_ROOT.parent),
        "-ExpectedEncryptionKeyFile",
        str(tmp_path / "encryption.key"),
        "-StatePath",
        str(state_path),
        "-RecoveryScript",
        str(recovery_script),
        "-FailureThreshold",
        "2",
        "-ProbeTimeoutSeconds",
        "1",
    )

    first = _run_powershell(watchdog, *arguments, env=environment)
    second = _run_powershell(watchdog, *arguments, env=environment)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert recovery_marker.exists()
    assert not state_path.exists()


def test_watchdog_success_resets_failure_state(tmp_path: Path, unauthorized_server: int) -> None:
    watchdog = OPS_ROOT / "codex-lb-request-watchdog.ps1"
    recovery_script, recovery_marker, environment = _write_recovery_probe(tmp_path)
    state_path = tmp_path / "watchdog-state.json"
    state_path.write_text('{"consecutive_failures":1}', encoding="utf-8")

    result = _run_powershell(
        watchdog,
        "-ProbeUri",
        f"http://127.0.0.1:{unauthorized_server}/v1/models",
        "-TaskName",
        "codex-lb-staging-main",
        "-ServiceKind",
        "main",
        "-ListenerPort",
        str(unauthorized_server),
        "-ExpectedRepoRoot",
        str(REPO_ROOT),
        "-ExpectedCodexHome",
        str(REPO_ROOT.parent),
        "-ExpectedEncryptionKeyFile",
        str(tmp_path / "encryption.key"),
        "-StatePath",
        str(state_path),
        "-RecoveryScript",
        str(recovery_script),
        "-FailureThreshold",
        "2",
        "-ProbeTimeoutSeconds",
        "1",
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert not state_path.exists()
    assert not recovery_marker.exists()


def test_watchdog_releases_mutex_after_healthy_early_return(tmp_path: Path, unauthorized_server: int) -> None:
    watchdog = OPS_ROOT / "codex-lb-request-watchdog.ps1"
    recovery_script, _recovery_marker, environment = _write_recovery_probe(tmp_path)
    state_path = tmp_path / "mutex-state.json"
    arguments = (
        "-ProbeUri",
        f"http://127.0.0.1:{unauthorized_server}/v1/models",
        "-TaskName",
        f"codex-lb-mutex-{os.getpid()}-{unauthorized_server}",
        "-ServiceKind",
        "main",
        "-ListenerPort",
        str(unauthorized_server),
        "-ExpectedRepoRoot",
        str(REPO_ROOT),
        "-ExpectedCodexHome",
        str(REPO_ROOT.parent),
        "-ExpectedEncryptionKeyFile",
        str(tmp_path / "encryption.key"),
        "-StatePath",
        str(state_path),
        "-RecoveryScript",
        str(recovery_script),
        "-ProbeTimeoutSeconds",
        "1",
    )

    first = _run_powershell(watchdog, *arguments, env=environment)
    second = _run_powershell(watchdog, *arguments, env=environment)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "watchdog healthy" in first.stdout
    assert "watchdog healthy" in second.stdout
    assert "watchdog skipped" not in second.stdout


def test_watchdog_does_not_restart_shim_while_main_request_path_is_down(tmp_path: Path) -> None:
    watchdog = OPS_ROOT / "codex-lb-request-watchdog.ps1"
    recovery_script, recovery_marker, environment = _write_recovery_probe(tmp_path)
    state_path = tmp_path / "shim-watchdog-state.json"
    shim_port = _unused_port()
    main_port = _unused_port()
    arguments = (
        "-ProbeUri",
        f"http://127.0.0.1:{shim_port}/v1/models",
        "-DependencyProbeUri",
        f"http://127.0.0.1:{main_port}/v1/models",
        "-TaskName",
        "codex-lb-staging-shim",
        "-ServiceKind",
        "shim",
        "-ListenerPort",
        str(shim_port),
        "-ExpectedRepoRoot",
        str(REPO_ROOT),
        "-ExpectedCodexHome",
        str(REPO_ROOT.parent),
        "-ExpectedEncryptionKeyFile",
        str(tmp_path / "encryption.key"),
        "-StatePath",
        str(state_path),
        "-RecoveryScript",
        str(recovery_script),
        "-FailureThreshold",
        "2",
        "-ProbeTimeoutSeconds",
        "1",
    )

    first = _run_powershell(watchdog, *arguments, env=environment)
    second = _run_powershell(watchdog, *arguments, env=environment)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert not state_path.exists()
    assert not recovery_marker.exists()
