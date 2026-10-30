"""API + front : colle un lien TikTok/Instagram, récupère une recette propre."""

from __future__ import annotations

import base64
import os
import datetime as dt
import json
import re
import threading
import time
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Request
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

from . import ask, db, health, nutrition, prices, products  # noqa: E402
from .extractor import Recipe, extract_recipe  # noqa: E402
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
    """Import complet : vidéo → recette, puis valeurs nutritionnelles et dossiers en parallèle."""
    try:
        db.update(rid, status="pending", stage="video", error=None)
        media = fetch(url)
        if not db.get(rid):
            return  # import annulé pendant le téléchargement
        db.update(rid, stage="writing")
        recipe = extract_recipe(media)
        frame = media.frames[len(media.frames) // 2] if media.frames else None
        db.update(rid, status="done", stage=None, error=None, data=recipe.model_dump(), author=media.author,
                  platform=media.platform, thumbnail=save_thumbnail(rid, media.thumbnail, frame),
                  url=media.url, nutrition_status="pending" if recipe.is_recipe else None)
    except Exception as e:  # noqa: BLE001 - on remonte l'erreur à l'utilisateur
        db.update(rid, status="error", stage=None, error=_friendly(e))
        return
    if recipe.is_recipe:
        # La nutrition part dans le pool : la file passe tout de suite à la vidéo suivante
        pool.submit(compute_nutrition, rid, True)


# File d'import : les vidéos sont traitées une par une, dans l'ordre d'ajout
import_wakeup = threading.Event()


def import_worker() -> None:
    while True:
        job = db.next_queued()
        if job is None:
            import_wakeup.wait(timeout=30)
            import_wakeup.clear()
            continue
        try:
            process(job["id"], job["url"])
        except Exception:  # noqa: BLE001 - la file ne doit jamais s'arrêter
            traceback.print_exc()


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
        if links := db.ingredient_products(rid):  # produits scannés : valeurs de leur étiquette
            summary = nutrition.apply_products(summary, r["data"], links)
        db.update(rid, nutrition=summary, nutrition_status="done")
        _fill_servings(rid)
        db.add_to_folders(rid, folder_ids)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        db.update(rid, nutrition_status="error", error=_friendly(e))


def _fill_servings(rid: str) -> None:
    """Recette sans nombre de portions (non dit dans la vidéo) : on reprend celui estimé par le
    calcul nutritionnel, pour que ingrédients, nutrition et liste de courses parlent du même nombre."""
    r = db.get(rid)
    d, n = (r or {}).get("data") or {}, (r or {}).get("nutrition") or {}
    if d and not d.get("servings") and n.get("servings"):
        db.update(rid, data={**d, "servings": n["servings"], "servings_estimated": True})


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
    import_wakeup.set()
    return db.get(rec["id"])


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
    _recipe_or_404(rid)
    db.update(rid, status="queued", stage=None, error=None, created_at=time.time())  # en fin de file
    import_wakeup.set()
    return db.get(rid)


@app.post("/api/recipes/{rid}/nutrition")
def recompute_nutrition(rid: str) -> dict:
    _recipe_or_404(rid)
    db.update(rid, nutrition_status="pending", error=None)
    pool.submit(compute_nutrition, rid)
    return db.get(rid)


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=300)
    servings: float | None = Field(default=None, gt=0, le=100)


@app.post("/api/recipes/{rid}/ask")
def ask_nutrition(rid: str, req: Question) -> dict:
    """Explication des valeurs nutritionnelles (Claude Haiku : rapide et peu coûteux)."""
    r = _recipe_or_404(rid)
    if r["nutrition_status"] != "done" or not r["nutrition"]:
        raise HTTPException(409, "Les valeurs nutritionnelles de cette recette ne sont pas encore calculées.")
    try:
        return {"answer": ask.answer(r, req.question.strip(), req.servings)}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        raise HTTPException(500, _friendly(e))


class FolderIds(BaseModel):
    folder_ids: list[str]


@app.put("/api/recipes/{rid}/folders")
def set_recipe_folders(rid: str, req: FolderIds) -> dict:
    _recipe_or_404(rid)
    known = {f["id"] for f in db.list_folders()}
    db.set_recipe_folders(rid, [fid for fid in req.folder_ids if fid in known])
    return db.get(rid)


class RecipeEdit(BaseModel):
    data: Recipe


@app.put("/api/recipes/{rid}")
def edit_recipe(rid: str, req: RecipeEdit) -> dict:
    """Modification manuelle de la recette ; nutrition recalculée si ingrédients ou portions changent."""
    r = _recipe_or_404(rid)
    if r["status"] != "done":
        raise HTTPException(409, "La recette est encore en cours d'import.")
    new = req.data.model_dump()
    old = r["data"] or {}
    db.update(rid, data=new)
    if new.get("is_recipe") and (new["ingredients"] != old.get("ingredients") or new["servings"] != old.get("servings")):
        db.update(rid, nutrition_status="pending", error=None)
        pool.submit(compute_nutrition, rid)
    return db.get(rid)


MAX_IMAGE = 8 * 1024 * 1024


@app.post("/api/recipes/{rid}/image")
async def upload_image(rid: str, request: Request) -> dict:
    """Nouvelle photo (corps brut, JPEG/PNG/WebP, déjà réduite par le téléphone)."""
    _recipe_or_404(rid)
    kind = (request.headers.get("content-type") or "").split(";")[0].strip()
    if kind not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(415, "Photo attendue (JPEG, PNG ou WebP).")
    body = await request.body()
    if not body or len(body) > MAX_IMAGE:
        raise HTTPException(413, "Photo vide ou trop lourde (8 Mo maximum).")
    ext = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}[kind]
    for old in ("jpg", "png", "webp"):
        if old != ext and os.path.exists(os.path.join(THUMBS, f"{rid}.{old}")):
            os.remove(os.path.join(THUMBS, f"{rid}.{old}"))
    with open(os.path.join(THUMBS, f"{rid}.{ext}"), "wb") as f:
        f.write(body)
    # ?v= : nouvelle adresse à chaque photo, sinon le téléphone garderait l'ancienne en cache
    db.update(rid, thumbnail=f"/thumbs/{rid}.{ext}?v={int(time.time())}")
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


# ---------- Produits (code-barres → Open Food Facts) ----------

PRODUCT_TTL = 30 * 24 * 3600  # les fiches Open Food Facts sont réinterrogées au bout de 30 jours
BARCODE_RE = re.compile(r"^\d{6,14}$")


@app.get("/api/products")
def saved_products() -> list[dict]:
    return db.list_saved_products()


@app.get("/api/products/{code}")
def get_product(code: str) -> dict:
    if not BARCODE_RE.match(code):
        raise HTTPException(400, "Code-barres invalide : il doit contenir de 6 à 14 chiffres.")
    cached = db.get_product(code)
    if cached and time.time() - cached["fetched_at"] < PRODUCT_TTL:
        return cached
    try:
        return db.upsert_product(products.fetch(code))
    except products.NotFound:
        raise HTTPException(404, "Produit inconnu d'Open Food Facts.") from None
    except Exception:  # noqa: BLE001 - Open Food Facts injoignable : on rend la fiche en cache si on l'a
        if cached:
            return cached
        raise HTTPException(503, "Open Food Facts ne répond pas, réessaie dans un instant.") from None


class SavedIn(BaseModel):
    saved: bool


@app.put("/api/products/{code}/saved")
def save_product(code: str, req: SavedIn) -> dict:
    if not db.get_product(code):
        raise HTTPException(404, "Produit introuvable.")
    db.set_product_saved(code, req.saved)
    return db.get_product(code)


# ---------- Liste de courses ----------

class FromRecipe(BaseModel):
    recipe_id: str
    servings: int | None = Field(default=None, ge=1, le=100)


class ShoppingIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    id: str | None = Field(default=None, pattern=r"^[0-9a-f]{10}$")  # choisi par l'appli quand l'ajout est fait hors ligne


class ShoppingPatch(BaseModel):
    checked: bool


class LinkProduct(BaseModel):
    code: str


@app.get("/api/shopping")
def shopping() -> list[dict]:
    return db.list_shopping()


@app.post("/api/shopping/from-recipe")
def shopping_from_recipe(req: FromRecipe) -> dict:
    """Ajoute les ingrédients d'une recette, pour le nombre de portions voulu. Si la recette est
    déjà dans la liste, ses articles pas encore achetés sont remplacés (pas de doublons)."""
    r = _recipe_or_404(req.recipe_id)
    d = r["data"] or {}
    if r["status"] != "done" or not d.get("ingredients"):
        raise HTTPException(409, "Cette recette n'a pas d'ingrédients à ajouter.")
    base = d.get("servings") or 1
    ratio = (req.servings or base) / base
    db.delete_shopping(recipe_id=r["id"])
    items = [{"name": i["name"], "quantity": i.get("quantity"), "unit": i.get("unit", ""), "note": i.get("note", ""),
              "ratio": ratio, "recipe_id": r["id"], "recipe_title": d["title"], "ingredient_index": k}
             for k, i in enumerate(d["ingredients"]) if i.get("name")]
    db.add_shopping_items(items)
    return {"added": len(items), "items": db.list_shopping()}


@app.post("/api/shopping")
def shopping_add(req: ShoppingIn) -> list[dict]:
    db.add_shopping_items([{"name": req.name.strip(), "id": req.id}])
    return db.list_shopping()


@app.patch("/api/shopping/{sid}")
def shopping_check(sid: str, req: ShoppingPatch) -> list[dict]:
    if not db.get_shopping_item(sid):
        raise HTTPException(404, "Article introuvable.")
    db.update_shopping_item(sid, checked=int(req.checked))
    return db.list_shopping()


@app.delete("/api/shopping/{sid}")
def shopping_delete(sid: str) -> list[dict]:
    db.delete_shopping(sid)
    return db.list_shopping()


@app.delete("/api/shopping")
def shopping_clear(checked: bool = True) -> list[dict]:
    db.delete_shopping(checked_only=checked)
    return db.list_shopping()


class PriceLines(BaseModel):
    lines: list[str] = Field(max_length=200)


price_lock = threading.Lock()


@app.post("/api/shopping/prices")
def shopping_prices(req: PriceLines) -> dict:
    """Prix estimé de chaque article affiché ; seuls les articles jamais vus passent par Claude."""
    lines = list(dict.fromkeys(line.strip() for line in req.lines if line.strip()))
    with price_lock:  # deux écrans ouverts : un seul calcul à la fois, le second lit le cache
        known = db.get_prices(lines)
        if missing := [line for line in lines if line not in known]:
            try:
                new = prices.estimate(missing)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                raise HTTPException(502, _friendly(e))
            db.save_prices(new)
            known |= new
    return known


@app.post("/api/shopping/{sid}/product")
def shopping_link_product(sid: str, req: LinkProduct) -> dict:
    """Produit scanné en magasin pour cet article : l'article est coché et, s'il vient d'une
    recette, ses valeurs nutritionnelles sont recalculées avec l'étiquette du produit."""
    item = db.get_shopping_item(sid)
    product = db.get_product(req.code)
    if not item or not product:
        raise HTTPException(404, "Article ou produit introuvable.")
    db.update_shopping_item(sid, checked=1, product_code=req.code)
    out = {"item": db.get_shopping_item(sid), "items": db.list_shopping(), "recipe": None}
    rid, idx = item["recipe_id"], item["ingredient_index"]
    if rid and idx is not None and (r := db.get(rid)):
        db.set_ingredient_product(rid, idx, req.code)
        if r["nutrition"]:
            before = r["nutrition"]["per_portion"]
            new = nutrition.apply_products(r["nutrition"], r["data"], db.ingredient_products(rid))
            db.update(rid, nutrition=new)
            out["recipe"] = {"id": rid, "title": r["data"]["title"],
                             "before": {k: before[k] for k in ("kcal", "protein_g")},
                             "after": {k: new["per_portion"][k] for k in ("kcal", "protein_g")}}
    return out


@app.delete("/api/recipes/{rid}/ingredients/{idx}/product")
def unlink_product(rid: str, idx: int) -> dict:
    """Revenir à l'estimation pour un ingrédient (produit retiré)."""
    r = _recipe_or_404(rid)
    db.set_ingredient_product(rid, idx, None)
    if r["nutrition"]:
        db.update(rid, nutrition=nutrition.apply_products(r["nutrition"], r["data"], db.ingredient_products(rid)))
    return db.get(rid)


# ---------- Santé (Raccourci iOS → appli) ----------

@app.post("/api/health")
async def receive_health(request: Request) -> dict:
    """Reçoit le résumé du jour envoyé par le Raccourci (JSON, ou formulaire en secours)."""
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except ValueError:
        # Secours : formulaire « clé=valeur » (option « Formulaire » du Raccourci)
        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
        if not form:
            raise HTTPException(400, "Le Raccourci doit envoyer un JSON (corps de la requête : JSON).") from None
        payload = {k: v[-1] for k, v in form.items()}
    if not isinstance(payload, dict):
        raise HTTPException(400, "Le Raccourci doit envoyer un dictionnaire (JSON).")
    day, summary = health.parse(payload)
    db.save_health_day(day, summary, payload)
    return {"date": day, **summary}


@app.get("/api/health/today")
def health_today() -> dict:
    today = dt.date.today().isoformat()
    return {"today": db.get_health_day(today), "latest": db.latest_health_day()}


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
    for r in db.list_all():  # recettes importées avant l'estimation du nombre de portions
        if r["status"] == "done":
            _fill_servings(r["id"])
    for f in db.list_folders():
        if f["status"] == "sorting":
            db.update_folder(f["id"], status="idle")
    for r in db.list_all():
        if r["status"] == "pending":
            db.update(r["id"], status="queued", stage=None)  # import interrompu : retour dans la file
            import_wakeup.set()
        elif (r["status"] == "done" and (r["data"] or {}).get("is_recipe")
              and r["nutrition_status"] != "error"
              and (r["nutrition"] is None or r["nutrition_status"] == "pending"
                   or r["nutrition"].get("version", 1) < nutrition.VERSION)):
            # Recettes sans valeurs nutritionnelles, calcul interrompu, ou méthode de calcul changée
            pool.submit(compute_nutrition, r["id"])


pool.submit(_backfill_thumbnails)
threading.Thread(target=import_worker, name="file-import", daemon=True).start()
if os.environ.get("RESUME_WORK", "1") != "0":
    _resume_work()
app.mount("/thumbs", StaticFiles(directory=THUMBS), name="thumbs")
app.mount("/", StaticFiles(directory=STATIC), name="static")
