# Mes Recettes — clone de ReciMe

Colle (ou partage) un lien TikTok, Instagram Reels ou YouTube Shorts : l'appli regarde la
vidéo et en sort une fiche recette propre (ingrédients avec quantités, étapes, temps, astuces).

## Comment ça marche

```
lien ─► yt-dlp ─► légende du post + sous-titres
                └► vidéo ─► 3 à 8 images clés (texte incrusté à l'écran)
                         └► transcription audio locale (faster-whisper) si pas de sous-titres
                                   │
                                   ▼
                 Claude (vision + sortie JSON structurée) ─► Recette ─► SQLite ─► web app
```

C'est aussi, très probablement, le fonctionnement de ReciMe côté serveur : récupérer
légende + audio + images, puis demander à un LLM de structurer le tout.

| Fichier | Rôle |
|---|---|
| `backend/media.py` | Téléchargement (yt-dlp), sous-titres, extraction d'images (ffmpeg), transcription |
| `backend/extractor.py` | Prompt + schéma `Recipe` (Pydantic), appel Claude |
| `backend/db.py` | Stockage SQLite |
| `backend/app.py` | API FastAPI, traitement en arrière-plan, endpoint de partage `/share` |
| `frontend/` | Web app mobile (PWA) : liste, recherche, fiche, portions, liste de courses |

## Lancer

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env    # puis colle ta clé (https://console.anthropic.com) dans .env
.venv/bin/uvicorn backend.app:app --host 0.0.0.0 --port 8765
```

Puis ouvre http://localhost:8765.

### Sans crédits API : via ton abonnement Claude

Si `.env` ne contient pas de clé `sk-ant-…`, l'appli passe automatiquement par
[Claude Code](https://claude.com/claude-code) (`claude -p`), qui utilise ton abonnement
Claude (Pro/Max) au lieu de crédits API. Il suffit que Claude Code soit connecté :

```bash
claude      # puis tape /login et suis les instructions
```

Chaque import consomme alors une partie des limites d'usage de ton abonnement.

Options (variables d'environnement) :
- `WHISPER_MODEL` : modèle de transcription (`small` par défaut, `base` = plus rapide, `medium` = plus précis).
- `YTDLP_COOKIES_FROM_BROWSER=chrome` : utilise les cookies de ton navigateur, utile pour
  Instagram qui bloque souvent les requêtes non connectées.
- `RECIPES_DB` : chemin de la base SQLite.

## Sur le téléphone

Le Mac et le téléphone sur le même Wi-Fi, ouvre `http://<ip-du-mac>:8765`
(IP : `ipconfig getifaddr en0`).

- **Android** : Chrome → « Ajouter à l'écran d'accueil ». L'appli apparaît ensuite dans
  le menu *Partager* de TikTok/Instagram (manifest `share_target`).
- **iPhone** : Safari ne gère pas `share_target`. Crée un Raccourci *« Recette »* :
  *Recevoir les URL depuis la feuille de partage* → *Ouvrir l'URL*
  `http://<ip-du-mac>:8765/share?url=[Entrée du raccourci]`. Il apparaît alors dans le
  menu Partager de TikTok.

## Limites connues

- Coût : un appel Claude Opus par import (images + texte), quelques centimes.
- Instagram et TikTok changent régulièrement leurs protections : si les imports échouent,
  mets à jour yt-dlp (`.venv/bin/pip install -U yt-dlp`).
- La transcription locale prend 5 à 30 s selon la longueur de la vidéo (premier lancement :
  téléchargement du modèle Whisper, ~500 Mo).
- Pas de comptes utilisateurs : c'est un outil perso, à ne pas exposer tel quel sur Internet.
