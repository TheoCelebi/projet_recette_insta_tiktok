"""API + front : colle un lien TikTok/Instagram, récupère une recette propre."""

from __future__ import annotations

import base64
import os
import re
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


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

from . import db, nutrition  # noqa: E402
from .extractor import extract_recipe  # noqa: E402
from .media import fetch  # noqa: E402

STATIC = os.path.join(os.path.dirname(__file__), "..", "frontend")
THUMBS = os.environ.get("THUMBS_DIR", os.path.join(os.path.dirname(__file__), "..", "thumbs"))
os.makedirs(THUMBS, exist_ok=True)
URL_RE = re.compile(r"https?://\S+")

app = FastAPI(title="Recettes")
pool = ThreadPoolExecutor(max_workers=2)
db.init()


def _friendly(e: Exception) -> str:
    msg = str(e)
    if "authentication method" in msg or "invalid x-api-key" in msg:
        msg = "Clé API Anthropic manquante ou invalide : définis ANTHROPIC_API_KEY puis réessaie."
    return msg[:500]


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


# ---------- Traitements en arrière-plan ----------

def process(rid: str, url: str) -> None:
    """Import complet : vidéo → recette → valeurs nutritionnelles + rangement en dossiers."""
    try:
        media = fetch(url)
        recipe = extract_recipe(media)
        frame = media.frames[len(media.frames) // 2] if media.frames else None
        db.update(rid, status="done", error=None, data=recipe.model_dump(), author=media.author,
                  platform=media.platform, thumbnail=save_thumbnail(rid, media.thumbnail, frame),
                  url=media.url, nutrition_status="pending" if recipe.is_recipe else None)
    except Exception as e:  # noqa: BLE001 - on remonte l'erreur à l'utilisateur
        db.update(rid, status="error", error=_friendly(e))
        return
    if recipe.is_recipe:
        compute_nutrition(rid, assign_folders=True)


def compute_nutrition(rid: str, assign_folders: bool = False) -> None:
    """Valeurs nutritionnelles ; à l'import, range aussi la recette dans les dossiers adaptés.
    Lors d'un recalcul on ne touche pas aux dossiers, pour respecter les choix manuels."""
    r = db.get(rid)
    if not r or r["status"] != "done" or not (r["data"] or {}).get("is_recipe"):
        return
    db.update(rid, nutrition_status="pending", error=None)
    try:
        folders = db.list_folders() if assign_folders else []
        summary, folder_ids = nutrition.analyze(r["data"], folders)
        db.update(rid, nutrition=summary, nutrition_status="done")
        db.add_to_folders(rid, folder_ids)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        db.update(rid, nutrition_status="error", error=_friendly(e))


def sort_folder(fid: str) -> None:
    """Range automatiquement dans le dossier les recettes existantes qui y ont leur place."""
    folder = db.get_folder(fid)
    if not folder:
        return
    db.update_folder(fid, status="sorting", error=None)
    try:
        recipes = [r for r in db.list_all() if r["status"] == "done" and (r["data"] or {}).get("is_recipe")]
        db.add_recipes_to_folder(fid, nutrition.classify(folder, recipes))
        db.update_folder(fid, status="idle")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        db.update_folder(fid, status="idle", error=_friendly(e))


# ---------- Recettes ----------

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


def _recipe_or_404(rid: str) -> dict:
    if not (r := db.get(rid)):
        raise HTTPException(404, "Recette introuvable.")
    return r


@app.post("/api/recipes")
def import_recipe(req: ImportRequest) -> dict:
    return _import(req.url)


@app.get("/api/recipes")
def list_recipes() -> list[dict]:
    return db.list_all()


@app.get("/api/recipes/{rid}")
def get_recipe(rid: str) -> dict:
    return _recipe_or_404(rid)


@app.post("/api/recipes/{rid}/retry")
def retry(rid: str) -> dict:
    r = _recipe_or_404(rid)
    db.update(rid, status="pending", error=None)
    pool.submit(process, rid, r["url"])
    return db.get(rid)


@app.post("/api/recipes/{rid}/nutrition")
def recompute_nutrition(rid: str) -> dict:
    _recipe_or_404(rid)
    db.update(rid, nutrition_status="pending", error=None)
    pool.submit(compute_nutrition, rid)
    return db.get(rid)


class FolderIds(BaseModel):
    folder_ids: list[str]


@app.put("/api/recipes/{rid}/folders")
def set_recipe_folders(rid: str, req: FolderIds) -> dict:
    _recipe_or_404(rid)
    known = {f["id"] for f in db.list_folders()}
    db.set_recipe_folders(rid, [fid for fid in req.folder_ids if fid in known])
    return db.get(rid)


@app.delete("/api/recipes/{rid}")
def delete_recipe(rid: str) -> dict:
    db.delete(rid)
    return {"ok": True}


# ---------- Dossiers ----------

class FolderIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=300)
    auto: bool = True  # ranger tout de suite les recettes existantes qui correspondent


class FolderPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    description: str | None = Field(default=None, max_length=300)


def _folder_or_404(fid: str) -> dict:
    if not (f := db.get_folder(fid)):
        raise HTTPException(404, "Dossier introuvable.")
    return f


def _check_name(name: str, exclude: str | None = None) -> None:
    if any(f["name"].casefold() == name.strip().casefold() and f["id"] != exclude for f in db.list_folders()):
        raise HTTPException(409, "Un dossier porte déjà ce nom.")


@app.get("/api/folders")
def list_folders() -> list[dict]:
    return db.list_folders()


@app.post("/api/folders")
def create_folder(req: FolderIn) -> dict:
    _check_name(req.name)
    f = db.create_folder(req.name, req.description)
    if req.auto:
        db.update_folder(f["id"], status="sorting")
        pool.submit(sort_folder, f["id"])
    return db.get_folder(f["id"])


@app.patch("/api/folders/{fid}")
def update_folder(fid: str, req: FolderPatch) -> dict:
    _folder_or_404(fid)
    fields = {k: v.strip() for k, v in req.model_dump().items() if v is not None}
    if "name" in fields:
        _check_name(fields["name"], exclude=fid)
    if fields:
        db.update_folder(fid, **fields)
    return db.get_folder(fid)


@app.post("/api/folders/{fid}/sort")
def resort_folder(fid: str) -> dict:
    _folder_or_404(fid)
    db.update_folder(fid, status="sorting", error=None)
    pool.submit(sort_folder, fid)
    return db.get_folder(fid)


@app.delete("/api/folders/{fid}")
def delete_folder(fid: str) -> dict:
    db.delete_folder(fid)
    return {"ok": True}


# ---------- Partage, front ----------

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


# ---------- Au démarrage : reprise des traitements interrompus et rattrapage ----------

def _backfill_thumbnails() -> None:
    for r in db.list_all():
        if (r["thumbnail"] or "").startswith("http"):
            db.update(r["id"], thumbnail=save_thumbnail(r["id"], r["thumbnail"]))


def _resume_work() -> None:
    for f in db.list_folders():
        if f["status"] == "sorting":
            db.update_folder(f["id"], status="idle")
    for r in db.list_all():
        if r["status"] == "pending":
            pool.submit(process, r["id"], r["url"])
        elif (r["status"] == "done" and (r["data"] or {}).get("is_recipe")
              and r["nutrition_status"] != "error"
              and (r["nutrition"] is None or r["nutrition_status"] == "pending"
                   or r["nutrition"].get("version", 1) < nutrition.VERSION)):
            # Recettes sans valeurs nutritionnelles, calcul interrompu, ou méthode de calcul changée
            pool.submit(compute_nutrition, r["id"])


pool.submit(_backfill_thumbnails)
if os.environ.get("RESUME_WORK", "1") != "0":
    _resume_work()
app.mount("/thumbs", StaticFiles(directory=THUMBS), name="thumbs")
app.mount("/", StaticFiles(directory=STATIC), name="static")
