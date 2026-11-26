"""SQLite store."""

from __future__ import annotations

import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces(id TEXT PRIMARY KEY, name TEXT NOT NULL, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS members(id TEXT PRIMARY KEY, workspace TEXT NOT NULL, handle TEXT NOT NULL UNIQUE, email TEXT UNIQUE, role TEXT NOT NULL, locked INTEGER NOT NULL DEFAULT 0, password TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS tokens(id TEXT PRIMARY KEY, hash TEXT NOT NULL UNIQUE, workspace TEXT NOT NULL, role TEXT NOT NULL, member TEXT NOT NULL, name TEXT, created TEXT NOT NULL, last_used TEXT);
CREATE TABLE IF NOT EXISTS keys(workspace TEXT NOT NULL, provider TEXT NOT NULL, ciphertext TEXT NOT NULL, last4 TEXT NOT NULL, PRIMARY KEY(workspace, provider));
CREATE TABLE IF NOT EXISTS tasks(id TEXT PRIMARY KEY, workspace TEXT NOT NULL, repo TEXT, prompt TEXT, agent TEXT, base TEXT, status TEXT NOT NULL, session TEXT, plan TEXT, answer TEXT, result TEXT, error TEXT, created TEXT, updated TEXT, owner TEXT NOT NULL, name TEXT NOT NULL, dir TEXT);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL, kind TEXT NOT NULL, data TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS learn_rows(workspace TEXT NOT NULL, member TEXT NOT NULL, rid TEXT NOT NULL, signal TEXT, row TEXT, at TEXT, UNIQUE(workspace, member, rid));
CREATE TABLE IF NOT EXISTS lessons(id TEXT PRIMARY KEY, workspace TEXT NOT NULL, text TEXT NOT NULL, proposed_by TEXT, status TEXT, approved_by TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS webhooks(id TEXT PRIMARY KEY, workspace TEXT NOT NULL, url TEXT NOT NULL, secret TEXT);
CREATE TABLE IF NOT EXISTS invites(code_hash TEXT PRIMARY KEY, workspace TEXT, member TEXT NOT NULL, expires REAL, used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS web_sessions(sid_hash TEXT PRIMARY KEY, member TEXT NOT NULL, expires REAL);
CREATE TABLE IF NOT EXISTS sign_in_misses(handle TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0, until REAL);
CREATE TABLE IF NOT EXISTS notices(id INTEGER PRIMARY KEY AUTOINCREMENT, member TEXT NOT NULL, task TEXT, by_member TEXT, text TEXT, seen INTEGER NOT NULL DEFAULT 0, at TEXT);
CREATE TABLE IF NOT EXISTS nudges(id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT NOT NULL, sender TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', at TEXT, delivered_at TEXT);
CREATE TABLE IF NOT EXISTS task_files(task_id TEXT PRIMARY KEY, state TEXT NOT NULL, branch TEXT, sha TEXT, pushed INTEGER, bundle TEXT, plans TEXT, edits_sha TEXT, edits_files TEXT, updated TEXT);
"""


class Store:
    def __init__(self, path: str):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, params).fetchall()]

    def exec(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            try:
                cur = self._db.execute(sql, params)
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            return cur.rowcount
