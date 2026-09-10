from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from t3_scheduler.config import load_config
from t3_scheduler.coordinator import CoordinatorAction, CoordinatorScanResult
from t3_scheduler.runner import SchedulerRunner, build_command, scheduled_occurrence
from t3_scheduler.state import RunJournal


class RunnerTests(unittest.TestCase):
    def _job(self, directory: str, cron: str = "0 20 * * *"):
        root = Path(directory)
        config = root / "jobs.toml"
        config.write_text(
            f'''[[jobs]]
id = "nightly"
cron = "{cron}"
timezone = "America/Los_Angeles"
project = "."
prompt = "run {{{{job_id}}}} at {{{{scheduled_for}}}}"
instance_id = "codex"
model = "gpt-test"
reasoning_effort = "high"
''',
            encoding="utf-8",
        )
        return load_config(config).jobs[0]

    def _failover_config(self, directory: str):
        config = Path(directory) / "jobs.toml"
        config.write_text(
            '''[failover]
enabled = true
mode = "shadow"
''',
            encoding="utf-8",
        )
        return load_config(config)

    def test_misfire_returns_latest_occurrence(self):
        with tempfile.TemporaryDirectory() as directory:
            job = self._job(directory)
            now = datetime(2026, 8, 29, 3, 7, tzinfo=timezone.utc)  # 20:07 PDT
            result = scheduled_occurrence(job, now, 15)
            self.assertIsNotNone(result)
            self.assertEqual((result.hour, result.minute), (20, 0))

    def test_builds_native_bootstrap_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            job = self._job(directory)
            at = datetime(2026, 8, 28, 20, 0, tzinfo=job.timezone)
            project = {"id": "project-1", "defaultModelSelection": None}
            thread_id, command = build_command(job, project, at)
            self.assertEqual(command["type"], "thread.turn.start")
            self.assertEqual(command["threadId"], thread_id)
            self.assertEqual(command["bootstrap"]["createThread"]["projectId"], "project-1")
            self.assertEqual(command["modelSelection"]["model"], "gpt-test")
            self.assertIn("nightly", command["message"]["text"])

    def test_tick_runs_failover_scan_even_when_no_jobs_are_due(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._failover_config(directory)
            result = CoordinatorScanResult(True, "shadow", 0, ())
            with patch("t3_scheduler.runner.FailoverCoordinator") as coordinator:
                coordinator.return_value.scan.return_value = result
                runner = SchedulerRunner(config, client=object())
                self.assertEqual(runner.tick(), 0)
                coordinator.return_value.scan.assert_called_once_with(dry_run=False)

    def test_tick_isolates_failover_transport_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._failover_config(directory)
            with patch("t3_scheduler.runner.FailoverCoordinator") as coordinator:
                coordinator.return_value.scan.side_effect = RuntimeError("incompatible T3")
                runner = SchedulerRunner(config, client=object())
                self.assertEqual(runner.tick(), 1)

    def test_tick_returns_nonzero_for_manual_review(self):
        with tempfile.TemporaryDirectory() as directory:
            config = self._failover_config(directory)
            result = CoordinatorScanResult(
                True,
                "active",
                1,
                (CoordinatorAction("thread-1", "manual-review", 1),),
            )
            with patch("t3_scheduler.runner.FailoverCoordinator") as coordinator:
                coordinator.return_value.scan.return_value = result
                runner = SchedulerRunner(config, client=object())
                self.assertEqual(runner.tick(), 1)

    def test_manual_run_is_journaled_for_scheduler_only_failover_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "jobs.toml"
            job = self._job(directory)
            config = load_config(config_path)
            client = Mock()
            client.shell_snapshot.return_value = {
                "projects": [
                    {"id": "project-1", "workspaceRoot": str(job.project)}
                ]
            }
            client.dispatch.return_value = {"sequence": 1}
            runner = SchedulerRunner(config, client=client)

            self.assertEqual(runner.run_manual("nightly"), 0)

            thread_id = client.dispatch.call_args.args[0]["threadId"]
            with RunJournal(config.state_dir / "runs.sqlite3") as journal:
                self.assertIn(thread_id, journal.scheduler_thread_ids())


if __name__ == "__main__":
    unittest.main()
