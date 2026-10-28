"""Valeurs nutritionnelles d'une recette et rangement automatique dans les dossiers.

Claude estime le poids et les nutriments de chaque ingrédient ; les totaux, les valeurs par
portion / pour 100 g, la répartition des macros et les repères sportifs sont calculés ici,
de façon déterministe.
"""

from __future__ import annotations

import json
import re
import time
from typing import Literal

from pydantic import BaseModel, Field

from .ai import structured

# À incrémenter quand la méthode de calcul change : les recettes sont alors recalculées
VERSION = 2

# Clés des nutriments, dans l'ordre d'affichage
NUTRIENTS = [
    "kcal", "protein_g", "carbs_g", "sugars_g", "fat_g", "saturated_fat_g", "fiber_g", "salt_g",
    "potassium_mg", "magnesium_mg", "calcium_mg", "iron_mg", "zinc_mg",
    "vitamin_c_mg", "vitamin_d_ug", "vitamin_b12_ug",
]

# Apports de référence d'un adulte (règlement UE 1169/2011 ; fibres : recommandation ANSES)
REFERENCE_INTAKES = {
    "kcal": 2000, "protein_g": 50, "carbs_g": 260, "sugars_g": 90, "fat_g": 70,
    "saturated_fat_g": 20, "fiber_g": 30, "salt_g": 6,
    "potassium_mg": 2000, "magnesium_mg": 375, "calcium_mg": 800, "iron_mg": 14, "zinc_mg": 10,
    "vitamin_c_mg": 80, "vitamin_d_ug": 5, "vitamin_b12_ug": 2.5,
}

SYSTEM = """Tu es diététicien du sport. On te donne une recette au format JSON. Estime ses \
valeurs nutritionnelles le plus justement possible, ingrédient par ingrédient.

Méthode :
- Pour chaque ingrédient, détermine le poids réellement consommé en grammes. Convertis les \
unités : 1 c. à soupe d'huile ≈ 13 g, 1 c. à café ≈ 5 g, 1 œuf moyen ≈ 50 g sans coquille, \
1 jaune ≈ 18 g, 1 gousse d'ail ≈ 5 g, 1 oignon moyen ≈ 100 g, etc.
- Pâtes, riz, céréales, légumineuses : si la recette donne un poids sans préciser, c'est le \
poids cru/sec ; utilise alors les valeurs du produit sec.
- Quantité non précisée : prends une quantité typique pour ce plat et ce nombre de portions, \
et mets quantity_estimated à true.
- Ce qui n'est pas mangé ne compte pas : eau de cuisson jetée, marinade jetée, bouquet garni. \
Friture : seulement l'huile absorbée. Sel de l'eau de cuisson : seulement la petite part absorbée.
- Ingrédient au choix (« pennes ou spaghettis ») : prends le premier.
- Ingrédient ou préparation présenté comme facultatif / optionnel (y compris une section entière, \
ex. « Sauce (optionnel) ») : compte-le quand même avec une quantité réaliste, comme dans la vidéo, \
et mets optional à true. Seuls le sel, le poivre et les épices « au goût » peuvent être négligeables.
- Donne les valeurs pour la quantité de l'ingrédient (pas pour 100 g), d'après les tables de \
composition de référence (Ciqual de l'ANSES, USDA).
- Vérifie la cohérence : kcal ≈ 4 × protéines + 4 × glucides + 9 × lipides + 2 × fibres.
- Les micronutriments sont des estimations : mets 0 seulement si l'ingrédient n'en contient pas.
- servings : le nombre de portions de la recette s'il est donné, sinon ton estimation.
- cooked_weight_g : poids total du plat fini (après cuisson, égouttage, évaporation).

Dossiers : si une liste de dossiers est fournie, mets dans folder_ids ceux où la recette a \
clairement sa place d'après leur nom et leur description, y compris les critères chiffrés \
(à évaluer par portion). Sois exigeant : en cas de doute, ne range pas. Sans liste, renvoie []."""

CLASSIFY_SYSTEM = """Tu ranges les recettes d'un carnet de cuisine dans des dossiers. On te \
donne un dossier (nom et description éventuelle) et les recettes du carnet, avec leurs \
valeurs nutritionnelles par portion quand elles sont connues. Renvoie les ids des recettes \
qui ont clairement leur place dans ce dossier. Respecte strictement les critères chiffrés \
éventuels (calories, protéines… par portion). En cas de doute, ne range pas."""


class IngredientNutrition(BaseModel):
    ingredient: str = Field(description="Nom de l'ingrédient, tel que dans la recette")
    grams: float = Field(description="Poids réellement consommé, en grammes")
    quantity_estimated: bool = Field(description="true si la quantité n'était pas donnée")
    optional: bool = Field(description="true si l'ingrédient est présenté comme facultatif")
    kcal: float
    protein_g: float
    carbs_g: float
    sugars_g: float
    fat_g: float
    saturated_fat_g: float
    fiber_g: float
    salt_g: float
    potassium_mg: float
    magnesium_mg: float
    calcium_mg: float
    iron_mg: float
    zinc_mg: float
    vitamin_c_mg: float
    vitamin_d_ug: float
    vitamin_b12_ug: float


class NutritionAnalysis(BaseModel):
    servings: int = Field(description="Nombre de portions")
    cooked_weight_g: float = Field(description="Poids total estimé du plat fini, en grammes")
    ingredients: list[IngredientNutrition]
    confidence: Literal["haute", "moyenne", "basse"] = Field(
        description="Fiabilité de l'estimation (basse si beaucoup de quantités manquent)")
    notes: str = Field(description="1-2 phrases : hypothèses importantes (quantités estimées, "
                                   "ingrédients non comptés)")
    folder_ids: list[str] = Field(description="Ids des dossiers où ranger la recette, [] si aucun")


class FolderMatch(BaseModel):
    recipe_ids: list[str] = Field(description="Ids des recettes qui ont leur place dans le dossier")


def _r(x: float, nd: int = 1) -> float:
    return round(x, nd)


def _block(ings: list[IngredientNutrition], servings: int, cooked_weight_g: float) -> dict:
    """Totaux, valeurs par portion et pour 100 g, macros et repères sportifs d'un ensemble d'ingrédients."""
    total = {k: sum(getattr(i, k) for i in ings) for k in NUTRIENTS}

    # Garde-fou : l'énergie doit correspondre aux macronutriments (facteurs d'Atwater)
    atwater = 4 * total["protein_g"] + 4 * total["carbs_g"] + 9 * total["fat_g"] + 2 * total["fiber_g"]
    kcal_adjusted = atwater > 0 and abs(total["kcal"] - atwater) / atwater > 0.15
    if kcal_adjusted:
        total["kcal"] = atwater

    portion = {k: v / servings for k, v in total.items()}
    per_100g = {k: v * 100 / cooked_weight_g for k, v in total.items()} if cooked_weight_g > 0 else None

    e_p, e_c, e_f = 4 * total["protein_g"], 4 * total["carbs_g"], 9 * total["fat_g"]
    e_tot = (e_p + e_c + e_f) or 1
    split = {"protein": e_p / e_tot * 100, "carbs": e_c / e_tot * 100, "fat": e_f / e_tot * 100}

    kcal_p = portion["kcal"] or 1
    fiber_per_100kcal = portion["fiber_g"] * 100 / kcal_p
    badges = []
    # Seuils des allégations nutritionnelles européennes (règlement CE 1924/2006)
    if split["protein"] >= 20:
        badges.append("Riche en protéines")
    elif split["protein"] >= 12:
        badges.append("Source de protéines")
    if (per_100g and per_100g["fiber_g"] >= 6) or fiber_per_100kcal >= 3:
        badges.append("Riche en fibres")
    elif (per_100g and per_100g["fiber_g"] >= 3) or fiber_per_100kcal >= 1.5:
        badges.append("Source de fibres")
    if per_100g and per_100g["sugars_g"] <= 5:
        badges.append("Pauvre en sucres")
    # Repères sport (indicatifs) : récupération = protéines + glucides sans excès de gras ;
    # avant l'effort = surtout des glucides, peu de graisses et de fibres (digestion facile)
    if portion["protein_g"] >= 20 and portion["carbs_g"] >= 40 and split["fat"] <= 35:
        badges.append("Idéal après l'effort")
    if split["carbs"] >= 50 and split["fat"] <= 25 and portion["fiber_g"] <= 6:
        badges.append("Avant l'effort")

    rounded = lambda d: {k: _r(v, 0 if k == "kcal" else 1) for k, v in d.items()} if d else None  # noqa: E731
    return {
        "cooked_weight_g": _r(cooked_weight_g, 0),
        "portion_weight_g": _r(cooked_weight_g / servings, 0) if cooked_weight_g else None,
        "total": rounded(total),
        "per_portion": rounded(portion),
        "per_100g": rounded(per_100g),
        "energy_split": {k: _r(v, 0) for k, v in split.items()},
        "protein_per_100kcal": _r(portion["protein_g"] * 100 / kcal_p),
        "carb_protein_ratio": _r(portion["carbs_g"] / portion["protein_g"]) if portion["protein_g"] else None,
        "badges": badges,
        "kcal_adjusted": kcal_adjusted,
    }


def summarize(a: NutritionAnalysis, recipe_servings: int | None) -> dict:
    """Résumé complet (avec les éléments facultatifs) et variante sans eux s'il y en a."""
    servings = max(1, recipe_servings or a.servings or 1)
    summary = _block(a.ingredients, servings, a.cooked_weight_g)
    core = [i for i in a.ingredients if not i.optional]
    without = None
    if len(core) < len(a.ingredients) and core:
        # Poids du plat sans les options : au prorata des poids crus
        all_g = sum(i.grams for i in a.ingredients) or 1
        without = _block(core, servings, a.cooked_weight_g * sum(i.grams for i in core) / all_g)
    return {
        **summary,
        "version": VERSION,
        "servings": servings,
        "without_optional": without,
        "ingredients": [i.model_dump() for i in a.ingredients],
        "confidence": a.confidence,
        "notes": a.notes,
        "reference_intakes": REFERENCE_INTAKES,
        "computed_at": time.time(),
    }


def analyze(recipe: dict, folders: list[dict]) -> tuple[dict, list[str]]:
    """Renvoie (résumé nutritionnel, ids des dossiers où ranger la recette)."""
    payload = {
        "recette": {k: recipe.get(k) for k in ("title", "servings", "ingredients", "steps")},
        "dossiers": [{"id": f["id"], "nom": f["name"], "description": f.get("description") or ""}
                     for f in folders],
    }
    a = structured(SYSTEM, json.dumps(payload, ensure_ascii=False), NutritionAnalysis, effort="high")
    valid = {f["id"] for f in folders}
    return summarize(a, recipe.get("servings")), [fid for fid in a.folder_ids if fid in valid]


def _brief(r: dict) -> dict:
    d, n = r["data"], r.get("nutrition") or {}
    brief = {
        "id": r["id"], "titre": d["title"], "tags": d.get("tags", []), "résumé": d.get("summary", ""),
        "ingrédients": [i["name"] for i in d.get("ingredients", [])],
        "temps_total_min": (d.get("prep_minutes") or 0) + (d.get("cook_minutes") or 0) or None,
    }
    if p := n.get("per_portion"):
        brief["par_portion"] = {k: p[k] for k in ("kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugars_g")}
    return brief


def classify(folder: dict, recipes: list[dict]) -> list[str]:
    """Ids des recettes existantes qui ont leur place dans `folder`."""
    if not recipes:
        return []
    payload = {
        "dossier": {"nom": folder["name"], "description": folder.get("description") or ""},
        "recettes": [_brief(r) for r in recipes],
    }
    m = structured(CLASSIFY_SYSTEM, json.dumps(payload, ensure_ascii=False), FolderMatch,
                   effort="medium", model="sonnet")  # tâche simple : Sonnet suffit et ménage le quota
    valid = {r["id"] for r in recipes}
    return [rid for rid in m.recipe_ids if rid in valid]


# ---------- Produits scannés : valeurs de l'étiquette à la place de l'estimation ----------

LABEL_KEYS = ["kcal", "protein_g", "carbs_g", "sugars_g", "fat_g", "saturated_fat_g", "fiber_g", "salt_g"]


def _words(name: str) -> set[str]:
    import unicodedata
    n = unicodedata.normalize("NFKD", name.lower()).encode("ascii", "ignore").decode()
    return {w.rstrip("s") for w in re.findall(r"[a-z]{3,}", n)}


def _match_index(recipe_ing: dict, analysis: list[dict]) -> int | None:
    """Ingrédient de l'analyse nutritionnelle qui correspond à un ingrédient de la recette."""
    target = _words(recipe_ing.get("name", ""))
    best, score = None, 0.0
    for k, a in enumerate(analysis):
        w = _words(a.get("ingredient", ""))
        if target and w and (s := len(target & w) / len(target | w)) > score:
            best, score = k, s
    return best if score >= 0.3 else None


def apply_products(summary: dict, recipe: dict, links: dict[int, dict]) -> dict:
    """Recalcule le résumé en remplaçant, pour les ingrédients reliés à un produit scanné, les
    macronutriments estimés par ceux de l'étiquette (pour le même poids). Les micronutriments,
    absents des étiquettes, restent estimés. Sans appel à l'IA."""
    ings = [dict(i) for i in summary["ingredients"]]
    for i in ings:  # on repart des valeurs estimées (un produit a pu être changé ou retiré)
        if "ai" in i:
            i.update(i.pop("ai"))
        i.pop("product", None)
    recipe_ings = recipe.get("ingredients", [])
    for idx, product in links.items():
        if idx >= len(recipe_ings) or not (per100 := product.get("per_100g")):
            continue
        k = idx if len(recipe_ings) == len(ings) else _match_index(recipe_ings[idx], ings)
        if k is None or k >= len(ings) or not ings[k].get("grams"):
            continue
        ing = ings[k]
        ing["ai"] = {key: ing[key] for key in LABEL_KEYS}
        for key in LABEL_KEYS:
            if per100.get(key) is not None:
                ing[key] = per100[key] * ing["grams"] / 100
        ing["product"] = {"code": product["code"], "name": product["name"], "brand": product.get("brand", "")}
    a = NutritionAnalysis(servings=summary["servings"], cooked_weight_g=summary["cooked_weight_g"] or 0,
                          ingredients=[IngredientNutrition(**i) for i in ings], confidence=summary["confidence"],
                          notes=summary["notes"], folder_ids=[])
    new = summarize(a, recipe.get("servings"))
    for out, src in zip(new["ingredients"], ings):  # summarize ne garde que les champs du schéma
        for extra in ("ai", "product"):
            if extra in src:
                out[extra] = src[extra]
    new["products_applied"] = sum(1 for i in ings if "product" in i)
    new["computed_at"] = summary.get("computed_at", new["computed_at"])
    return new
