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
            status TEXT NOT NULL,          -- queued (en file) | pending (en cours) | done | error
            error TEXT,
            author TEXT, platform TEXT, thumbnail TEXT,
            data TEXT,                     -- Recipe JSON
            created_at REAL NOT NULL
        )""")
        # Migration : colonnes ajoutées après la première version
        cols = {r["name"] for r in c.execute("PRAGMA table_info(recipes)")}
        if "nutrition" not in cols:
            c.execute("ALTER TABLE recipes ADD COLUMN nutrition TEXT")         # résumé JSON
        if "stage" not in cols:
            c.execute("ALTER TABLE recipes ADD COLUMN stage TEXT")             # étape de l'import en cours
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
        c.execute("""CREATE TABLE IF NOT EXISTS health_days (
            date TEXT PRIMARY KEY,             -- AAAA-MM-JJ
            data TEXT NOT NULL,                -- résumé du jour (JSON)
            raw TEXT NOT NULL,                 -- dernier envoi brut du Raccourci (pour vérifier)
            updated_at REAL NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS shopping_items (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            quantity REAL,                     -- quantité de base de la recette (ou saisie)
            unit TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT '',
            ratio REAL NOT NULL DEFAULT 1,     -- portions voulues / portions de la recette
            recipe_id TEXT, recipe_title TEXT, ingredient_index INTEGER,
            checked INTEGER NOT NULL DEFAULT 0,
            product_code TEXT,                 -- produit scanné en magasin pour cet article
            created_at REAL NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS ingredient_products (
            recipe_id TEXT NOT NULL,
            ingredient_index INTEGER NOT NULL,
            product_code TEXT NOT NULL,
            PRIMARY KEY (recipe_id, ingredient_index)
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


def _queue_positions(c: sqlite3.Connection) -> dict[str, int]:
    """Place de chaque import en attente (1 = le prochain à être traité)."""
    rows = c.execute("SELECT id FROM recipes WHERE status = 'queued' ORDER BY created_at").fetchall()
    return {r["id"]: i + 1 for i, r in enumerate(rows)}


def _row(r: sqlite3.Row, folders: dict[str, list[str]], positions: dict[str, int] | None = None) -> dict:
    d = dict(r)
    d["queue_pos"] = (positions or {}).get(d["id"])
    d["data"] = json.loads(d["data"]) if d["data"] else None
    d["nutrition"] = json.loads(d["nutrition"]) if d["nutrition"] else None
    d["folders"] = folders.get(d["id"], [])
    return d


def create(url: str) -> dict:
    rid = uuid.uuid4().hex[:12]
    with _conn() as c:
        c.execute("INSERT INTO recipes (id, url, status, created_at) VALUES (?, ?, 'queued', ?)",
                  (rid, url, time.time()))
    return get(rid)


def find_by_url(url: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM recipes WHERE url = ? AND status != 'error'", (url,)).fetchone()
        return _row(r, _folder_map(c), _queue_positions(c)) if r else None


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
        return _row(r, _folder_map(c), _queue_positions(c)) if r else None


def list_all() -> list[dict]:
    with _conn() as c:
        rows = c.execute("SELECT * FROM recipes ORDER BY created_at DESC").fetchall()
        folders, positions = _folder_map(c), _queue_positions(c)
    return [_row(r, folders, positions) for r in rows]


def next_queued() -> dict | None:
    """Prochain import à traiter (le plus ancien en file)."""
    with _conn() as c:
        r = c.execute("SELECT id FROM recipes WHERE status = 'queued' ORDER BY created_at LIMIT 1").fetchone()
    return get(r["id"]) if r else None


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


# ---------- Santé (envoyé par le Raccourci iOS) ----------

def save_health_day(day: str, data: dict, raw: dict) -> None:
    with _conn() as c:
        c.execute("""INSERT INTO health_days (date, data, raw, updated_at) VALUES (?, ?, ?, ?)
                     ON CONFLICT(date) DO UPDATE SET data = excluded.data, raw = excluded.raw,
                     updated_at = excluded.updated_at""",
                  (day, json.dumps(data, ensure_ascii=False), json.dumps(raw, ensure_ascii=False), time.time()))


def get_health_day(day: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM health_days WHERE date = ?", (day,)).fetchone()
    return {"date": r["date"], **json.loads(r["data"]), "raw": json.loads(r["raw"]), "updated_at": r["updated_at"]} if r else None


def latest_health_day() -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT date FROM health_days ORDER BY updated_at DESC LIMIT 1").fetchone()
    return get_health_day(r["date"]) if r else None


# ---------- Liste de courses ----------

def list_shopping() -> list[dict]:
    with _conn() as c:
        return [dict(r) | {"checked": bool(r["checked"])} for r in
                c.execute("SELECT * FROM shopping_items ORDER BY checked, created_at")]


def get_shopping_item(sid: str) -> dict | None:
    with _conn() as c:
        r = c.execute("SELECT * FROM shopping_items WHERE id = ?", (sid,)).fetchone()
    return dict(r) | {"checked": bool(r["checked"])} if r else None


def add_shopping_items(items: list[dict]) -> None:
    now = time.time()
    with _conn() as c:
        c.executemany("""INSERT INTO shopping_items (id, name, quantity, unit, note, ratio, recipe_id, recipe_title,
                         ingredient_index, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                      [(uuid.uuid4().hex[:10], i["name"], i.get("quantity"), i.get("unit") or "", i.get("note") or "",
                        i.get("ratio", 1), i.get("recipe_id"), i.get("recipe_title"), i.get("ingredient_index"),
                        now + k / 1000) for k, i in enumerate(items)])


def update_shopping_item(sid: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as c:
        c.execute(f"UPDATE shopping_items SET {cols} WHERE id = ?", (*fields.values(), sid))


def delete_shopping(sid: str | None = None, checked_only: bool = False, recipe_id: str | None = None) -> None:
    with _conn() as c:
        if sid:
            c.execute("DELETE FROM shopping_items WHERE id = ?", (sid,))
        elif recipe_id:  # articles pas encore achetés d'une recette (ajoutée à nouveau)
            c.execute("DELETE FROM shopping_items WHERE recipe_id = ? AND checked = 0", (recipe_id,))
        elif checked_only:
            c.execute("DELETE FROM shopping_items WHERE checked = 1")
        else:
            c.execute("DELETE FROM shopping_items")


# ---------- Produits reliés aux ingrédients des recettes ----------

def set_ingredient_product(rid: str, idx: int, code: str | None) -> None:
    with _conn() as c:
        if code:
            c.execute("""INSERT INTO ingredient_products (recipe_id, ingredient_index, product_code) VALUES (?, ?, ?)
                         ON CONFLICT(recipe_id, ingredient_index) DO UPDATE SET product_code = excluded.product_code""",
                      (rid, idx, code))
        else:
            c.execute("DELETE FROM ingredient_products WHERE recipe_id = ? AND ingredient_index = ?", (rid, idx))


def ingredient_products(rid: str) -> dict[int, dict]:
    """Produits reliés aux ingrédients d'une recette : {index de l'ingrédient: produit}."""
    with _conn() as c:
        rows = c.execute("SELECT ingredient_index, product_code FROM ingredient_products WHERE recipe_id = ?",
                         (rid,)).fetchall()
    out = {}
    for r in rows:
        if p := get_product(r["product_code"]):
            out[r["ingredient_index"]] = p
    return out
