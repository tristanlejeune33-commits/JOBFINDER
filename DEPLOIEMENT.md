# 🚀 Guide de Déploiement — JobFinder

## Option recommandée : Railway (~5 $/mois)

Railway est la solution la plus simple pour héberger une app Python/Flask avec une base persistante.

---

## Étape 1 — Préparer GitHub

1. Crée un dépôt **privé** sur [github.com](https://github.com) (ex : `jobfinder`)
2. Pousse le code :

```bash
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/TON_USERNAME/jobfinder.git
git push -u origin main
```

> ⚠️ Le `.gitignore` est déjà configuré — il exclut `data/`, `cv_adaptes/`, `.env`, `*.db`, `.claude/settings.local.json` etc.

---

## Étape 2 — Déployer sur Railway

1. [railway.app](https://railway.app) → connecte-toi avec GitHub
2. **New Project** → **Deploy from GitHub repo** → sélectionne ton repo
3. Railway détecte Python et lance le build (3-5 min, Playwright est long à installer)

---

## Étape 3 — Variables d'environnement (OBLIGATOIRES)

Dans Railway → projet → **Variables**, ajoute :

| Variable | Obligatoire | Valeur |
|----------|-------------|--------|
| `SECRET_KEY` | ✅ **OUI** | Génère avec : `python -c "import secrets; print(secrets.token_hex(32))"` |
| `OPENAI_API_KEY` | recommandé | `sk-proj-...` (sinon la récup d'URL d'offre est désactivée) |
| `ADMIN_EMAIL` | recommandé | Ton email — sera auto-promu admin à l'inscription |
| `FLASK_ENV` | optionnel | `production` (déjà détecté par `RAILWAY_ENVIRONMENT`) |
| `LOG_LEVEL` | optionnel | `INFO` (par défaut) — `DEBUG` si tu veux plus de logs |

> 🔑 **`SECRET_KEY` doit être stable**. Si tu la changes, toutes les sessions sont invalidées.
> Le serveur **refuse de démarrer** en prod sans `SECRET_KEY`.

---

## Étape 4 — Volumes persistants (IMPORTANT)

Sans volume, Railway **efface les données** à chaque redéploiement.

Dans Railway → projet → **Volumes** → **New Volume** :

| Mount Path | Description |
|-----------|-------------|
| `/app/data` | Base de données SQLite |
| `/app/cv_adaptes` | CV générés (organisés par user_id) |

---

## Étape 5 — Vérifier le déploiement

1. **Deployments** → attends ✅ (3-5 min)
2. Ouvre le lien (ex : `jobfinder-production.up.railway.app`)
3. Test du health check : `https://ton-url/healthz` → doit renvoyer `{"ok":true,...}`
4. Crée le compte admin avec l'email défini dans `ADMIN_EMAIL`

---

## 🛡️ Sécurité — checklist avant ouverture publique

- [ ] `SECRET_KEY` défini, stable, ≥ 32 caractères
- [ ] `ADMIN_EMAIL` défini (sinon le 1er user inscrit serait admin par défaut — c'est un raccourci dev, désactivé en prod)
- [ ] HTTPS actif (Railway le fait automatiquement)
- [ ] `OPENAI_API_KEY` avec une **limite de dépenses** sur https://platform.openai.com/account/billing/limits (le rate limit applicatif protège, mais ceinture + bretelles)
- [ ] Volumes montés sur `/app/data` et `/app/cv_adaptes`
- [ ] Backup de la DB SQLite (Railway propose des snapshots de volume)

### Protections en place côté code
- Rate limiting : login (20/5min/IP), registration (5/h/IP), endpoints IA (20/h/user), download PDF (30/h/user)
- Lockout : 10 échecs login en 15 min → 429 pour 15 min
- Cookies session : `HttpOnly` + `Secure` + `SameSite=Strict` en prod
- Headers : `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`, HSTS en prod
- Upload max 12 Mo (Flask `MAX_CONTENT_LENGTH`)
- Stockage CV par user_id (pas de fuite inter-comptes via `/api/cv-file/...`)
- Validation email regex + password ≥ 8 caractères

---

## Problèmes fréquents

**❌ Build échoue sur Playwright**
→ `nixpacks.toml` est déjà configuré. Si ça échoue encore, passe en Dockerfile (voir plus bas).

**❌ "RuntimeError: SECRET_KEY est obligatoire en production" au démarrage**
→ Définis la variable `SECRET_KEY` dans Railway → Variables.

**❌ Sessions déconnectées à chaque redéploiement**
→ `SECRET_KEY` change. Vérifie qu'elle est bien définie en variable d'env (et pas générée à chaque fois).

**❌ CV qui disparaissent**
→ Volumes pas configurés (Étape 4).

**❌ Trop de "429 Trop de requêtes"**
→ C'est le rate limiting. Si légitime, augmente les limites dans `jobfinder.py` (constantes en haut).

---

## Alternative : Dockerfile (si nixpacks ne marche pas)

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

> ⚠️ Garde `--workers 1`. Le rate limiting est en mémoire — avec plusieurs workers, chaque worker aurait son propre compteur (donc multiplication des limites). Pour passer à 2+ workers, il faut un store partagé (Redis).
