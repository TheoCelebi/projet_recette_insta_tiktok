"""Stockage SQLite : recettes (JSON), valeurs nutritionnelles et dossiers."""

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
        # Migration : colonnes ajoutées après la première version
        cols = {r["name"] for r in c.execute("PRAGMA table_info(recipes)")}
        if "nutrition" not in cols:
            c.execute("ALTER TABLE recipes ADD COLUMN nutrition TEXT")         # résumé JSON
        if "nutrition_status" not in cols:
            # pending | done | error (le message d'erreur va alors dans la colonne error)
            c.execute("ALTER TABLE recipes ADD COLUMN nutrition_status TEXT")
        c.execute("""CREATE TABLE IF NOT EXISTS folders (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'idle',   -- idle | sorting (rangement auto en cours)
            error TEXT,                            -- dernier échec du rangement auto
            created_at REAL NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS products (
            code TEXT PRIMARY KEY,             -- code-barres
            data TEXT NOT NULL,                -- produit normalisé (JSON)
            saved INTEGER NOT NULL DEFAULT 0,  -- dans « Mes produits »
            fetched_at REAL NOT NULL,
            saved_at REAL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS recipe_folders (
            recipe_id TEXT NOT NULL,
            folder_id TEXT NOT NULL,
            PRIMARY KEY (recipe_id, folder_id)
        )""")


# ---------- Recettes ----------

def _folder_map(c: sqlite3.Connection) -> dict[str, list[str]]:
    m: dict[str, list[str]] = {}
    for r in c.execute("SELECT recipe_id, folder_id FROM recipe_folders"):
        m.setdefault(r["recipe_id"], []).append(r["folder_id"])
    return m


def _row(r: sqlite3.Row, folders: dict[str, list[str]]) -> dict:
    d = dict(r)
    d["data"] = json.loads(d["data"]) if d["data"] else None
    d["nutrition"] = json.loads(d["nutrition"]) if d["nutrition"] else None
    d["folders"] = folders.get(d["id"], [])
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
        return _row(r, _folder_map(c)) if r else None


def update(rid: str, **fields) -> None:
    for key in ("data", "nutrition"):
        if fields.get(key) is not None:
            fields[key] = json.dumps(fields[key], ensure_ascii=False)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE recipes SET {cols} WHERE id = ?", (*fields.values(), rid))


def get(rid: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM recipes WHERE id = ?", (rid,)).fetchone()
        return _row(r, _folder_map(c)) if r else None


def list_all() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM recipes ORDER BY created_at DESC").fetchall()
        folders = _folder_map(c)
    return [_row(r, folders) for r in rows]


def delete(rid: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM recipes WHERE id = ?", (rid,))
        c.execute("DELETE FROM recipe_folders WHERE recipe_id = ?", (rid,))


# ---------- Dossiers ----------

def create_folder(name: str, description: str = "") -> dict:
    fid = uuid.uuid4().hex[:10]
    with _conn() as c:
        c.execute("INSERT INTO folders (id, name, description, created_at) VALUES (?, ?, ?, ?)",
                  (fid, name.strip(), description.strip(), time.time()))
    return get_folder(fid)


def get_folder(fid: str) -> dict | None:
    return next((f for f in list_folders() if f["id"] == fid), None)


def list_folders() -> list[dict]:
    """Dossiers avec leur nombre de recettes et jusqu'à 4 photos de couverture."""
    with _conn() as c:
        folders = [dict(r) for r in c.execute("SELECT * FROM folders ORDER BY created_at")]
        links = c.execute("""SELECT rf.folder_id, r.thumbnail FROM recipe_folders rf
                             JOIN recipes r ON r.id = rf.recipe_id
                             ORDER BY r.created_at DESC""").fetchall()
    for f in folders:
        mine = [link for link in links if link["folder_id"] == f["id"]]
        f["count"] = len(mine)
        f["covers"] = [link["thumbnail"] for link in mine if link["thumbnail"]][:4]
    return folders


def update_folder(fid: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE folders SET {cols} WHERE id = ?", (*fields.values(), fid))


def delete_folder(fid: str) -> None:
    """Supprime le dossier ; les recettes restent dans le carnet."""
    with _conn() as c:
        c.execute("DELETE FROM folders WHERE id = ?", (fid,))
        c.execute("DELETE FROM recipe_folders WHERE folder_id = ?", (fid,))


def add_to_folders(rid: str, folder_ids: list[str]) -> None:
    with _conn() as c:
        c.executemany("INSERT OR IGNORE INTO recipe_folders (recipe_id, folder_id) VALUES (?, ?)",
                      [(rid, fid) for fid in folder_ids])


def add_recipes_to_folder(fid: str, recipe_ids: list[str]) -> None:
    with _conn() as c:
        c.executemany("INSERT OR IGNORE INTO recipe_folders (recipe_id, folder_id) VALUES (?, ?)",
                      [(rid, fid) for rid in recipe_ids])


def set_recipe_folders(rid: str, folder_ids: list[str]) -> None:
    with _conn() as c:
        c.execute("DELETE FROM recipe_folders WHERE recipe_id = ?", (rid,))
        c.executemany("INSERT OR IGNORE INTO recipe_folders (recipe_id, folder_id) VALUES (?, ?)",
                      [(rid, fid) for fid in folder_ids])


# ---------- Produits (Open Food Facts) ----------

def _product(r: sqlite3.Row) -> dict:
    return {**json.loads(r["data"]), "saved": bool(r["saved"]), "fetched_at": r["fetched_at"]}


def get_product(code: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM products WHERE code = ?", (code,)).fetchone()
    return _product(r) if r else None


def upsert_product(data: dict) -> dict:
    with _conn() as c:
        c.execute("""INSERT INTO products (code, data, fetched_at) VALUES (?, ?, ?)
                     ON CONFLICT(code) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at""",
                  (data["code"], json.dumps(data, ensure_ascii=False), time.time()))
    return get_product(data["code"])


def set_product_saved(code: str, saved: bool) -> None:
    with _conn() as c:
        c.execute("UPDATE products SET saved = ?, saved_at = ? WHERE code = ?",
                  (int(saved), time.time() if saved else None, code))


def list_saved_products() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM products WHERE saved = 1 ORDER BY saved_at DESC").fetchall()
    return [_product(r) for r in rows]
