"""Questions sur les valeurs nutritionnelles d'une recette, répondues par Claude Haiku.

Haiku ne recalcule rien : il explique les chiffres déjà calculés (poids et nutriments de chaque
ingrédient, totaux, portion), qu'on lui donne en entier.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from .ai import structured

SYSTEM = """Tu expliques les valeurs nutritionnelles d'une recette d'un carnet de cuisine, à un \
étudiant sportif. On te donne la recette, le détail calculé pour chaque ingrédient (poids en \
grammes, calories, protéines, glucides, lipides…), les totaux, les valeurs par portion et le \
nombre de personnes qu'il affiche. Réponds à sa question en t'appuyant uniquement sur ces \
chiffres : dis d'où vient une valeur (quels ingrédients y contribuent et combien), comment on \
passe de la recette entière à une portion, pourquoi une quantité a été estimée. Si tu donnes \
un repère général (ex. ~23 g de protéines pour 100 g de poulet cru), dis que c'est un repère. \
Ne recalcule pas d'autres valeurs que celles fournies, sauf une division ou une addition simple \
pour expliquer. Changer le nombre de personnes affiché change les quantités d'ingrédients, \
pas les valeurs par portion : n'en parle que si on te le demande. Si la question ne porte pas \
sur la nutrition de cette recette, dis en une phrase \
que tu sers seulement à expliquer ses valeurs nutritionnelles. Réponds en français, en texte \
simple sans markdown, en 120 mots maximum."""

KEYS = ["kcal", "protein_g", "carbs_g", "sugars_g", "fat_g", "saturated_fat_g", "fiber_g", "salt_g"]


class Answer(BaseModel):
    answer: str = Field(description="Réponse en texte simple, 120 mots maximum")


def _pick(d: dict | None) -> dict:
    return {k: d[k] for k in KEYS if d and d.get(k) is not None}


def answer(recipe: dict, question: str, servings: float | None) -> str:
    d, n = recipe.get("data") or {}, recipe.get("nutrition") or {}
    base = d.get("servings") or n.get("servings") or 1
    context = {
        "recette": d.get("title"),
        "personnes_dans_la_recette": base,
        "personnes_affichées": servings or base,
        "ingrédients": [{k: i.get(k) for k in ("name", "quantity", "unit", "note") if i.get(k) not in (None, "")}
                        for i in d.get("ingredients", [])],
        "détail_par_ingrédient_recette_entière": [
            {"ingrédient": i["ingredient"], "grammes": i.get("grams"),
             **({"quantité_estimée": True} if i.get("quantity_estimated") else {}),
             **({"facultatif": True} if i.get("optional") else {}),
             **({"étiquette_produit_scanné": i["product"].get("name")} if i.get("product") else {}),
             **_pick(i)}
            for i in n.get("ingredients", [])],
        "total_recette": _pick(n.get("total")),
        "par_portion": _pick(n.get("per_portion")),
        "pour_100_g": _pick(n.get("per_100g")),
        "poids_cuit_total_g": n.get("cooked_weight_g"),
        "poids_portion_g": n.get("portion_weight_g"),
        "notes_du_calcul": n.get("notes"),
        "fiabilité": n.get("confidence"),
    }
    prompt = json.dumps({"données": context, "question": question}, ensure_ascii=False)
    return structured(SYSTEM, prompt, Answer, effort="low", model="haiku").answer.strip()
