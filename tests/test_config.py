from pathlib import Path
import tempfile
import unittest

from t3_scheduler.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_loads_inline_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "jobs.toml"
            config.write_text(
                """
[scheduler]
state_dir = ".state"
[[jobs]]
id = "hello"
cron = "0 8 * * *"
project = "."
prompt = "hello"
""",
                encoding="utf-8",
            )
            loaded = load_config(config)
            self.assertEqual(loaded.jobs[0].id, "hello")
            self.assertEqual(loaded.jobs[0].project, root.resolve())

    def test_rejects_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "jobs.toml"
            config.write_text(
                '[[jobs]]\nid="x"\ncron="* * * * *"\nproject="."\nprompt="a"\n'
                '[[jobs]]\nid="x"\ncron="* * * * *"\nproject="."\nprompt="b"\n',
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                load_config(config)


if __name__ == "__main__":
    unittest.main()
