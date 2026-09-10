from __future__ import annotations

import base64
import binascii
import copy
import ctypes
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import T3Config


class T3Error(RuntimeError):
    pass


class T3HttpError(T3Error):
    """An HTTP failure whose response remains available without leaking it in logs."""

    def __init__(self, status_code: int, body: str, url: str):
        self.status_code = status_code
        self.body = body
        self.url = url
        try:
            self.response_json = json.loads(body)
        except json.JSONDecodeError:
            self.response_json = None
        path = urllib.parse.urlsplit(url).path or "/"
        super().__init__(f"T3 HTTP {status_code} for {path}")


@dataclass(frozen=True)
class BaseDirIdentityCheck:
    """Whether the protected scheduler credential belongs to the configured T3 store."""

    compatible: bool | None
    reason: str
    configured_base_dir: Path


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def protect_for_current_user(value: str) -> str:
    if os.name != "nt":
        raise T3Error("credential protection currently requires Windows")
    source, source_buffer = _blob(value.encode("utf-8"))
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptProtectData(
        ctypes.byref(source), "T3 Scheduler", None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        protected = ctypes.string_at(output.pbData, output.cbData)
        return base64.b64encode(protected).decode("ascii")
    finally:
        kernel32.LocalFree(output.pbData)
        del source_buffer


def unprotect_for_current_user(value: str) -> str:
    if os.name != "nt":
        raise T3Error("credential protection currently requires Windows")
    source, source_buffer = _blob(base64.b64decode(value))
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        kernel32.LocalFree(output.pbData)
        del source_buffer


class T3Client:
    def __init__(self, config: T3Config, state_dir: Path):
        self.config = config
        self.state_dir = state_dir
        self.credential_path = state_dir / "credential.json"
        self._token: str | None = None

    def _installation(self) -> tuple[Path, Path]:
        candidates: list[Path] = []
        if self.config.executable:
            candidates.append(self.config.executable)
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        program_dir = local / "Programs" / "t3code"
        candidates.extend(sorted(program_dir.glob("T3 Code*.exe"), reverse=True))
        for executable in candidates:
            archive = executable.parent / "resources" / "server.asar"
            server = archive / "apps" / "server" / "dist" / "bin.mjs"
            # Electron resolves paths inside an ASAR archive even though the
            # operating system cannot stat the virtual child path.
            if executable.is_file() and archive.exists():
                return executable, server
        raise T3Error("could not find the installed T3 Code executable and bundled server")

    def _cli(self, arguments: list[str]) -> str:
        executable, server = self._installation()
        environment = os.environ.copy()
        environment["ELECTRON_RUN_AS_NODE"] = "1"
        result = subprocess.run(
            [str(executable), str(server), *arguments],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise T3Error(f"T3 CLI failed ({result.returncode}): {detail}")
        return result.stdout.strip()

    def is_reachable(self) -> bool:
        try:
            urllib.request.urlopen(f"{self.config.base_url}/api/orchestration/shell", timeout=2)
            return True
        except urllib.error.HTTPError:
            return True
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def ensure_server(self) -> None:
        if self.is_reachable():
            return
        if not self.config.auto_start:
            raise T3Error(f"T3 server is not reachable at {self.config.base_url}")
        executable, server = self._installation()
        parsed = urllib.parse.urlparse(self.config.base_url)
        if parsed.scheme != "http" or not parsed.hostname or not parsed.port:
            raise T3Error("auto_start requires an http base_url with an explicit port")
        environment = os.environ.copy()
        environment["ELECTRON_RUN_AS_NODE"] = "1"
        creation_flags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        subprocess.Popen(
            [
                str(executable),
                str(server),
                "serve",
                "--host",
                parsed.hostname,
                "--port",
                str(parsed.port),
                "--base-dir",
                str(self.config.base_dir),
            ],
            cwd=str(self.config.base_dir),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creation_flags,
        )
        deadline = time.monotonic() + self.config.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self.is_reachable():
                return
            time.sleep(0.5)
        raise T3Error(f"headless T3 server did not start within {self.config.startup_timeout_seconds}s")

    def _load_token(self) -> str | None:
        if self._token:
            return self._token
        try:
            record = json.loads(self.credential_path.read_text(encoding="utf-8"))
            expires_at = datetime.fromisoformat(record["expires_at"])
            if expires_at > datetime.now(timezone.utc) + timedelta(minutes=5):
                self._token = unprotect_for_current_user(record["protected_token"])
                return self._token
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            return None
        return None

    def _renew_token(self) -> str:
        self.ensure_server()
        output = self._cli(
            [
                "auth",
                "pairing",
                "create",
                "--base-dir",
                str(self.config.base_dir),
                "--ttl",
                "5m",
                "--label",
                "t3-scheduler",
                "--json",
            ]
        )
        try:
            pairing = json.loads(output)
            credential = pairing["credential"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            # A successful pairing response contains a bearer-like one-time
            # credential. Never include the response body in an exception.
            raise T3Error("unexpected T3 pairing response") from exc

        form = urllib.parse.urlencode(
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": credential,
                "subject_token_type": "urn:t3:params:oauth:token-type:environment-bootstrap",
                "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
                "scope": "orchestration:read orchestration:operate",
                "client_label": "T3 Scheduler",
                "client_device_type": "bot",
                "client_os": "Windows",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.base_url}/oauth/token",
            method="POST",
            data=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        result = self._open_json(request)
        try:
            token = str(result["access_token"])
            expires_in = float(result["expires_in"])
            token_type = result["token_type"]
        except (KeyError, TypeError, ValueError) as exc:
            raise T3Error("unexpected T3 token response") from exc
        if token_type != "Bearer":
            raise T3Error(f"T3 returned unsupported token type {token_type!r}")
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "protected_token": protect_for_current_user(token),
            "expires_at": expires_at.isoformat(),
            "scope": result.get("scope"),
        }
        self.credential_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        self._token = token
        return token

    def token(self, *, force_renew: bool = False) -> str:
        if force_renew:
            self._token = None
        return (None if force_renew else self._load_token()) or self._renew_token()

    @staticmethod
    def _open_json(request: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise T3HttpError(exc.code, detail, request.full_url) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise T3Error(f"cannot reach T3: {exc}") from exc
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise T3Error(f"T3 returned invalid JSON: {body[:300]}") from exc

    def _authorized_json(self, path: str, *, method: str = "GET", payload: Any = None) -> Any:
        self.ensure_server()
        for attempt in range(2):
            token = self.token(force_renew=attempt == 1)
            data = None if payload is None else json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(
                f"{self.config.base_url}{path}",
                method=method,
                data=data,
                headers={
                    "Authorization": f"Bearer {token}",
                    **({"Content-Type": "application/json"} if data is not None else {}),
                },
            )
            try:
                return self._open_json(request)
            except T3HttpError as exc:
                if attempt == 0 and exc.status_code == 401:
                    continue
                raise
        raise AssertionError("unreachable")

    def shell_snapshot(self) -> dict[str, Any]:
        result = self._authorized_json("/api/orchestration/shell")
        if not isinstance(result, dict):
            raise T3Error("T3 shell snapshot is not an object")
        return result

    def thread_snapshot(self, thread_id: str, *, turn_limit: int = 50) -> dict[str, Any]:
        """Read a bounded detail snapshot for one existing T3 thread."""
        if turn_limit < 1:
            raise ValueError("turn_limit must be at least 1")
        encoded_thread_id = urllib.parse.quote(str(thread_id), safe="")
        query = urllib.parse.urlencode({"turnLimit": turn_limit})
        result = self._authorized_json(
            f"/api/orchestration/threads/{encoded_thread_id}?{query}"
        )
        if not isinstance(result, dict):
            raise T3Error("T3 thread snapshot is not an object")
        return result

    @staticmethod
    def _decode_credential_session_id(token: str) -> str | None:
        """Read the non-secret session id claim without verifying or logging the token."""
        try:
            encoded_payload, _signature = token.split(".", 1)
            padding = "=" * (-len(encoded_payload) % 4)
            payload = json.loads(base64.urlsafe_b64decode(encoded_payload + padding))
            session_id = payload.get("sid")
            return session_id if isinstance(session_id, str) and session_id else None
        except (AttributeError, ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
            return None

    def check_base_dir_identity(self) -> BaseDirIdentityCheck:
        """Check that the saved token's session exists in ``config.base_dir``.

        This is read-only. It catches the common split-control-plane failure in
        which the scheduler mints credentials against one T3 data directory but
        sends them to a server running from another directory.
        """
        configured_base_dir = self.config.base_dir.resolve()
        token = self._load_token()
        if token is None:
            return BaseDirIdentityCheck(None, "no_stored_credential", configured_base_dir)
        session_id = self._decode_credential_session_id(token)
        if session_id is None:
            return BaseDirIdentityCheck(False, "malformed_stored_credential", configured_base_dir)
        try:
            raw = self._cli(
                [
                    "auth",
                    "session",
                    "list",
                    "--base-dir",
                    str(configured_base_dir),
                    "--json",
                ]
            )
            decoded = json.loads(raw)
        except (T3Error, json.JSONDecodeError):
            return BaseDirIdentityCheck(
                None, "configured_session_list_unavailable", configured_base_dir
            )
        sessions = decoded.get("sessions", []) if isinstance(decoded, dict) else decoded
        if not isinstance(sessions, list):
            return BaseDirIdentityCheck(None, "configured_session_list_invalid", configured_base_dir)
        belongs_to_configured_store = any(
            isinstance(item, dict) and item.get("sessionId") == session_id for item in sessions
        )
        return BaseDirIdentityCheck(
            belongs_to_configured_store,
            (
                "credential_session_found"
                if belongs_to_configured_store
                else "credential_session_missing_from_configured_base_dir"
            ),
            configured_base_dir,
        )

    def _websocket_url(self) -> str:
        parsed = urllib.parse.urlsplit(self.config.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise T3Error("T3 base_url must be HTTP(S) to derive the WebSocket endpoint")
        scheme = "wss" if parsed.scheme == "https" else "ws"
        base_path = parsed.path.rstrip("/")
        return urllib.parse.urlunsplit((scheme, parsed.netloc, f"{base_path}/ws", "", ""))

    @staticmethod
    def _effect_rpc_request(tag: str, payload: dict[str, Any], request_id: str) -> str:
        """Encode the JSON framing used by Effect RPC's ``layerJson`` transport."""
        return json.dumps(
            {
                "_tag": "Request",
                "id": request_id,
                "tag": tag,
                "payload": payload,
                "headers": [],
            },
            separators=(",", ":"),
        )

    @staticmethod
    def _effect_rpc_result(raw: str | bytes, request_id: str, method: str) -> Any | None:
        try:
            message = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise T3Error(f"T3 WebSocket returned invalid Effect RPC JSON for {method}") from exc
        if not isinstance(message, dict):
            raise T3Error(f"T3 WebSocket returned an invalid Effect RPC envelope for {method}")
        if message.get("_tag") in {"Ack", "Pong"}:
            return None
        if message.get("_tag") == "Defect":
            raise T3Error(f"T3 WebSocket reported an Effect RPC defect for {method}")
        if message.get("_tag") != "Exit" or str(message.get("requestId")) != request_id:
            return None
        exit_value = message.get("exit")
        if not isinstance(exit_value, dict):
            raise T3Error(f"T3 WebSocket returned an invalid Effect RPC exit for {method}")
        if exit_value.get("_tag") != "Success":
            # Failure causes can include provider diagnostics. Keep them out of
            # the exception string while still reporting which RPC failed.
            raise T3Error(f"T3 WebSocket RPC {method} failed")
        return exit_value.get("value")

    def _effect_rpc_call(self, websocket: Any, method: str, payload: dict[str, Any]) -> Any:
        request_id = str(uuid.uuid4())
        websocket.send(self._effect_rpc_request(method, payload, request_id))
        while True:
            raw = websocket.recv(timeout=30)
            try:
                decoded = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = None
            if isinstance(decoded, dict) and decoded.get("_tag") == "Ping":
                websocket.send('{"_tag":"Pong"}')
                continue
            result = self._effect_rpc_result(raw, request_id, method)
            if result is not None:
                return result

    def _connect_websocket(self, token: str):
        try:
            from websockets.sync.client import connect
        except ImportError as exc:  # pragma: no cover - installation error
            raise T3Error("provider snapshots require the 'websockets' dependency") from exc
        return connect(
            self._websocket_url(),
            additional_headers={"Authorization": f"Bearer {token}"},
            open_timeout=10,
            close_timeout=5,
        )

    def provider_snapshots(self, *, refresh: bool = True) -> list[dict[str, Any]]:
        """Read T3's current per-instance provider and subscription-usage snapshots.

        The installed T3 build exposes these only through its Effect RPC
        WebSocket surface. ``refresh=True`` asks every configured provider for a
        fresh status/usage probe; ``False`` reads the server's current cache.
        """
        self.ensure_server()
        token = self.token()
        method = "server.refreshProviders" if refresh else "server.getConfig"
        try:
            with self._connect_websocket(token) as websocket:
                result = self._effect_rpc_call(websocket, method, {})
        except T3Error:
            raise
        except Exception as exc:
            # WebSocket exception text can contain request headers or URLs.
            raise T3Error(f"T3 WebSocket provider snapshot request failed ({method})") from exc
        providers = result.get("providers") if isinstance(result, dict) else None
        if not isinstance(providers, list) or not all(isinstance(item, dict) for item in providers):
            raise T3Error(f"T3 WebSocket RPC {method} returned no provider snapshots")
        return providers

    def dispatch(self, command: dict[str, Any]) -> dict[str, Any]:
        bootstrap = command.get("bootstrap")
        if isinstance(bootstrap, dict) and (
            "prepareWorktree" in bootstrap or "runSetupScript" in bootstrap
        ):
            raise T3Error(
                "HTTP dispatch does not support worktree or setup-script bootstrap"
            )
        create_thread = (
            bootstrap.get("createThread") if isinstance(bootstrap, dict) else None
        )
        if isinstance(create_thread, dict):
            command_id = str(command.get("commandId", ""))
            if not command_id:
                raise T3Error("bootstrap dispatch requires a commandId")
            bootstrap_command_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"t3-scheduler:bootstrap-thread-create:{command_id}",
                )
            )
            create_command = {
                "type": "thread.create",
                "commandId": bootstrap_command_id,
                "threadId": command.get("threadId"),
                "projectId": create_thread.get("projectId"),
                "title": create_thread.get("title"),
                "modelSelection": create_thread.get("modelSelection"),
                "runtimeMode": create_thread.get("runtimeMode"),
                "interactionMode": create_thread.get("interactionMode"),
                "branch": create_thread.get("branch"),
                "worktreePath": create_thread.get("worktreePath"),
                "createdAt": create_thread.get("createdAt"),
            }
            create_result = self._authorized_json(
                "/api/orchestration/dispatch",
                method="POST",
                payload=create_command,
            )
            if not isinstance(create_result, dict) or "sequence" not in create_result:
                raise T3Error("unexpected T3 thread-create response")
            command = copy.deepcopy(command)
            command.pop("bootstrap", None)
        result = self._authorized_json(
            "/api/orchestration/dispatch", method="POST", payload=command
        )
        if not isinstance(result, dict) or "sequence" not in result:
            raise T3Error(f"unexpected T3 dispatch response: {result}")
        return result
