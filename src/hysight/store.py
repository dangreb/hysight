from __future__ import annotations

import contextlib
import dataclasses
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from .config import ConfigurationError, Rule


class WriterLock:
    """OS lock: one writer process, local filesystem only. Released on process exit."""
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            handle.close()
            raise RuntimeError("Another Hysight writer owns this state directory") from exc
        self.handle = handle

    def release(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class Store:
    APPLICATION_ID = 0x48595331  # HYS1; do not initialize an unrelated SQLite file.
    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            # Fail rather than chmod a pre-existing shared directory such as /tmp
            # or /srv/appdata. The operator must prepare a dedicated directory.
            if self.path.parent.stat().st_mode & 0o077:
                raise ConfigurationError("State directory must be dedicated and mode 0700")
        with self.connect() as con:
            version = con.execute("PRAGMA user_version").fetchone()[0]
            app_id = con.execute("PRAGMA application_id").fetchone()[0]
            existing_tables = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            if app_id != self.APPLICATION_ID and (app_id != 0 or existing_tables):
                raise ConfigurationError("Refusing to initialize a non-Hysight SQLite database")
            if version not in (0, 1):
                raise ConfigurationError("Unsupported Hysight state schema")
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(f"PRAGMA application_id={self.APPLICATION_ID}")
            con.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    rule_id TEXT PRIMARY KEY, rule_json TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    attempt_hash TEXT, published_hash TEXT, due REAL NOT NULL DEFAULT 0,
                    last_action TEXT, last_error TEXT, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL,
                    rule_id TEXT, kind TEXT NOT NULL, details TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                PRAGMA user_version=1;
            """)
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextlib.contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA synchronous=FULL")
            with con:
                yield con
        finally:
            con.close()

    @staticmethod
    def _event(con: sqlite3.Connection, rule_id: str | None, kind: str, details: dict) -> None:
        con.execute("INSERT INTO events(created,rule_id,kind,details) VALUES (?,?,?,?)",
                    (time.time(), rule_id, kind, json.dumps(details, sort_keys=True)))
        con.execute("DELETE FROM events WHERE id <= (SELECT COALESCE(MAX(id),0)-10000 FROM events)")

    def register(self, rules: list[Rule], schema: str = "public") -> None:
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            namespace = con.execute("SELECT value FROM settings WHERE key='schema'").fetchone()
            if namespace and namespace["value"] != schema:
                raise ConfigurationError("State database belongs to a different tenant schema")
            con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('schema',?)", (schema,))
            existing = {r["rule_id"]: dict(r) for r in con.execute("SELECT * FROM jobs")}
            for rule in rules:
                old = existing.get(rule.id)
                if old and Rule.parse(json.loads(old["rule_json"])).identity != rule.identity:
                    raise ConfigurationError(f"Rule {rule.id}: changing endpoints/model requires a new rule id; disable the old rule first")
            # Exclusive writer startup: all interrupted attempts can safely resume.
            con.execute("UPDATE jobs SET state='pending', due=0 WHERE state='running'")
            current_ids = {r.id for r in rules}
            for rule in rules:
                encoded = json.dumps(rule.as_dict(), sort_keys=True)
                old = existing.get(rule.id)
                if not old:
                    con.execute("INSERT INTO jobs(rule_id,rule_json,updated) VALUES (?,?,?)", (rule.id, encoded, time.time()))
                elif old["rule_json"] != encoded:
                    con.execute("UPDATE jobs SET rule_json=?,generation=generation+1,state='pending',attempts=0,attempt_hash=NULL,due=0,updated=? WHERE rule_id=?", (encoded, time.time(), rule.id))
            for rid, old in existing.items():
                if rid not in current_ids:
                    retired = dataclasses.replace(Rule.parse(json.loads(old["rule_json"])), enabled=False)
                    con.execute("UPDATE jobs SET rule_json=?,generation=generation+1,state='pending',attempts=0,attempt_hash=NULL,due=0,updated=? WHERE rule_id=?",
                                (json.dumps(retired.as_dict(), sort_keys=True), time.time(), rid))

    def schedule(self, rule_id: str, *, hook: bool = False, context: dict | None = None) -> None:
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT state FROM jobs WHERE rule_id=?", (rule_id,)).fetchone()
            if row is None:
                raise KeyError(rule_id)
            if hook:
                # Do not invalidate a claim or reset its attempts. A changed source is
                # recognized by hash in the worker, including after a terminal failure.
                con.execute("UPDATE jobs SET generation=generation+1,state=CASE WHEN state='running' THEN state ELSE 'pending' END,due=CASE WHEN state='retry' THEN due ELSE 0 END,updated=? WHERE rule_id=?", (time.time(), rule_id))
                self._event(con, rule_id, "refresh_hook", context or {})
            elif row["state"] not in ("running", "pending", "retry"):
                con.execute("UPDATE jobs SET state='pending',generation=generation+1,due=0,updated=? WHERE rule_id=?", (time.time(), rule_id))

    def claim(self) -> dict | None:
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM jobs WHERE state IN ('pending','retry') AND due<=? ORDER BY due,updated,rule_id LIMIT 1", (time.time(),)).fetchone()
            if row is None:
                return None
            con.execute("UPDATE jobs SET state='running',updated=? WHERE rule_id=?", (time.time(), row["rule_id"]))
            return dict(row)

    def attempt(self, job: dict, fingerprint: str) -> int:
        attempts = job["attempts"] if job["attempt_hash"] == fingerprint else 0
        with self.connect() as con:
            con.execute("UPDATE jobs SET attempt_hash=?,attempts=?,updated=? WHERE rule_id=?", (fingerprint, attempts, time.time(), job["rule_id"]))
        return attempts

    def finish(self, job: dict, action: str, fingerprint: str | None) -> None:
        with self.connect() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT generation FROM jobs WHERE rule_id=?", (job["rule_id"],)).fetchone()
            state = "done" if row["generation"] == job["generation"] else "pending"
            con.execute("UPDATE jobs SET state=?,attempts=0,attempt_hash=NULL,published_hash=?,due=0,last_action=?,last_error=NULL,updated=? WHERE rule_id=?",
                        (state, fingerprint, action, time.time(), job["rule_id"]))
            self._event(con, job["rule_id"], action, {"fingerprint": fingerprint})

    def fail(self, job: dict, error: str, attempts: int, max_attempts: int, backoff: float) -> None:
        with self.connect() as con:
            state = "failed" if attempts >= max_attempts else "retry"
            con.execute("UPDATE jobs SET state=?,attempts=?,due=?,last_error=?,updated=? WHERE rule_id=?",
                        (state, attempts, time.time() + backoff, error, time.time(), job["rule_id"]))
            self._event(con, job["rule_id"], state, {"error": error, "attempts": attempts})

    def blocked(self, job: dict, reason: str) -> None:
        with self.connect() as con:
            row = con.execute("SELECT generation FROM jobs WHERE rule_id=?", (job["rule_id"],)).fetchone()
            state = "waiting" if row["generation"] == job["generation"] else "pending"
            con.execute("UPDATE jobs SET state=?,last_action=?,updated=? WHERE rule_id=?", (state, reason, time.time(), job["rule_id"]))
            self._event(con, job["rule_id"], reason, {})

    def snapshot(self) -> dict[str, Any]:
        # Read-only and without initialize(): CLI must not mutate a live store.
        uri = self.path.resolve().as_uri() + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=15)) as con:
            con.row_factory = sqlite3.Row
            if con.execute("PRAGMA application_id").fetchone()[0] != self.APPLICATION_ID:
                raise ConfigurationError("Not a Hysight state database")
            jobs = [dict(r) for r in con.execute("SELECT * FROM jobs ORDER BY rule_id")]
            events = [dict(r) for r in con.execute("SELECT * FROM events ORDER BY id DESC LIMIT 50")]
            return {"schema_version": con.execute("PRAGMA user_version").fetchone()[0], "jobs": jobs, "events": events}

