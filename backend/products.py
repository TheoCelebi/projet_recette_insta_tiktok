"""Produits du commerce : valeurs de l'étiquette via Open Food Facts (code-barres)."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json?fields=" + ",".join([
    "product_name", "product_name_fr", "brands", "quantity", "serving_size", "serving_quantity",
    "nutriscore_grade", "image_front_small_url", "image_front_url", "nutriments",
])
# Open Food Facts demande d'identifier l'application qui interroge l'API
USER_AGENT = "MonCarnetDeCuisine/1.0 (usage personnel)"

# Clé Open Food Facts (pour 100 g) -> clé de l'appli
FIELDS = {
    "energy-kcal_100g": "kcal", "proteins_100g": "protein_g", "carbohydrates_100g": "carbs_g",
    "sugars_100g": "sugars_g", "fat_100g": "fat_g", "saturated-fat_100g": "saturated_fat_g",
    "fiber_100g": "fiber_g", "salt_100g": "salt_g",
}


class NotFound(Exception):
    pass


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch(code: str) -> dict:
    """Interroge Open Food Facts et renvoie le produit normalisé ; NotFound s'il est inconnu."""
    req = urllib.request.Request(OFF_URL.format(code=code), headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            data = json.load(res)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(code) from e
        raise
    if data.get("status") != 1 or not data.get("product"):
        raise NotFound(code)
    return normalize(code, data["product"])


def normalize(code: str, p: dict) -> dict:
    n = p.get("nutriments") or {}
    per_100g = {ours: _num(n.get(theirs)) for theirs, ours in FIELDS.items()}
    if per_100g["kcal"] is None and (kj := _num(n.get("energy_100g"))) is not None:
        per_100g["kcal"] = kj / 4.184  # certaines fiches ne donnent l'énergie qu'en kJ
    has_values = any(v is not None for v in per_100g.values())

    serving_g = _num(p.get("serving_quantity"))
    per_serving = ({k: (v * serving_g / 100 if v is not None else None) for k, v in per_100g.items()}
                   if serving_g and has_values else None)

    kcal, prot = per_100g["kcal"], per_100g["protein_g"]
    badges = []
    if kcal and prot is not None:
        # Seuils des allégations nutritionnelles européennes (règlement CE 1924/2006)
        share = prot * 4 / kcal * 100
        if share >= 20:
            badges.append("Riche en protéines")
        elif share >= 12:
            badges.append("Source de protéines")
    if (fiber := per_100g["fiber_g"]) is not None:
        if fiber >= 6:
            badges.append("Riche en fibres")
        elif fiber >= 3:
            badges.append("Source de fibres")
    if per_100g["sugars_g"] is not None and per_100g["sugars_g"] <= 5:
        badges.append("Pauvre en sucres")
    if per_100g["fat_g"] is not None and per_100g["fat_g"] <= 3:
        badges.append("Pauvre en matières grasses")

    grade = (p.get("nutriscore_grade") or "").lower()
    return {
        "code": code,
        "name": p.get("product_name_fr") or p.get("product_name") or "Produit sans nom",
        "brand": (p.get("brands") or "").split(",")[0].strip(),
        # « 400 g e » : le « e » (℮) est la mention légale de poids estimé des emballages
        "quantity": re.sub(r"\s*[e℮]$", "", (p.get("quantity") or "").strip()),
        "image": p.get("image_front_small_url") or p.get("image_front_url") or "",
        "nutriscore": grade if grade in {"a", "b", "c", "d", "e"} else None,
        "serving_g": serving_g,
        "serving_label": p.get("serving_size") or "",
        "per_100g": {k: (round(v, 1) if v is not None else None) for k, v in per_100g.items()} if has_values else None,
        "per_serving": {k: (round(v, 1) if v is not None else None) for k, v in per_serving.items()} if per_serving else None,
        "protein_per_100kcal": round(prot * 100 / kcal, 1) if kcal and prot is not None else None,
        "badges": badges,
        "source_url": f"https://fr.openfoodfacts.org/produit/{code}",
    }
