"""Atomic SQLite persistence; no provider credentials in this database."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from filelock import FileLock

from .config import defaults, validate_config
from .security import sanitize


class ConflictError(ValueError):
    pass


CLI_TERMINAL = {"completed", "failed", "cancelled", "timed_out", "interrupted"}


def _projects_overlap(first: str, second: str) -> bool:
    a, b = Path(first).resolve(), Path(second).resolve()
    if a == b or a.is_relative_to(b) or b.is_relative_to(a):
        return True
    # Default macOS filesystems can refer to the same directory with different
    # letter case; filesystem identity also catches aliases outside the CLI path.
    for candidate in (a, *a.parents):
        try:
            if candidate.samefile(b):
                return True
        except OSError:
            continue
    for candidate in (b, *b.parents):
        try:
            if candidate.samefile(a):
                return True
        except OSError:
            continue
    return False


class Store:
    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.db_path = self.state_dir / "pair.sqlite3"
        with FileLock(self.state_dir / "schema.lock"):
            with self.connect() as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, value TEXT NOT NULL);
                """)
                db.execute("INSERT OR IGNORE INTO settings VALUES(1,?)", (json.dumps(defaults()),))
        os.chmod(self.db_path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=15)
        db.execute("PRAGMA busy_timeout=15000")
        try:
            with db:
                yield db
        finally:
            db.close()

    def config(self):
        with self.connect() as db:
            return json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])

    def save_config(self, value):
        cfg = validate_config(value)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = json.loads(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
            if cfg["revision"] != old["revision"]:
                raise ConflictError("Settings changed elsewhere. Reload before saving.")
            cfg["revision"] += 1
            db.execute("UPDATE settings SET value=? WHERE id=1", (json.dumps(cfg),))
        return cfg

    def create_job(self, kind, request):
        now = time.time()
        job = {"id": str(uuid.uuid4()), "kind": kind, "status": "queued", "request": sanitize(request), "createdAt": now, "updatedAt": now, "result": None, "error": None, "pid": None, "costUsd": None, "reservedUsd": 0}
        if kind == "cli":
            # Reconciliation may release an unregistered reservation only once
            # the process responsible for launching its worker is gone.
            job["launcherPid"] = os.getpid()
        with self.connect() as db:
            if kind == "cli" and request.get("permission") != "read-only" and isinstance(request.get("project"), str):
                # Check and reserve the project in one transaction: simultaneous
                # callers must not start two native writers in overlapping trees.
                db.execute("BEGIN IMMEDIATE")
                for (value,) in db.execute("SELECT value FROM jobs"):
                    current = json.loads(value)
                    other = current.get("request", {})
                    if (current.get("kind") == "cli" and current.get("status") not in CLI_TERMINAL
                            and other.get("permission") != "read-only" and isinstance(other.get("project"), str)
                            and _projects_overlap(request["project"], other["project"])):
                        raise ConflictError(f"Another project writer is active (job {current['id']}); wait for its terminal result before starting a writer")
            db.execute("INSERT INTO jobs VALUES(?,?)", (job["id"], json.dumps(job)))
        return job

    def update_job(self, job_id, **fields):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError("Unknown job")
            value = json.loads(row[0])
            fields.pop("id", None)
            if (value.get("kind") == "cli" and value.get("status") in CLI_TERMINAL
                    and "status" in fields and fields["status"] != value["status"]):
                raise ConflictError("CLI job is already terminal")
            value.update(sanitize(fields))
            value["updatedAt"] = time.time()
            db.execute("UPDATE jobs SET value=? WHERE id=?", (json.dumps(value), job_id))
        return value

    def resolve_cli_without_pid(self, job_id, *, status, error):
        """Atomically close a queued reservation before any worker can run.

        A worker must persist running + its PID before invoking the native CLI.
        A later worker cannot reopen a terminal job through update_job.
        """
        if status not in {"failed", "interrupted"}:
            raise ValueError("Unsupported CLI recovery status")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise KeyError("Unknown job")
            value = json.loads(row[0])
            pid = value.get("pid")
            if (value.get("kind") != "cli" or value.get("status") != "queued"
                    or isinstance(pid, int) and not isinstance(pid, bool) and pid > 0):
                return value, False
            now = time.time()
            value.update(status=status, error=error, completedAt=now, updatedAt=now)
            if value.get("request", {}).get("mode") == "subscription":
                value.update(costUsd=0.0, reservedUsd=0.0)
            else:
                value.update(costUsd=None, reservedUsd=None)
            db.execute("UPDATE jobs SET value=? WHERE id=?", (json.dumps(value), job_id))
        return value, True

    def request_cli_cancel(self, job_id):
        """Cancel an unregistered worker immediately, or flag an active one."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ValueError("Unknown CLI job")
            value = json.loads(row[0])
            if value.get("kind") != "cli":
                raise ValueError("Unknown CLI job")
            if value.get("status") in CLI_TERMINAL:
                return value, None
            pid = value.get("pid")
            immediate = value.get("status") == "queued" and not (isinstance(pid, int) and not isinstance(pid, bool) and pid > 0)
            now = time.time()
            value.update(cancelRequested=True, updatedAt=now)
            if immediate:
                value.update(status="cancelled", error=None, completedAt=now)
                if value.get("request", {}).get("mode") == "subscription":
                    value.update(costUsd=0.0, reservedUsd=0.0)
                else:
                    value.update(costUsd=None, reservedUsd=None)
            db.execute("UPDATE jobs SET value=? WHERE id=?", (json.dumps(value), job_id))
        return value, "cancelled" if immediate else "cancel_requested"

    def job(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT value FROM jobs WHERE id=?", (job_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def history(self):
        with self.connect() as db:
            rows = db.execute("SELECT value FROM jobs").fetchall()
        return sorted((json.loads(r[0]) for r in rows), key=lambda j: j["createdAt"], reverse=True)

    def event(self, job_id, kind, data):
        value = sanitize({"kind": kind, "at": time.time(), "data": data})
        with self.connect() as db:
            db.execute("INSERT INTO events(job_id,value) VALUES(?,?)", (job_id, json.dumps(value)))

    def events(self, job_id):
        with self.connect() as db:
            rows = db.execute("SELECT value FROM events WHERE job_id=? ORDER BY seq", (job_id,)).fetchall()
        return [json.loads(r[0]) for r in rows]
