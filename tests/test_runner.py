from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from t3_scheduler.config import load_config
from t3_scheduler.runner import build_command, scheduled_occurrence


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


if __name__ == "__main__":
    unittest.main()
