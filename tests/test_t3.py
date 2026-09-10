import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
import uuid

from t3_scheduler.config import T3Config
from t3_scheduler.t3 import BaseDirIdentityCheck, T3Client, T3HttpError


class _FakeWebSocket:
    def __init__(self, response_factory):
        self.response_factory = response_factory
        self.sent: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def send(self, value: str):
        self.sent.append(value)

    def recv(self, *, timeout: int):
        self.asserted_timeout = timeout
        request = json.loads(self.sent[0])
        return json.dumps(self.response_factory(request))


class T3ClientTests(unittest.TestCase):
    def _client(self, root: Path) -> T3Client:
        config = T3Config(
            base_url="http://127.0.0.1:3773",
            base_dir=root / "t3-home",
            auto_start=False,
            startup_timeout_seconds=5,
        )
        return T3Client(config, root / "state")

    def test_thread_snapshot_uses_bounded_encoded_http_route(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            client._authorized_json = Mock(return_value={"id": "thread/id"})

            result = client.thread_snapshot("thread/id", turn_limit=17)

            self.assertEqual(result["id"], "thread/id")
            client._authorized_json.assert_called_once_with(
                "/api/orchestration/threads/thread%2Fid?turnLimit=17"
            )

    def test_thread_snapshot_rejects_empty_window(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            with self.assertRaisesRegex(ValueError, "at least 1"):
                client.thread_snapshot("thread-1", turn_limit=0)

    def test_bootstrap_dispatch_creates_thread_then_starts_turn_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            client._authorized_json = Mock(
                side_effect=[{"sequence": 10}, {"sequence": 11}]
            )
            command = {
                "type": "thread.turn.start",
                "commandId": "command-1",
                "threadId": "thread-1",
                "message": {
                    "messageId": "message-1",
                    "role": "user",
                    "text": "hello",
                    "attachments": [],
                },
                "modelSelection": {"instanceId": "codex", "model": "gpt-test"},
                "runtimeMode": "full-access",
                "interactionMode": "default",
                "createdAt": "2099-01-01T00:00:00Z",
                "bootstrap": {
                    "createThread": {
                        "projectId": "project-1",
                        "title": "Test",
                        "modelSelection": {
                            "instanceId": "codex",
                            "model": "gpt-test",
                        },
                        "runtimeMode": "full-access",
                        "interactionMode": "default",
                        "branch": None,
                        "worktreePath": None,
                        "createdAt": "2099-01-01T00:00:00Z",
                    }
                },
            }

            result = client.dispatch(command)

            self.assertEqual(result, {"sequence": 11})
            calls = client._authorized_json.call_args_list
            self.assertEqual(len(calls), 2)
            create = calls[0].kwargs["payload"]
            turn = calls[1].kwargs["payload"]
            self.assertEqual(create["type"], "thread.create")
            self.assertEqual(create["threadId"], "thread-1")
            self.assertEqual(
                create["commandId"],
                str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "t3-scheduler:bootstrap-thread-create:command-1",
                    )
                ),
            )
            self.assertNotIn("bootstrap", turn)
            self.assertIn("bootstrap", command)

    def test_nonbootstrap_dispatch_remains_one_http_command(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            client._authorized_json = Mock(return_value={"sequence": 12})
            command = {
                "type": "thread.turn.start",
                "commandId": "command-2",
                "threadId": "thread-2",
            }

            self.assertEqual(client.dispatch(command), {"sequence": 12})
            client._authorized_json.assert_called_once_with(
                "/api/orchestration/dispatch", method="POST", payload=command
            )

    def test_bootstrap_dispatch_stops_after_invalid_create_response(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            client._authorized_json = Mock(return_value={})
            command = {
                "type": "thread.turn.start",
                "commandId": "command-3",
                "threadId": "thread-3",
                "bootstrap": {
                    "createThread": {
                        "projectId": "project-1",
                        "title": "Test",
                        "createdAt": "2099-01-01T00:00:00Z",
                    }
                },
            }

            with self.assertRaisesRegex(Exception, "thread-create response"):
                client.dispatch(command)
            self.assertEqual(client._authorized_json.call_count, 1)

    def test_http_dispatch_rejects_unsupported_worktree_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            client._authorized_json = Mock()
            command = {
                "type": "thread.turn.start",
                "commandId": "command-4",
                "threadId": "thread-4",
                "bootstrap": {"prepareWorktree": {"projectCwd": "C:/work"}},
            }

            with self.assertRaisesRegex(Exception, "worktree or setup-script"):
                client.dispatch(command)
            client._authorized_json.assert_not_called()

    def test_http_error_preserves_status_and_body_without_logging_body(self):
        request = urllib.request.Request("http://127.0.0.1:3773/private")
        error = urllib.error.HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b'{"reason":"usage","secret":"do-not-log"}'),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(T3HttpError) as raised:
                T3Client._open_json(request)

        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.body, '{"reason":"usage","secret":"do-not-log"}')
        self.assertEqual(raised.exception.response_json["reason"], "usage")
        self.assertNotIn("do-not-log", str(raised.exception))

    def test_effect_rpc_provider_snapshot_framing_matches_t3_layer_json(self):
        providers = [
            {
                "instanceId": "codex_school",
                "usageLimits": {
                    "checkedAt": "2026-09-09T20:00:00Z",
                    "windows": [{"id": "primary", "usedPercent": 25}],
                },
            }
        ]

        def response(request):
            self.assertEqual(
                request,
                {
                    "_tag": "Request",
                    "id": request["id"],
                    "tag": "server.getConfig",
                    "payload": {},
                    "headers": [],
                },
            )
            return {
                "_tag": "Exit",
                "requestId": request["id"],
                "exit": {"_tag": "Success", "value": {"providers": providers}},
            }

        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            websocket = _FakeWebSocket(response)
            client.ensure_server = Mock()
            client.token = Mock(return_value="credential-that-must-not-be-serialized")
            client._connect_websocket = Mock(return_value=websocket)

            result = client.provider_snapshots(refresh=False)

        self.assertEqual(result, providers)
        self.assertNotIn("credential-that-must-not-be-serialized", websocket.sent[0])
        client._connect_websocket.assert_called_once_with("credential-that-must-not-be-serialized")

    def test_provider_snapshot_refresh_uses_targeted_t3_rpc(self):
        def response(request):
            self.assertEqual(request["tag"], "server.refreshProviders")
            return {
                "_tag": "Exit",
                "requestId": request["id"],
                "exit": {"_tag": "Success", "value": {"providers": []}},
            }

        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            client.ensure_server = Mock()
            client.token = Mock(return_value="token")
            client._connect_websocket = Mock(return_value=_FakeWebSocket(response))
            self.assertEqual(client.provider_snapshots(), [])

    def test_base_dir_identity_matches_token_session_without_exposing_id(self):
        claims = base64.urlsafe_b64encode(json.dumps({"sid": "session-1"}).encode()).decode()
        token = f"{claims.rstrip('=')}.signature-secret"
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            client._load_token = Mock(return_value=token)
            client._cli = Mock(return_value=json.dumps([{"sessionId": "session-1"}]))

            result = client.check_base_dir_identity()

        self.assertEqual(
            result,
            BaseDirIdentityCheck(True, "credential_session_found", client.config.base_dir.resolve()),
        )
        self.assertNotIn("session-1", repr(result))
        self.assertNotIn("signature-secret", repr(result))

    def test_base_dir_identity_detects_wrong_t3_store(self):
        claims = base64.urlsafe_b64encode(json.dumps({"sid": "session-live"}).encode()).decode()
        token = f"{claims.rstrip('=')}.signature"
        with tempfile.TemporaryDirectory() as directory:
            client = self._client(Path(directory))
            client._load_token = Mock(return_value=token)
            client._cli = Mock(return_value="[]")

            result = client.check_base_dir_identity()

        self.assertFalse(result.compatible)
        self.assertEqual(result.reason, "credential_session_missing_from_configured_base_dir")


if __name__ == "__main__":
    unittest.main()
