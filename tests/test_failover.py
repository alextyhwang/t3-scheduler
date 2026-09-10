from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from t3_scheduler.config import FailoverConfig, FailoverProviderConfig
from t3_scheduler.failover import (
    ProviderCandidate,
    ProviderUsage,
    UsageWindow,
    select_provider,
)


NOW = datetime(2026, 9, 9, 20, 0, tzinfo=timezone.utc)


class FailoverSelectionTests(unittest.TestCase):
    def test_priority_tier_wins_before_usage_headroom(self):
        policy = FailoverConfig(
            enabled=True,
            mode="active",
            max_usage_age_seconds=300,
            allow_unknown_usage=False,
            providers=(
                FailoverProviderConfig("preferred", 100, True),
                FailoverProviderConfig("roomier", 50, True),
            ),
        )
        candidates = (
            self._candidate("preferred", used_percent=90),
            self._candidate("roomier", used_percent=10),
        )

        selected = select_provider(
            policy,
            candidates,
            current_instance_id="current",
            driver="codex",
            continuation_group="codex:home:shared",
            model="gpt-test",
            now=NOW,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.instance_id, "preferred")
        self.assertEqual(selected.headroom_percent, 10)

    def test_most_constrained_active_window_determines_headroom(self):
        policy = self._policy("constrained", "balanced")
        constrained = self._candidate("constrained", used_percent=20)
        constrained = replace(
            constrained,
            usage=ProviderUsage(
                checked_at=NOW,
                windows=(UsageWindow(20), UsageWindow(95)),
            ),
        )
        balanced = self._candidate("balanced", used_percent=70)

        selected = select_provider(
            policy,
            (constrained, balanced),
            current_instance_id="current",
            driver="codex",
            continuation_group="codex:home:shared",
            model="gpt-test",
            now=NOW,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.instance_id, "balanced")
        self.assertEqual(selected.headroom_percent, 30)

    def test_never_selects_the_current_provider(self):
        policy = FailoverConfig(
            enabled=True,
            mode="active",
            max_usage_age_seconds=300,
            allow_unknown_usage=False,
            providers=(
                FailoverProviderConfig("current", 200, True),
                FailoverProviderConfig("backup", 100, True),
            ),
        )

        selected = select_provider(
            policy,
            (
                self._candidate("current", used_percent=1),
                self._candidate("backup", used_percent=50),
            ),
            current_instance_id="current",
            driver="codex",
            continuation_group="codex:home:shared",
            model="gpt-test",
            now=NOW,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.instance_id, "backup")

    def test_requires_compatible_driver_and_continuation_group(self):
        policy = FailoverConfig(
            enabled=True,
            mode="active",
            max_usage_age_seconds=300,
            allow_unknown_usage=False,
            providers=(
                FailoverProviderConfig("wrong-driver", 300, True),
                FailoverProviderConfig("wrong-home", 200, True),
                FailoverProviderConfig("compatible", 100, True),
            ),
        )
        wrong_driver = replace(self._candidate("wrong-driver", used_percent=1), driver="claude")
        wrong_home = replace(
            self._candidate("wrong-home", used_percent=1),
            continuation_group="codex:home:other",
        )

        selected = select_provider(
            policy,
            (wrong_driver, wrong_home, self._candidate("compatible", used_percent=50)),
            current_instance_id="current",
            driver="codex",
            continuation_group="codex:home:shared",
            model="gpt-test",
            now=NOW,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.instance_id, "compatible")

    def test_requires_available_authenticated_runtime_with_the_model(self):
        ids = ("disabled", "missing", "signed-out", "unavailable", "wrong-model", "healthy")
        policy = FailoverConfig(
            enabled=True,
            mode="active",
            max_usage_age_seconds=300,
            allow_unknown_usage=False,
            providers=tuple(
                FailoverProviderConfig(instance_id, 600 - order * 100, True)
                for order, instance_id in enumerate(ids)
            ),
        )
        base = {instance_id: self._candidate(instance_id, used_percent=10) for instance_id in ids}
        candidates = (
            replace(base["disabled"], enabled=False),
            replace(base["missing"], installed=False),
            replace(base["signed-out"], authenticated=False),
            replace(base["unavailable"], available=False),
            replace(base["wrong-model"], models=frozenset({"other-model"})),
            base["healthy"],
        )

        selected = select_provider(
            policy,
            candidates,
            current_instance_id="current",
            driver="codex",
            continuation_group="codex:home:shared",
            model="gpt-test",
            now=NOW,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.instance_id, "healthy")

    def test_stale_usage_is_ineligible_by_default(self):
        policy = self._policy("stale", "fresh")
        stale = replace(
            self._candidate("stale", used_percent=1),
            usage=ProviderUsage(
                checked_at=NOW - timedelta(seconds=301),
                windows=(UsageWindow(1),),
            ),
        )

        selected = select_provider(
            policy,
            (stale, self._candidate("fresh", used_percent=80)),
            current_instance_id="current",
            driver="codex",
            continuation_group="codex:home:shared",
            model="gpt-test",
            now=NOW,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.instance_id, "fresh")

    def test_unknown_usage_can_be_allowed_but_loses_to_known_headroom_in_its_tier(self):
        policy = replace(self._policy("unknown", "known"), allow_unknown_usage=True)
        unknown = replace(self._candidate("unknown", used_percent=1), usage=None)

        selected = select_provider(
            policy,
            (unknown, self._candidate("known", used_percent=99)),
            current_instance_id="current",
            driver="codex",
            continuation_group="codex:home:shared",
            model="gpt-test",
            now=NOW,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.instance_id, "known")
        self.assertEqual(selected.headroom_percent, 1)

    def test_exhausted_provider_is_ineligible(self):
        policy = FailoverConfig(
            enabled=True,
            mode="active",
            max_usage_age_seconds=300,
            allow_unknown_usage=False,
            providers=(
                FailoverProviderConfig("exhausted", 200, True),
                FailoverProviderConfig("backup", 100, True),
            ),
        )

        selected = select_provider(
            policy,
            (
                self._candidate("exhausted", used_percent=100),
                self._candidate("backup", used_percent=80),
            ),
            current_instance_id="current",
            driver="codex",
            continuation_group="codex:home:shared",
            model="gpt-test",
            now=NOW,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.instance_id, "backup")

    def test_equal_rank_uses_configuration_order(self):
        policy = self._policy("first", "second")

        selected = select_provider(
            policy,
            (
                self._candidate("second", used_percent=50),
                self._candidate("first", used_percent=50),
            ),
            current_instance_id="current",
            driver="codex",
            continuation_group="codex:home:shared",
            model="gpt-test",
            now=NOW,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.instance_id, "first")

    def test_disabled_and_previously_attempted_provider_entries_are_skipped(self):
        policy = FailoverConfig(
            enabled=True,
            mode="active",
            max_usage_age_seconds=300,
            allow_unknown_usage=False,
            providers=(
                FailoverProviderConfig("disabled", 300, False),
                FailoverProviderConfig("attempted", 200, True),
                FailoverProviderConfig("next", 100, True),
            ),
        )

        selected = select_provider(
            policy,
            tuple(
                self._candidate(instance_id, used_percent=10)
                for instance_id in ("disabled", "attempted", "next")
            ),
            current_instance_id="current",
            driver="codex",
            continuation_group="codex:home:shared",
            model="gpt-test",
            excluded_instance_ids={"attempted"},
            now=NOW,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.instance_id, "next")

    def test_expired_windows_do_not_reduce_current_headroom(self):
        policy = self._policy("reset", "other")
        reset = replace(
            self._candidate("reset", used_percent=1),
            usage=ProviderUsage(
                checked_at=NOW,
                windows=(
                    UsageWindow(100, resets_at=NOW - timedelta(seconds=1)),
                    UsageWindow(20),
                ),
            ),
        )

        selected = select_provider(
            policy,
            (reset, self._candidate("other", used_percent=30)),
            current_instance_id="current",
            driver="codex",
            continuation_group="codex:home:shared",
            model="gpt-test",
            now=NOW,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.instance_id, "reset")
        self.assertEqual(selected.headroom_percent, 80)

    @staticmethod
    def _policy(*instance_ids: str) -> FailoverConfig:
        return FailoverConfig(
            enabled=True,
            mode="active",
            max_usage_age_seconds=300,
            allow_unknown_usage=False,
            providers=tuple(
                FailoverProviderConfig(instance_id, 100, True)
                for instance_id in instance_ids
            ),
        )

    @staticmethod
    def _candidate(instance_id: str, *, used_percent: float) -> ProviderCandidate:
        return ProviderCandidate(
            instance_id=instance_id,
            driver="codex",
            continuation_group="codex:home:shared",
            enabled=True,
            installed=True,
            authenticated=True,
            available=True,
            models=frozenset({"gpt-test"}),
            usage=ProviderUsage(
                checked_at=NOW,
                windows=(UsageWindow(used_percent=used_percent),),
            ),
        )


if __name__ == "__main__":
    unittest.main()
