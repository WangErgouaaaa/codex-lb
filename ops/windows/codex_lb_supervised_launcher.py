from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
from pathlib import Path
from typing import TextIO


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a codex-lb process directly under Windows Task Scheduler.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    main = subparsers.add_parser("main")
    main.add_argument("--repo-root", type=Path, required=True)
    main.add_argument("--data-root", type=Path, required=True)
    main.add_argument("--encryption-key-file", type=Path, required=True)
    main.add_argument("--bind-host", choices=("0.0.0.0", "127.0.0.1"), default="0.0.0.0")
    main.add_argument("--bind-port", type=int, required=True)
    main.add_argument("--outbound-proxy", default="http://127.0.0.1:8800")
    main.add_argument("--dry-run", action="store_true")

    shim = subparsers.add_parser("shim")
    shim.add_argument("--repo-root", type=Path, required=True)
    shim.add_argument("--workspace-root", type=Path, required=True)
    shim.add_argument("--data-root", type=Path, required=True)
    shim.add_argument("--codex-home", type=Path, required=True)
    shim.add_argument("--listen-port", type=int, required=True)
    shim.add_argument("--upstream-port", type=int, required=True)
    shim.add_argument("--max-body-bytes", type=int, default=128 * 1024 * 1024)
    shim.add_argument("--dry-run", action="store_true")
    return parser


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _redirect_output(logs_root: Path, prefix: str) -> tuple[TextIO, TextIO]:
    logs_root.mkdir(parents=True, exist_ok=True)
    timestamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
    stdout = (logs_root / f"{prefix}-{timestamp}.out.log").open("a", encoding="utf-8", buffering=1)
    stderr = (logs_root / f"{prefix}-{timestamp}.err.log").open("a", encoding="utf-8", buffering=1)
    os.dup2(stdout.fileno(), 1)
    os.dup2(stderr.fileno(), 2)
    sys.stdout = stdout
    sys.stderr = stderr
    return stdout, stderr


def _main_configuration(args: argparse.Namespace) -> dict[str, object]:
    repo_root = _resolved(args.repo_root)
    data_root = _resolved(args.data_root)
    encryption_key = _resolved(args.encryption_key_file)
    database_path = data_root / "store.db"
    return {
        "command": "main",
        "repo_root": str(repo_root),
        "data_root": str(data_root),
        "bind_host": args.bind_host,
        "bind_port": args.bind_port,
        "database_url": f"sqlite+aiosqlite:///{database_path.as_posix()}",
        "encryption_key_file": encryption_key.as_posix(),
        "outbound_proxy": args.outbound_proxy,
    }


def _shim_configuration(args: argparse.Namespace) -> dict[str, object]:
    repo_root = _resolved(args.repo_root)
    workspace_root = _resolved(args.workspace_root)
    data_root = _resolved(args.data_root)
    codex_home = _resolved(args.codex_home)
    return {
        "command": "shim",
        "repo_root": str(repo_root),
        "workspace_root": str(workspace_root),
        "data_root": str(data_root),
        "codex_home": str(codex_home),
        "listen_port": args.listen_port,
        "upstream_port": args.upstream_port,
        "max_body_bytes": args.max_body_bytes,
        "proxy_script": str(workspace_root / "GPT5.6-Win11-local-runtime" / "codex-56-tools-proxy.py"),
        "tools_cache": str(codex_home / "cache" / "codex-56-tools.json"),
    }


def _run_main(args: argparse.Namespace) -> None:
    configuration = _main_configuration(args)
    if args.dry_run:
        print(json.dumps(configuration))
        return

    repo_root = Path(str(configuration["repo_root"]))
    data_root = Path(str(configuration["data_root"]))
    encryption_key = Path(str(configuration["encryption_key_file"]))
    if not repo_root.is_dir():
        raise SystemExit(f"Repository root not found: {repo_root}")
    if not encryption_key.is_file():
        raise SystemExit(f"Encryption key not found: {encryption_key}")

    os.chdir(repo_root)
    os.environ.update(
        {
            "CODEX_LB_SERVICE_TASK": "1",
            "CODEX_LB_DATABASE_URL": str(configuration["database_url"]),
            "CODEX_LB_ENCRYPTION_KEY_FILE": str(configuration["encryption_key_file"]),
            "CODEX_LB_TRACE": "",
            "CODEX_LB_STRICT_SERVICE_TIER_ACCOUNT_FILTER": "false",
            "CODEX_LB_STREAM_IDLE_TIMEOUT_SECONDS": "300",
        }
    )
    outbound_proxy = str(configuration["outbound_proxy"])
    if outbound_proxy:
        for name in ("all_proxy", "socks_proxy", "https_proxy", "http_proxy"):
            os.environ[name] = outbound_proxy

    streams = _redirect_output(data_root / "logs", f"codex-lb-{args.bind_port}")
    try:
        from app.cli import main as codex_lb_main

        codex_lb_main(["--host", args.bind_host, "--port", str(args.bind_port)])
    finally:
        for stream in streams:
            stream.flush()


def _run_shim(args: argparse.Namespace) -> None:
    configuration = _shim_configuration(args)
    if args.dry_run:
        print(json.dumps(configuration))
        return

    repo_root = Path(str(configuration["repo_root"]))
    data_root = Path(str(configuration["data_root"]))
    proxy_script = Path(str(configuration["proxy_script"]))
    if not repo_root.is_dir():
        raise SystemExit(f"Repository root not found: {repo_root}")
    if not proxy_script.is_file():
        raise SystemExit(f"Proxy script not found: {proxy_script}")

    os.chdir(repo_root)
    os.environ.update(
        {
            "CODEX_56_PROXY_HOST": "127.0.0.1",
            "CODEX_56_PROXY_PORT": str(args.listen_port),
            "CODEX_56_PROXY_UPSTREAM": f"http://127.0.0.1:{args.upstream_port}",
            "CODEX_56_TOOLS_CACHE": str(configuration["tools_cache"]),
            "CODEX_56_STRIP_RESPONSES_LITE": "1",
            "CODEX_56_PROXY_MAX_BODY_BYTES": str(args.max_body_bytes),
        }
    )
    streams = _redirect_output(data_root / "logs", f"codex-56-tools-proxy-{args.listen_port}")
    try:
        sys.argv = [str(proxy_script)]
        runpy.run_path(str(proxy_script), run_name="__main__")
    finally:
        for stream in streams:
            stream.flush()


def main() -> None:
    args = _parser().parse_args()
    if args.command == "main":
        _run_main(args)
    else:
        _run_shim(args)


if __name__ == "__main__":
    main()
