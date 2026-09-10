from pathlib import Path
import sqlite3
import tempfile
import unittest

from t3_scheduler.state import SCHEMA_VERSION, RunJournal


class StateTests(unittest.TestCase):
    def test_success_is_idempotent_and_failure_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunJournal(Path(directory) / "runs.sqlite3") as journal:
                first_command = {"threadId": "thread", "commandId": "command"}
                self.assertTrue(journal.claim("job", "time", first_command).acquired)
                journal.succeeded("job", "time", "thread")
                self.assertFalse(journal.claim("job", "time", first_command).acquired)
                self.assertTrue(journal.claim("job", "other", first_command).acquired)
                journal.failed("job", "other", "boom")
                replacement = {"threadId": "duplicate", "commandId": "different"}
                retry = journal.claim("job", "other", replacement)
                self.assertTrue(retry.acquired)
                self.assertEqual(retry.prior_status, "failed")
                self.assertEqual(retry.command, first_command)

    def test_additive_migration_preserves_legacy_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """CREATE TABLE runs (
                    job_id TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    thread_id TEXT,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    PRIMARY KEY (job_id, scheduled_for)
                )"""
            )
            connection.execute(
                """INSERT INTO runs
                   (job_id, scheduled_for, status, thread_id, error, started_at)
                   VALUES ('legacy', 'minute', 'failed', 'thread-1', 'old failure', 'then')"""
            )
            connection.commit()
            connection.close()

            with RunJournal(path) as journal:
                row = journal.connection.execute(
                    "SELECT job_id, status, thread_id, error FROM runs"
                ).fetchone()
                self.assertEqual(tuple(row), ("legacy", "failed", "thread-1", "old failure"))
                columns = {
                    item[1]
                    for item in journal.connection.execute("PRAGMA table_info(runs)").fetchall()
                }
                self.assertIn("command_json", columns)
                self.assertEqual(
                    journal.connection.execute("PRAGMA user_version").fetchone()[0],
                    SCHEMA_VERSION,
                )

    def test_incident_detection_is_unique_and_keeps_first_normalized_facts(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunJournal(Path(directory) / "runs.sqlite3") as journal:
                first = journal.detect_incident(
                    "thread-1",
                    "turn-1",
                    failed_instance_id="codex-school",
                    failure_code="quota_exhausted",
                )
                duplicate = journal.detect_incident(
                    "thread-1",
                    "turn-1",
                    failed_instance_id="codex-other",
                    failure_code="rate_limited",
                )

                self.assertEqual(duplicate.id, first.id)
                self.assertEqual(duplicate.failed_instance_id, "codex-school")
                self.assertEqual(duplicate.failure_code, "quota_exhausted")
                self.assertEqual(duplicate.state, "detected")
                count = journal.connection.execute(
                    "SELECT COUNT(*) FROM failover_incidents"
                ).fetchone()[0]
                self.assertEqual(count, 1)

    def test_active_incident_for_thread_returns_oldest_unresolved_incident(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunJournal(Path(directory) / "runs.sqlite3") as journal:
                terminal = journal.detect_incident(
                    "thread-1",
                    "turn-terminal",
                    failed_instance_id=None,
                    failure_code="quota_exhausted",
                )
                terminal_claim = journal.claim_incident(terminal.id, "coordinator")
                journal.transition_incident(
                    terminal.id, terminal_claim.claim_token or "", "manual-review"
                )
                oldest = journal.detect_incident(
                    "thread-1",
                    "turn-oldest",
                    failed_instance_id=None,
                    failure_code="quota_exhausted",
                )
                journal.detect_incident(
                    "thread-1",
                    "turn-newest",
                    failed_instance_id=None,
                    failure_code="quota_exhausted",
                )

                active = journal.active_incident_for_thread("thread-1")
                self.assertEqual(active.id if active else None, oldest.id)
                self.assertIsNone(journal.active_incident_for_thread("unknown-thread"))

    def test_scheduler_thread_ids_are_distinct_and_non_null(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunJournal(Path(directory) / "runs.sqlite3") as journal:
                journal.claim(
                    "job-1", "minute-1", {"threadId": "thread-1", "commandId": "one"}
                )
                journal.claim(
                    "job-2", "minute-2", {"threadId": "thread-1", "commandId": "two"}
                )
                journal.claim("job-3", "minute-3", {"commandId": "three"})

                self.assertEqual(journal.scheduler_thread_ids(), frozenset({"thread-1"}))

    def test_list_failover_incidents_is_newest_first_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunJournal(Path(directory) / "runs.sqlite3") as journal:
                incidents = [
                    journal.detect_incident(
                        "thread-1",
                        f"turn-{index}",
                        failed_instance_id=None,
                        failure_code="quota_exhausted",
                    )
                    for index in range(3)
                ]

                listed = journal.list_failover_incidents(limit=2)
                self.assertEqual(
                    [incident.id for incident in listed],
                    [incidents[2].id, incidents[1].id],
                )
                with self.assertRaises(ValueError):
                    journal.list_failover_incidents(limit=0)

    def test_incident_claim_is_exclusive_and_releasable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.sqlite3"
            with RunJournal(path) as first_journal, RunJournal(path) as second_journal:
                incident = first_journal.detect_incident(
                    "thread-1",
                    "turn-1",
                    failed_instance_id=None,
                    failure_code="quota_exhausted",
                )
                first = first_journal.claim_incident(incident.id, "coordinator-a")
                blocked = second_journal.claim_incident(incident.id, "coordinator-b")

                self.assertTrue(first.acquired)
                self.assertIsNotNone(first.claim_token)
                self.assertFalse(blocked.acquired)
                first_journal.release_incident_claim(incident.id, first.claim_token or "")
                second = second_journal.claim_incident(incident.id, "coordinator-b")
                self.assertTrue(second.acquired)
                self.assertNotEqual(second.claim_token, first.claim_token)

    def test_claim_next_skips_a_live_claim_and_recovers_an_expired_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.sqlite3"
            with RunJournal(path) as journal:
                first_incident = journal.detect_incident(
                    "thread-1",
                    "turn-1",
                    failed_instance_id=None,
                    failure_code="quota_exhausted",
                )
                second_incident = journal.detect_incident(
                    "thread-2",
                    "turn-2",
                    failed_instance_id=None,
                    failure_code="quota_exhausted",
                )
                first = journal.claim_next_incident("coordinator-a")
                second = journal.claim_next_incident("coordinator-b")
                self.assertEqual(first.incident.id if first else None, first_incident.id)
                self.assertEqual(second.incident.id if second else None, second_incident.id)

                journal.connection.execute(
                    """UPDATE failover_incidents
                       SET claim_expires_at = '2000-01-01T00:00:00+00:00'
                       WHERE incident_id = ?""",
                    (first_incident.id,),
                )
                journal.connection.commit()
                recovered = journal.claim_next_incident("coordinator-c")
                self.assertEqual(recovered.incident.id if recovered else None, first_incident.id)

    def test_provider_attempt_is_unique_and_retains_complete_original_command(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunJournal(Path(directory) / "runs.sqlite3") as journal:
                incident = journal.detect_incident(
                    "thread-1",
                    "turn-1",
                    failed_instance_id="codex-school",
                    failure_code="quota_exhausted",
                )
                claim = journal.claim_incident(incident.id, "coordinator")
                token = claim.claim_token or ""
                command = {
                    "type": "thread.turn.start",
                    "threadId": "thread-1",
                    "commandId": "command-1",
                    "message": {"messageId": "message-1", "text": "continue"},
                    "modelSelection": {
                        "instanceId": "codex-alvin",
                        "model": "gpt-test",
                        "options": [{"id": "reasoningEffort", "value": "high"}],
                    },
                }
                first = journal.claim_provider_attempt(
                    incident.id, token, "codex-alvin", command
                )
                replacement = {
                    "threadId": "thread-1",
                    "commandId": "replacement-that-must-not-win",
                }
                duplicate = journal.claim_provider_attempt(
                    incident.id, token, "codex-alvin", replacement
                )
                other = journal.claim_provider_attempt(
                    incident.id,
                    token,
                    "codex-jimmy",
                    {"threadId": "thread-1", "commandId": "command-2"},
                )

                self.assertTrue(first.acquired)
                self.assertEqual(
                    journal.active_incident_for_thread("thread-1").state,
                    "dispatching",
                )
                self.assertFalse(duplicate.acquired)
                self.assertEqual(duplicate.attempt.command, command)
                self.assertTrue(other.acquired)
                attempts = journal.provider_attempts(incident.id)
                self.assertEqual(
                    [attempt.provider_instance_id for attempt in attempts],
                    ["codex-alvin", "codex-jimmy"],
                )
                self.assertEqual(attempts[0].command, command)

    def test_incident_and_attempt_lifecycle_require_the_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunJournal(Path(directory) / "runs.sqlite3") as journal:
                incident = journal.detect_incident(
                    "thread-1",
                    "turn-1",
                    failed_instance_id="codex-school",
                    failure_code="quota_exhausted",
                )
                claim = journal.claim_incident(incident.id, "coordinator")
                token = claim.claim_token or ""
                dispatching = journal.transition_incident(
                    incident.id, token, "dispatching"
                )
                self.assertEqual(dispatching.state, "dispatching")
                attempt = journal.claim_provider_attempt(
                    incident.id,
                    token,
                    "codex-alvin",
                    {"threadId": "thread-1", "commandId": "command-1"},
                ).attempt
                verifying_attempt = journal.transition_provider_attempt(
                    attempt.id, token, "verifying"
                )
                self.assertEqual(verifying_attempt.state, "verifying")
                verifying = journal.transition_incident(incident.id, token, "verifying")
                self.assertEqual(verifying.state, "verifying")
                recovered_attempt = journal.transition_provider_attempt(
                    attempt.id, token, "recovered"
                )
                self.assertEqual(recovered_attempt.state, "recovered")
                recovered = journal.transition_incident(incident.id, token, "recovered")
                self.assertEqual(recovered.state, "recovered")
                self.assertIsNone(recovered.claim_owner)
                self.assertFalse(
                    journal.claim_incident(incident.id, "another-coordinator").acquired
                )
                with self.assertRaises(RuntimeError):
                    journal.transition_incident(incident.id, token, "manual-review")

    def test_only_normalized_failure_codes_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunJournal(Path(directory) / "runs.sqlite3") as journal:
                with self.assertRaises(ValueError):
                    journal.detect_incident(
                        "thread-1",
                        "turn-1",
                        failed_instance_id="codex-school",
                        failure_code="HTTP 429: raw provider payload",
                    )

    def test_manual_review_and_exhausted_are_terminal_incident_states(self):
        with tempfile.TemporaryDirectory() as directory:
            with RunJournal(Path(directory) / "runs.sqlite3") as journal:
                for index, terminal_state in enumerate(("manual-review", "exhausted")):
                    incident = journal.detect_incident(
                        f"thread-{index}",
                        f"turn-{index}",
                        failed_instance_id=None,
                        failure_code="quota_exhausted",
                    )
                    claim = journal.claim_incident(incident.id, "coordinator")
                    terminal = journal.transition_incident(
                        incident.id, claim.claim_token or "", terminal_state
                    )
                    self.assertEqual(terminal.state, terminal_state)
                    self.assertIsNone(terminal.claim_owner)
                    self.assertFalse(
                        journal.claim_incident(incident.id, "another").acquired
                    )


if __name__ == "__main__":
    unittest.main()
