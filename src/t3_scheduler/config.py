from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .cron import CronError, CronSchedule


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class T3Config:
    base_url: str
    base_dir: Path
    auto_start: bool
    startup_timeout_seconds: int
    executable: Path | None = None


@dataclass(frozen=True)
class JobConfig:
    id: str
    enabled: bool
    cron: CronSchedule
    timezone: ZoneInfo
    timezone_name: str
    project: Path
    title: str
    prompt: str | None
    prompt_file: Path | None
    instance_id: str | None
    model: str | None
    reasoning_effort: str | None
    service_tier: str | None
    runtime_mode: str
    interaction_mode: str
    misfire_policy: str


@dataclass(frozen=True)
class AppConfig:
    path: Path
    state_dir: Path
    misfire_grace_minutes: int
    t3: T3Config
    jobs: tuple[JobConfig, ...]


def _path(value: str, *, relative_to: Path) -> Path:
    expanded = Path(os.path.expandvars(os.path.expanduser(value)))
    return expanded if expanded.is_absolute() else relative_to / expanded


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    try:
        with config_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load {config_path}: {exc}") from exc

    root = config_path.parent
    scheduler = raw.get("scheduler", {})
    t3_raw = raw.get("t3", {})
    state_dir = _path(str(scheduler.get("state_dir", ".state")), relative_to=root).resolve()
    grace = int(scheduler.get("misfire_grace_minutes", 15))
    if grace < 0 or grace > 1440:
        raise ConfigError("scheduler.misfire_grace_minutes must be between 0 and 1440")

    base_url = str(t3_raw.get("base_url", "http://127.0.0.1:3773")).rstrip("/")
    base_dir = _path(
        str(t3_raw.get("base_dir", r"%LOCALAPPDATA%\t3-code")), relative_to=root
    ).resolve()
    executable_raw = t3_raw.get("executable")
    t3 = T3Config(
        base_url=base_url,
        base_dir=base_dir,
        auto_start=bool(t3_raw.get("auto_start", True)),
        startup_timeout_seconds=int(t3_raw.get("startup_timeout_seconds", 30)),
        executable=_path(str(executable_raw), relative_to=root).resolve() if executable_raw else None,
    )

    jobs: list[JobConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(raw.get("jobs", []), start=1):
        try:
            job_id = str(item["id"]).strip()
            if not job_id or job_id in seen:
                raise ConfigError(f"job #{index} has an empty or duplicate id {job_id!r}")
            seen.add(job_id)
            expression = CronSchedule.parse(str(item["cron"]))
            timezone_name = str(item.get("timezone", "UTC"))
            timezone = ZoneInfo(timezone_name)
            project = _path(str(item["project"]), relative_to=root).resolve()
            prompt = item.get("prompt")
            prompt_file_raw = item.get("prompt_file")
            if (prompt is None) == (prompt_file_raw is None):
                raise ConfigError(f"job {job_id!r} must set exactly one of prompt or prompt_file")
            prompt_file = (
                _path(str(prompt_file_raw), relative_to=root).resolve() if prompt_file_raw else None
            )
            runtime_mode = str(item.get("runtime_mode", "full-access"))
            if runtime_mode not in {"approval-required", "auto-accept-edits", "auto", "full-access"}:
                raise ConfigError(f"job {job_id!r} has invalid runtime_mode {runtime_mode!r}")
            interaction_mode = str(item.get("interaction_mode", "default"))
            if interaction_mode not in {"default", "plan"}:
                raise ConfigError(f"job {job_id!r} has invalid interaction_mode {interaction_mode!r}")
            misfire_policy = str(item.get("misfire_policy", "run-once"))
            if misfire_policy not in {"run-once", "skip"}:
                raise ConfigError(f"job {job_id!r} has invalid misfire_policy {misfire_policy!r}")
            jobs.append(
                JobConfig(
                    id=job_id,
                    enabled=bool(item.get("enabled", True)),
                    cron=expression,
                    timezone=timezone,
                    timezone_name=timezone_name,
                    project=project,
                    title=str(item.get("title", job_id)).strip() or job_id,
                    prompt=str(prompt) if prompt is not None else None,
                    prompt_file=prompt_file,
                    instance_id=str(item["instance_id"]) if item.get("instance_id") else None,
                    model=str(item["model"]) if item.get("model") else None,
                    reasoning_effort=(
                        str(item["reasoning_effort"]) if item.get("reasoning_effort") else None
                    ),
                    service_tier=str(item["service_tier"]) if item.get("service_tier") else None,
                    runtime_mode=runtime_mode,
                    interaction_mode=interaction_mode,
                    misfire_policy=misfire_policy,
                )
            )
        except (KeyError, CronError, ZoneInfoNotFoundError, TypeError, ValueError) as exc:
            if isinstance(exc, ConfigError):
                raise
            raise ConfigError(f"invalid job #{index}: {exc}") from exc
    return AppConfig(config_path, state_dir, grace, t3, tuple(jobs))
