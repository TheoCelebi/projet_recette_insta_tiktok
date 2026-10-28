"""Données de l'app Santé envoyées par un Raccourci iOS (une web app ne peut pas lire Santé).

Le Raccourci envoie un JSON du type :
    {"active_kcal": "512 kcal", "steps": 9800, "weight_kg": "78,4", "workouts": "..."}
Les valeurs peuvent arriver en texte, avec unités et virgules : on les lit avec tolérance.
"""

from __future__ import annotations

import datetime as dt
import re

NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def num(v) -> float | None:
    """'512 kcal', '78,4', 9800, ['12', '30'] (somme) → nombre."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, list):
        vals = [x for x in (num(i) for i in v) if x is not None]
        return sum(vals) if vals else None
    m = NUM_RE.search(str(v).replace(" ", "").replace("\xa0", "").replace(" ", ""))
    return float(m.group().replace(",", ".")) if m else None


def _minutes(text: str) -> float | None:
    """'52 min', '1 h 05', '01:05:00', '3120 s' → minutes."""
    t = text.lower()
    if m := re.search(r"(\d+):(\d{2}):(\d{2})", t):
        return int(m[1]) * 60 + int(m[2]) + int(m[3]) / 60
    if m := re.search(r"(\d+)\s*h\s*(\d{1,2})\b(?!\s*s)", t):  # « 1 h 05 »
        return int(m[1]) * 60 + int(m[2])
    h = re.search(r"(\d+(?:[.,]\d+)?)\s*h(?:eures?)?\b", t)
    mi = re.search(r"(\d+(?:[.,]\d+)?)\s*min", t)
    if h or mi:
        return (float(h[1].replace(",", ".")) * 60 if h else 0) + (float(mi[1].replace(",", ".")) if mi else 0)
    if s := re.search(r"(\d+(?:[.,]\d+)?)\s*s(?:ec(?:ondes?)?)?\b", t):
        return float(s[1].replace(",", ".")) / 60
    return None


def _workout(item) -> dict | None:
    """Une séance, envoyée en objet ({type, duration, kcal…}) ou en ligne de texte."""
    if isinstance(item, dict):
        get = lambda *keys: next((item[k] for k in keys if k in item and item[k] not in (None, "")), None)  # noqa: E731
        label = str(get("type", "name", "label", "activity", "Type d'entraînement", "Type") or "Séance")
        duration = get("minutes", "duration", "Durée", "duree")
        minutes = num(duration) if isinstance(duration, (int, float)) else _minutes(str(duration or ""))
        if duration is not None and minutes is None:
            minutes = num(duration)
        if get("seconds") is not None:
            minutes = num(get("seconds")) / 60
        return {"label": label.strip(), "minutes": round(minutes) if minutes else None,
                "kcal": round(num(get("kcal", "energy", "calories", "Énergie active", "Énergie")) or 0) or None}
    text = str(item).strip()
    if not text:
        return None
    kcal = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:kcal|cal)\b", text, re.I)
    minutes = _minutes(re.sub(r"\d+(?:[.,]\d+)?\s*(?:kcal|cal)\b", "", text, flags=re.I))
    label = re.split(r"[\d,;:(|–-]", text, maxsplit=1)[0].strip(" ·-") or "Séance"
    return {"label": label, "minutes": round(minutes) if minutes else None,
            "kcal": round(float(kcal[1].replace(",", "."))) if kcal else None}


def workouts(v) -> list[dict]:
    if v is None:
        return []
    items = v if isinstance(v, list) else [line for line in str(v).splitlines() if line.strip()]
    return [w for w in (_workout(i) for i in items) if w]


def parse(payload: dict) -> tuple[str, dict]:
    """Renvoie (date AAAA-MM-JJ, résumé du jour)."""
    raw_date = str(payload.get("date") or "").strip()
    try:
        day = dt.date.fromisoformat(raw_date[:10]).isoformat()
    except ValueError:
        day = dt.date.today().isoformat()  # sans date : aujourd'hui (heure du Mac)
    ws = workouts(payload.get("workouts"))
    # « Minutes d'exercice » (anneau vert d'Activité) : quand le Raccourci n'a pas accès aux entraînements
    exercise_raw = next((payload[k] for k in ("exercise_minutes", "exercise_minute", "exercise") if k in payload), None)
    exercise = round(num(exercise_raw) or 0) or None
    summary = {
        "active_kcal": round(num(payload.get("active_kcal")) or 0) or None,
        "steps": round(num(payload.get("steps")) or 0) or None,
        "weight_kg": (round(w, 1) if (w := num(payload.get("weight_kg"))) else None),
        "workouts": ws,
        "exercise_minutes": exercise,
        "workout_minutes": sum(w["minutes"] or 0 for w in ws) or exercise,
    }
    return day, summary
