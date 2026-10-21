"""API + front : colle un lien TikTok/Instagram, récupère une recette propre."""

from __future__ import annotations

import base64
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel



def _load_env(path: str) -> None:
    """Charge le fichier .env (KEY=valeur) sans écraser les variables déjà définies."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env(os.path.join(os.path.dirname(__file__), "..", ".env"))

from . import db  # noqa: E402
from .extractor import extract_recipe  # noqa: E402
from .media import fetch  # noqa: E402

STATIC = os.path.join(os.path.dirname(__file__), "..", "frontend")
THUMBS = os.environ.get("THUMBS_DIR", os.path.join(os.path.dirname(__file__), "..", "thumbs"))
os.makedirs(THUMBS, exist_ok=True)
URL_RE = re.compile(r"https?://\S+")

app = FastAPI(title="Recettes")
pool = ThreadPoolExecutor(max_workers=2)
db.init()


def save_thumbnail(rid: str, remote_url: str, fallback_frame: str | None = None) -> str:
    """Copie locale de la miniature : les URL des CDN TikTok/Instagram expirent vite,
    et une image servie par nous peut être gardée en cache sur le téléphone (hors ligne)."""
    data = None
    if remote_url:
        try:
            req = urllib.request.Request(remote_url, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=15).read()
        except Exception:  # noqa: BLE001
            data = None
    if not data and fallback_frame:
        data = base64.standard_b64decode(fallback_frame)
    if not data:
        return ""
    with open(os.path.join(THUMBS, f"{rid}.jpg"), "wb") as f:
        f.write(data)
    return f"/thumbs/{rid}.jpg"


def process(rid: str, url: str) -> None:
    try:
        media = fetch(url)
        recipe = extract_recipe(media)
        frame = media.frames[len(media.frames) // 2] if media.frames else None
        db.update(rid, status="done", data=recipe.model_dump(), author=media.author,
                  platform=media.platform, thumbnail=save_thumbnail(rid, media.thumbnail, frame),
                  url=media.url)
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


@app.get("/sw.js")
def service_worker():
    # Servi à la racine pour que le service worker contrôle toute l'appli
    return FileResponse(os.path.join(STATIC, "sw.js"), media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


def _backfill_thumbnails() -> None:
    for r in db.list_all():
        if (r["thumbnail"] or "").startswith("http"):
            db.update(r["id"], thumbnail=save_thumbnail(r["id"], r["thumbnail"]))


pool.submit(_backfill_thumbnails)
app.mount("/thumbs", StaticFiles(directory=THUMBS), name="thumbs")
app.mount("/", StaticFiles(directory=STATIC), name="static")
