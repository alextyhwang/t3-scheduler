from pathlib import Path
import tempfile
import unittest

from t3_scheduler.state import RunJournal


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


if __name__ == "__main__":
    unittest.main()
