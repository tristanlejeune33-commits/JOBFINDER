# JobFinder — Guide de déploiement Railway (PostgreSQL)

Guide pas-à-pas pour mettre JobFinder en ligne avec une vraie base PostgreSQL et le reset de mot de passe par email.

---

## Étape 0 — Préparer GitHub

Pousse le projet sur un repo GitHub privé :

```bash
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/TON_USER/jobfinder.git
git push -u origin main
```

Le `.gitignore` est déjà configuré pour exclure `data/`, `cv_adaptes/`, `.env`, `__pycache__/`, etc.

---

## Étape 1 — Créer le projet Railway

1. Va sur [railway.app](https://railway.app), connecte-toi avec GitHub.
2. **New Project** → **Deploy from GitHub repo** → choisis `jobfinder`.
3. Railway détecte Python via `nixpacks.toml` et lance le build. Ne t'inquiète pas si ça échoue à ce stade, il manque encore des choses.

---

## Étape 2 — Ajouter le plugin PostgreSQL

1. Dans le projet Railway, clique **New** → **Database** → **Add PostgreSQL**.
2. Railway crée un service Postgres et injecte automatiquement la variable `DATABASE_URL` dans ton service web. Tu n'as rien à copier-coller.
3. Sur le service Postgres → onglet **Variables** tu peux vérifier que `DATABASE_URL` existe. Sur ton service web (JobFinder) → onglet **Variables** tu vois un lien `DATABASE_URL` partagé.

---

## Étape 3 — Créer le schéma

Deux options, choisis la plus simple.

**Option A — Interface Railway (le plus rapide)**

1. Clique sur le service Postgres → onglet **Data** → **Query**.
2. Ouvre le fichier `schema.sql` de ce repo.
3. Copie tout son contenu, colle-le dans la fenêtre Query et clique **Run**.
4. Tu devrais voir les tables `users`, `applications`, `interview_stages`, `user_data`, `cv_templates`, `cv_adaptes`, `password_reset_tokens` apparaître dans l'onglet **Data**.

**Option B — psql en local**

```bash
# Récupère l'URL publique depuis Railway → service Postgres → Variables → DATABASE_PUBLIC_URL
psql "$DATABASE_PUBLIC_URL" -f schema.sql
```

---

## Étape 4 — Variables d'environnement du service web

Dans Railway → service `jobfinder` → onglet **Variables**, ajoute :

| Variable | Valeur | Obligatoire |
|----------|--------|-------------|
| `SECRET_KEY` | Généré avec `python -c "import secrets; print(secrets.token_hex(32))"` | Oui |
| `OPENAI_API_KEY` | `sk-proj-...` ta clé OpenAI | Oui (fallback IA) |
| `APP_BASE_URL` | `https://<ton-app>.up.railway.app` (onglet Settings → Domains) | Oui (liens reset) |
| `SMTP_HOST` | `smtp.gmail.com` (ou ton fournisseur) | Si reset password |
| `SMTP_PORT` | `587` | Si reset password |
| `SMTP_USER` | Ton adresse d'envoi | Si reset password |
| `SMTP_PASS` | Mot de passe d'application Gmail ou clé API | Si reset password |
| `SMTP_FROM` | Adresse affichée en expéditeur (souvent = SMTP_USER) | Si reset password |

`DATABASE_URL` est injectée automatiquement par le plugin Postgres → ne la définis pas à la main.

### Configurer Gmail pour l'envoi

1. Compte Google → Sécurité → Active la validation en 2 étapes.
2. [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) → génère un mot de passe d'application pour "Mail".
3. Utilise ce mot de passe de 16 caractères dans `SMTP_PASS`.

Alternatives sans Gmail : [Resend](https://resend.com) (3000 emails gratuits/mois), [SendGrid](https://sendgrid.com), [Brevo](https://www.brevo.com).

---

## Étape 5 — Vérifier le déploiement

1. Onglet **Deployments** → attends que le build passe en vert (3–5 min, Playwright est long).
2. Onglet **Settings** → **Networking** → **Generate Domain** si ce n'est pas fait.
3. Ouvre l'URL. Tu dois voir l'écran de connexion.
4. Crée un compte → le premier devient automatiquement `admin`.
5. Teste le reset password : clique "mot de passe oublié", vérifie que l'email arrive.

---

## Étape 6 — Migrer ta base SQLite locale (optionnel)

Si tu veux récupérer tes utilisateurs / candidatures / CV créés en local :

1. Récupère l'URL publique Postgres : Railway → service Postgres → onglet **Variables** → `DATABASE_PUBLIC_URL`.
2. En local, dans le dossier du projet :

```bash
# Linux / Mac
export DATABASE_URL="postgresql://postgres:xxx@containers-us-west-X.railway.app:PORT/railway"

# Windows PowerShell
$env:DATABASE_URL="postgresql://postgres:xxx@containers-us-west-X.railway.app:PORT/railway"

python migrate_to_postgres.py --dry-run   # simule
python migrate_to_postgres.py             # applique
```

Le script respecte les contraintes UNIQUE (emails déjà présents sont ignorés), importe tes candidatures, étapes, templates CV, et les fichiers HTML de `cv_adaptes/` sont poussés dans la table `cv_adaptes` (rattachés à ton compte admin).

---

## Étape 7 — Volumes (désormais optionnel)

Avec la nouvelle architecture, **tous les CV adaptés sont stockés en PostgreSQL**. Tu n'as **plus besoin** de monter un volume `/app/cv_adaptes`. Les CV survivent aux redéploiements.

Si tu veux quand même un volume (utile pour Playwright qui génère des PDFs à la volée), ajoute :

| Mount Path | Rôle |
|-----------|------|
| `/app/cv_adaptes` | Cache disque pour Playwright (non critique, reconstructible depuis la DB) |

`/app/data` n'est plus nécessaire non plus (SQLite local n'est utilisé qu'en dev).

---

## Problèmes fréquents

**Build échoue sur Playwright** — Passe en builder Dockerfile :

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

**Erreur `relation "users" does not exist`** — Tu as oublié l'étape 3 (exécuter `schema.sql`).

**Internal Server Error au lancement** — Regarde les logs dans l'onglet **Deployments**. Souvent : `SECRET_KEY` ou `DATABASE_URL` manquant.

**Email reset password jamais reçu** — Vérifie les logs : si `[EMAIL-DEV]` apparaît c'est que les variables SMTP ne sont pas vues par l'app. Avec Gmail, assure-toi d'utiliser le mot de passe d'application et non ton mot de passe Google classique.

**Session perdue à chaque refresh** — `SECRET_KEY` change à chaque restart. Définis-la en variable d'environnement (Étape 4).

**Conflit id après migration** — Le script de migration réutilise les IDs Postgres (ils ne sont pas alignés sur SQLite). Les FK sont remappées correctement via `id_map_users` et `id_map_apps`, donc pas de casse.

---

## Récap des fichiers créés / mis à jour

| Fichier | Rôle |
|---------|------|
| `schema.sql` | Schéma PostgreSQL natif complet (idempotent) |
| `migrate_to_postgres.py` | Import des données SQLite locales vers Postgres |
| `jobfinder.py` | Backend : nouvelles tables, routes reset password, stockage CV en DB |
| `.env.example` | Modèle des variables (DB, SMTP, APP_BASE_URL) |
| `nixpacks.toml` | Config Railway Playwright/Chromium |
| `Procfile` | Commande de démarrage gunicorn |
| `requirements.txt` | Deps (inchangé — psycopg2-binary déjà présent) |

---

## Checklist finale avant d'annoncer l'app

- [ ] `SECRET_KEY` définie en variable d'env (stable entre restarts)
- [ ] `DATABASE_URL` fournie par le plugin Postgres
- [ ] `schema.sql` exécuté, les 7 tables visibles dans l'onglet Data
- [ ] Création de compte fonctionne → le premier user est admin
- [ ] Login / logout fonctionnent
- [ ] Ajouter une candidature → la voir dans le dashboard
- [ ] Adapter un CV → le filename apparaît, téléchargement PDF fonctionne
- [ ] Mot de passe oublié → email arrive → nouveau mot de passe accepté
- [ ] Après un redéploiement Railway → les données et CV sont toujours là
