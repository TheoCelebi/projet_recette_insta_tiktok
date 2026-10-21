"""Stockage SQLite minimaliste : une table, la recette en JSON."""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid

DB_PATH = os.environ.get("RECIPES_DB", os.path.join(os.path.dirname(__file__), "..", "recipes.db"))


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS recipes (
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            status TEXT NOT NULL,          -- pending | done | error
            error TEXT,
            author TEXT, platform TEXT, thumbnail TEXT,
            data TEXT,                     -- Recipe JSON
            created_at REAL NOT NULL
        )""")


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["data"] = json.loads(d["data"]) if d["data"] else None
    return d


def create(url: str) -> dict:
    rid = uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute("INSERT INTO recipes (id, url, status, created_at) VALUES (?, ?, 'pending', ?)",
                  (rid, url, time.time()))
    return get(rid)


def find_by_url(url: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM recipes WHERE url = ? AND status != 'error'", (url,)).fetchone()
    return _row(r) if r else None


def update(rid: str, **fields) -> None:
    if "data" in fields and fields["data"] is not None:
        fields["data"] = json.dumps(fields["data"], ensure_ascii=False)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE recipes SET {cols} WHERE id = ?", (*fields.values(), rid))


def get(rid: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM recipes WHERE id = ?", (rid,)).fetchone()
    return _row(r) if r else None


def list_all() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM recipes ORDER BY created_at DESC").fetchall()
    return [_row(r) for r in rows]


def delete(rid: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM recipes WHERE id = ?", (rid,))
