from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
INCIDENT_STATES = frozenset(
    {"detected", "dispatching", "verifying", "recovered", "manual-review", "exhausted"}
)
ACTIVE_INCIDENT_STATES = frozenset({"detected", "dispatching", "verifying"})
TERMINAL_INCIDENT_STATES = INCIDENT_STATES - ACTIVE_INCIDENT_STATES
ATTEMPT_STATES = frozenset(
    {"dispatching", "verifying", "recovered", "failed", "manual-review"}
)
_INCIDENT_TRANSITIONS = {
    "detected": frozenset({"dispatching", "recovered", "manual-review", "exhausted"}),
    "dispatching": frozenset({"detected", "verifying", "manual-review", "exhausted"}),
    "verifying": frozenset({"detected", "recovered", "manual-review", "exhausted"}),
    "recovered": frozenset(),
    "manual-review": frozenset(),
    "exhausted": frozenset(),
}
_ATTEMPT_TRANSITIONS = {
    "dispatching": frozenset({"verifying", "recovered", "failed", "manual-review"}),
    "verifying": frozenset({"recovered", "failed", "manual-review"}),
    "recovered": frozenset(),
    "failed": frozenset(),
    "manual-review": frozenset(),
}
_SAFE_CODE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")


@dataclass(frozen=True)
class Claim:
    acquired: bool
    prior_status: str | None = None
    command: dict[str, Any] | None = None


@dataclass(frozen=True)
class FailoverIncident:
    id: int
    thread_id: str
    failed_turn_id: str
    failed_instance_id: str | None
    failure_code: str
    state: str
    detected_at: str
    updated_at: str
    claim_owner: str | None
    claim_expires_at: str | None


@dataclass(frozen=True)
class IncidentClaim:
    acquired: bool
    incident: FailoverIncident
    claim_token: str | None = None


@dataclass(frozen=True)
class ProviderAttempt:
    id: int
    incident_id: int
    provider_instance_id: str
    source_failed_turn_id: str
    source_failed_instance_id: str | None
    state: str
    command: dict[str, Any]
    error_code: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class AttemptClaim:
    acquired: bool
    attempt: ProviderAttempt


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _required_text(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _safe_code(value: str, name: str) -> str:
    if not _SAFE_CODE.fullmatch(value):
        raise ValueError(
            f"{name} must be a normalized code containing only lowercase letters, "
            "digits, dots, underscores, or hyphens"
        )
    return value


class RunJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"run journal schema version {version} is newer than supported version "
                f"{SCHEMA_VERSION}"
            )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    job_id TEXT NOT NULL,
                    scheduled_for TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    thread_id TEXT,
                    command_json TEXT,
                    error TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    PRIMARY KEY (job_id, scheduled_for)
                )
                """
            )
            columns = {
                row[1]
                for row in self.connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "command_json" not in columns:
                self.connection.execute("ALTER TABLE runs ADD COLUMN command_json TEXT")
            if version < 1:
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS failover_incidents (
                        incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        thread_id TEXT NOT NULL,
                        failed_turn_id TEXT NOT NULL,
                        failed_instance_id TEXT,
                        failure_code TEXT NOT NULL,
                        state TEXT NOT NULL DEFAULT 'detected'
                            CHECK (state IN (
                                'detected', 'dispatching', 'verifying', 'recovered',
                                'manual-review', 'exhausted'
                            )),
                        detected_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        claim_owner TEXT,
                        claim_token TEXT,
                        claim_expires_at TEXT,
                        UNIQUE (thread_id, failed_turn_id)
                    )
                    """
                )
                self.connection.execute(
                    """CREATE INDEX IF NOT EXISTS failover_incidents_claimable
                       ON failover_incidents (state, claim_expires_at, detected_at)"""
                )
                self.connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS failover_provider_attempts (
                        attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        incident_id INTEGER NOT NULL
                            REFERENCES failover_incidents(incident_id) ON DELETE CASCADE,
                        provider_instance_id TEXT NOT NULL,
                        source_failed_turn_id TEXT,
                        source_failed_instance_id TEXT,
                        state TEXT NOT NULL DEFAULT 'dispatching'
                            CHECK (state IN (
                                'dispatching', 'verifying', 'recovered', 'failed',
                                'manual-review'
                            )),
                        command_json TEXT NOT NULL,
                        error_code TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE (incident_id, provider_instance_id)
                    )
                    """
                )
                self.connection.execute("PRAGMA user_version = 1")
            if version < 2:
                attempt_columns = {
                    row[1]
                    for row in self.connection.execute(
                        "PRAGMA table_info(failover_provider_attempts)"
                    ).fetchall()
                }
                if "source_failed_turn_id" not in attempt_columns:
                    self.connection.execute(
                        "ALTER TABLE failover_provider_attempts "
                        "ADD COLUMN source_failed_turn_id TEXT"
                    )
                if "source_failed_instance_id" not in attempt_columns:
                    self.connection.execute(
                        "ALTER TABLE failover_provider_attempts "
                        "ADD COLUMN source_failed_instance_id TEXT"
                    )
                self.connection.execute(
                    """UPDATE failover_provider_attempts
                       SET source_failed_turn_id = (
                           SELECT failed_turn_id FROM failover_incidents
                           WHERE failover_incidents.incident_id =
                                 failover_provider_attempts.incident_id
                       )
                       WHERE source_failed_turn_id IS NULL"""
                )
                self.connection.execute(
                    """UPDATE failover_provider_attempts
                       SET source_failed_instance_id = (
                           SELECT failed_instance_id FROM failover_incidents
                           WHERE failover_incidents.incident_id =
                                 failover_provider_attempts.incident_id
                       )
                       WHERE source_failed_instance_id IS NULL"""
                )
                self.connection.execute("PRAGMA user_version = 2")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "RunJournal":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def claim(
        self, job_id: str, scheduled_for: str, command: dict[str, Any]
    ) -> Claim:
        now = _utc_now().isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        row = self.connection.execute(
            "SELECT status, started_at, command_json FROM runs WHERE job_id = ? AND scheduled_for = ?",
            (job_id, scheduled_for),
        ).fetchone()
        stored_command = json.loads(row[2]) if row and row[2] else command
        pending_is_fresh = False
        if row and row[0] == "pending":
            try:
                pending_is_fresh = datetime.fromisoformat(row[1]) > _utc_now() - timedelta(
                    minutes=5
                )
            except (TypeError, ValueError):
                pending_is_fresh = False
        if row and (row[0] == "succeeded" or pending_is_fresh):
            self.connection.commit()
            return Claim(False, row[0], stored_command)
        serialized_command = json.dumps(stored_command, separators=(",", ":"))
        if row:
            self.connection.execute(
                """UPDATE runs SET status = 'pending', attempts = attempts + 1,
                   thread_id = ?, command_json = ?, error = NULL, started_at = ?, finished_at = NULL
                   WHERE job_id = ? AND scheduled_for = ?""",
                (
                    stored_command.get("threadId"),
                    serialized_command,
                    now,
                    job_id,
                    scheduled_for,
                ),
            )
        else:
            self.connection.execute(
                """INSERT INTO runs
                   (job_id, scheduled_for, status, thread_id, command_json, started_at)
                   VALUES (?, ?, 'pending', ?, ?, ?)""",
                (
                    job_id,
                    scheduled_for,
                    command.get("threadId"),
                    serialized_command,
                    now,
                ),
            )
        self.connection.commit()
        return Claim(True, row[0] if row else None, stored_command)

    def succeeded(self, job_id: str, scheduled_for: str, thread_id: str) -> None:
        self.connection.execute(
            """UPDATE runs SET status = 'succeeded', thread_id = ?, finished_at = ?
               WHERE job_id = ? AND scheduled_for = ?""",
            (thread_id, _utc_now().isoformat(), job_id, scheduled_for),
        )
        self.connection.commit()

    def failed(self, job_id: str, scheduled_for: str, error: str) -> None:
        self.connection.execute(
            """UPDATE runs SET status = 'failed', error = ?, finished_at = ?
               WHERE job_id = ? AND scheduled_for = ?""",
            (error[:4000], _utc_now().isoformat(), job_id, scheduled_for),
        )
        self.connection.commit()

    @staticmethod
    def _incident(row: sqlite3.Row) -> FailoverIncident:
        return FailoverIncident(
            id=int(row["incident_id"]),
            thread_id=str(row["thread_id"]),
            failed_turn_id=str(row["failed_turn_id"]),
            failed_instance_id=row["failed_instance_id"],
            failure_code=str(row["failure_code"]),
            state=str(row["state"]),
            detected_at=str(row["detected_at"]),
            updated_at=str(row["updated_at"]),
            claim_owner=row["claim_owner"],
            claim_expires_at=row["claim_expires_at"],
        )

    @staticmethod
    def _attempt(row: sqlite3.Row) -> ProviderAttempt:
        command = json.loads(row["command_json"])
        if not isinstance(command, dict):
            raise RuntimeError("stored failover provider command is not a JSON object")
        return ProviderAttempt(
            id=int(row["attempt_id"]),
            incident_id=int(row["incident_id"]),
            provider_instance_id=str(row["provider_instance_id"]),
            source_failed_turn_id=str(row["source_failed_turn_id"]),
            source_failed_instance_id=row["source_failed_instance_id"],
            state=str(row["state"]),
            command=command,
            error_code=row["error_code"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def detect_incident(
        self,
        thread_id: str,
        failed_turn_id: str,
        *,
        failed_instance_id: str | None,
        failure_code: str,
    ) -> FailoverIncident:
        """Record normalized failure facts once; never persist the raw provider payload."""
        thread_id = _required_text(thread_id, "thread_id")
        failed_turn_id = _required_text(failed_turn_id, "failed_turn_id")
        if failed_instance_id is not None:
            failed_instance_id = _required_text(failed_instance_id, "failed_instance_id")
        failure_code = _safe_code(failure_code, "failure_code")
        now = _utc_now().isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                """INSERT INTO failover_incidents
                   (thread_id, failed_turn_id, failed_instance_id, failure_code,
                    state, detected_at, updated_at)
                   VALUES (?, ?, ?, ?, 'detected', ?, ?)
                   ON CONFLICT(thread_id, failed_turn_id) DO NOTHING""",
                (
                    thread_id,
                    failed_turn_id,
                    failed_instance_id,
                    failure_code,
                    now,
                    now,
                ),
            )
            row = self.connection.execute(
                """SELECT * FROM failover_incidents
                   WHERE thread_id = ? AND failed_turn_id = ?""",
                (thread_id, failed_turn_id),
            ).fetchone()
            assert row is not None
            self.connection.commit()
            return self._incident(row)
        except Exception:
            self.connection.rollback()
            raise

    def active_incident_for_thread(self, thread_id: str) -> FailoverIncident | None:
        """Return the oldest unresolved incident for a thread, if one exists."""
        thread_id = _required_text(thread_id, "thread_id")
        row = self.connection.execute(
            """SELECT * FROM failover_incidents
               WHERE thread_id = ? AND state IN ('detected', 'dispatching', 'verifying')
               ORDER BY detected_at, incident_id
               LIMIT 1""",
            (thread_id,),
        ).fetchone()
        return self._incident(row) if row is not None else None

    def scheduler_thread_ids(self) -> frozenset[str]:
        """Return every distinct non-null T3 thread ID recorded by scheduled runs."""
        rows = self.connection.execute(
            "SELECT DISTINCT thread_id FROM runs WHERE thread_id IS NOT NULL"
        ).fetchall()
        return frozenset(str(row[0]) for row in rows)

    def list_failover_incidents(self, *, limit: int = 50) -> tuple[FailoverIncident, ...]:
        """Return the newest incidents first for bounded status output."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        rows = self.connection.execute(
            """SELECT * FROM failover_incidents
               ORDER BY detected_at DESC, incident_id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return tuple(self._incident(row) for row in rows)

    def _claim_row(
        self,
        row: sqlite3.Row,
        coordinator_id: str,
        lease_seconds: int,
        now: datetime,
    ) -> IncidentClaim:
        incident = self._incident(row)
        if incident.state not in ACTIVE_INCIDENT_STATES:
            return IncidentClaim(False, incident)
        claim_is_live = False
        if row["claim_token"] and row["claim_expires_at"]:
            try:
                claim_is_live = datetime.fromisoformat(row["claim_expires_at"]) > now
            except (TypeError, ValueError):
                claim_is_live = False
        if claim_is_live:
            return IncidentClaim(False, incident)
        token = str(uuid.uuid4())
        expires_at = (now + timedelta(seconds=lease_seconds)).isoformat()
        self.connection.execute(
            """UPDATE failover_incidents
               SET claim_owner = ?, claim_token = ?, claim_expires_at = ?, updated_at = ?
               WHERE incident_id = ?""",
            (coordinator_id, token, expires_at, now.isoformat(), incident.id),
        )
        claimed = self.connection.execute(
            "SELECT * FROM failover_incidents WHERE incident_id = ?", (incident.id,)
        ).fetchone()
        assert claimed is not None
        return IncidentClaim(True, self._incident(claimed), token)

    def claim_incident(
        self, incident_id: int, coordinator_id: str, *, lease_seconds: int = 300
    ) -> IncidentClaim:
        """Atomically lease one active incident to one coordinator."""
        coordinator_id = _required_text(coordinator_id, "coordinator_id")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM failover_incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown failover incident {incident_id}")
            result = self._claim_row(row, coordinator_id, lease_seconds, now)
            self.connection.commit()
            return result
        except Exception:
            self.connection.rollback()
            raise

    def claim_next_incident(
        self, coordinator_id: str, *, lease_seconds: int = 300
    ) -> IncidentClaim | None:
        """Atomically select and lease the oldest active, unclaimed incident."""
        coordinator_id = _required_text(coordinator_id, "coordinator_id")
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            rows = self.connection.execute(
                """SELECT * FROM failover_incidents
                   WHERE state IN ('detected', 'dispatching', 'verifying')
                   ORDER BY detected_at, incident_id"""
            ).fetchall()
            for row in rows:
                result = self._claim_row(row, coordinator_id, lease_seconds, now)
                if result.acquired:
                    self.connection.commit()
                    return result
            self.connection.commit()
            return None
        except Exception:
            self.connection.rollback()
            raise

    def transition_incident(
        self, incident_id: int, claim_token: str, new_state: str
    ) -> FailoverIncident:
        if new_state not in INCIDENT_STATES:
            raise ValueError(f"invalid failover incident state {new_state!r}")
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                "SELECT * FROM failover_incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown failover incident {incident_id}")
            if row["claim_token"] != claim_token:
                raise RuntimeError("failover incident claim is not owned by this coordinator")
            try:
                claim_expired = datetime.fromisoformat(row["claim_expires_at"]) <= now
            except (TypeError, ValueError):
                claim_expired = True
            if claim_expired:
                raise RuntimeError("failover incident claim has expired")
            old_state = str(row["state"])
            if new_state not in _INCIDENT_TRANSITIONS[old_state]:
                raise ValueError(f"invalid failover incident transition {old_state!r} -> {new_state!r}")
            terminal = new_state in TERMINAL_INCIDENT_STATES
            self.connection.execute(
                """UPDATE failover_incidents
                   SET state = ?, updated_at = ?,
                       claim_owner = CASE WHEN ? THEN NULL ELSE claim_owner END,
                       claim_token = CASE WHEN ? THEN NULL ELSE claim_token END,
                       claim_expires_at = CASE WHEN ? THEN NULL ELSE claim_expires_at END
                   WHERE incident_id = ?""",
                (new_state, now.isoformat(), terminal, terminal, terminal, incident_id),
            )
            updated = self.connection.execute(
                "SELECT * FROM failover_incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            assert updated is not None
            self.connection.commit()
            return self._incident(updated)
        except Exception:
            self.connection.rollback()
            raise

    def release_incident_claim(self, incident_id: int, claim_token: str) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = self.connection.execute(
                """UPDATE failover_incidents
                   SET claim_owner = NULL, claim_token = NULL, claim_expires_at = NULL,
                       updated_at = ?
                   WHERE incident_id = ? AND claim_token = ?""",
                (_utc_now().isoformat(), incident_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("failover incident claim is not owned by this coordinator")
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def claim_provider_attempt(
        self,
        incident_id: int,
        claim_token: str,
        provider_instance_id: str,
        command: dict[str, Any],
        *,
        source_failed_turn_id: str | None = None,
        source_failed_instance_id: str | None = None,
    ) -> AttemptClaim:
        """Store one immutable complete command per provider for uncertain retries."""
        provider_instance_id = _required_text(provider_instance_id, "provider_instance_id")
        if not isinstance(command, dict):
            raise ValueError("command must be a JSON object")
        try:
            serialized_command = json.dumps(command, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"command is not JSON serializable: {exc}") from exc
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            incident = self.connection.execute(
                "SELECT * FROM failover_incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if incident is None:
                raise KeyError(f"unknown failover incident {incident_id}")
            if incident["claim_token"] != claim_token:
                raise RuntimeError("failover incident claim is not owned by this coordinator")
            try:
                claim_expired = datetime.fromisoformat(incident["claim_expires_at"]) <= now
            except (TypeError, ValueError):
                claim_expired = True
            if claim_expired:
                raise RuntimeError("failover incident claim has expired")
            if incident["state"] not in {"detected", "dispatching"}:
                raise RuntimeError(
                    "provider attempts can only be claimed while the incident is detected "
                    "or dispatching"
                )
            if command.get("threadId") != incident["thread_id"]:
                raise ValueError("provider attempt command must target the incident thread")
            source_failed_turn_id = _required_text(
                source_failed_turn_id or str(incident["failed_turn_id"]),
                "source_failed_turn_id",
            )
            if source_failed_instance_id is None:
                source_failed_instance_id = incident["failed_instance_id"]
            elif source_failed_instance_id is not None:
                source_failed_instance_id = _required_text(
                    source_failed_instance_id, "source_failed_instance_id"
                )
            cursor = self.connection.execute(
                """INSERT INTO failover_provider_attempts
                   (incident_id, provider_instance_id, source_failed_turn_id,
                    source_failed_instance_id, state, command_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'dispatching', ?, ?, ?)
                   ON CONFLICT(incident_id, provider_instance_id) DO NOTHING""",
                (
                    incident_id,
                    provider_instance_id,
                    source_failed_turn_id,
                    source_failed_instance_id,
                    serialized_command,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            row = self.connection.execute(
                """SELECT * FROM failover_provider_attempts
                   WHERE incident_id = ? AND provider_instance_id = ?""",
                (incident_id, provider_instance_id),
            ).fetchone()
            assert row is not None
            if incident["state"] == "detected":
                self.connection.execute(
                    """UPDATE failover_incidents
                       SET state = 'dispatching', updated_at = ?
                       WHERE incident_id = ?""",
                    (now.isoformat(), incident_id),
                )
            self.connection.commit()
            return AttemptClaim(cursor.rowcount == 1, self._attempt(row))
        except Exception:
            self.connection.rollback()
            raise

    def provider_attempts(self, incident_id: int) -> tuple[ProviderAttempt, ...]:
        """Return persisted attempts in dispatch order for routing and recovery."""
        rows = self.connection.execute(
            """SELECT * FROM failover_provider_attempts
               WHERE incident_id = ? ORDER BY attempt_id""",
            (incident_id,),
        ).fetchall()
        return tuple(self._attempt(row) for row in rows)

    def transition_provider_attempt(
        self,
        attempt_id: int,
        claim_token: str,
        new_state: str,
        *,
        error_code: str | None = None,
    ) -> ProviderAttempt:
        if new_state not in ATTEMPT_STATES:
            raise ValueError(f"invalid failover provider attempt state {new_state!r}")
        if error_code is not None:
            error_code = _safe_code(error_code, "error_code")
        now = _utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            row = self.connection.execute(
                """SELECT a.*, i.claim_token, i.claim_expires_at
                   FROM failover_provider_attempts AS a
                   JOIN failover_incidents AS i ON i.incident_id = a.incident_id
                   WHERE a.attempt_id = ?""",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown failover provider attempt {attempt_id}")
            if row["claim_token"] != claim_token:
                raise RuntimeError("failover incident claim is not owned by this coordinator")
            try:
                claim_expired = datetime.fromisoformat(row["claim_expires_at"]) <= now
            except (TypeError, ValueError):
                claim_expired = True
            if claim_expired:
                raise RuntimeError("failover incident claim has expired")
            old_state = str(row["state"])
            if new_state not in _ATTEMPT_TRANSITIONS[old_state]:
                raise ValueError(
                    f"invalid failover provider attempt transition {old_state!r} -> {new_state!r}"
                )
            self.connection.execute(
                """UPDATE failover_provider_attempts
                   SET state = ?, error_code = ?, updated_at = ?
                   WHERE attempt_id = ?""",
                (new_state, error_code, now.isoformat(), attempt_id),
            )
            updated = self.connection.execute(
                "SELECT * FROM failover_provider_attempts WHERE attempt_id = ?", (attempt_id,)
            ).fetchone()
            assert updated is not None
            self.connection.commit()
            return self._attempt(updated)
        except Exception:
            self.connection.rollback()
            raise
