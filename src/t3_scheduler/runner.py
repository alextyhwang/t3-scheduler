from __future__ import annotations

import copy
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig, JobConfig
from .state import RunJournal
from .t3 import T3Client, T3Error


class RunnerError(RuntimeError):
    pass


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def scheduled_occurrence(job: JobConfig, now: datetime, grace_minutes: int) -> datetime | None:
    local_now = now.astimezone(job.timezone).replace(second=0, microsecond=0)
    lookback = 0 if job.misfire_policy == "skip" else grace_minutes
    for minutes_ago in range(lookback + 1):
        candidate = local_now - timedelta(minutes=minutes_ago)
        if job.cron.matches(candidate):
            return candidate
    return None


def read_prompt(job: JobConfig, scheduled_for: datetime) -> str:
    if job.prompt is not None:
        prompt = job.prompt
    else:
        assert job.prompt_file is not None
        try:
            prompt = job.prompt_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunnerError(f"cannot read prompt for {job.id}: {exc}") from exc
    return prompt.replace("{{job_id}}", job.id).replace(
        "{{scheduled_for}}", scheduled_for.isoformat()
    )


def _normalized_path(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))


def resolve_project(snapshot: dict[str, Any], workspace: Path) -> dict[str, Any]:
    wanted = _normalized_path(workspace)
    matches = [
        project
        for project in snapshot.get("projects", [])
        if _normalized_path(project.get("workspaceRoot", "")) == wanted
    ]
    if len(matches) != 1:
        available = ", ".join(
            str(project.get("workspaceRoot")) for project in snapshot.get("projects", [])
        )
        raise RunnerError(
            f"T3 project {workspace} was not found uniquely. Available project roots: {available}"
        )
    return matches[0]


def model_selection(job: JobConfig, project: dict[str, Any]) -> dict[str, Any]:
    default = project.get("defaultModelSelection")
    if default:
        selection = copy.deepcopy(default)
    else:
        selection = {}
    if job.instance_id:
        selection["instanceId"] = job.instance_id
    if job.model:
        selection["model"] = job.model
    if "instanceId" not in selection or "model" not in selection:
        raise RunnerError(
            f"job {job.id!r} needs instance_id/model because its T3 project has no default model"
        )
    options_by_id = {
        str(option.get("id")): dict(option) for option in selection.get("options", [])
    }
    if job.reasoning_effort:
        options_by_id["reasoningEffort"] = {
            "id": "reasoningEffort",
            "value": job.reasoning_effort,
        }
    if job.service_tier:
        options_by_id["serviceTier"] = {"id": "serviceTier", "value": job.service_tier}
    if options_by_id:
        selection["options"] = list(options_by_id.values())
    return selection


def build_command(
    job: JobConfig,
    project: dict[str, Any],
    scheduled_for: datetime,
    *,
    now: datetime | None = None,
) -> tuple[str, dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    created_at = _utc_text(now)
    thread_id = str(uuid.uuid4())
    selection = model_selection(job, project)
    command = {
        "type": "thread.turn.start",
        "commandId": str(uuid.uuid4()),
        "threadId": thread_id,
        "message": {
            "messageId": str(uuid.uuid4()),
            "role": "user",
            "text": read_prompt(job, scheduled_for),
            "attachments": [],
        },
        "modelSelection": selection,
        "titleSeed": job.title,
        "runtimeMode": job.runtime_mode,
        "interactionMode": job.interaction_mode,
        "bootstrap": {
            "createThread": {
                "projectId": project["id"],
                "title": job.title,
                "modelSelection": selection,
                "runtimeMode": job.runtime_mode,
                "interactionMode": job.interaction_mode,
                "branch": None,
                "worktreePath": None,
                "createdAt": created_at,
            }
        },
        "createdAt": created_at,
    }
    return thread_id, command


class SchedulerRunner:
    def __init__(self, config: AppConfig, client: T3Client | None = None):
        self.config = config
        self.client = client or T3Client(config.t3, config.state_dir)

    def _job(self, job_id: str) -> JobConfig:
        for job in self.config.jobs:
            if job.id == job_id:
                return job
        raise RunnerError(f"unknown job {job_id!r}")

    def preview(self, job: JobConfig, scheduled_for: datetime) -> dict[str, Any]:
        snapshot = self.client.shell_snapshot()
        project = resolve_project(snapshot, job.project)
        _, command = build_command(job, project, scheduled_for)
        return command

    def dispatch_one(
        self,
        job: JobConfig,
        scheduled_for: datetime,
        *,
        dry_run: bool,
        journal: RunJournal | None = None,
    ) -> str | None:
        key = scheduled_for.astimezone(timezone.utc).isoformat(timespec="minutes")
        try:
            snapshot = self.client.shell_snapshot()
            project = resolve_project(snapshot, job.project)
            thread_id, command = build_command(job, project, scheduled_for)
            if dry_run:
                print(json.dumps(command, indent=2))
                return thread_id
            if journal:
                claim = journal.claim(job.id, key, command)
                if not claim.acquired:
                    print(f"SKIP {job.id}: {key} is already {claim.prior_status}")
                    return None
                assert claim.command is not None
                command = claim.command
                thread_id = str(command["threadId"])
            result = self.client.dispatch(command)
            if journal:
                journal.succeeded(job.id, key, thread_id)
            print(f"SENT {job.id}: thread {thread_id}, sequence {result['sequence']}")
            return thread_id
        except Exception as exc:
            if journal and not dry_run:
                journal.failed(job.id, key, str(exc))
            raise

    def tick(self, *, now: datetime | None = None, dry_run: bool = False) -> int:
        now = now or datetime.now(timezone.utc)
        due = [
            (job, occurrence)
            for job in self.config.jobs
            if job.enabled
            if (occurrence := scheduled_occurrence(job, now, self.config.misfire_grace_minutes))
            is not None
        ]
        if not due:
            print("No jobs due.")
            return 0
        failures = 0
        with RunJournal(self.config.state_dir / "runs.sqlite3") as journal:
            for job, occurrence in due:
                try:
                    self.dispatch_one(job, occurrence, dry_run=dry_run, journal=journal)
                except Exception as exc:
                    failures += 1
                    print(f"ERROR {job.id}: {exc}")
        return 1 if failures else 0

    def run_manual(self, job_id: str, *, dry_run: bool = False) -> int:
        job = self._job(job_id)
        now = datetime.now(job.timezone).replace(microsecond=0)
        self.dispatch_one(job, now, dry_run=dry_run)
        return 0
