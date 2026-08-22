from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TEST_PORT = 2456
STARTUP_TIMEOUT_SECONDS = 90.0


@dataclass(slots=True)
class UpstreamCall:
    account_id: str
    payload: dict[str, Any]
    turn_state_header: str | None


@dataclass(slots=True)
class FakeUpstreamState:
    scenario_name: str = "positive"
    owner_account_id: str | None = None
    owner_request_count: int = 0
    calls: list[UpstreamCall] = field(default_factory=list)


FAKE_UPSTREAM_STATE = FakeUpstreamState()
fake_upstream_app = FastAPI()


def _record_upstream_call(
    *,
    account_id: str,
    payload: dict[str, Any],
    turn_state_header: str | None,
) -> tuple[FakeUpstreamState, bool]:
    state = FAKE_UPSTREAM_STATE
    state.calls.append(
        UpstreamCall(
            account_id=account_id,
            payload=payload,
            turn_state_header=turn_state_header,
        )
    )
    if state.owner_account_id is None:
        state.owner_account_id = account_id
    if account_id == state.owner_account_id:
        state.owner_request_count += 1
    owner_quota_threshold = 3 if state.scenario_name == "positive_http" else 2
    owner_quota = account_id == state.owner_account_id and state.owner_request_count >= owner_quota_threshold
    return state, owner_quota


def _fake_response_id(state: FakeUpstreamState, account_id: str) -> str:
    role = "initial" if account_id == state.owner_account_id else "recovered"
    return f"resp_{role}_{state.scenario_name}_2456"


@fake_upstream_app.websocket("/backend-api/codex/responses")
async def fake_responses(websocket: WebSocket) -> None:
    account_id = (websocket.headers.get("chatgpt-account-id") or "").strip()
    turn_state_header = websocket.headers.get("x-codex-turn-state")
    if not account_id:
        raise RuntimeError("codex-lb did not forward chatgpt-account-id")
    await websocket.accept(headers=[(b"x-codex-turn-state", f"upstream-{account_id}".encode())])
    try:
        while True:
            payload = json.loads(await websocket.receive_text())
            state, owner_quota = _record_upstream_call(
                account_id=account_id,
                payload=payload,
                turn_state_header=turn_state_header,
            )
            if owner_quota:
                await websocket.send_json(
                    {
                        "type": "error",
                        "code": "usage_limit_reached",
                        "error_type": "rate_limit_error",
                        "message": "controlled 2456 quota exhaustion",
                    }
                )
                continue

            response_id = _fake_response_id(state, account_id)
            await websocket.send_json(
                {
                    "type": "response.created",
                    "response": {"id": response_id, "status": "in_progress"},
                }
            )
            await websocket.send_json(
                {
                    "type": "response.completed",
                    "response": {
                        "id": response_id,
                        "status": "completed",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [{"type": "output_text", "text": "isolated success"}],
                            }
                        ],
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    },
                }
            )
    except WebSocketDisconnect:
        return


@fake_upstream_app.post("/backend-api/codex/responses", response_model=None)
async def fake_http_responses(request: Request) -> StreamingResponse | JSONResponse:
    account_id = (request.headers.get("chatgpt-account-id") or "").strip()
    if not account_id:
        raise RuntimeError("codex-lb did not forward chatgpt-account-id")
    payload = await request.json()
    if not isinstance(payload, dict):
        raise RuntimeError("codex-lb did not send a JSON object upstream")
    state, owner_quota = _record_upstream_call(
        account_id=account_id,
        payload=payload,
        turn_state_header=request.headers.get("x-codex-turn-state"),
    )
    if owner_quota:
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "type": "usage_limit_reached",
                    "code": "usage_limit_reached",
                    "message": "controlled 2456 quota exhaustion",
                }
            },
        )

    response_id = _fake_response_id(state, account_id)
    events = [
        {
            "type": "response.created",
            "response": {"id": response_id, "status": "in_progress"},
        },
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "isolated success"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            },
        },
    ]

    async def event_stream():
        for event in events:
            yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"x-codex-turn-state": f"upstream-{account_id}"},
    )


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _assert_test_port_available() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", TEST_PORT))
        except OSError as exc:
            raise RuntimeError(f"isolated test port {TEST_PORT} is already in use") from exc


def _isolated_environment(
    data_dir: Path,
    upstream_port: int,
    *,
    upstream_transport: str,
) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("CODEX_LB_")
        and key.lower() not in {"all_proxy", "http_proxy", "https_proxy", "socks_proxy"}
    }
    database_path = (data_dir / "store.db").as_posix()
    environment.update(
        {
            "PORT": str(TEST_PORT),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "CODEX_LB_DATA_DIR": str(data_dir),
            "CODEX_LB_DATABASE_URL": f"sqlite+aiosqlite:///{database_path}",
            "CODEX_LB_ENCRYPTION_KEY_FILE": str(data_dir / "encryption.key"),
            "CODEX_LB_ENCRYPTION_KEY_FINGERPRINT_MODE": "off",
            "CODEX_LB_UPSTREAM_BASE_URL": f"http://127.0.0.1:{upstream_port}/backend-api",
            "CODEX_LB_UPSTREAM_STREAM_TRANSPORT": upstream_transport,
            "CODEX_LB_UPSTREAM_WEBSOCKET_TRUST_ENV": "false",
            "CODEX_LB_HTTP_DOWNSTREAM_TRANSPORT_POLICY": (
                "always_websocket" if upstream_transport == "websocket" else "always_http"
            ),
            "CODEX_LB_HTTP_RESPONSES_SESSION_BRIDGE_ENABLED": "true",
            "CODEX_LB_DASHBOARD_AUTH_MODE": "disabled",
            "CODEX_LB_DASHBOARD_BOOTSTRAP_TOKEN": secrets.token_urlsafe(32),
            "CODEX_LB_USAGE_REFRESH_ENABLED": "false",
            "CODEX_LB_LIVE_USAGE_INGESTION_ENABLED": "false",
            "CODEX_LB_MODEL_REGISTRY_ENABLED": "false",
            "CODEX_LB_STICKY_SESSION_CLEANUP_ENABLED": "false",
            "CODEX_LB_QUOTA_PLANNER_SCHEDULER_ENABLED": "false",
            "CODEX_LB_AUTOMATIONS_SCHEDULER_ENABLED": "false",
            "CODEX_LB_AUTH_GUARDIAN_ENABLED": "false",
            "CODEX_LB_METRICS_ENABLED": "false",
            "CODEX_LB_OTEL_ENABLED": "false",
        }
    )
    return environment


def _run_isolated_backend() -> None:
    from app.core.config import settings as settings_module

    empty_env_file = Path(os.environ["CODEX_LB_DATA_DIR"]) / ".isolated-2456.env"
    settings_module.ENV_FILES = (empty_env_file, empty_env_file)
    settings_module.Settings.model_config["env_file"] = None

    uvicorn.run(
        "app.main:app",
        host="127.0.0.1",
        port=TEST_PORT,
        log_level="info",
        timeout_keep_alive=30,
        proxy_headers=False,
    )


def _encode_jwt(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    return f"header.{body}.sig"


def _auth_json(account_id: str, email: str) -> dict[str, Any]:
    now = int(datetime.now(tz=timezone.utc).timestamp())
    claims = {
        "email": email,
        "chatgpt_account_id": account_id,
        "iat": now,
        "exp": now + 86_400,
        "https://api.openai.com/auth": {"chatgpt_plan_type": "plus"},
    }
    return {
        "tokens": {
            "idToken": _encode_jwt(claims),
            "accessToken": f"access-{account_id}",
            "refreshToken": f"refresh-{account_id}",
            "accountId": account_id,
        }
    }


async def _wait_for_url(url: str, *, process: subprocess.Popen[bytes] | None = None) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    async with httpx.AsyncClient(proxy=None, timeout=1.0) as client:
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                raise RuntimeError(f"isolated codex-lb exited during startup with code {process.returncode}")
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError(f"{url} was not ready within {STARTUP_TIMEOUT_SECONDS:.0f}s")


async def _import_account(client: httpx.AsyncClient, account_id: str, email: str) -> None:
    response = await client.post(
        "/api/accounts/import",
        files={"auth_json": ("auth.json", json.dumps(_auth_json(account_id, email)), "application/json")},
    )
    response.raise_for_status()


async def _stream_events(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    input_items: list[dict[str, Any]],
    turn_state: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    events: list[dict[str, Any]] = []
    headers = {
        "Accept": "text/event-stream",
        "session_id": session_id,
    }
    if turn_state is not None:
        headers["x-codex-turn-state"] = turn_state
    response_turn_state: str | None = None
    async with client.stream(
        "POST",
        "/backend-api/codex/responses",
        headers=headers,
        json={
            "model": "gpt-5.6-sol",
            "instructions": "isolated 2456 failover verification",
            "input": input_items,
            "stream": True,
        },
    ) as response:
        response_turn_state = response.headers.get("x-codex-turn-state")
        if response.status_code >= 400:
            raw_body = await response.aread()
            try:
                body: Any = json.loads(raw_body)
            except json.JSONDecodeError:
                body = raw_body.decode(errors="replace")
            events.append(
                {
                    "type": "http.error",
                    "status": response.status_code,
                    "body": body,
                }
            )
            return events, response_turn_state
        async for line in response.aiter_lines():
            if line.startswith("data: ") and line != "data: [DONE]":
                event = json.loads(line[6:])
                events.append(event)
                if event.get("type") == "response.metadata":
                    metadata_headers = event.get("headers")
                    if isinstance(metadata_headers, dict):
                        value = metadata_headers.get("x-codex-turn-state")
                        if isinstance(value, str) and value.strip():
                            response_turn_state = value.strip()
    return events, response_turn_state


def _write_upstream_trace(artifact_dir: Path) -> list[dict[str, Any]]:
    trace = [
        {
            "account_id": call.account_id,
            "turn_state_header": call.turn_state_header,
            "previous_response_id": call.payload.get("previous_response_id"),
            "input": call.payload.get("input"),
        }
        for call in FAKE_UPSTREAM_STATE.calls
    ]
    (artifact_dir / "upstream-request-trace.json").write_text(json.dumps(trace, indent=2), encoding="utf-8")
    return trace


def _downstream_failure_code(events: list[dict[str, Any]]) -> str | None:
    for event in reversed(events):
        if event.get("type") == "http.error":
            body = event.get("body")
            if not isinstance(body, dict):
                return None
            error = body.get("error")
            if not isinstance(error, dict):
                return None
            code = error.get("code")
            return code if isinstance(code, str) else None
        if event.get("type") != "response.failed":
            continue
        response = event.get("response")
        if not isinstance(response, dict):
            return None
        error = response.get("error")
        if not isinstance(error, dict):
            return None
        code = error.get("code")
        return code if isinstance(code, str) else None
    return None


async def _exercise_positive_live_server(artifact_dir: Path) -> dict[str, Any]:
    base_url = f"http://127.0.0.1:{TEST_PORT}"
    scenario_name = FAKE_UPSTREAM_STATE.scenario_name
    turn_state = f"turn-state-{scenario_name}-2456"
    owner_id = f"chatgpt-owner-{scenario_name}-2456"
    replacement_id = f"chatgpt-replacement-{scenario_name}-2456"
    initial_input = [{"role": "user", "content": "first turn"}]
    retained_first_turn_input = [
        *initial_input,
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "bind returned turn state"},
    ]
    full_input = [
        *retained_first_turn_input,
        {"role": "assistant", "content": "turn-state binding answer"},
        {"role": "user", "content": "continue after owner quota"},
    ]
    normalized_retained_first_turn_input = [
        *initial_input,
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "first answer"}],
        },
        {"role": "user", "content": "bind returned turn state"},
    ]
    normalized_full_input = [
        *normalized_retained_first_turn_input,
        {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "turn-state binding answer"}],
        },
        {"role": "user", "content": "continue after owner quota"},
    ]

    async with httpx.AsyncClient(base_url=base_url, proxy=None, timeout=30.0) as client:
        await _import_account(client, owner_id, f"owner-{scenario_name}@example.test")
        first_events, returned_turn_state = await _stream_events(
            client,
            session_id=turn_state,
            input_items=initial_input,
            turn_state=None,
        )
        assert returned_turn_state is not None
        if scenario_name == "positive_http":
            binding_events, _ = await _stream_events(
                client,
                session_id=turn_state,
                input_items=retained_first_turn_input,
                turn_state=returned_turn_state,
            )
            assert [event.get("type") for event in binding_events][-1:] == ["response.completed"], binding_events
        await _import_account(client, replacement_id, f"replacement-{scenario_name}@example.test")
        second_events, _ = await _stream_events(
            client,
            session_id=turn_state,
            input_items=full_input,
            turn_state=returned_turn_state,
        )
        health = (await client.get("/health/live")).json()
        accounts = (await client.get("/api/accounts")).json()

    first_types = [event.get("type") for event in first_events]
    second_types = [event.get("type") for event in second_events]
    call_accounts = [call.account_id for call in FAKE_UPSTREAM_STATE.calls]
    upstream_request_trace = _write_upstream_trace(artifact_dir)

    assert first_types[-1:] == ["response.completed"], first_events
    assert "response.failed" not in second_types, second_events
    assert second_types[-1:] == ["response.completed"], second_events
    assert second_events[-1]["response"]["id"] == f"resp_recovered_{scenario_name}_2456"
    if scenario_name == "positive_http":
        assert call_accounts == [owner_id, owner_id, owner_id, replacement_id], call_accounts
        assert all(call.payload.get("previous_response_id") is None for call in FAKE_UPSTREAM_STATE.calls)
        assert FAKE_UPSTREAM_STATE.calls[1].payload["input"] == normalized_retained_first_turn_input
        assert FAKE_UPSTREAM_STATE.calls[2].payload["input"] == normalized_full_input
        assert FAKE_UPSTREAM_STATE.calls[3].payload["input"] == normalized_full_input
        assert FAKE_UPSTREAM_STATE.calls[1].turn_state_header is not None, upstream_request_trace
        assert FAKE_UPSTREAM_STATE.calls[2].turn_state_header is not None, upstream_request_trace
        assert FAKE_UPSTREAM_STATE.calls[3].turn_state_header is None, upstream_request_trace
    else:
        assert call_accounts == [owner_id, owner_id, replacement_id], call_accounts
        assert FAKE_UPSTREAM_STATE.calls[1].payload.get("previous_response_id") == f"resp_initial_{scenario_name}_2456"
        assert FAKE_UPSTREAM_STATE.calls[1].payload["input"] == normalized_full_input[1:]
        assert FAKE_UPSTREAM_STATE.calls[2].payload.get("previous_response_id") is None
        assert FAKE_UPSTREAM_STATE.calls[1].turn_state_header is not None, upstream_request_trace
        assert FAKE_UPSTREAM_STATE.calls[2].turn_state_header is None, upstream_request_trace
        assert FAKE_UPSTREAM_STATE.calls[2].payload["input"] == normalized_full_input

    account_rows = accounts.get("accounts", accounts if isinstance(accounts, list) else [])
    assert len(account_rows) == 2, accounts
    result = {
        "port": TEST_PORT,
        "health": health,
        "account_count": len(account_rows),
        "upstream_accounts": call_accounts,
        "upstream_previous_response_ids": [
            call.payload.get("previous_response_id") for call in FAKE_UPSTREAM_STATE.calls
        ],
        "upstream_turn_state_headers": [call.turn_state_header for call in FAKE_UPSTREAM_STATE.calls],
        "upstream_input_counts": [len(call.payload["input"]) for call in FAKE_UPSTREAM_STATE.calls],
        "second_request_event_types": second_types,
        "recovered_response_id": second_events[-1]["response"]["id"],
        "artifact_dir": str(artifact_dir),
    }
    (artifact_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


async def _exercise_fail_closed_live_server(
    artifact_dir: Path,
    *,
    scenario_name: str,
) -> dict[str, Any]:
    base_url = f"http://127.0.0.1:{TEST_PORT}"
    session_id = f"turn-state-{scenario_name}-2456"
    owner_id = f"chatgpt-owner-{scenario_name}-2456"
    replacement_id = f"chatgpt-replacement-{scenario_name}-2456"
    initial_input = [{"role": "user", "content": "first turn"}]
    if scenario_name == "partial_history":
        second_input = [{"role": "user", "content": "partial follow-up without retained answer"}]
    elif scenario_name == "account_scoped_file":
        second_input = [
            *initial_input,
            {"role": "assistant", "content": "first answer"},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "follow up with owner-scoped file"},
                    {"type": "input_file", "file_id": "file_owner_scoped_2456"},
                ],
            },
        ]
    else:
        raise ValueError(f"unknown fail-closed scenario: {scenario_name}")

    async with httpx.AsyncClient(base_url=base_url, proxy=None, timeout=30.0) as client:
        await _import_account(client, owner_id, f"owner-{scenario_name}@example.test")
        first_events, returned_turn_state = await _stream_events(
            client,
            session_id=session_id,
            input_items=initial_input,
            turn_state=None,
        )
        assert returned_turn_state is not None
        await _import_account(client, replacement_id, f"replacement-{scenario_name}@example.test")
        second_events, _ = await _stream_events(
            client,
            session_id=session_id,
            input_items=second_input,
            turn_state=returned_turn_state,
        )
        health = (await client.get("/health/live")).json()

    first_types = [event.get("type") for event in first_events]
    second_types = [event.get("type") for event in second_events]
    call_accounts = [call.account_id for call in FAKE_UPSTREAM_STATE.calls]
    upstream_request_trace = _write_upstream_trace(artifact_dir)
    failure_code = _downstream_failure_code(second_events)

    assert first_types[-1:] == ["response.completed"], first_events
    assert second_types[-1:] in (["response.failed"], ["http.error"]), second_events
    assert failure_code in {"upstream_unavailable", "stream_incomplete"}, second_events
    assert call_accounts == [owner_id, owner_id], upstream_request_trace
    assert replacement_id not in call_accounts

    result = {
        "port": TEST_PORT,
        "scenario": scenario_name,
        "health": health,
        "upstream_accounts": call_accounts,
        "second_request_event_types": second_types,
        "failure_code": failure_code,
        "replacement_attempted": replacement_id in call_accounts,
        "artifact_dir": str(artifact_dir),
    }
    (artifact_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


async def _wait_for_test_port_release() -> None:
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            _assert_test_port_available()
        except RuntimeError:
            await asyncio.sleep(0.1)
            continue
        return
    raise TimeoutError(f"isolated test port {TEST_PORT} was not released")


async def _run_isolated_scenario(
    artifact_dir: Path,
    *,
    upstream_port: int,
    scenario_name: str,
    upstream_transport: str,
    exercise: Callable[[Path], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    global FAKE_UPSTREAM_STATE

    _assert_test_port_available()
    FAKE_UPSTREAM_STATE = FakeUpstreamState(scenario_name=scenario_name)
    data_dir = artifact_dir / "data"
    data_dir.mkdir()
    stdout_path = artifact_dir / "codex-lb.stdout.log"
    stderr_path = artifact_dir / "codex-lb.stderr.log"
    environment = _isolated_environment(
        data_dir,
        upstream_port,
        upstream_transport=upstream_transport,
    )
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        backend = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--backend"],
            cwd=REPOSITORY_ROOT,
            env=environment,
            stdout=stdout,
            stderr=stderr,
            creationflags=creation_flags,
        )
        try:
            await _wait_for_url(f"http://127.0.0.1:{TEST_PORT}/health/live", process=backend)
            return await exercise(artifact_dir)
        finally:
            if backend.poll() is None:
                backend.terminate()
                try:
                    backend.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    backend.kill()
                    backend.wait(timeout=5)
            await _wait_for_test_port_release()


async def _run_verification() -> int:
    _assert_test_port_available()
    artifact_root = REPOSITORY_ROOT / ".tmp"
    artifact_root.mkdir(exist_ok=True)
    artifact_dir = artifact_root / f"turn-state-failover-2456-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    artifact_dir.mkdir()
    upstream_port = _reserve_loopback_port()
    upstream_server = uvicorn.Server(
        uvicorn.Config(fake_upstream_app, host="127.0.0.1", port=upstream_port, log_level="warning")
    )
    upstream_task = asyncio.create_task(upstream_server.serve())
    await _wait_for_url(f"http://127.0.0.1:{upstream_port}/openapi.json")

    try:
        positive_results: dict[str, dict[str, Any]] = {}
        for scenario_name, upstream_transport in (
            ("positive_websocket", "websocket"),
            ("positive_http", "http"),
        ):
            scenario_dir = artifact_dir / scenario_name
            scenario_dir.mkdir()
            positive_results[scenario_name] = await _run_isolated_scenario(
                scenario_dir,
                upstream_port=upstream_port,
                scenario_name=scenario_name,
                upstream_transport=upstream_transport,
                exercise=_exercise_positive_live_server,
            )

        negative_results: dict[str, dict[str, Any]] = {}
        for scenario_name in ("partial_history", "account_scoped_file"):
            scenario_dir = artifact_dir / scenario_name
            scenario_dir.mkdir()

            async def exercise_negative(path: Path, *, name: str = scenario_name) -> dict[str, Any]:
                return await _exercise_fail_closed_live_server(path, scenario_name=name)

            negative_results[scenario_name] = await _run_isolated_scenario(
                scenario_dir,
                upstream_port=upstream_port,
                scenario_name=scenario_name,
                upstream_transport="websocket",
                exercise=exercise_negative,
            )

        result = {
            "port": TEST_PORT,
            "positive": positive_results,
            "negative": negative_results,
            "artifact_dir": str(artifact_dir),
        }
        (artifact_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    finally:
        upstream_server.should_exit = True
        await upstream_task
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", action="store_true")
    args = parser.parse_args()
    if args.backend:
        _run_isolated_backend()
        return 0
    return asyncio.run(_run_verification())


if __name__ == "__main__":
    raise SystemExit(main())
