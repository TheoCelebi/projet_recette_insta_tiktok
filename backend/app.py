"""API + front : colle un lien TikTok/Instagram, récupère une recette propre."""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .extractor import extract_recipe
from .media import fetch

STATIC = os.path.join(os.path.dirname(__file__), "..", "frontend")
URL_RE = re.compile(r"https?://\S+")

app = FastAPI(title="Recettes")
pool = ThreadPoolExecutor(max_workers=2)
db.init()


def process(rid: str, url: str) -> None:
    try:
        media = fetch(url)
        recipe = extract_recipe(media)
        db.update(rid, status="done", data=recipe.model_dump(), author=media.author,
                  platform=media.platform, thumbnail=media.thumbnail, url=media.url)
    except Exception as e:  # noqa: BLE001 - on remonte l'erreur à l'utilisateur
        msg = str(e)
        if "authentication method" in msg or "invalid x-api-key" in msg:
            msg = "Clé API Anthropic manquante ou invalide : définis ANTHROPIC_API_KEY puis réessaie."
        db.update(rid, status="error", error=msg[:500])


class ImportRequest(BaseModel):
    url: str


def _import(text: str) -> dict:
    # Les partages contiennent souvent du texte autour du lien ("Regarde ça ! https://...")
    m = URL_RE.search(text)
    if not m:
        raise HTTPException(400, "Aucun lien trouvé.")
    url = m.group(0)
    if existing := db.find_by_url(url):
        return existing
    rec = db.create(url)
    pool.submit(process, rec["id"], url)
    return rec


@app.post("/api/recipes")
def import_recipe(req: ImportRequest) -> dict:
    return _import(req.url)


@app.get("/api/recipes")
def list_recipes() -> list[dict]:
    return db.list_all()


@app.get("/api/recipes/{rid}")
def get_recipe(rid: str) -> dict:
    if not (r := db.get(rid)):
        raise HTTPException(404)
    return r


@app.post("/api/recipes/{rid}/retry")
def retry(rid: str) -> dict:
    if not (r := db.get(rid)):
        raise HTTPException(404)
    db.update(rid, status="pending", error=None)
    pool.submit(process, rid, r["url"])
    return db.get(rid)


@app.delete("/api/recipes/{rid}")
def delete_recipe(rid: str) -> dict:
    db.delete(rid)
    return {"ok": True}


@app.get("/share")
def share(url: str = "", text: str = "", title: str = ""):
    """Cible de partage (PWA Android / raccourci iOS) : /share?url=... ou ?text=..."""
    rec = _import(" ".join([url, text, title]))
    return RedirectResponse(f"/#/r/{rec['id']}", status_code=303)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


app.mount("/", StaticFiles(directory=STATIC), name="static")
