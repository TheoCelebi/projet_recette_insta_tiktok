"""Transforme le contenu brut d'une vidéo en recette structurée avec Claude."""

from __future__ import annotations

import anthropic
from pydantic import BaseModel, Field

from .media import MediaContent

MODEL = "claude-opus-5-5"

SYSTEM = """Tu es un assistant culinaire. On te donne le contenu d'une vidéo de recette \
(TikTok, Instagram Reels, YouTube Shorts) : légende du post, sous-titres ou transcription \
de l'audio, et des images extraites de la vidéo (le texte incrusté contient souvent les \
ingrédients et quantités).

Reconstitue la recette la plus fidèle possible, en français :
- Croise toutes les sources. Les quantités lues à l'écran ou dans la légende priment.
- N'invente pas de quantité : si elle n'est jamais donnée, laisse quantity vide et mets \
  une estimation raisonnable dans note (ex. "quantité non précisée, ~1 c. à soupe").
- Unités métriques quand c'est possible (g, ml, c. à soupe, c. à café).
- Étapes courtes, actionnables, dans l'ordre, avec durées/températures quand elles sont données.
- Si la vidéo n'est pas une recette, mets is_recipe à false et remplis le minimum."""


class Ingredient(BaseModel):
    name: str = Field(description="Nom de l'ingrédient, ex. 'farine T55'")
    quantity: float | None = Field(description="Quantité numérique, null si inconnue")
    unit: str = Field(description="Unité (g, ml, c. à soupe, pièce...), vide si sans unité")
    note: str = Field(description="Précision éventuelle (haché, à température ambiante...)")
    section: str = Field(description="Groupe, ex. 'Pâte', 'Sauce'; vide si un seul groupe")


class Recipe(BaseModel):
    is_recipe: bool
    title: str
    summary: str = Field(description="Résumé appétissant en 1-2 phrases")
    servings: int | None
    prep_minutes: int | None
    cook_minutes: int | None
    difficulty: str = Field(description="facile, moyen ou difficile")
    ingredients: list[Ingredient]
    steps: list[str]
    tips: list[str] = Field(description="Astuces du créateur ou variantes mentionnées")
    tags: list[str] = Field(description="3-6 tags : type de plat, cuisine, régime (végé...)")


def _build_content(media: MediaContent) -> list[dict]:
    parts: list[dict] = []
    for frame in media.frames:
        parts.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": frame},
        })
    text = [
        f"URL : {media.url}",
        f"Plateforme : {media.platform}",
        f"Auteur : {media.author}",
        f"Titre : {media.title}",
        f"Légende / description :\n{media.description or '(vide)'}",
    ]
    if media.subtitles:
        text.append(f"Sous-titres :\n{media.subtitles}")
    if media.transcript:
        text.append(f"Transcription de l'audio :\n{media.transcript}")
    if media.frames:
        text.append(f"Les {len(media.frames)} images ci-dessus sont extraites de la vidéo, dans l'ordre.")
    parts.append({"type": "text", "text": "\n\n".join(text)})
    return parts


def extract_recipe(media: MediaContent, client: anthropic.Anthropic | None = None) -> Recipe:
    client = client or anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": _build_content(media)}],
        output_config={"effort": "medium"},
        output_format=Recipe,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("Claude a refusé de traiter cette vidéo.")
    if response.parsed_output is None:
        raise RuntimeError(f"Réponse inexploitable (stop_reason={response.stop_reason}).")
    return response.parsed_output
