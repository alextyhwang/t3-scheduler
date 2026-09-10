from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
import math

from .config import FailoverConfig


@dataclass(frozen=True)
class UsageWindow:
    used_percent: float
    resets_at: datetime | None = None


@dataclass(frozen=True)
class ProviderUsage:
    checked_at: datetime
    windows: tuple[UsageWindow, ...]


@dataclass(frozen=True)
class ProviderCandidate:
    instance_id: str
    driver: str
    continuation_group: str
    enabled: bool
    installed: bool
    authenticated: bool
    available: bool
    models: frozenset[str]
    usage: ProviderUsage | None


@dataclass(frozen=True)
class ProviderSelection:
    instance_id: str
    priority: int
    headroom_percent: float | None


def _headroom(
    usage: ProviderUsage | None,
    now: datetime,
    max_age_seconds: int,
) -> float | None:
    if usage is None:
        return None
    try:
        if (now - usage.checked_at).total_seconds() > max_age_seconds:
            return None
        active = [
            window
            for window in usage.windows
            if window.resets_at is None or window.resets_at > now
        ]
    except TypeError:
        return None
    if any(
        not math.isfinite(window.used_percent) or not 0 <= window.used_percent <= 100
        for window in active
    ):
        return None
    if not active:
        return 100.0
    return min(100.0 - window.used_percent for window in active)


def select_provider(
    config: FailoverConfig,
    candidates: Iterable[ProviderCandidate],
    *,
    current_instance_id: str,
    driver: str,
    continuation_group: str,
    model: str | None,
    excluded_instance_ids: Collection[str] = (),
    now: datetime | None = None,
) -> ProviderSelection | None:
    """Choose an eligible provider by priority, headroom, then config order."""
    if not config.enabled:
        return None
    now = now or datetime.now(timezone.utc)
    by_id = {candidate.instance_id: candidate for candidate in candidates}
    excluded = set(excluded_instance_ids)
    ranked: list[tuple[int, float, int, ProviderSelection]] = []
    for order, provider in enumerate(config.providers):
        candidate = by_id.get(provider.instance_id)
        if (
            not provider.enabled
            or candidate is None
            or candidate.instance_id == current_instance_id
            or candidate.instance_id in excluded
            or candidate.driver != driver
            or candidate.continuation_group != continuation_group
            or not candidate.enabled
            or not candidate.installed
            or not candidate.authenticated
            or not candidate.available
            or (model is not None and model not in candidate.models)
        ):
            continue
        headroom = _headroom(candidate.usage, now, config.max_usage_age_seconds)
        if headroom is None and not config.allow_unknown_usage:
            continue
        if headroom is not None and headroom <= 0:
            continue
        score = headroom if headroom is not None else -1.0
        ranked.append(
            (
                provider.priority,
                score,
                -order,
                ProviderSelection(candidate.instance_id, provider.priority, headroom),
            )
        )
    return max(ranked, default=None, key=lambda item: item[:3])[3] if ranked else None
