from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from t3_scheduler.config import (
    AppConfig,
    FailoverConfig,
    FailoverProviderConfig,
    T3Config,
)
from t3_scheduler.coordinator import FailoverCoordinator, provider_candidates
from t3_scheduler.state import RunJournal
from t3_scheduler.t3 import T3HttpError


def provider(instance_id: str, used: float = 10) -> dict:
    return {
        "instanceId": instance_id,
        "driver": "codex",
        "continuation": {"groupKey": "shared"},
        "enabled": True,
        "installed": True,
        "availability": "available",
        "auth": {"status": "authenticated"},
        "models": [{"slug": "gpt-test"}],
        "usageLimits": {
            "checkedAt": "2099-01-01T00:00:00Z",
            "windows": [{"usedPercent": used}],
        },
    }


def failed_thread(instance_id: str = "current", turn_id: str = "turn-1") -> dict:
    return {
        "id": "thread-1",
        "projectId": "project-1",
        "modelSelection": {
            "instanceId": instance_id,
            "model": "gpt-test",
            "options": [{"id": "reasoningEffort", "value": "high"}],
        },
        "runtimeMode": "full-access",
        "interactionMode": "default",
        "latestTurn": {"turnId": turn_id, "state": "error"},
        "session": {
            "status": "error",
            "providerName": "codex",
            "providerInstanceId": instance_id,
            "activeTurnId": None,
            "lastError": "Codex usage limit reached. Weekly limit resets later.",
        },
        "activities": [],
    }


class FakeClient:
    def __init__(self, thread: dict):
        self.thread = thread
        self.dispatched: list[dict] = []
        self.dispatch_error = False

    def shell_snapshot(self):
        return {
            "projects": [{"id": "project-1", "workspaceRoot": "C:/work/project"}],
            "threads": [self.thread],
        }

    def thread_snapshot(self, _thread_id, *, turn_limit):
        self.turn_limit = turn_limit
        return {
            "snapshotSequence": 42,
            "thread": self.thread,
            "page": {"limit": turn_limit, "hasMore": False},
        }

    def provider_snapshots(self, *, refresh):
        self.refreshed = refresh
        return [provider("current", 100), provider("backup", 20), provider("last", 40)]

    def dispatch(self, command):
        self.dispatched.append(command)
        if self.dispatch_error:
            raise OSError("outcome unknown")
        return {"sequence": 1}


class CoordinatorTests(unittest.TestCase):
    def _config(self, root: Path, **changes) -> AppConfig:
        policy = FailoverConfig(
            enabled=True,
            mode="active",
            allow_interactive_threads=True,
            providers=(
                FailoverProviderConfig("backup", 100, True),
                FailoverProviderConfig("last", 50, True),
            ),
        )
        policy = replace(policy, **changes)
        return AppConfig(
            root / "jobs.toml",
            root / ".state",
            15,
            T3Config("http://127.0.0.1:3773", root / ".t3", False, 5),
            (),
            policy,
        )

    def test_active_scan_persists_and_dispatches_existing_thread_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(failed_thread())
            result = FailoverCoordinator(self._config(root), client).scan()

            self.assertEqual(result.actions[0].outcome, "verifying")
            self.assertEqual(len(client.dispatched), 1)
            command = client.dispatched[0]
            self.assertEqual(command["threadId"], "thread-1")
            self.assertEqual(command["modelSelection"]["instanceId"], "backup")
            self.assertEqual(command["modelSelection"]["options"][0]["value"], "high")
            self.assertNotIn("bootstrap", command)
            with RunJournal(root / ".state" / "runs.sqlite3") as journal:
                incident = journal.active_incident_for_thread("thread-1")
                self.assertIsNotNone(incident)
                attempt = journal.provider_attempts(incident.id)[0]
                self.assertEqual(incident.state, "verifying")
                self.assertEqual(attempt.command, command)

    def test_shadow_records_decision_but_never_dispatches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(failed_thread())
            config = self._config(root, mode="shadow")

            result = FailoverCoordinator(config, client).scan()

            self.assertEqual(result.actions[0].outcome, "would-switch")
            self.assertEqual(client.dispatched, [])
            with RunJournal(root / ".state" / "runs.sqlite3") as journal:
                incident = journal.active_incident_for_thread("thread-1")
                self.assertIsNotNone(incident)
                self.assertEqual(incident.state, "detected")

    def test_shadow_closes_stale_detected_incident_when_failure_clears(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(failed_thread())
            coordinator = FailoverCoordinator(self._config(root, mode="shadow"), client)
            coordinator.scan()
            client.thread["latestTurn"]["state"] = "completed"
            client.thread["session"]["status"] = "idle"
            client.thread["session"]["lastError"] = None

            result = coordinator.scan()

            self.assertEqual(result.actions[0].outcome, "recovered")
            with RunJournal(root / ".state" / "runs.sqlite3") as journal:
                self.assertIsNone(journal.active_incident_for_thread("thread-1"))

    def test_dry_run_does_not_persist_an_incident_or_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(failed_thread())

            result = FailoverCoordinator(self._config(root), client).scan(dry_run=True)

            self.assertEqual(result.actions[0].outcome, "would-switch")
            self.assertIsNone(result.actions[0].incident_id)
            self.assertEqual(client.dispatched, [])
            with RunJournal(root / ".state" / "runs.sqlite3") as journal:
                self.assertEqual(journal.list_failover_incidents(), ())
                attempt_count = journal.connection.execute(
                    "SELECT COUNT(*) FROM failover_provider_attempts"
                ).fetchone()[0]
                self.assertEqual(attempt_count, 0)

    def test_default_scope_ignores_interactive_thread_and_allowlist_is_enforced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(failed_thread())
            config = self._config(root, allow_interactive_threads=False)
            self.assertEqual(FailoverCoordinator(config, client).scan().actions, ())

            with RunJournal(root / ".state" / "runs.sqlite3") as journal:
                claim = journal.claim(
                    "scheduled-job",
                    "2099-01-01T00:00:00+00:00",
                    {"threadId": "thread-1"},
                )
                self.assertTrue(claim.acquired)
                journal.succeeded(
                    "scheduled-job", "2099-01-01T00:00:00+00:00", "thread-1"
                )
            self.assertEqual(
                FailoverCoordinator(config, client).scan().actions[0].outcome,
                "verifying",
            )

            other_root = root / "other-state"
            config = self._config(
                other_root,
                project_allowlist=(root / "somewhere-else",),
            )
            self.assertEqual(FailoverCoordinator(config, client).scan().actions, ())

    def test_unmatched_tool_call_requires_manual_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thread = failed_thread()
            thread["activities"] = [
                {
                    "kind": "tool.started",
                    "turnId": "turn-1",
                    "payload": {"toolCallId": "tool-1"},
                }
            ]
            client = FakeClient(thread)

            result = FailoverCoordinator(self._config(root), client).scan()

            self.assertEqual(result.actions[0].outcome, "manual-review")
            self.assertEqual(result.actions[0].reason, "unmatched_tool_call")
            self.assertEqual(client.dispatched, [])

    def test_uncertain_dispatch_reuses_identical_persisted_command(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(failed_thread())
            client.dispatch_error = True
            coordinator = FailoverCoordinator(self._config(root), client)

            first = coordinator.scan()
            second = coordinator.scan()

            self.assertEqual(first.actions[0].outcome, "dispatch-uncertain")
            self.assertEqual(second.actions[0].outcome, "dispatch-uncertain")
            self.assertEqual(client.dispatched[0], client.dispatched[1])

    def test_recovers_when_attempt_was_marked_verifying_before_incident(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(failed_thread())
            coordinator = FailoverCoordinator(self._config(root), client)
            coordinator.scan()
            with RunJournal(root / ".state" / "runs.sqlite3") as journal:
                journal.connection.execute(
                    "UPDATE failover_incidents SET state = 'dispatching'"
                )
                journal.connection.commit()

            result = coordinator.scan()

            self.assertEqual(result.actions[0].outcome, "verifying")
            self.assertEqual(len(client.dispatched), 1)

    def test_target_running_stays_in_same_incident_until_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(failed_thread())
            coordinator = FailoverCoordinator(self._config(root), client)
            coordinator.scan()
            message_id = client.dispatched[0]["message"]["messageId"]
            client.thread = {
                **failed_thread("backup", "turn-2"),
                "latestTurn": {"turnId": "turn-2", "state": "running"},
                "session": {
                    "status": "running",
                    "providerName": "codex",
                    "providerInstanceId": "backup",
                    "activeTurnId": "turn-2",
                    "lastError": None,
                },
                "messages": [{"id": message_id, "turnId": "turn-2"}],
            }

            result = coordinator.scan()

            self.assertEqual(result.actions[0].outcome, "verifying")
            with RunJournal(root / ".state" / "runs.sqlite3") as journal:
                row = journal.connection.execute(
                    "SELECT state FROM failover_incidents"
                ).fetchone()
                self.assertEqual(row[0], "verifying")

            client.thread["latestTurn"]["state"] = "completed"
            client.thread["session"]["status"] = "idle"
            client.thread["session"]["activeTurnId"] = None
            completed = coordinator.scan()
            self.assertEqual(completed.actions[0].outcome, "recovered")

    def test_target_quota_failure_selects_next_unattempted_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(failed_thread())
            coordinator = FailoverCoordinator(self._config(root), client)
            coordinator.scan()
            client.thread = failed_thread("backup", "turn-2")
            client.thread["messages"] = [
                {
                    "messageId": client.dispatched[0]["message"]["messageId"],
                    "turnId": "turn-2",
                }
            ]

            result = coordinator.scan()

            self.assertEqual(result.actions[0].outcome, "verifying")
            self.assertEqual(result.actions[0].target_instance_id, "last")
            self.assertEqual(client.dispatched[-1]["modelSelection"]["instanceId"], "last")

    def test_running_fallback_that_hits_quota_advances_same_incident_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(failed_thread())
            coordinator = FailoverCoordinator(self._config(root), client)

            first = coordinator.scan()
            first_message_id = client.dispatched[0]["message"]["messageId"]
            client.thread = {
                **failed_thread("backup", "turn-2"),
                "latestTurn": {"turnId": "turn-2", "state": "running"},
                "session": {
                    "status": "running",
                    "providerName": "codex",
                    "providerInstanceId": "backup",
                    "activeTurnId": "turn-2",
                    "lastError": None,
                },
                "messages": [{"id": first_message_id, "turnId": "turn-2"}],
            }

            running = coordinator.scan()
            client.thread["latestTurn"]["state"] = "error"
            client.thread["session"].update(
                {
                    "status": "error",
                    "activeTurnId": None,
                    "lastError": "Codex usage limit reached. Weekly limit resets later.",
                }
            )
            second_failure = coordinator.scan()

            self.assertEqual(first.actions[0].outcome, "verifying")
            self.assertEqual(running.actions[0].outcome, "verifying")
            self.assertEqual(second_failure.actions[0].outcome, "verifying")
            self.assertEqual(
                second_failure.actions[0].incident_id,
                first.actions[0].incident_id,
            )
            self.assertEqual(second_failure.actions[0].target_instance_id, "last")
            self.assertEqual(
                [command["modelSelection"]["instanceId"] for command in client.dispatched],
                ["backup", "last"],
            )
            with RunJournal(root / ".state" / "runs.sqlite3") as journal:
                incident = journal.active_incident_for_thread("thread-1")
                self.assertIsNotNone(incident)
                attempts = journal.provider_attempts(incident.id)
                self.assertEqual(
                    [attempt.provider_instance_id for attempt in attempts],
                    ["backup", "last"],
                )
                self.assertEqual(attempts[0].state, "failed")
                self.assertEqual(attempts[0].error_code, "quota_exhausted")
                self.assertEqual(attempts[1].state, "verifying")

    def test_reconcile_accepts_target_turn_when_coordinator_message_is_unbound(self):
        for turn_state, session_status, expected_outcome in (
            ("running", "running", "verifying"),
            ("completed", "idle", "recovered"),
        ):
            with self.subTest(turn_state=turn_state):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    client = FakeClient(failed_thread())
                    coordinator = FailoverCoordinator(self._config(root), client)
                    first = coordinator.scan()
                    coordinator_message_id = client.dispatched[0]["message"]["messageId"]
                    client.thread = {
                        **failed_thread("backup", "turn-2"),
                        "latestTurn": {"turnId": "turn-2", "state": turn_state},
                        "session": {
                            "status": session_status,
                            "providerName": "codex",
                            "providerInstanceId": "backup",
                            "activeTurnId": "turn-2" if turn_state == "running" else None,
                            "lastError": None,
                        },
                        "messages": [
                            {
                                "id": "manual-message",
                                "role": "user",
                                "turnId": "turn-1",
                            },
                            {
                                "id": coordinator_message_id,
                                "role": "user",
                                "turnId": None,
                            },
                        ],
                    }

                    result = coordinator.scan()

                    self.assertEqual(first.actions[0].outcome, "verifying")
                    self.assertEqual(result.actions[0].outcome, expected_outcome)
                    self.assertEqual(len(client.dispatched), 1)
                    with RunJournal(root / ".state" / "runs.sqlite3") as journal:
                        incident = journal.connection.execute(
                            "SELECT state FROM failover_incidents"
                        ).fetchone()
                        self.assertEqual(incident[0], expected_outcome)

    def test_reconcile_rejects_coordinator_message_bound_to_another_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(failed_thread())
            coordinator = FailoverCoordinator(self._config(root), client)
            coordinator.scan()
            coordinator_message_id = client.dispatched[0]["message"]["messageId"]
            client.thread = {
                **failed_thread("backup", "turn-2"),
                "latestTurn": {"turnId": "turn-2", "state": "running"},
                "session": {
                    "status": "running",
                    "providerName": "codex",
                    "providerInstanceId": "backup",
                    "activeTurnId": "turn-2",
                    "lastError": None,
                },
                "messages": [
                    {
                        "id": coordinator_message_id,
                        "role": "user",
                        "turnId": "different-turn",
                    }
                ],
            }

            result = coordinator.scan()

            self.assertEqual(result.actions[0].outcome, "manual-review")
            self.assertEqual(result.actions[0].reason, "unexpected_thread_change")

    def test_reconcile_rejects_coordinator_message_before_new_user_message(self):
        for coordinator_turn_id in (None, "turn-2"):
            with self.subTest(coordinator_turn_id=coordinator_turn_id):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    client = FakeClient(failed_thread())
                    coordinator = FailoverCoordinator(self._config(root), client)
                    coordinator.scan()
                    coordinator_message_id = client.dispatched[0]["message"]["messageId"]
                    client.thread = {
                        **failed_thread("backup", "turn-2"),
                        "latestTurn": {"turnId": "turn-2", "state": "running"},
                        "session": {
                            "status": "running",
                            "providerName": "codex",
                            "providerInstanceId": "backup",
                            "activeTurnId": "turn-2",
                            "lastError": None,
                        },
                        "messages": [
                            {
                                "id": coordinator_message_id,
                                "role": "user",
                                "turnId": coordinator_turn_id,
                            },
                            {
                                "id": "later-user-message",
                                "role": "user",
                                "turnId": None,
                            },
                        ],
                    }

                    result = coordinator.scan()

                    self.assertEqual(result.actions[0].outcome, "manual-review")
                    self.assertEqual(result.actions[0].reason, "unexpected_thread_change")

    def test_chained_uncertain_dispatch_retries_identical_second_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(failed_thread())
            coordinator = FailoverCoordinator(self._config(root), client)
            coordinator.scan()
            first_message_id = client.dispatched[0]["message"]["messageId"]
            client.thread = failed_thread("backup", "turn-2")
            client.thread["messages"] = [
                {"messageId": first_message_id, "turnId": "turn-2"}
            ]
            client.dispatch_error = True

            chained = coordinator.scan()
            retried = coordinator.scan()

            self.assertEqual(chained.actions[0].outcome, "dispatch-uncertain")
            self.assertEqual(retried.actions[0].outcome, "dispatch-uncertain")
            self.assertEqual(client.dispatched[-2], client.dispatched[-1])
            self.assertEqual(
                client.dispatched[-1]["modelSelection"]["instanceId"], "last"
            )

    def test_noncanonical_error_is_not_a_failover_incident(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thread = failed_thread()
            thread["session"]["lastError"] = "Provider request failed."
            client = FakeClient(thread)

            result = FailoverCoordinator(self._config(root), client).scan()

            self.assertEqual(result.actions, ())
            self.assertEqual(client.dispatched, [])

    def test_definite_dispatch_rejection_moves_to_manual_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(failed_thread())

            def reject(_command):
                raise T3HttpError(403, '{"private":"not logged"}', "http://t3/dispatch")

            client.dispatch = reject
            result = FailoverCoordinator(self._config(root), client).scan()

            self.assertEqual(result.actions[0].outcome, "manual-review")
            self.assertEqual(result.actions[0].reason, "dispatch_rejected_403")

    def test_async_provider_start_failure_moves_to_manual_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeClient(failed_thread())
            coordinator = FailoverCoordinator(self._config(root), client)
            coordinator.scan()
            message_id = client.dispatched[0]["message"]["messageId"]
            client.thread["activities"] = [
                {
                    "kind": "provider.turn.start.failed",
                    "turnId": None,
                    "payload": {
                        "requestId": message_id,
                        "detail": "private provider diagnostic",
                    },
                }
            ]

            result = coordinator.scan()

            self.assertEqual(result.actions[0].outcome, "manual-review")
            self.assertEqual(result.actions[0].reason, "provider_turn_start_failed")
            with RunJournal(root / ".state" / "runs.sqlite3") as journal:
                attempt = journal.provider_attempts(1)[0]
                self.assertEqual(attempt.error_code, "provider_turn_start_failed")

    def test_shell_pending_input_blocks_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thread = failed_thread()
            thread["hasPendingUserInput"] = True
            client = FakeClient(thread)

            result = FailoverCoordinator(self._config(root), client).scan()

            self.assertEqual(result.actions[0].outcome, "manual-review")
            self.assertEqual(result.actions[0].reason, "pending_user_input")
            self.assertEqual(client.dispatched, [])

    def test_provider_snapshot_adaptation_preserves_usage_and_rejects_unavailable(self):
        healthy = provider("healthy", 25)
        unavailable = provider("unknown", 0)
        unavailable["usageLimits"]["unavailable"] = {
            "reason": "Provider did not report usage limits"
        }
        unavailable["status"] = "error"

        candidates = {item.instance_id: item for item in provider_candidates([healthy, unavailable])}

        self.assertEqual(candidates["healthy"].usage.windows[0].used_percent, 25)
        self.assertIsNone(candidates["unknown"].usage)
        self.assertFalse(candidates["unknown"].available)

    def test_empty_usage_windows_are_unknown(self):
        empty = provider("empty", 25)
        empty["usageLimits"]["windows"] = []

        candidate = provider_candidates([empty])[0]

        self.assertIsNone(candidate.usage)

    def test_idle_scan_does_not_refresh_provider_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thread = failed_thread()
            thread["session"]["lastError"] = None
            thread["latestTurn"]["state"] = "completed"
            client = FakeClient(thread)

            result = FailoverCoordinator(self._config(root), client).scan()

            self.assertEqual(result.actions, ())
            self.assertFalse(hasattr(client, "refreshed"))


if __name__ == "__main__":
    unittest.main()
