# 🚀 Guide de Déploiement — JobFinder

## Option recommandée : Railway (~5$/mois)

Railway est la solution la plus simple pour héberger une appli Python/Flask avec une vraie base de données persistante.

---

## Étape 1 — Préparer GitHub

1. Va sur [github.com](https://github.com) et crée un compte si besoin
2. Crée un **nouveau dépôt privé** (ex: `jobfinder`)
3. Dans ton dossier JOBFINDER, ouvre un terminal et tape :

```bash
git init
git add .
git commit -m "premier commit"
git branch -M main
git remote add origin https://github.com/TON_USERNAME/jobfinder.git
git push -u origin main
```

> ⚠️ Avant de pusher, crée un fichier `.gitignore` (voir Étape 1b)

---

## Étape 1b — Créer .gitignore (IMPORTANT)

Crée un fichier `.gitignore` dans le dossier JOBFINDER avec ce contenu :

```
data/
cv_adaptes/
__pycache__/
*.pyc
.env
*.db
*.sqlite
```

Cela évite d'envoyer ta base de données et tes CVs sur GitHub.

---

## Étape 2 — Déployer sur Railway

1. Va sur [railway.app](https://railway.app) et connecte-toi avec GitHub
2. Clique sur **"New Project"** → **"Deploy from GitHub repo"**
3. Sélectionne ton dépôt `jobfinder`
4. Railway détecte automatiquement Python et lance le build

---

## Étape 3 — Variables d'environnement (OBLIGATOIRE)

Dans Railway → ton projet → onglet **Variables**, ajoute :

| Variable | Valeur |
|----------|--------|
| `OPENAI_API_KEY` | `sk-proj-ta-vraie-cle-openai` |
| `SECRET_KEY` | `ea776f705647254a8bd31990615a69b72a762202a17fba90e3cc95d373c17811` |

> 🔑 Le `SECRET_KEY` sert à chiffrer les sessions. Il doit rester **stable et secret**.
> Tu peux en générer un autre avec : `python -c "import secrets; print(secrets.token_hex(32))"`

---

## Étape 4 — Volumes persistants (IMPORTANT pour les données)

Sans volume, Railway **efface les données** à chaque redéploiement (CVs, base de données).

Dans Railway → ton projet → onglet **Volumes** → **New Volume** :

| Mount Path | Description |
|-----------|-------------|
| `/app/data` | Base de données SQLite |
| `/app/cv_adaptes` | Fichiers CV générés |

---

## Étape 5 — Vérifier le déploiement

1. Dans l'onglet **Deployments**, clique sur le build en cours
2. Attends que ça devienne ✅ (peut prendre 3-5 min, Playwright est long à installer)
3. Clique sur le lien généré (ex: `jobfinder-production.up.railway.app`)

---

## Problèmes fréquents

**❌ Build échoue sur Playwright**
→ Le fichier `nixpacks.toml` est déjà créé pour ça. Si ça échoue encore, dans Railway → Settings → Builder → passe à **Dockerfile** et utilise le Dockerfile ci-dessous.

**❌ "Internal Server Error" au lancement**
→ Vérifie les logs dans Railway → Deployments → clic sur le déploiement
→ Vérifie que `OPENAI_API_KEY` et `SECRET_KEY` sont bien définis

**❌ Les CVs disparaissent après redéploiement**
→ Les volumes ne sont pas configurés (voir Étape 4)

**❌ Session déconnectée à chaque fois**
→ `SECRET_KEY` change à chaque restart → bien vérifier qu'il est défini en variable d'environnement

---

## Alternative : Dockerfile (si nixpacks.toml ne marche pas)

Crée un fichier `Dockerfile` à la racine :

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
RUN playwright install chromium

COPY . .

EXPOSE 8080
CMD gunicorn jobfinder:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120
```

---

## Récap des fichiers créés

| Fichier | Rôle |
|---------|------|
| `Procfile` | Commande de démarrage pour Railway/Heroku |
| `requirements.txt` | Dépendances Python (avec gunicorn ajouté) |
| `nixpacks.toml` | Config Railway pour installer Playwright |
| `.env.example` | Modèle des variables d'environnement |
| `.gitignore` | Exclut data/, cv_adaptes/, .env du git |
