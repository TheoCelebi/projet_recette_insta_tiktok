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

## Valeurs nutritionnelles

Après l'import, Claude estime le poids réellement consommé de chaque ingrédient et ses
nutriments : énergie, protéines, glucides, sucres, lipides, saturés, fibres, sel, potassium,
magnésium, calcium, fer, zinc, vitamines C, D et B12. Le serveur calcule ensuite
(`backend/nutrition.py`) :

- les valeurs par portion, pour la recette entière et pour 100 g ;
- la répartition de l'énergie entre protéines, glucides et lipides ;
- les repères sportifs : protéines pour 100 kcal, rapport glucides/protéines, badges
  « Riche en protéines », « Riche en fibres » (seuils des allégations UE), « Idéal après
  l'effort », « Avant l'effort » ;
- les % des apports de référence d'un adulte (règlement UE 1169/2011).

Garde-fou : si les calories ne correspondent pas aux macros (facteurs d'Atwater, ±15 %),
elles sont recalculées. Les éléments facultatifs (sauce optionnelle…) sont comptés, avec une
bascule sur la fiche pour voir les valeurs sans eux. Ce sont des **estimations** : prévu
ensuite, l'adossement à la table Ciqual de l'ANSES et aux produits Open Food Facts.

## Dossiers

Crée des dossiers (« Healthy », « Prise de masse »…) avec, si tu veux, une règle en clair
(« au moins 25 g de protéines par portion »). Chaque nouvelle recette est rangée
automatiquement dans les dossiers qui lui correspondent, et à la création d'un dossier les
recettes existantes y sont rangées. Le bouton « Ranger dans un dossier » d'une fiche permet
de corriger à la main.

## Trier, filtrer, hors ligne

- **Trier et filtrer** : plus protéinées, moins caloriques, meilleur rapport protéines/calories,
  plus rapides ; filtres calories max, protéines min, prêt en 30 min, repères sportifs.
  Les choix sont mémorisés sur l'appareil.
- **Ajout hors ligne** : un lien ajouté quand le Mac est injoignable (formulaire ou partage
  depuis TikTok via le Raccourci) est gardé sur le téléphone, affiché dans « En attente de
  connexion », puis envoyé automatiquement dès que le Mac répond (nouvel essai toutes les 10 s).
  Sur iPhone, Safari et l'appli de l'écran d'accueil ont chacun leur propre mémoire : un lien
  partagé via le Raccourci (Safari) part quand Safari rejoint le Mac.

## Scanner un produit

Le bouton code-barres de l'accueil ouvre la caméra (lecteur intégré du navigateur quand il
existe, sinon la bibliothèque ZXing, embarquée dans `frontend/vendor/`). Le serveur interroge
Open Food Facts (`backend/products.py`) et garde la fiche en cache 30 jours : valeurs pour
100 g et par portion, Nutri-Score, protéines pour 100 kcal, repères (riche en protéines…).
Les produits ajoutés à « Mes produits » apparaissent sur l'accueil. Saisie manuelle du code
possible si la caméra n'est pas disponible.

## App Santé (via un Raccourci)

Une web app ne peut pas lire l'app Santé (réservé aux applis natives). Un Raccourci iOS lit
les calories actives, les pas, le poids et les séances du jour, et les envoie en JSON à
`POST /api/health` (`backend/health.py`, lecture tolérante des unités et formats). L'accueil
affiche alors une carte « Aujourd'hui » et propose des recettes « Idéal après l'effort » après
une séance. Les étapes du Raccourci, avec la bonne adresse, sont dans l'appli : carte
« Relie l'app Santé » sur l'accueil (page `#/sante`).

## Modifier une recette, file d'import

- **Modifier** : bouton crayon en haut de la fiche. Photo (réduite sur le téléphone avant
  l'envoi), titre, résumé, portions, temps, niveau, ingrédients, étapes, astuces, tags. Si les
  ingrédients ou les portions changent, les valeurs nutritionnelles sont recalculées.
- **File d'import** : on peut coller plusieurs liens d'un coup (bouton ⊕). Le serveur traite les
  vidéos une par une, dans l'ordre d'ajout (`import_worker`) ; l'accueil affiche « Import en
  cours » avec l'étape ou la place dans la file, et permet d'annuler un lien en attente. Un import
  interrompu (serveur arrêté) reprend sa place dans la file au redémarrage.

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

## Sur l'iPhone, partout (Tailscale)

[Tailscale](https://tailscale.com) relie le Mac et l'iPhone dans un réseau privé : l'appli
est accessible en Wi-Fi comme en 4G, sans IP à taper, et sans l'exposer sur Internet.

1. Installe Tailscale sur le Mac et sur l'iPhone, connecte-les au même compte.
2. Dans la console Tailscale (*DNS*), active **MagicDNS** et **HTTPS Certificates**.
3. Sur le Mac, publie l'appli en HTTPS dans ton réseau Tailscale :
   ```bash
   tailscale serve --bg 8765
   ```
   L'adresse affichée ressemble à `https://mon-mac.tailXXXX.ts.net`.
4. Sur l'iPhone, ouvre cette adresse dans Safari → *Partager* → **Sur l'écran d'accueil**.

**Mac éteint** : l'appli s'ouvre quand même et affiche les recettes déjà importées
(copie gardée sur le téléphone, photos comprises). Seul l'ajout de recettes demande le Mac
allumé avec le serveur lancé. Le HTTPS est nécessaire pour ce mode hors ligne.

**Importer depuis TikTok** : crée un Raccourci *« Recette »* : *Recevoir les URL depuis la
feuille de partage* → *Ouvrir l'URL* `https://mon-mac.tailXXXX.ts.net/share?url=[Entrée du raccourci]`.
Il apparaît alors dans le menu Partager de TikTok/Instagram.

Sur Android, *Ajouter à l'écran d'accueil* depuis Chrome suffit : l'appli apparaît
directement dans le menu Partager (manifest `share_target`).

## Limites connues

- Coût : un appel Claude Opus par import (images + texte), quelques centimes.
- Instagram et TikTok changent régulièrement leurs protections : si les imports échouent,
  mets à jour yt-dlp (`.venv/bin/pip install -U yt-dlp`).
- La transcription locale prend 5 à 30 s selon la longueur de la vidéo (premier lancement :
  téléchargement du modèle Whisper, ~500 Mo).
- Pas de comptes utilisateurs : c'est un outil perso, à ne pas exposer tel quel sur Internet.

## Crédits

Lecteur de codes-barres [ZXing](https://github.com/zxing-js/library) 0.21.3 (licence MIT). Données produits : [Open Food Facts](https://fr.openfoodfacts.org) (licence ODbL).

Polices [Fraunces](https://fonts.google.com/specimen/Fraunces) et [DM Sans](https://fonts.google.com/specimen/DM+Sans), sous licence SIL Open Font License, intégrées dans `frontend/fonts/` pour fonctionner hors ligne.
