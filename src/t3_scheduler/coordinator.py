from __future__ import annotations

import copy
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .failover import (
    ProviderCandidate,
    ProviderUsage,
    UsageWindow,
    select_provider,
)
from .state import FailoverIncident, ProviderAttempt, RunJournal
from .t3 import T3Client, T3HttpError


QUOTA_MESSAGE_PREFIX = "Codex usage limit reached."
CONTINUATION_PROMPT = (
    "Continue the previous request from the last confirmed completed step. "
    "First reconcile any uncertain tool or external-write outcome; do not repeat "
    "an operation whose result may already have been committed."
)


@dataclass(frozen=True)
class CoordinatorAction:
    thread_id: str
    outcome: str
    incident_id: int | None = None
    target_instance_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CoordinatorScanResult:
    enabled: bool
    mode: str
    scanned_threads: int
    actions: tuple[CoordinatorAction, ...]


def _utc_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _path_key(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(str(value))))


def _canonical_quota_failure(thread: dict[str, Any]) -> bool:
    latest = thread.get("latestTurn")
    session = thread.get("session")
    return bool(
        isinstance(latest, dict)
        and latest.get("state") == "error"
        and latest.get("turnId")
        and isinstance(session, dict)
        and session.get("status") == "error"
        and not session.get("activeTurnId")
        and isinstance(session.get("lastError"), str)
        and session["lastError"].startswith(QUOTA_MESSAGE_PREFIX)
        and session.get("providerName") == "codex"
    )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def provider_candidates(snapshots: list[dict[str, Any]]) -> tuple[ProviderCandidate, ...]:
    candidates: list[ProviderCandidate] = []
    for snapshot in snapshots:
        instance_id = snapshot.get("instanceId")
        driver = snapshot.get("driver")
        continuation = snapshot.get("continuation")
        group = continuation.get("groupKey") if isinstance(continuation, dict) else None
        if not all(isinstance(value, str) and value for value in (instance_id, driver, group)):
            continue
        models: set[str] = set()
        for model in snapshot.get("models", []):
            if isinstance(model, str) and model:
                models.add(model)
                continue
            if not isinstance(model, dict):
                continue
            if isinstance(model.get("id"), str):
                models.add(model["id"])
            if isinstance(model.get("model"), str):
                models.add(model["model"])
            if isinstance(model.get("slug"), str):
                models.add(model["slug"])
            aliases = model.get("aliases", [])
            if isinstance(aliases, list):
                models.update(alias for alias in aliases if isinstance(alias, str))

        usage: ProviderUsage | None = None
        raw_usage = snapshot.get("usageLimits")
        checked_at = _parse_time(raw_usage.get("checkedAt")) if isinstance(raw_usage, dict) else None
        if (
            checked_at is not None
            and raw_usage.get("unavailable") is None
            and isinstance(raw_usage.get("windows"), list)
            and raw_usage["windows"]
        ):
            windows: list[UsageWindow] = []
            valid = True
            for raw_window in raw_usage["windows"]:
                if (
                    not isinstance(raw_window, dict)
                    or isinstance(raw_window.get("usedPercent"), bool)
                    or not isinstance(raw_window.get("usedPercent"), (int, float))
                ):
                    valid = False
                    break
                resets_at = _parse_time(raw_window.get("resetsAt"))
                windows.append(UsageWindow(float(raw_window["usedPercent"]), resets_at))
            if valid:
                usage = ProviderUsage(checked_at, tuple(windows))

        auth = snapshot.get("auth")
        candidates.append(
            ProviderCandidate(
                instance_id=instance_id,
                driver=driver,
                continuation_group=group,
                enabled=snapshot.get("enabled") is True,
                installed=snapshot.get("installed") is True,
                authenticated=isinstance(auth, dict) and auth.get("status") == "authenticated",
                available=(
                    snapshot.get("availability", "available") != "unavailable"
                    and snapshot.get("status") not in {"error", "disabled"}
                ),
                models=frozenset(models),
                usage=usage,
            )
        )
    return tuple(candidates)


def _unresolved_activity_reason(detail: dict[str, Any], turn_id: str) -> str | None:
    activities = [
        activity
        for activity in detail.get("activities", [])
        if isinstance(activity, dict)
    ]

    def unresolved(open_kind: str, closed_kind: str) -> bool:
        opened: set[str] = set()
        closed: set[str] = set()
        missing_id = False
        for activity in activities:
            kind = activity.get("kind")
            if kind not in {open_kind, closed_kind}:
                continue
            payload = activity.get("payload")
            request_id = payload.get("requestId") if isinstance(payload, dict) else None
            if not isinstance(request_id, str) or not request_id:
                missing_id = missing_id or kind == open_kind
            elif kind == open_kind:
                opened.add(request_id)
            else:
                closed.add(request_id)
        return missing_id or bool(opened - closed)

    if unresolved("approval.requested", "approval.resolved"):
        return "pending_approval"
    if unresolved("user-input.requested", "user-input.resolved"):
        return "pending_user_input"

    started: set[str] = set()
    terminal: set[str] = set()
    missing_tool_id = False
    for activity in activities:
        kind = activity.get("kind")
        if kind not in {"tool.started", "tool.completed", "tool.denied"}:
            continue
        payload = activity.get("payload")
        tool_call_id = payload.get("toolCallId") if isinstance(payload, dict) else None
        if not isinstance(tool_call_id, str) or not tool_call_id:
            missing_tool_id = missing_tool_id or kind == "tool.started"
        elif kind == "tool.started":
            started.add(tool_call_id)
        else:
            terminal.add(tool_call_id)
    return "unmatched_tool_call" if missing_tool_id or started - terminal else None


def _has_message(detail: dict[str, Any], message_id: str, turn_id: object) -> bool:
    messages = [message for message in detail.get("messages", []) if isinstance(message, dict)]
    for index, message in enumerate(messages):
        if message_id not in {message.get("id"), message.get("messageId")}:
            continue
        if any(item.get("role") == "user" for item in messages[index + 1 :]):
            return False
        if message.get("turnId") == turn_id:
            return True
        if message.get("turnId") is None:
            return True
    return False


def _pending_shell_reason(thread: dict[str, Any]) -> str | None:
    if thread.get("hasPendingApprovals") is True:
        return "pending_approval"
    if thread.get("hasPendingUserInput") is True:
        return "pending_user_input"
    return None


def _provider_start_failed(detail: dict[str, Any], message_id: object) -> bool:
    if not isinstance(message_id, str) or not message_id:
        return False
    return any(
        isinstance(activity, dict)
        and activity.get("kind") == "provider.turn.start.failed"
        and isinstance(activity.get("payload"), dict)
        and activity["payload"].get("requestId") == message_id
        for activity in detail.get("activities", [])
    )


def _definite_dispatch_rejection(error: Exception) -> bool:
    return isinstance(error, T3HttpError) and 400 <= error.status_code < 500


def _command_context(thread: dict[str, Any]) -> tuple[dict[str, Any], str, str] | None:
    selection = thread.get("modelSelection")
    runtime_mode = thread.get("runtimeMode")
    interaction_mode = thread.get("interactionMode")
    if (
        not isinstance(selection, dict)
        or not isinstance(selection.get("instanceId"), str)
        or not selection["instanceId"]
        or not isinstance(selection.get("model"), str)
        or not selection["model"]
        or not isinstance(runtime_mode, str)
        or not runtime_mode
        or not isinstance(interaction_mode, str)
        or not interaction_mode
    ):
        return None
    return selection, runtime_mode, interaction_mode


def _snapshot_thread(snapshot: object) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict):
        return None
    thread = snapshot.get("thread")
    return thread if isinstance(thread, dict) else None


class FailoverCoordinator:
    def __init__(
        self,
        config: AppConfig,
        client: T3Client | None = None,
        *,
        coordinator_id: str | None = None,
    ):
        self.config = config
        self.client = client or T3Client(config.t3, config.state_dir)
        self.coordinator_id = coordinator_id or f"coordinator-{uuid.uuid4()}"

    @staticmethod
    def _is_scheduler_thread(journal: RunJournal, thread_id: str) -> bool:
        return (
            journal.connection.execute(
                "SELECT 1 FROM runs WHERE thread_id = ? LIMIT 1", (thread_id,)
            ).fetchone()
            is not None
        )

    def _in_scope(
        self,
        journal: RunJournal,
        thread: dict[str, Any],
        projects: dict[str, dict[str, Any]],
    ) -> bool:
        thread_id = thread.get("id")
        if not isinstance(thread_id, str):
            return False
        if not self.config.failover.allow_interactive_threads and not self._is_scheduler_thread(
            journal, thread_id
        ):
            return False
        allowlist = self.config.failover.project_allowlist
        if not allowlist:
            return True
        project = projects.get(str(thread.get("projectId")))
        workspace = project.get("workspaceRoot") if isinstance(project, dict) else None
        return isinstance(workspace, str) and _path_key(workspace) in {
            _path_key(path) for path in allowlist
        }

    @staticmethod
    def _build_command(thread: dict[str, Any], target_instance_id: str) -> dict[str, Any]:
        context = _command_context(thread)
        if context is None:
            raise ValueError("thread lacks a complete continuation context")
        selection, runtime_mode, interaction_mode = copy.deepcopy(context)
        selection["instanceId"] = target_instance_id
        return {
            "type": "thread.turn.start",
            "commandId": str(uuid.uuid4()),
            "threadId": thread["id"],
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "text": CONTINUATION_PROMPT,
                "attachments": [],
            },
            "modelSelection": selection,
            "runtimeMode": runtime_mode,
            "interactionMode": interaction_mode,
            "createdAt": _utc_text(),
        }

    def _selection(
        self,
        journal: RunJournal,
        incident: FailoverIncident,
        thread: dict[str, Any],
        candidates: tuple[ProviderCandidate, ...],
    ):
        current_id = thread.get("session", {}).get(
            "providerInstanceId"
        ) or incident.failed_instance_id
        current = next((item for item in candidates if item.instance_id == current_id), None)
        if current is None:
            return None
        attempted = {attempt.provider_instance_id for attempt in journal.provider_attempts(incident.id)}
        model = thread.get("modelSelection", {}).get("model")
        return select_provider(
            self.config.failover,
            candidates,
            current_instance_id=current.instance_id,
            driver=current.driver,
            continuation_group=current.continuation_group,
            model=model if isinstance(model, str) else None,
            excluded_instance_ids=attempted,
        )

    def _manual_review(
        self,
        journal: RunJournal,
        incident: FailoverIncident,
        token: str,
        attempt: ProviderAttempt | None,
        reason: str,
    ) -> CoordinatorAction:
        if attempt and attempt.state in {"dispatching", "verifying"}:
            journal.transition_provider_attempt(attempt.id, token, "manual-review", error_code=reason)
        journal.transition_incident(incident.id, token, "manual-review")
        return CoordinatorAction(incident.thread_id, "manual-review", incident.id, reason=reason)

    def _shell_preflight(
        self,
        thread_id: str,
        expected_turn_id: str,
        expected_context: tuple[dict[str, Any], str, str] | None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        shell = self.client.shell_snapshot()
        current = next(
            (
                item
                for item in shell.get("threads", [])
                if isinstance(item, dict) and item.get("id") == thread_id
            ),
            None,
        )
        if current is None:
            return None, "thread_missing"
        pending = _pending_shell_reason(current)
        if pending:
            return None, pending
        latest = current.get("latestTurn")
        if (
            not isinstance(latest, dict)
            or latest.get("turnId") != expected_turn_id
            or not _canonical_quota_failure(current)
            or _command_context(current) != expected_context
        ):
            return None, "thread_changed"
        return current, None

    def _dispatch_detected(
        self,
        journal: RunJournal,
        incident: FailoverIncident,
        token: str,
        thread: dict[str, Any],
        candidates: tuple[ProviderCandidate, ...],
        *,
        expected_turn_id: str | None = None,
    ) -> CoordinatorAction:
        expected_turn_id = expected_turn_id or incident.failed_turn_id
        detail = _snapshot_thread(
            self.client.thread_snapshot(incident.thread_id, turn_limit=50)
        )
        if detail is None:
            return self._manual_review(
                journal, incident, token, None, "invalid_thread_snapshot"
            )
        if (
            detail.get("latestTurn", {}).get("turnId") != expected_turn_id
            or not _canonical_quota_failure(detail)
            or _command_context(detail) is None
            or _command_context(detail) != _command_context(thread)
        ):
            return self._manual_review(journal, incident, token, None, "thread_changed")
        unsafe = _unresolved_activity_reason(detail, expected_turn_id)
        if unsafe:
            return self._manual_review(journal, incident, token, None, unsafe)
        _, unsafe = self._shell_preflight(
            incident.thread_id, expected_turn_id, _command_context(detail)
        )
        if unsafe:
            return self._manual_review(journal, incident, token, None, unsafe)
        selected = self._selection(journal, incident, detail, candidates)
        if selected is None:
            journal.transition_incident(incident.id, token, "exhausted")
            return CoordinatorAction(incident.thread_id, "exhausted", incident.id, reason="no_provider")

        claimed = journal.claim_provider_attempt(
            incident.id,
            token,
            selected.instance_id,
            self._build_command(detail, selected.instance_id),
            source_failed_turn_id=expected_turn_id,
            source_failed_instance_id=detail.get("session", {}).get(
                "providerInstanceId"
            ),
        )
        attempt = claimed.attempt
        try:
            self.client.dispatch(attempt.command)
        except Exception as exc:
            if _definite_dispatch_rejection(exc):
                return self._manual_review(
                    journal,
                    incident,
                    token,
                    attempt,
                    f"dispatch_rejected_{exc.status_code}",
                )
            journal.release_incident_claim(incident.id, token)
            return CoordinatorAction(
                incident.thread_id,
                "dispatch-uncertain",
                incident.id,
                selected.instance_id,
                "dispatch_uncertain",
            )
        journal.transition_provider_attempt(attempt.id, token, "verifying")
        journal.transition_incident(incident.id, token, "verifying")
        journal.release_incident_claim(incident.id, token)
        return CoordinatorAction(
            incident.thread_id, "verifying", incident.id, selected.instance_id
        )

    def _reconcile(
        self,
        journal: RunJournal,
        incident: FailoverIncident,
        token: str,
        thread: dict[str, Any],
        candidates: tuple[ProviderCandidate, ...],
    ) -> CoordinatorAction:
        attempts = journal.provider_attempts(incident.id)
        attempt = attempts[-1] if attempts else None
        if attempt is None:
            return self._manual_review(journal, incident, token, None, "missing_attempt")
        detail = _snapshot_thread(
            self.client.thread_snapshot(incident.thread_id, turn_limit=50)
        )
        if detail is None:
            return self._manual_review(
                journal, incident, token, attempt, "invalid_thread_snapshot"
            )
        latest = detail.get("latestTurn") if isinstance(detail.get("latestTurn"), dict) else {}
        session = detail.get("session") if isinstance(detail.get("session"), dict) else {}
        latest_id = latest.get("turnId")
        target = attempt.provider_instance_id
        target_active = session.get("providerInstanceId") == target
        message_id = attempt.command.get("message", {}).get("messageId")
        dispatched_message_present = isinstance(message_id, str) and _has_message(
            detail, message_id, latest_id
        )

        if latest_id != attempt.source_failed_turn_id:
            if (
                target_active
                and dispatched_message_present
                and latest.get("state") == "completed"
            ):
                if attempt.state != "recovered":
                    journal.transition_provider_attempt(attempt.id, token, "recovered")
                journal.transition_incident(incident.id, token, "recovered")
                return CoordinatorAction(incident.thread_id, "recovered", incident.id, target)
            if (
                target_active
                and dispatched_message_present
                and latest.get("state") == "running"
            ):
                journal.release_incident_claim(incident.id, token)
                return CoordinatorAction(
                    incident.thread_id, "verifying", incident.id, target
                )
            if target_active and dispatched_message_present and _canonical_quota_failure(detail):
                journal.transition_provider_attempt(
                    attempt.id, token, "failed", error_code="quota_exhausted"
                )
                current = journal.transition_incident(incident.id, token, "detected")
                return self._dispatch_detected(
                    journal,
                    current,
                    token,
                    detail,
                    candidates,
                    expected_turn_id=str(latest_id),
                )
            return self._manual_review(
                journal, incident, token, attempt, "unexpected_thread_change"
            )
        if session.get("providerInstanceId") not in {
            attempt.source_failed_instance_id,
            target,
        }:
            return self._manual_review(journal, incident, token, attempt, "unexpected_provider")
        if (
            not session.get("activeTurnId")
            and _provider_start_failed(detail, message_id)
        ):
            return self._manual_review(
                journal,
                incident,
                token,
                attempt,
                "provider_turn_start_failed",
            )
        journal.release_incident_claim(incident.id, token)
        return CoordinatorAction(incident.thread_id, "verifying", incident.id, target)

    def _retry_dispatching(
        self,
        journal: RunJournal,
        incident: FailoverIncident,
        token: str,
        thread: dict[str, Any],
        candidates: tuple[ProviderCandidate, ...],
    ) -> CoordinatorAction:
        attempts = journal.provider_attempts(incident.id)
        attempt = attempts[-1] if attempts else None
        if attempt is None:
            return self._manual_review(journal, incident, token, None, "missing_attempt")
        if attempt.state == "verifying":
            current = journal.transition_incident(incident.id, token, "verifying")
            return self._reconcile(journal, current, token, thread, candidates)
        detail = _snapshot_thread(
            self.client.thread_snapshot(incident.thread_id, turn_limit=50)
        )
        if detail is None:
            return self._manual_review(
                journal, incident, token, attempt, "invalid_thread_snapshot"
            )
        source_turn_id = attempt.source_failed_turn_id
        source_instance_id = attempt.source_failed_instance_id
        if detail.get("latestTurn", {}).get("turnId") != source_turn_id:
            journal.transition_provider_attempt(attempt.id, token, "verifying")
            current = journal.transition_incident(incident.id, token, "verifying")
            return self._reconcile(journal, current, token, detail, candidates)
        if (
            not _canonical_quota_failure(detail)
            or detail.get("session", {}).get("providerInstanceId")
            != source_instance_id
        ):
            return self._manual_review(journal, incident, token, attempt, "thread_changed")
        unsafe = _unresolved_activity_reason(detail, source_turn_id)
        if unsafe:
            return self._manual_review(journal, incident, token, attempt, unsafe)
        _, unsafe = self._shell_preflight(
            incident.thread_id, source_turn_id, _command_context(detail)
        )
        if unsafe:
            return self._manual_review(journal, incident, token, attempt, unsafe)
        try:
            self.client.dispatch(attempt.command)
        except Exception as exc:
            if _definite_dispatch_rejection(exc):
                return self._manual_review(
                    journal,
                    incident,
                    token,
                    attempt,
                    f"dispatch_rejected_{exc.status_code}",
                )
            journal.release_incident_claim(incident.id, token)
            return CoordinatorAction(
                incident.thread_id,
                "dispatch-uncertain",
                incident.id,
                attempt.provider_instance_id,
                "dispatch_uncertain",
            )
        journal.transition_provider_attempt(attempt.id, token, "verifying")
        journal.transition_incident(incident.id, token, "verifying")
        journal.release_incident_claim(incident.id, token)
        return CoordinatorAction(
            incident.thread_id, "verifying", incident.id, attempt.provider_instance_id
        )

    def scan(self, *, dry_run: bool = False) -> CoordinatorScanResult:
        policy = self.config.failover
        if not policy.enabled:
            return CoordinatorScanResult(False, policy.mode, 0, ())
        shell = self.client.shell_snapshot()
        threads = {
            thread["id"]: thread
            for thread in shell.get("threads", [])
            if isinstance(thread, dict) and isinstance(thread.get("id"), str)
        }
        projects = {
            str(project.get("id")): project
            for project in shell.get("projects", [])
            if isinstance(project, dict)
        }
        actions: list[CoordinatorAction] = []
        with RunJournal(self.config.state_dir / "runs.sqlite3") as journal:
            active_by_thread = {
                thread_id: incident
                for thread_id in threads
                if (incident := journal.active_incident_for_thread(thread_id)) is not None
            }
            for thread in threads.values():
                if not self._in_scope(journal, thread, projects) or not _canonical_quota_failure(thread):
                    continue
                if thread["id"] not in active_by_thread:
                    latest = thread["latestTurn"]
                    session = thread["session"]
                    if dry_run:
                        active_by_thread[thread["id"]] = FailoverIncident(
                            id=-1,
                            thread_id=thread["id"],
                            failed_turn_id=latest["turnId"],
                            failed_instance_id=session.get("providerInstanceId"),
                            failure_code="quota_exhausted",
                            state="detected",
                            detected_at="",
                            updated_at="",
                            claim_owner=None,
                            claim_expires_at=None,
                        )
                    else:
                        active_by_thread[thread["id"]] = journal.detect_incident(
                            thread["id"],
                            latest["turnId"],
                            failed_instance_id=session.get("providerInstanceId"),
                            failure_code="quota_exhausted",
                        )

            actionable = {
                thread_id: incident
                for thread_id, incident in active_by_thread.items()
                if (thread := threads.get(thread_id)) is not None
                and self._in_scope(journal, thread, projects)
            }
            if not actionable:
                return CoordinatorScanResult(True, policy.mode, len(threads), ())
            snapshots = self.client.provider_snapshots(refresh=True)
            candidates = provider_candidates(snapshots)

            for incident in actionable.values():
                thread = threads.get(incident.thread_id)
                assert thread is not None
                if dry_run or policy.mode == "shadow":
                    if incident.state == "detected" and not _canonical_quota_failure(
                        thread
                    ):
                        if dry_run:
                            actions.append(
                                CoordinatorAction(
                                    incident.thread_id,
                                    "would-recover",
                                    incident.id,
                                    reason="failure_cleared",
                                )
                            )
                        else:
                            claim = journal.claim_incident(
                                incident.id, self.coordinator_id
                            )
                            if claim.acquired and claim.claim_token is not None:
                                journal.transition_incident(
                                    incident.id, claim.claim_token, "recovered"
                                )
                                actions.append(
                                    CoordinatorAction(
                                        incident.thread_id,
                                        "recovered",
                                        incident.id,
                                        reason="failure_cleared",
                                    )
                                )
                        continue
                    if incident.state in {"dispatching", "verifying"}:
                        attempts = journal.provider_attempts(incident.id)
                        attempt = attempts[-1] if attempts else None
                        actions.append(
                            CoordinatorAction(
                                incident.thread_id,
                                (
                                    "would-retry"
                                    if incident.state == "dispatching" and attempt
                                    else "would-verify"
                                    if incident.state == "verifying" and attempt
                                    else "would-review"
                                ),
                                incident.id,
                                attempt.provider_instance_id if attempt else None,
                                None if attempt else "missing_attempt",
                            )
                        )
                        continue
                    selected = self._selection(journal, incident, thread, candidates)
                    actions.append(
                        CoordinatorAction(
                            incident.thread_id,
                            "would-switch" if selected else "would-exhaust",
                            incident.id if incident.id > 0 else None,
                            selected.instance_id if selected else None,
                        )
                    )
                    continue
                claim = journal.claim_incident(incident.id, self.coordinator_id)
                if not claim.acquired or claim.claim_token is None:
                    continue
                current = claim.incident
                if current.state == "detected":
                    actions.append(
                        self._dispatch_detected(
                            journal, current, claim.claim_token, thread, candidates
                        )
                    )
                elif current.state == "dispatching":
                    actions.append(
                        self._retry_dispatching(
                            journal,
                            current,
                            claim.claim_token,
                            thread,
                            candidates,
                        )
                    )
                else:
                    actions.append(
                        self._reconcile(
                            journal, current, claim.claim_token, thread, candidates
                        )
                    )
        return CoordinatorScanResult(True, policy.mode, len(threads), tuple(actions))
