from pathlib import Path
import tempfile
import unittest

from t3_scheduler.config import AppConfig, ConfigError, T3Config, load_config


class ConfigTests(unittest.TestCase):
    def test_legacy_app_config_construction_defaults_failover_off(self):
        app = AppConfig(
            path=Path("jobs.toml"),
            state_dir=Path(".state"),
            misfire_grace_minutes=15,
            t3=T3Config("http://example.test", Path("."), False, 30),
            jobs=(),
        )

        self.assertFalse(app.failover.enabled)

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
            self.assertEqual(loaded.t3.base_dir, (Path.home() / ".t3").resolve())
            self.assertFalse(loaded.failover.enabled)
            self.assertEqual(loaded.failover.mode, "shadow")
            self.assertFalse(loaded.failover.allow_interactive_threads)
            self.assertEqual(loaded.failover.project_allowlist, ())
            self.assertEqual(loaded.failover.providers, ())

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

    def test_loads_failover_policy_and_provider_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "jobs.toml"
            config.write_text(
                '''[failover]
enabled = true
mode = "active"
max_usage_age_seconds = 120
allow_unknown_usage = true

[[failover.providers]]
instance_id = "codex_school"
priority = 100

[[failover.providers]]
instance_id = "codex_backup"
priority = 25
enabled = false
''',
                encoding="utf-8",
            )

            loaded = load_config(config).failover

            self.assertTrue(loaded.enabled)
            self.assertEqual(loaded.mode, "active")
            self.assertEqual(loaded.max_usage_age_seconds, 120)
            self.assertTrue(loaded.allow_unknown_usage)
            self.assertEqual(
                [
                    (provider.instance_id, provider.priority, provider.enabled)
                    for provider in loaded.providers
                ],
                [("codex_school", 100, True), ("codex_backup", 25, False)],
            )

    def test_rejects_duplicate_failover_provider_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "jobs.toml"
            config.write_text(
                '''[[failover.providers]]
instance_id = "codex_school"

[[failover.providers]]
instance_id = "codex_school"
''',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "duplicate instance_id"):
                load_config(config)

    def test_loads_safe_interactive_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "jobs.toml"
            config.write_text(
                '''[failover]
allow_interactive_threads = true
project_allowlist = [".", "projects/allowed"]
''',
                encoding="utf-8",
            )

            loaded = load_config(config).failover

            self.assertTrue(loaded.allow_interactive_threads)
            self.assertEqual(
                loaded.project_allowlist,
                (root.resolve(), (root / "projects" / "allowed").resolve()),
            )

    def test_rejects_invalid_failover_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "jobs.toml"
            config.write_text('[failover]\nmode = "automatic"\n', encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "shadow.*active"):
                load_config(config)

    def test_rejects_invalid_usage_freshness(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "jobs.toml"
            config.write_text(
                "[failover]\nmax_usage_age_seconds = 0\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ConfigError, "between 1 and 86400"):
                load_config(config)

    def test_keeps_explicit_t3_base_directory_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "jobs.toml"
            config.write_text('[t3]\nbase_dir = "runtime"\n', encoding="utf-8")

            self.assertEqual(load_config(config).t3.base_dir, (root / "runtime").resolve())


if __name__ == "__main__":
    unittest.main()
