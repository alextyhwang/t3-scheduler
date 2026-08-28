from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Claim:
    acquired: bool
    prior_status: str | None = None
    command: dict[str, Any] | None = None


class RunJournal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.execute("PRAGMA journal_mode=WAL")
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
            row[1] for row in self.connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        if "command_json" not in columns:
            self.connection.execute("ALTER TABLE runs ADD COLUMN command_json TEXT")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "RunJournal":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def claim(
        self, job_id: str, scheduled_for: str, command: dict[str, Any]
    ) -> Claim:
        now = datetime.now(timezone.utc).isoformat()
        self.connection.execute("BEGIN IMMEDIATE")
        row = self.connection.execute(
            "SELECT status, started_at, command_json FROM runs WHERE job_id = ? AND scheduled_for = ?",
            (job_id, scheduled_for),
        ).fetchone()
        stored_command = json.loads(row[2]) if row and row[2] else command
        pending_is_fresh = False
        if row and row[0] == "pending":
            try:
                pending_is_fresh = datetime.fromisoformat(row[1]) > datetime.now(
                    timezone.utc
                ) - timedelta(minutes=5)
            except (TypeError, ValueError):
                pending_is_fresh = False
        if row and (row[0] == "succeeded" or pending_is_fresh):
            self.connection.commit()
            return Claim(False, row[0], stored_command)
        if row:
            self.connection.execute(
                """UPDATE runs SET status = 'pending', attempts = attempts + 1,
                   thread_id = ?, command_json = ?, error = NULL, started_at = ?, finished_at = NULL
                   WHERE job_id = ? AND scheduled_for = ?""",
                (
                    stored_command.get("threadId"),
                    json.dumps(stored_command, separators=(",", ":")),
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
                    json.dumps(command, separators=(",", ":")),
                    now,
                ),
            )
        self.connection.commit()
        return Claim(True, row[0] if row else None, stored_command)

    def succeeded(self, job_id: str, scheduled_for: str, thread_id: str) -> None:
        self.connection.execute(
            """UPDATE runs SET status = 'succeeded', thread_id = ?, finished_at = ?
               WHERE job_id = ? AND scheduled_for = ?""",
            (thread_id, datetime.now(timezone.utc).isoformat(), job_id, scheduled_for),
        )
        self.connection.commit()

    def failed(self, job_id: str, scheduled_for: str, error: str) -> None:
        self.connection.execute(
            """UPDATE runs SET status = 'failed', error = ?, finished_at = ?
               WHERE job_id = ? AND scheduled_for = ?""",
            (error[:4000], datetime.now(timezone.utc).isoformat(), job_id, scheduled_for),
        )
        self.connection.commit()
