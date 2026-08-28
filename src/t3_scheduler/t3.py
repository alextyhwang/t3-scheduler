from __future__ import annotations

import base64
import ctypes
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import T3Config


class T3Error(RuntimeError):
    pass


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
            raise T3Error(f"unexpected T3 pairing response: {output[:300]}") from exc

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
            raise T3Error(f"unexpected T3 token response: {result}") from exc
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
            raise T3Error(f"T3 HTTP {exc.code}: {detail[:1000]}") from exc
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
            except T3Error as exc:
                if attempt == 0 and "T3 HTTP 401" in str(exc):
                    continue
                raise
        raise AssertionError("unreachable")

    def shell_snapshot(self) -> dict[str, Any]:
        result = self._authorized_json("/api/orchestration/shell")
        if not isinstance(result, dict):
            raise T3Error("T3 shell snapshot is not an object")
        return result

    def dispatch(self, command: dict[str, Any]) -> dict[str, Any]:
        result = self._authorized_json(
            "/api/orchestration/dispatch", method="POST", payload=command
        )
        if not isinstance(result, dict) or "sequence" not in result:
            raise T3Error(f"unexpected T3 dispatch response: {result}")
        return result
