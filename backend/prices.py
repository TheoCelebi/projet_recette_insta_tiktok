"""Prix estimé des articles de la liste de courses (moyenne en supermarché, en France).

Claude (Sonnet, tâche simple) estime le prix de ce qu'on achète réellement : le paquet entier
le plus courant qui couvre la quantité demandée. Les estimations sont gardées en base pour ne
pas redemander le prix d'un article déjà vu.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from .ai import structured

SYSTEM = """Tu estimes le prix d'articles de courses dans un supermarché français ordinaire \
(prix moyen type Carrefour, Leclerc, Intermarché, marque distributeur, en euros, année en cours). \
Pour chaque article, on te donne la quantité nécessaire (parfois absente) et le nom. Estime le \
prix de ce qu'on achète vraiment : le conditionnement courant le plus petit qui couvre la \
quantité (ex. « 50 g de beurre » → une plaquette de 250 g). Sans quantité, prends le \
conditionnement standard. Pour le sel, le poivre, les épices et autres placards, prends le \
prix d'un paquet standard. Réponds pour chaque article, dans le même ordre."""


class Price(BaseModel):
    index: int = Field(description="Position de l'article dans la liste reçue (0 = premier)")
    eur: float = Field(description="Prix estimé en euros de ce qu'on achète")
    pack: str = Field(description="Ce qu'on achète, court : « barquette de 1 kg », « plaquette 250 g »")


class Prices(BaseModel):
    prices: list[Price]


def estimate(lines: list[str]) -> dict[str, dict]:
    """{« 1,6 kg poulet »: {"eur": 12.5, "pack": "…"}} pour chaque ligne reçue."""
    if not lines:
        return {}
    payload = [{"index": k, "article": line} for k, line in enumerate(lines)]
    result = structured(SYSTEM, json.dumps(payload, ensure_ascii=False), Prices, effort="low", model="sonnet")
    return {lines[p.index]: {"eur": round(max(p.eur, 0), 2), "pack": p.pack.strip()}
            for p in result.prices if 0 <= p.index < len(lines)}
