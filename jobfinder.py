#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JobFinder — Backend Flask + IA + Auth + SQLite"""

from flask import Flask, jsonify, request, send_file, Response, session
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from collections import defaultdict
import sqlite3, json, os, re, datetime, threading, base64, secrets, logging, time
import requests as http_req
from bs4 import BeautifulSoup

# ── Logging ─────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("jobfinder")

# ── Environnement ───────────────────────────────────────────────────────────────
IS_PROD = bool(
    os.environ.get("RAILWAY_ENVIRONMENT")
    or os.environ.get("DYNO")
    or os.environ.get("FLASK_ENV") == "production"
)

# ── Cloudscraper ────────────────────────────────────────────────────────────────
try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper(browser={"browser":"chrome","platform":"windows","mobile":False})
    def web_get(url, **kwargs): return _scraper.get(url, **kwargs)
    def web_session():
        return cloudscraper.create_scraper(browser={"browser":"chrome","platform":"windows","mobile":False})
except ImportError:
    def web_get(url, **kwargs): return http_req.get(url, **kwargs)
    def web_session(): return http_req.Session()

# ── Secrets / clés API ──────────────────────────────────────────────────────────
OPENAI_API_KEY    = os.environ.get("OPENAI_API_KEY", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
if IS_PROD and not (OPENAI_API_KEY or ANTHROPIC_API_KEY):
    log.warning("Aucune clé IA (OPENAI_API_KEY / ANTHROPIC_API_KEY) — les endpoints IA renverront 503.")

SECRET_KEY = os.environ.get("SECRET_KEY", "").strip()
if IS_PROD and not SECRET_KEY:
    raise RuntimeError(
        "SECRET_KEY est obligatoire en production. "
        "Génère-en une avec: python -c \"import secrets; print(secrets.token_hex(32))\""
    )
if not SECRET_KEY:
    SECRET_KEY = secrets.token_hex(32)
    log.warning("SECRET_KEY auto-généré (dev only) — sessions invalidées au redémarrage.")

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()  # auto-promu admin si défini

# ── Chemins ─────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.path.join(BASE_DIR, "data")
CV_DIR      = os.path.join(BASE_DIR, "cv_adaptes")
DB_PATH     = os.path.join(DATA_DIR, "jobfinder.db")
TEMPLATE_CV = os.path.join(BASE_DIR, "cv_vibe_modern.html")
for d in [DATA_DIR, CV_DIR]:
    os.makedirs(d, exist_ok=True)

def user_cv_dir(user_id):
    """Dossier CV par utilisateur (évite collisions et fuites entre comptes)."""
    p = os.path.join(CV_DIR, str(int(user_id)))
    os.makedirs(p, exist_ok=True)
    return p

# ── Flask app + durcissement session ────────────────────────────────────────────
app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")
app.secret_key = SECRET_KEY
app.config.update(
    MAX_CONTENT_LENGTH=12 * 1024 * 1024,            # 12 Mo max par requête
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict" if IS_PROD else "Lax",
    SESSION_COOKIE_SECURE=IS_PROD,                  # HTTPS only en prod
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30),
    JSON_SORT_KEYS=False,
)

# ── Validations ─────────────────────────────────────────────────────────────────
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
MIN_PASSWORD_LEN = 8

def valid_email(s):
    return bool(s) and len(s) <= 254 and bool(EMAIL_RE.match(s))

def valid_password(s):
    return isinstance(s, str) and MIN_PASSWORD_LEN <= len(s) <= 256

# ── Rate limiting (in-memory ; OK pour single-worker Railway) ───────────────────
_rate_buckets = defaultdict(list)   # key -> [timestamps]
_rl_lock      = threading.Lock()

def _rate_check(key, max_calls, window_sec):
    """True si OK, False si dépassé. Nettoie les anciens timestamps."""
    now = time.time()
    with _rl_lock:
        bucket = _rate_buckets[key]
        cutoff = now - window_sec
        # purge les vieux
        i = 0
        for i, t in enumerate(bucket):
            if t >= cutoff:
                break
        else:
            i = len(bucket)
        if i:
            del bucket[:i]
        if len(bucket) >= max_calls:
            return False
        bucket.append(now)
        return True

def rate_limit(max_calls, window_sec, key_fn):
    """Décorateur générique. key_fn(request) -> str."""
    def deco(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            key = key_fn()
            if not _rate_check(key, max_calls, window_sec):
                log.warning(f"rate-limit hit: {key}")
                return jsonify({"error": "Trop de requêtes — réessaye plus tard."}), 429
            return f(*args, **kwargs)
        return wrapper
    return deco

def _client_ip():
    # Railway / proxies → X-Forwarded-For (premier IP)
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "unknown"

# ── Headers de sécurité ─────────────────────────────────────────────────────────
# CSP: pas d'inline script désactivé (l'UI a du JS inline historique) —
# on durcit le reste. À durcir davantage quand le JS sera externalisé.
CSP_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com https://hcaptcha.com https://*.hcaptcha.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://hcaptcha.com https://*.hcaptcha.com; "
    "font-src 'self' data: https://fonts.gstatic.com; "
    "img-src 'self' data: blob: https:; "
    "frame-src 'self' blob: https://hcaptcha.com https://*.hcaptcha.com; "
    "connect-src 'self' https://hcaptcha.com https://*.hcaptcha.com; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self';"
)

@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    resp.headers.setdefault("Content-Security-Policy", CSP_POLICY)
    if IS_PROD:
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return resp

# ── Health check (Railway / monitoring) ─────────────────────────────────────────
@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "ts": int(time.time())})

# ── Diagnostic (debug, accès libre — utilisé pour debug prod) ──────────────────
@app.route("/api/_diag")
def route_diag():
    """Diagnostic public : check DB connection + tables. Utilisé pour debug."""
    out = {
        "db_backend":      "postgres" if USE_POSTGRES else "sqlite",
        "is_prod":         IS_PROD,
        "smtp":            bool(SMTP_HOST),
        "hcaptcha_secret": bool(HCAPTCHA_SECRET),
        "hcaptcha_key":    bool(HCAPTCHA_SITE_KEY),
        "hcaptcha_active": HCAPTCHA_ENABLED,
        "openai":          bool(OPENAI_API_KEY),
        "anthropic":       bool(ANTHROPIC_API_KEY),
        "tables":          None,
        "user_count":      None,
        "error":           None,
    }
    try:
        with get_db() as db:
            if USE_POSTGRES:
                rows = db.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name"
                ).fetchall()
                out["tables"] = [r["table_name"] for r in rows]
            else:
                rows = db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
                out["tables"] = [r["name"] for r in rows]
            row = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()
            out["user_count"] = (row.get("c") if isinstance(row, dict) else row["c"]) if row else 0
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    return jsonify(out)

# ── Sentry (monitoring d'erreurs, no-op si SENTRY_DSN absent) ───────────────────
SENTRY_DSN = os.environ.get("SENTRY_DSN", "").strip()
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        sentry_sdk.init(
            dsn=SENTRY_DSN,
            integrations=[FlaskIntegration()],
            traces_sample_rate=0.1,
            send_default_pii=False,
            environment="production" if IS_PROD else "dev",
        )
        log.info("Sentry initialisé.")
    except Exception as e:
        log.warning(f"Sentry init failed: {e}")

# ── hCaptcha (anti-bot register, no-op si non-configuré OU configuration partielle) ─
HCAPTCHA_SECRET   = os.environ.get("HCAPTCHA_SECRET", "").strip()
HCAPTCHA_SITE_KEY = os.environ.get("HCAPTCHA_SITE_KEY", "").strip()
# On exige que LES DEUX soient set, sinon on désactive (config incomplète = foot-gun)
HCAPTCHA_ENABLED  = bool(HCAPTCHA_SECRET and HCAPTCHA_SITE_KEY)
if HCAPTCHA_SECRET and not HCAPTCHA_SITE_KEY:
    log.warning("HCAPTCHA_SECRET défini mais HCAPTCHA_SITE_KEY manquant → captcha désactivé.")
elif HCAPTCHA_SITE_KEY and not HCAPTCHA_SECRET:
    log.warning("HCAPTCHA_SITE_KEY défini mais HCAPTCHA_SECRET manquant → captcha désactivé.")

def verify_hcaptcha(token, ip):
    """True si OK, False sinon. True si captcha désactivé (no-op)."""
    if not HCAPTCHA_ENABLED:
        return True
    if not token:
        return False
    try:
        r = http_req.post(
            "https://api.hcaptcha.com/siteverify",
            data={"secret": HCAPTCHA_SECRET, "response": token, "remoteip": ip},
            timeout=10,
        )
        return bool(r.json().get("success"))
    except Exception as e:
        log.warning(f"hCaptcha network error: {e}")
        return True  # fail-open

# ── Email (SMTP, no-op si non configuré → log le lien à la place) ─────────────
SMTP_HOST     = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "").strip()
SMTP_PASS     = os.environ.get("SMTP_PASS", "")
SMTP_FROM     = os.environ.get("SMTP_FROM", SMTP_USER).strip()
APP_URL       = os.environ.get("APP_URL", "").strip().rstrip("/")  # https://jobfinder-...up.railway.app

def app_url():
    if APP_URL: return APP_URL
    return request.url_root.rstrip("/") if request else "http://localhost:5151"

def send_email(to, subject, body_text, body_html=None):
    """Envoie un email. Si SMTP non configuré, log le contenu (mode dev)."""
    if not SMTP_HOST:
        log.info(f"[EMAIL DEV] To: {to} | Subject: {subject}\n{body_text}")
        return True
    try:
        import smtplib
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = SMTP_FROM or SMTP_USER
        msg["To"]      = to
        msg.set_content(body_text)
        if body_html:
            msg.add_alternative(body_html, subtype="html")
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        log.info(f"email sent to {to} subject={subject!r}")
        return True
    except Exception as e:
        log.error(f"send_email failed: {e}")
        return False

# ── Statuts de candidature (whitelist côté serveur, anti-XSS et data hygiene) ──
ALLOWED_STATUSES = {
    "Envoyée", "Refusée", "Entretien", "Proposition", "Acceptée", "Pas de réponse",
    "En attente", "Annulée"
}
ALLOWED_STAGE_RESULTS = {"En attente", "Réussi", "Échoué", "Annulé"}

def sanitize_status(s, default="Envoyée"):
    return s if s in ALLOWED_STATUSES else default

def sanitize_stage_result(s, default="En attente"):
    return s if s in ALLOWED_STAGE_RESULTS else default

# ── Quota mensuel par user (compte les appels IA) ───────────────────────────────
DEFAULT_MONTHLY_AI_QUOTA = int(os.environ.get("MONTHLY_AI_QUOTA", "200"))

def _ym():
    return datetime.datetime.now().strftime("%Y-%m")

def check_and_increment_quota(user_id):
    """True si OK + incrémente. False si quota atteint."""
    ym = _ym()
    with get_db() as db:
        row = db.execute(
            "SELECT count FROM usage_quotas WHERE user_id=? AND ym=?", (user_id, ym)
        ).fetchone()
        used = row["count"] if row else 0
        # Override par user (table users.monthly_quota) si défini
        u = db.execute("SELECT monthly_quota FROM users WHERE id=?", (user_id,)).fetchone()
        quota = (u["monthly_quota"] if u and u["monthly_quota"] else DEFAULT_MONTHLY_AI_QUOTA)
        if used >= quota:
            return False, used, quota
        if row:
            db.execute("UPDATE usage_quotas SET count=count+1 WHERE user_id=? AND ym=?", (user_id, ym))
        else:
            db.execute("INSERT INTO usage_quotas(user_id,ym,count) VALUES(?,?,?)", (user_id, ym, 1))
        db.commit()
    return True, used + 1, quota

def get_quota_status(user_id):
    ym = _ym()
    with get_db() as db:
        row = db.execute(
            "SELECT count FROM usage_quotas WHERE user_id=? AND ym=?", (user_id, ym)
        ).fetchone()
        u = db.execute("SELECT monthly_quota FROM users WHERE id=?", (user_id,)).fetchone()
    used  = row["count"] if row else 0
    quota = (u["monthly_quota"] if u and u["monthly_quota"] else DEFAULT_MONTHLY_AI_QUOTA)
    return {"used": used, "quota": quota, "remaining": max(0, quota - used), "ym": ym}

def require_quota(f):
    """Decorator: vérifie + incrémente le quota IA. À mettre APRÈS @require_auth."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        u = get_current_user()
        if not u:
            return jsonify({"error": "Non authentifié"}), 401
        ok, used, quota = check_and_increment_quota(u["id"])
        if not ok:
            return jsonify({
                "error": f"Quota mensuel atteint ({used}/{quota}). Recommence le mois prochain.",
                "quota_exceeded": True, "used": used, "quota": quota
            }), 429
        return f(*args, **kwargs)
    return wrapper

# ── Tokens (email verify + password reset) ──────────────────────────────────────
def gen_token():
    return secrets.token_urlsafe(32)

def create_email_token(user_id, kind, ttl_hours=24):
    """kind ∈ {'verify_email', 'reset_password'}"""
    token = gen_token()
    expires = int(time.time()) + ttl_hours * 3600
    with get_db() as db:
        db.execute("DELETE FROM email_tokens WHERE user_id=? AND kind=?", (user_id, kind))
        db.execute("INSERT INTO email_tokens(user_id,kind,token,expires_at) VALUES(?,?,?,?)",
                   (user_id, kind, token, expires))
        db.commit()
    return token

def consume_email_token(token, kind, max_age_hours=24):
    """Retourne user_id si OK, None sinon. Le token est consommé."""
    with get_db() as db:
        row = db.execute(
            "SELECT user_id, expires_at FROM email_tokens WHERE token=? AND kind=?",
            (token, kind)
        ).fetchone()
        if not row: return None
        if row["expires_at"] < int(time.time()): return None
        db.execute("DELETE FROM email_tokens WHERE token=?", (token,))
        db.commit()
        return row["user_id"]

# ── Base de données ─────────────────────────────────────────────────────────────
# Support transparent SQLite (dev) / PostgreSQL (prod via DATABASE_URL).
# Le reste du code utilise la même API : `with get_db() as db: db.execute(...)`.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
    log.info("Backend DB : PostgreSQL (DATABASE_URL détecté)")
else:
    log.info(f"Backend DB : SQLite ({DB_PATH})")

# Traductions SQLite → Postgres (idempotentes pour SQLite)
_LASTROWID_MARKER = "__LASTROWID__"

def _translate_sql(sql):
    """Traduit du SQLite-flavored SQL en Postgres."""
    s = sql
    # Placeholders ? → %s
    s = s.replace("?", "%s")
    # AUTOINCREMENT
    s = s.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    # Functions de date
    s = re.sub(r"datetime\('now'\)", "(now() at time zone 'utc')::text", s)
    s = re.sub(r"date\('now'\)", "current_date::text", s)
    s = re.sub(r"datetime\('now','-(\d+)\s*days'\)", r"((now() at time zone 'utc') - interval '\1 days')::text", s)
    # INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
    if re.search(r"\bINSERT\s+OR\s+IGNORE\b", s, re.IGNORECASE):
        s = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT", s, flags=re.IGNORECASE)
        if "ON CONFLICT" not in s.upper():
            s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    # Strip PRAGMA (Postgres ne connaît pas)
    s = re.sub(r"PRAGMA[^;]+;?", "", s, flags=re.IGNORECASE)
    return s


class _PgCursor:
    """Wrapper minimaliste : émule l'API SQLite (.execute/.fetchone/.fetchall/.lastrowid)."""
    def __init__(self, raw):
        self._cur = raw
        self.lastrowid = None
    def fetchone(self):
        row = self._cur.fetchone()
        return row  # déjà un dict-like (RealDictRow)
    def fetchall(self):
        return self._cur.fetchall()
    def __iter__(self):
        return iter(self._cur)
    def close(self):
        self._cur.close()


class _PgConn:
    """Wrapper qui présente l'API SQLite tout en parlant à Postgres."""
    def __init__(self):
        self._conn = psycopg2.connect(DATABASE_URL)
        self._conn.autocommit = False

    def execute(self, sql, params=()):
        translated = _translate_sql(sql)
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        wrap = _PgCursor(cur)
        try:
            # Auto-RETURNING * sur les INSERT pour exposer lastrowid quand "id" existe.
            # On utilise * et pas "id" car certaines tables (user_data, usage_quotas)
            # n'ont pas de colonne id et provoqueraient une erreur.
            is_insert = bool(re.match(r"\s*INSERT\s+INTO\s+(\w+)", translated, re.IGNORECASE))
            if is_insert and "RETURNING" not in translated.upper():
                translated = translated.rstrip().rstrip(";") + " RETURNING *"
                cur.execute(translated, params or None)
                try:
                    row = cur.fetchone()
                    if row and isinstance(row, dict) and "id" in row:
                        wrap.lastrowid = row["id"]
                except psycopg2.ProgrammingError:
                    pass  # pas de result set (ON CONFLICT DO NOTHING + 0 row)
            else:
                cur.execute(translated, params or None)
        except Exception:
            self._conn.rollback()
            raise
        return wrap

    def executescript(self, script):
        # Postgres : on découpe sur ; et on exécute chaque statement
        cur = self._conn.cursor()
        try:
            for stmt in re.split(r";\s*\n", script):
                stmt = stmt.strip()
                if not stmt: continue
                cur.execute(_translate_sql(stmt))
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            try: self._conn.commit()
            except Exception: pass
        else:
            try: self._conn.rollback()
            except Exception: pass
        self.close()


def get_db():
    if USE_POSTGRES:
        return _PgConn()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Erreur d'index/colonne — pour les migrations idempotentes
def _is_dup_column_err(e):
    msg = str(e).lower()
    return ("duplicate column" in msg) or ("already exists" in msg) or ("operationalerror" in msg)

def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            email         TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name          TEXT DEFAULT '',
            role          TEXT DEFAULT 'membre',
            created_at    TEXT DEFAULT (datetime('now')),
            api_key_claude  TEXT DEFAULT '',
            api_key_openai  TEXT DEFAULT '',
            ai_provider     TEXT DEFAULT 'Claude (Anthropic)'
        );

        CREATE TABLE IF NOT EXISTS applications (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            company       TEXT DEFAULT '',
            role_name     TEXT DEFAULT '',
            job_desc      TEXT DEFAULT '',
            status        TEXT DEFAULT 'Envoyée',
            cv_filename   TEXT DEFAULT '',
            notes         TEXT DEFAULT '',
            url           TEXT DEFAULT '',
            applied_date  TEXT DEFAULT (date('now')),
            created_at    TEXT DEFAULT (datetime('now')),
            interview_prep TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS interview_stages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            application_id  INTEGER NOT NULL,
            user_id         INTEGER NOT NULL,
            stage_type      TEXT DEFAULT 'Entretien',
            scheduled_date  TEXT,
            notes           TEXT DEFAULT '',
            result          TEXT DEFAULT 'En attente',
            created_at      TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS user_data (
            user_id    INTEGER PRIMARY KEY,
            doc_text   TEXT DEFAULT '',
            doc_names  TEXT DEFAULT '[]',
            summary    TEXT DEFAULT '',
            photo_b64  TEXT DEFAULT '',
            photo_mime TEXT DEFAULT 'image/jpeg',
            cv_html    TEXT DEFAULT '',
            cv_name    TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS cv_templates (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            name         TEXT DEFAULT 'Template',
            style        TEXT DEFAULT 'Moderne',
            color        TEXT DEFAULT '#7c3aed',
            html_content TEXT DEFAULT '',
            created_at   TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS email_tokens (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            kind        TEXT NOT NULL,           -- verify_email | reset_password
            token       TEXT UNIQUE NOT NULL,
            expires_at  INTEGER NOT NULL,         -- unix ts
            created_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_email_tokens_token ON email_tokens(token);

        CREATE TABLE IF NOT EXISTS usage_quotas (
            user_id   INTEGER NOT NULL,
            ym        TEXT NOT NULL,              -- "2026-04"
            count     INTEGER DEFAULT 0,
            PRIMARY KEY (user_id, ym),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """)

try:
    init_db()
except Exception as e:
    log.error(f"init_db FAILED at boot — app continuera quand même : {e}")

# Migrations idempotentes
def _migrate_columns():
    cols = [
        ("user_data",        "cv_pdf_b64",     "TEXT DEFAULT ''"),
        ("user_data",        "cv_pdf_name",    "TEXT DEFAULT ''"),
        ("user_data",        "cv_pdf_path",    "TEXT DEFAULT ''"),    # disque (remplace b64)
        ("users",            "email_verified", "INTEGER DEFAULT 0"),
        ("users",            "monthly_quota",  "INTEGER DEFAULT 0"),
        ("users",            "deleted_at",     "TEXT DEFAULT ''"),
        ("interview_stages", "reminder_sent",  "INTEGER DEFAULT 0"),
    ]
    for table, col, ddl in cols:
        # Postgres ne tolère pas un ALTER qui rate dans une transaction → 1 conn par essai
        try:
            with get_db() as db:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                db.commit()
        except Exception as e:
            if not _is_dup_column_err(e):
                # Vraie erreur (ex: table absente) — log mais on continue
                log.warning(f"migrate {table}.{col}: {e}")
try:
    _migrate_columns()
except Exception as e:
    log.error(f"_migrate_columns FAILED at boot — continuing : {e}")

# ── Helpers SQL ─────────────────────────────────────────────────────────────────
def row_to_dict(row):
    return dict(row) if row else None

def app_row_to_dict(row):
    d = dict(row)
    d["id"] = str(d["id"])
    d["role"] = d.pop("role_name", "")
    d.setdefault("date", d.get("applied_date", ""))
    return d

def get_user_data(user_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM user_data WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            db.execute("INSERT OR IGNORE INTO user_data(user_id) VALUES(?)", (user_id,))
            db.commit()
            row = db.execute("SELECT * FROM user_data WHERE user_id=?", (user_id,)).fetchone()
        return dict(row)

# ── Auth helpers ────────────────────────────────────────────────────────────────
def get_current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return row_to_dict(row)

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not get_current_user():
            return jsonify({"error": "Non authentifié", "auth_required": True}), 401
        return f(*args, **kwargs)
    return decorated

def require_role(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            u = get_current_user()
            if not u:
                return jsonify({"error": "Non authentifié", "auth_required": True}), 401
            if u["role"] not in roles:
                return jsonify({"error": "Accès refusé — rôle insuffisant"}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

# ── IA helpers ──────────────────────────────────────────────────────────────────
CLAUDE_MAX_TOKENS = 8000
OPENAI_MAX_TOKENS = 16000

def call_ai(provider, api_key, prompt, max_tokens=8000):
    if provider == "OpenAI (ChatGPT)":
        import openai
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o", max_tokens=min(max_tokens, OPENAI_MAX_TOKENS),
            messages=[{"role": "user", "content": prompt}]
        )
        return resp.choices[0].message.content
    else:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-opus-4-6", max_tokens=min(max_tokens, CLAUDE_MAX_TOKENS),
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text

def get_ai_keys(user=None):
    """Renvoie (provider, api_key) à partir des clés globales (env).
    Priorité Anthropic si présent, sinon OpenAI."""
    if ANTHROPIC_API_KEY:
        return "Claude (Anthropic)", ANTHROPIC_API_KEY
    return "OpenAI (ChatGPT)", OPENAI_API_KEY

def get_docs_context(user_id):
    ud = get_user_data(user_id)
    parts = []
    if ud.get("summary","").strip():
        parts.append("=== RÉSUMÉ PERSONNEL (priorité haute) ===\n" + ud["summary"].strip())
    if ud.get("doc_text","").strip():
        parts.append("=== DOCUMENTS IMPORTÉS (anciens CV, etc.) ===\n" + ud["doc_text"].strip())
    return "\n\n".join(parts)

# ── Utils ────────────────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
}

def today():
    return datetime.datetime.now().strftime("%Y-%m-%d")

def safe_name(s):
    return re.sub(r"[^\w\-_]", "_", s or "unknown")

def search_indeed(query, location="France", nb=15):
    """Scraping Indeed (fragile : leur HTML change tous les 6 mois,
    ils blacklistent les IPs scraper). Fallback uniquement."""
    url = f"https://fr.indeed.com/jobs?q={http_req.utils.quote(query)}&l={http_req.utils.quote(location)}&lang=fr"
    try:
        sess = web_session()
        sess.get("https://fr.indeed.com", headers=HEADERS, timeout=10)
        r = sess.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, "html.parser")
        cards = []
        for sel in [".job_seen_beacon", "[data-testid='slider_item']", ".tapItem"]:
            cards = soup.select(sel)
            if cards: break
        jobs = []
        for card in cards[:nb]:
            title_el   = card.select_one("[data-testid='jobTitle'] span") or card.select_one(".jobTitle span")
            company_el = card.select_one("[data-testid='company-name']")
            loc_el     = card.select_one("[data-testid='text-location']")
            link_el    = card.select_one("a[data-jk], a.jcs-JobTitle, h2 a")
            date_el    = card.select_one("[data-testid='myJobsStateDate']")
            title = title_el.get_text(strip=True) if title_el else None
            if not title: continue
            href = link_el.get("href","") if link_el else ""
            jobs.append({
                "title":   title,
                "company": company_el.get_text(strip=True) if company_el else "N/A",
                "location":loc_el.get_text(strip=True)     if loc_el     else "N/A",
                "url":     ("https://fr.indeed.com"+href) if href.startswith("/") else href,
                "date":    date_el.get_text(strip=True)   if date_el    else "",
                "description": "",
            })
        return jobs, None
    except Exception as e:
        return [], str(e)

# ── Adzuna API (fiable, gratuit jusqu'à 250 req/mois) ──────────────────────────
ADZUNA_APP_ID  = os.environ.get("ADZUNA_APP_ID", "").strip()
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY", "").strip()

def search_adzuna(query, location="France", nb=15):
    """Cherche via l'API Adzuna. Retourne (jobs, error)."""
    if not (ADZUNA_APP_ID and ADZUNA_APP_KEY):
        return None, "Adzuna non configuré"
    # Mapping pays→code Adzuna (ISO alpha-2 lowercase)
    country = "fr"  # JobFinder = focus FR par défaut
    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
    try:
        r = http_req.get(url, params={
            "app_id":  ADZUNA_APP_ID,
            "app_key": ADZUNA_APP_KEY,
            "what":    query,
            "where":   location,
            "results_per_page": min(nb, 50),
            "content-type": "application/json",
        }, timeout=15)
        if r.status_code != 200:
            return None, f"Adzuna {r.status_code}"
        data = r.json()
        jobs = []
        for item in data.get("results", [])[:nb]:
            jobs.append({
                "title":       (item.get("title") or "")[:200],
                "company":     (item.get("company") or {}).get("display_name", "N/A"),
                "location":    (item.get("location") or {}).get("display_name", "N/A"),
                "url":         item.get("redirect_url") or "",
                "date":        (item.get("created") or "")[:10],
                "description": (item.get("description") or "")[:1000],
            })
        return jobs, None
    except Exception as e:
        return None, str(e)

def search_jobs(query, location="France", nb=15):
    """Stratégie : Adzuna en priorité (API stable), Indeed en fallback (scraping fragile)."""
    if ADZUNA_APP_ID and ADZUNA_APP_KEY:
        jobs, err = search_adzuna(query, location, nb)
        if jobs is not None:
            return jobs, None, "adzuna"
        log.warning(f"Adzuna failed: {err} — fallback Indeed")
    jobs, err = search_indeed(query, location, nb)
    return jobs, err, "indeed"

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES AUTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "ui.html"))

REQUIRE_EMAIL_VERIFICATION = bool(os.environ.get("REQUIRE_EMAIL_VERIFICATION", "1") == "1" and SMTP_HOST)

@app.route("/api/auth/register", methods=["POST"])
@rate_limit(max_calls=5, window_sec=3600, key_fn=lambda: f"reg:{_client_ip()}")
def route_register():
    try:
        data  = request.json or {}
        email = (data.get("email","") or "").strip().lower()
        pwd   = (data.get("password","") or "")
        name  = (data.get("name","") or "").strip()[:80]
        captcha_token = (data.get("hcaptcha_token","") or "").strip()
        if not valid_email(email):
            return jsonify({"error": "Email invalide"}), 400
        if not valid_password(pwd):
            return jsonify({"error": f"Mot de passe trop faible (min {MIN_PASSWORD_LEN} caractères)"}), 400
        if not verify_hcaptcha(captcha_token, _client_ip()):
            return jsonify({"error": "Vérification anti-bot échouée. Réessaye."}), 400

        with get_db() as db:
            existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
            if existing:
                return jsonify({"error": "Email déjà utilisé"}), 400
            # Rôle
            if ADMIN_EMAIL and email == ADMIN_EMAIL:
                role = "admin"
            elif not IS_PROD:
                row = db.execute("SELECT COUNT(*) AS c FROM users").fetchone()
                # Postgres minuscule + RealDict → "c"; SQLite → row["c"]
                count = (row.get("c") if isinstance(row, dict) else row["c"]) if row else 0
                role  = "admin" if count == 0 else "membre"
            else:
                role = "membre"
            # Python bool : SQLite le stocke comme 0/1 (INTEGER), Postgres comme BOOLEAN
            verified = (role == "admin") or (not REQUIRE_EMAIL_VERIFICATION)
            cur = db.execute(
                "INSERT INTO users(email,password_hash,name,role,email_verified) VALUES(?,?,?,?,?)",
                (email, generate_password_hash(pwd), name or email.split("@")[0], role, verified)
            )
            new_user_id = cur.lastrowid
            db.commit()
            user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()

        if not user:
            log.error(f"register: user not found after insert for email={email}")
            return jsonify({"error": "Erreur serveur après création (DB ?)"}), 500

        # Helper d'accès tolérant dict/Row
        def g(r, k, default=""):
            try:
                v = r[k] if not isinstance(r, dict) else r.get(k, default)
                return v if v is not None else default
            except Exception:
                return default

        user_id = g(user, "id") or new_user_id

        # Email de vérification
        if REQUIRE_EMAIL_VERIFICATION and not verified:
            try:
                token = create_email_token(user_id, "verify_email", ttl_hours=48)
                verify_url = f"{app_url()}/api/auth/verify-email?token={token}"
                send_email(
                    email,
                    "Vérifie ton email — JobFinder",
                    f"Bienvenue sur JobFinder !\n\nClique sur ce lien pour activer ton compte :\n{verify_url}\n\nLien valide 48h.",
                    f"<p>Bienvenue sur JobFinder !</p><p><a href='{verify_url}'>Activer mon compte</a> (lien valide 48h)</p>",
                )
            except Exception as e:
                log.warning(f"register: send verification email failed: {e}")

        session.clear()
        session.permanent = True
        session["user_id"] = user_id
        log.info(f"register OK: id={user_id} email={email} role={role} verified={verified} ip={_client_ip()}")
        return jsonify({
            "ok": True,
            "needs_verification": bool(REQUIRE_EMAIL_VERIFICATION and not verified),
            "user": {
                "id":    user_id,
                "email": g(user, "email", email),
                "name":  g(user, "name", name),
                "role":  g(user, "role", role),
            }
        })
    except Exception as e:
        log.exception(f"register FAILED: {e}")
        return jsonify({"error": f"Erreur serveur : {type(e).__name__}: {str(e)[:200]}"}), 500


# Lockout login : 10 échecs / 15 min par IP+email
LOGIN_MAX_FAILS = 10
LOGIN_WINDOW   = 15 * 60

@app.route("/api/auth/login", methods=["POST"])
@rate_limit(max_calls=20, window_sec=300, key_fn=lambda: f"login-ip:{_client_ip()}")
def route_login():
    try:
        data  = request.json or {}
        email = (data.get("email","") or "").strip().lower()
        pwd   = (data.get("password","") or "")
        if not email or not pwd:
            return jsonify({"error": "Email et mot de passe requis"}), 400
        lockout_key = f"login-fail:{_client_ip()}:{email}"
        with _rl_lock:
            bucket = _rate_buckets[lockout_key]
            cutoff = time.time() - LOGIN_WINDOW
            bucket[:] = [t for t in bucket if t >= cutoff]
            if len(bucket) >= LOGIN_MAX_FAILS:
                log.warning(f"login locked: ip={_client_ip()} email={email}")
                return jsonify({"error": "Trop de tentatives. Réessaye dans 15 minutes."}), 429
        with get_db() as db:
            row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        ok = bool(row and check_password_hash(row["password_hash"], pwd))
        if not ok:
            with _rl_lock:
                _rate_buckets[lockout_key].append(time.time())
            log.info(f"login fail: ip={_client_ip()} email={email}")
            return jsonify({"error": "Email ou mot de passe incorrect"}), 401
        # Compte supprimé ? (peut être absent si vieille DB)
        deleted_at = row.get("deleted_at") if isinstance(row, dict) else (row["deleted_at"] if "deleted_at" in row.keys() else "")
        if deleted_at:
            return jsonify({"error": "Ce compte a été supprimé."}), 403

        # Auto-restore admin si email == ADMIN_EMAIL et le rôle a été changé par accident
        current_role = row["role"]
        if ADMIN_EMAIL and email == ADMIN_EMAIL and current_role != "admin":
            try:
                with get_db() as db2:
                    db2.execute("UPDATE users SET role=? WHERE id=?", ("admin", row["id"]))
                    db2.commit()
                current_role = "admin"
                log.info(f"admin auto-restore: id={row['id']} email={email}")
            except Exception as e:
                log.warning(f"admin auto-restore failed: {e}")

        with _rl_lock:
            _rate_buckets.pop(lockout_key, None)
        session.clear()
        remember = bool(data.get("remember", True))
        session.permanent = remember
        session["user_id"] = row["id"]
        log.info(f"login ok: id={row['id']} email={email} role={current_role} ip={_client_ip()}")
        return jsonify({
            "ok": True,
            "user": {"id": row["id"], "email": row["email"], "name": row["name"], "role": current_role},
            "email_verified": bool(row.get("email_verified", 0) if isinstance(row, dict) else (row["email_verified"] if "email_verified" in row.keys() else 0)),
        })
    except Exception as e:
        log.exception(f"login FAILED: {e}")
        return jsonify({"error": f"Erreur serveur : {type(e).__name__}: {str(e)[:200]}"}), 500

@app.route("/api/auth/logout", methods=["POST"])
def route_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/auth/me")
def route_me():
    u = get_current_user()
    # Site key uniquement si captcha est réellement activable côté serveur
    site_key = HCAPTCHA_SITE_KEY if HCAPTCHA_ENABLED else ""
    if not u:
        return jsonify({"user": None, "hcaptcha_site_key": site_key})
    quota = get_quota_status(u["id"])
    return jsonify({
        "user": {"id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"]},
        "email_verified": bool(u.get("email_verified", 0)),
        "needs_verification": REQUIRE_EMAIL_VERIFICATION and not bool(u.get("email_verified", 0)),
        "quota": quota,
        "hcaptcha_site_key": site_key,
    })

# ── Email verification ─────────────────────────────────────────────────────────
@app.route("/api/auth/verify-email")
def route_verify_email():
    token = (request.args.get("token") or "").strip()
    uid = consume_email_token(token, "verify_email", max_age_hours=48)
    if not uid:
        return Response("<h1>Lien invalide ou expiré</h1>", mimetype="text/html"), 400
    with get_db() as db:
        db.execute("UPDATE users SET email_verified=? WHERE id=?", (True, uid))
        db.commit()
    log.info(f"email verified: user_id={uid}")
    # redirect vers l'app
    return Response(
        f"<h1>✅ Email vérifié !</h1><p><a href='{app_url()}'>Retour à l'app</a></p>",
        mimetype="text/html",
    )

@app.route("/api/auth/resend-verification", methods=["POST"])
@require_auth
@rate_limit(max_calls=3, window_sec=600, key_fn=lambda: f"resend:{session.get('user_id','?')}")
def route_resend_verification():
    u = get_current_user()
    if u.get("email_verified"):
        return jsonify({"ok": True, "already_verified": True})
    token = create_email_token(u["id"], "verify_email", ttl_hours=48)
    verify_url = f"{app_url()}/api/auth/verify-email?token={token}"
    send_email(
        u["email"], "Vérifie ton email — JobFinder",
        f"Lien d'activation (valide 48h) :\n{verify_url}",
        f"<p><a href='{verify_url}'>Activer mon compte</a> (valide 48h)</p>",
    )
    return jsonify({"ok": True})

# ── Password reset ─────────────────────────────────────────────────────────────
@app.route("/api/auth/forgot-password", methods=["POST"])
@rate_limit(max_calls=5, window_sec=3600, key_fn=lambda: f"forgot:{_client_ip()}")
def route_forgot_password():
    data  = request.json or {}
    email = (data.get("email","") or "").strip().lower()
    if not valid_email(email):
        return jsonify({"error": "Email invalide"}), 400
    with get_db() as db:
        row = db.execute("SELECT id FROM users WHERE email=? AND (deleted_at='' OR deleted_at IS NULL)", (email,)).fetchone()
    # Réponse identique que l'email existe ou non (anti-énumération)
    if row:
        token = create_email_token(row["id"], "reset_password", ttl_hours=2)
        reset_url = f"{app_url()}/?reset_token={token}"
        send_email(
            email, "Réinitialisation de mot de passe — JobFinder",
            f"Tu as demandé une réinitialisation. Clique ici (valide 2h) :\n{reset_url}\n\n"
            f"Si ce n'était pas toi, ignore cet email.",
            f"<p><a href='{reset_url}'>Réinitialiser mon mot de passe</a> (valide 2h)</p>"
            f"<p>Si ce n'était pas toi, ignore cet email.</p>",
        )
        log.info(f"forgot-password: id={row['id']} email={email} ip={_client_ip()}")
    return jsonify({"ok": True, "message": "Si l'email existe, un lien de réinitialisation a été envoyé."})

@app.route("/api/auth/reset-password", methods=["POST"])
@rate_limit(max_calls=10, window_sec=3600, key_fn=lambda: f"reset:{_client_ip()}")
def route_reset_password():
    data  = request.json or {}
    token = (data.get("token","") or "").strip()
    pwd   = (data.get("password","") or "")
    if not valid_password(pwd):
        return jsonify({"error": f"Mot de passe trop faible (min {MIN_PASSWORD_LEN} caractères)"}), 400
    uid = consume_email_token(token, "reset_password", max_age_hours=2)
    if not uid:
        return jsonify({"error": "Lien invalide ou expiré."}), 400
    with get_db() as db:
        db.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pwd), uid))
        # invalide les sessions existantes de fait via password change (cookies ne portent que user_id)
        db.commit()
    log.info(f"password reset: user_id={uid} ip={_client_ip()}")
    return jsonify({"ok": True})

# ── Suppression de compte (RGPD : droit à l'effacement) ─────────────────────────
@app.route("/api/auth/delete-account", methods=["POST"])
@require_auth
def route_delete_account():
    u = get_current_user()
    data = request.json or {}
    pwd  = (data.get("password","") or "")
    if not pwd or not check_password_hash(u["password_hash"], pwd):
        return jsonify({"error": "Mot de passe requis pour confirmer"}), 401
    uid = u["id"]
    # Soft-delete : email anonymisé + flag deleted_at, pour préserver la cohérence
    # des candidatures liées (non, on hard-delete car ON DELETE CASCADE est défini).
    # Choix : hard-delete pour respecter pleinement le droit à l'effacement.
    with get_db() as db:
        db.execute("DELETE FROM users WHERE id=?", (uid,))
        db.commit()
    # Supprime les fichiers CV
    user_dir = os.path.join(CV_DIR, str(uid))
    if os.path.isdir(user_dir):
        try:
            import shutil; shutil.rmtree(user_dir)
        except Exception as e:
            log.warning(f"cleanup user_dir failed: {e}")
    session.clear()
    log.info(f"account deleted: user_id={uid}")
    return jsonify({"ok": True})

# ── Export RGPD (droit d'accès) ────────────────────────────────────────────────
@app.route("/api/auth/export-data", methods=["GET"])
@require_auth
@rate_limit(max_calls=3, window_sec=3600, key_fn=lambda: f"export:{session.get('user_id','?')}")
def route_export_data():
    u = get_current_user()
    uid = u["id"]
    with get_db() as db:
        user        = dict(db.execute("SELECT id,email,name,role,created_at,email_verified FROM users WHERE id=?", (uid,)).fetchone())
        apps        = [dict(r) for r in db.execute("SELECT * FROM applications WHERE user_id=?", (uid,))]
        stages      = [dict(r) for r in db.execute("SELECT * FROM interview_stages WHERE user_id=?", (uid,))]
        ud_row      = db.execute("SELECT * FROM user_data WHERE user_id=?", (uid,)).fetchone()
        user_data   = dict(ud_row) if ud_row else {}
        templates   = [dict(r) for r in db.execute("SELECT id,name,style,color,created_at FROM cv_templates WHERE user_id=?", (uid,))]
    # On exclut les blobs lourds de l'export JSON (CV PDF binaire en base64)
    user_data.pop("cv_pdf_b64", None)
    payload = {
        "exported_at": datetime.datetime.now().isoformat(),
        "user": user,
        "applications": apps,
        "interview_stages": stages,
        "user_data": user_data,
        "cv_templates": templates,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return Response(
        body, mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="jobfinder_export_{uid}.json"'}
    )

# ── Pages légales statiques (CGU + Privacy) ─────────────────────────────────────
LEGAL_HTML = """<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8">
<title>{title} — JobFinder</title>
<style>body{{font-family:system-ui;max-width:780px;margin:40px auto;padding:0 24px;color:#1E3A5F;line-height:1.7}}
h1{{color:#2F5DA8}} h2{{margin-top:28px;color:#1E3A5F}} a{{color:#2F5DA8}} code{{background:#EEF3FB;padding:2px 6px;border-radius:4px}}
.foot{{margin-top:40px;padding-top:20px;border-top:1px solid #E2EAF4;font-size:13px;color:#6B7280}}</style></head>
<body>{body}<div class="foot"><a href="/">← Retour à JobFinder</a></div></body></html>"""

@app.route("/cgu")
def route_cgu():
    body = """<h1>Conditions Générales d'Utilisation</h1>
    <p><strong>Dernière mise à jour :</strong> 2026-04-27</p>
    <h2>1. Service</h2>
    <p>JobFinder est un assistant de recherche d'emploi qui utilise des modèles d'IA pour adapter des CV
    et préparer des entretiens. Le service est fourni « tel quel », sans garantie de résultat.</p>
    <h2>2. Compte</h2>
    <p>Tu es responsable de la confidentialité de ton mot de passe. Un compte = une personne physique.</p>
    <h2>3. Usage</h2>
    <p>Interdit : automatiser des requêtes massives, contourner les quotas, uploader du contenu illégal,
    usurper l'identité d'un tiers, faire de l'ingénierie inverse du service.</p>
    <h2>4. Propriété intellectuelle</h2>
    <p>Tu conserves la propriété des contenus que tu importes (CV, documents). Tu nous accordes une licence
    technique pour les traiter (envoi à l'IA, stockage, génération de fichiers dérivés).</p>
    <h2>5. IA et exactitude</h2>
    <p>L'IA peut faire des erreurs. Tu es responsable de relire et corriger tout contenu généré avant usage.</p>
    <h2>6. Suspension / résiliation</h2>
    <p>Tu peux supprimer ton compte à tout moment via Paramètres → Supprimer mon compte. Nous pouvons suspendre
    un compte en cas de violation des présentes CGU.</p>
    <h2>7. Limitation de responsabilité</h2>
    <p>Notre responsabilité ne saurait excéder le montant payé pour le service sur les 12 derniers mois.</p>
    <h2>8. Droit applicable</h2>
    <p>Droit français. Tribunaux compétents : ceux du ressort du siège de l'éditeur.</p>"""
    return Response(LEGAL_HTML.format(title="CGU", body=body), mimetype="text/html")

@app.route("/privacy")
def route_privacy():
    body = """<h1>Politique de confidentialité</h1>
    <p><strong>Dernière mise à jour :</strong> 2026-04-27</p>
    <h2>Données collectées</h2>
    <ul>
      <li>Email, nom, mot de passe (hashé) — pour ton compte</li>
      <li>Tes CV, documents et candidatures — pour le service</li>
      <li>Logs techniques (IP, user-agent) — sécurité, conservés 30 jours</li>
    </ul>
    <h2>Finalités</h2>
    <p>Uniquement la fourniture du service. Pas de revente. Pas de profilage publicitaire.</p>
    <h2>Sous-traitants</h2>
    <ul>
      <li><strong>OpenAI / Anthropic</strong> — traitement IA. Données envoyées : extraits de CV + offre. Non stockées par défaut.</li>
      <li><strong>Hébergeur</strong> (Railway) — UE/US selon région</li>
      <li><strong>SMTP</strong> (si configuré) — envoi d'emails de vérification / reset</li>
    </ul>
    <h2>Tes droits (RGPD)</h2>
    <ul>
      <li><strong>Accès</strong> : Paramètres → <code>Exporter mes données</code> (JSON)</li>
      <li><strong>Effacement</strong> : Paramètres → <code>Supprimer mon compte</code></li>
      <li><strong>Rectification</strong> : modifier directement dans l'app</li>
      <li><strong>Réclamation</strong> : <a href='https://www.cnil.fr'>CNIL</a></li>
    </ul>
    <h2>Conservation</h2>
    <p>Tes données sont conservées tant que ton compte existe, supprimées définitivement à la suppression du compte.</p>
    <h2>Cookies</h2>
    <p>Un seul cookie : <code>session</code> (HttpOnly, Secure en prod, SameSite=Strict) pour te garder connecté.
    Pas de cookie tiers, pas de tracking publicitaire.</p>
    <h2>Contact</h2>
    <p>Pour toute question : <a href='mailto:contact@jobfinder.app'>contact@jobfinder.app</a> (à adapter)</p>"""
    return Response(LEGAL_HTML.format(title="Confidentialité", body=body), mimetype="text/html")

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES CONFIG (par utilisateur)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/config", methods=["GET","POST"])
@require_auth
def route_config():
    """État du profil utilisateur (pour les bandeaux d'onboarding).
    Plus de gestion de clé API par user : tout passe par les clés globales
    OPENAI_API_KEY / ANTHROPIC_API_KEY (env)."""
    u = get_current_user()
    if request.method == "GET":
        ud = get_user_data(u["id"])
        return jsonify({
            "has_summary": bool(ud.get("summary","").strip()),
            "has_docs":    bool(ud.get("doc_text","").strip()),
            "has_photo":   bool(ud.get("photo_b64","").strip()),
            "ai_ready":    bool(OPENAI_API_KEY or ANTHROPIC_API_KEY),
        })
    # POST : conservé pour compatibilité, plus de champ accepté.
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES CANDIDATURES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/applications", methods=["GET"])
@require_auth
def route_get_apps():
    u = get_current_user()
    with get_db() as db:
        rows = db.execute("SELECT * FROM applications WHERE user_id=? ORDER BY created_at DESC", (u["id"],)).fetchall()
    return jsonify([app_row_to_dict(r) for r in rows])

def _trim(s, n):
    return (s or "")[:n] if isinstance(s, str) else ""

@app.route("/api/applications", methods=["POST"])
@require_auth
def route_add_app():
    u    = get_current_user()
    data = request.json or {}
    with get_db() as db:
        applied = data.get("date", today())
        cur = db.execute(
            """INSERT INTO applications(user_id,company,role_name,job_desc,status,cv_filename,notes,url,applied_date)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (u["id"],
             _trim(data.get("company"), 200),
             _trim(data.get("role"),    200),
             _trim(data.get("job_desc"), 12000),
             sanitize_status(data.get("status")),
             _trim(data.get("cv_filename"), 200),
             _trim(data.get("notes"), 12000),
             _trim(data.get("url"), 2048),
             _trim(applied, 20))
        )
        app_id = cur.lastrowid
        # Crée automatiquement un stage "Candidature envoyée" dans le planning
        db.execute(
            "INSERT INTO interview_stages(application_id,user_id,stage_type,scheduled_date,notes,result) VALUES(?,?,?,?,?,?)",
            (app_id, u["id"], "Candidature envoyée", applied, "", "En attente")
        )
        db.commit()
        row = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    return jsonify(app_row_to_dict(row))

@app.route("/api/applications/<app_id>", methods=["PUT","DELETE"])
@require_auth
def route_update_app(app_id):
    u = get_current_user()
    with get_db() as db:
        row = db.execute("SELECT * FROM applications WHERE id=? AND user_id=?", (app_id, u["id"])).fetchone()
        if not row:
            return jsonify({"error": "Non trouvé"}), 404
        if request.method == "DELETE":
            db.execute("DELETE FROM applications WHERE id=?", (app_id,))
            db.commit()
            return jsonify({"ok": True})
        data = request.json or {}
        sets, vals = [], []
        mapping = {"company":"company","role":"role_name","job_desc":"job_desc",
                   "status":"status","cv_filename":"cv_filename","notes":"notes",
                   "url":"url","date":"applied_date","interview_prep":"interview_prep"}
        # Bornes par champ (anti-bloat)
        limits = {"company":200,"role":200,"job_desc":12000,"status":50,
                  "cv_filename":200,"notes":12000,"url":2048,"date":20,
                  "interview_prep":50000}
        for k, col in mapping.items():
            if k in data:
                v = data[k]
                if k == "status":
                    v = sanitize_status(v)
                elif isinstance(v, str):
                    v = v[:limits[k]]
                sets.append(f"{col}=?")
                vals.append(v)
        if sets:
            db.execute(f"UPDATE applications SET {','.join(sets)} WHERE id=?", (*vals, app_id))
            db.commit()
        row = db.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    return jsonify(app_row_to_dict(row))

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES STAGES D'ENTRETIEN
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/interview-stages", methods=["GET"])
@require_auth
def route_get_stages():
    u = get_current_user()
    app_id = request.args.get("app_id")
    with get_db() as db:
        if app_id:
            rows = db.execute(
                "SELECT * FROM interview_stages WHERE user_id=? AND application_id=? ORDER BY scheduled_date",
                (u["id"], app_id)
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT s.*, a.company, a.role_name, a.status as app_status FROM interview_stages s JOIN applications a ON s.application_id=a.id WHERE s.user_id=? ORDER BY s.scheduled_date",
                (u["id"],)
            ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/interview-stages", methods=["POST"])
@require_auth
def route_add_stage():
    u    = get_current_user()
    data = request.json or {}
    app_id = data.get("application_id")
    if not app_id:
        return jsonify({"error": "application_id requis"}), 400
    with get_db() as db:
        # Vérifie que la candidature appartient à l'utilisateur
        own = db.execute("SELECT id FROM applications WHERE id=? AND user_id=?", (app_id, u["id"])).fetchone()
        if not own:
            return jsonify({"error": "Candidature non trouvée"}), 404
        cur = db.execute(
            "INSERT INTO interview_stages(application_id,user_id,stage_type,scheduled_date,notes,result) VALUES(?,?,?,?,?,?)",
            (app_id, u["id"], data.get("stage_type","Entretien"),
             data.get("scheduled_date"), data.get("notes",""), data.get("result","En attente"))
        )
        db.commit()
        row = db.execute("SELECT * FROM interview_stages WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row))

@app.route("/api/interview-stages/<int:stage_id>", methods=["PUT","DELETE"])
@require_auth
def route_update_stage(stage_id):
    u = get_current_user()
    with get_db() as db:
        row = db.execute("SELECT * FROM interview_stages WHERE id=? AND user_id=?", (stage_id, u["id"])).fetchone()
        if not row:
            return jsonify({"error": "Stage non trouvé"}), 404
        if request.method == "DELETE":
            db.execute("DELETE FROM interview_stages WHERE id=?", (stage_id,))
            db.commit()
            return jsonify({"ok": True})
        data = request.json or {}
        sets, vals = [], []
        for k in ["stage_type","scheduled_date","notes","result"]:
            if k in data:
                sets.append(f"{k}=?"); vals.append(data[k])
        if sets:
            db.execute(f"UPDATE interview_stages SET {','.join(sets)} WHERE id=?", (*vals, stage_id))
            db.commit()
        row = db.execute("SELECT * FROM interview_stages WHERE id=?", (stage_id,)).fetchone()
    return jsonify(dict(row))

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES CV
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/cv-base", methods=["GET"])
@require_auth
def route_get_cv():
    u  = get_current_user()
    ud = get_user_data(u["id"])
    return jsonify({"html": ud.get("cv_html",""), "name": ud.get("cv_name","")})

@app.route("/api/cv-base", methods=["POST"])
@require_auth
def route_upload_cv():
    u    = get_current_user()
    data = request.json or {}
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO user_data(user_id) VALUES(?)", (u["id"],))
        db.execute("UPDATE user_data SET cv_html=?, cv_name=? WHERE user_id=?",
                   (data.get("html",""), data.get("name","cv_base.html"), u["id"]))
        db.commit()
    return jsonify({"ok": True})

@app.route("/api/cv-file/<filename>")
@require_auth
def route_serve_cv(filename):
    """Sert UNIQUEMENT les CV du user connecté (dossier dédié par user_id).
    Fallback sur l'ancien dossier global pour compat avec les CV historiques."""
    u = get_current_user()
    safe = os.path.basename(filename)
    if not safe or safe.startswith("."):
        return "CV introuvable", 404
    user_path   = os.path.join(user_cv_dir(u["id"]), safe)
    legacy_path = os.path.join(CV_DIR, safe)
    if os.path.isfile(user_path):
        path = user_path
    elif os.path.isfile(legacy_path):
        # Compat ascendante : on n'expose que les fichiers liés à une candidature du user
        with get_db() as db:
            owned = db.execute(
                "SELECT 1 FROM applications WHERE user_id=? AND cv_filename=? LIMIT 1",
                (u["id"], safe)
            ).fetchone()
        if not owned:
            log.warning(f"cv-file forbidden: user={u['id']} file={safe}")
            return "Accès refusé", 403
        path = legacy_path
    else:
        return "CV introuvable", 404
    mtype = "application/pdf" if safe.lower().endswith(".pdf") else "text/html"
    return send_file(path, mimetype=mtype)

@app.route("/api/adapt-cv", methods=["POST"])
@require_auth
@rate_limit(max_calls=20, window_sec=3600, key_fn=lambda: f"ai:{session.get('user_id','?')}")
@require_quota
def route_adapt_cv():
    u    = get_current_user()
    data = request.json or {}
    provider, api_key = get_ai_keys(u)
    if not api_key:
        return jsonify({"error": "Clé API manquante. Configurez-la dans Paramètres."}), 400
    ud = get_user_data(u["id"])
    cv_html = data.get("cv_html","") or ud.get("cv_html","")
    if not cv_html:
        return jsonify({"error": "Aucun CV trouvé. Importez votre CV dans Paramètres."}), 400
    docs_text = get_docs_context(u["id"])
    job_desc  = data.get("job_desc","")
    company   = data.get("company","")
    role      = data.get("role","")
    prompt = f"""You are a professional CV writer. Adapt the following HTML CV to best match the target job position.

Target position: {role}
Company: {company}
Job description:
{job_desc[:4000]}

{("Additional candidate documentation:" + chr(10) + docs_text[:4000]) if docs_text else ""}

Current CV (HTML):
{cv_html[:8000]}

Instructions:
- Keep every HTML tag, class, ID and structure exactly as-is
- Only adapt the visible text content
- Do not invent any experience, qualification, date or skill not present in the source CV or documentation
- Highlight the skills and experiences most relevant to the target role
- Return only the complete modified HTML, no markdown, no explanations

Writing style — critical:
- Write like a real person, not a corporate template. Short, direct sentences.
- No filler adjectives or adverbs: avoid "highly motivated", "passionate", "dynamic", "results-driven", "strong ability to", "excellent communication skills", "proven track record", "leverage", "synergy" and similar hollow phrases.
- Describe what was actually done and the concrete result. Prefer action verbs with numbers when available.
- Do not start every bullet with the same structure. Vary sentence rhythm.
- Descriptions should sound like they were written by the candidate, not by a recruiter.

Modified HTML:"""
    try:
        result = call_ai(provider, api_key, prompt, max_tokens=8192)
        fname  = f"CV_{safe_name(company)}_{safe_name(role)}_{today()}.html"
        with open(os.path.join(user_cv_dir(u["id"]), fname), "w", encoding="utf-8") as f:
            f.write(result)
        return jsonify({"html": result, "filename": fname})
    except Exception as e:
        log.exception("adapt-cv failed")
        return jsonify({"error": str(e)}), 500

@app.route("/api/adapt-cv-template", methods=["POST"])
@require_auth
@rate_limit(max_calls=20, window_sec=3600, key_fn=lambda: f"ai:{session.get('user_id','?')}")
@require_quota
def route_adapt_cv_template():
    u    = get_current_user()
    data = request.json or {}
    provider, api_key = get_ai_keys(u)
    if not api_key:
        return jsonify({"error": "Clé API manquante."}), 400
    docs_text = get_docs_context(u["id"])
    if not docs_text:
        return jsonify({"error": "Aucune documentation trouvée. Importez vos documents dans Paramètres."}), 400
    # Support custom template from DB (template_id) or fallback to cv_vibe_modern file
    tpl_id = data.get("template_id")
    if tpl_id:
        with get_db() as db:
            row = db.execute("SELECT html_content FROM cv_templates WHERE id=? AND user_id=?",
                             (tpl_id, u["id"])).fetchone()
        if not row:
            return jsonify({"error": "Template introuvable."}), 404
        template_html = row["html_content"]
    else:
        if not os.path.exists(TEMPLATE_CV):
            return jsonify({"error": "Template cv_vibe_modern introuvable."}), 400
        with open(TEMPLATE_CV, encoding="utf-8") as f:
            template_html = f.read()
    ud = get_user_data(u["id"])
    PHOTO_MARKER = "PORTRAIT_SRC_PLACEHOLDER"
    head_match = re.search(r'^([\s\S]*?<body[^>]*>)', template_html, re.IGNORECASE)
    body_match = re.search(r'<body[^>]*>([\s\S]*)</body>', template_html, re.IGNORECASE)
    head_part  = head_match.group(1) if head_match else ""
    body_part  = body_match.group(1) if body_match else template_html
    body_for_ai = re.sub(
        r'(<div[^>]*class="portrait-wrap"[^>]*>\s*<img[^>]*\ssrc=")[^"]*(")',
        lambda m: m.group(1)+PHOTO_MARKER+m.group(2), body_part, flags=re.DOTALL)
    body_for_ai = re.sub(
        r'(<img[^>]*\ssrc=")(?:/mnt/data/[^"]*|[^"]*profile_only[^"]*)(")',
        lambda m: m.group(1)+PHOTO_MARKER+m.group(2), body_for_ai)
    job_desc = data.get("job_desc",""); company = data.get("company",""); role = data.get("role","")
    prompt = f"""You are a professional CV writer. Rewrite the content of this CV template for a specific job application.

Target position: {role}
Company: {company}
Job description:
{job_desc[:4000]}

Candidate documentation (ONLY source of truth — do not invent anything):
{docs_text[:8000]}

HTML body to rewrite (keep all tags/classes/IDs, only replace text content):
{body_for_ai}

Instructions:
- Keep every HTML tag, class, ID and attribute exactly as-is
- Only replace visible text with adapted versions based on the documentation above
- Do not invent experiences, skills, dates or qualifications not in the documentation
- Keep src="{PHOTO_MARKER}" exactly as-is
- Return only the rewritten HTML body, no markdown, no code fences

Writing style — critical:
- Write like a real person, not a corporate template. Short, direct sentences.
- No hollow adjectives or adverbs: ban "highly motivated", "passionate", "dynamic", "results-driven", "strong ability to", "excellent communication skills", "proven track record", "leverage", "synergy", "team player" and similar empty phrases.
- Every sentence must say something concrete: what was done, for whom, with what result.
- Vary sentence length and structure. Avoid starting every item the same way.
- The tone should feel like the candidate wrote it themselves — confident but grounded, not inflated.
- If a section has bullet points in the HTML, keep them short (one idea each, max 12 words).

Rewritten HTML body:"""
    try:
        result = call_ai(provider, api_key, prompt, max_tokens=8000)
        result = re.sub(r"^```[\w]*\n?", "", result.strip())
        result = re.sub(r"\n?```$", "", result.strip())
        if head_part:
            result = head_part + result + "\n</body>\n</html>"
        if ud.get("photo_b64"):
            photo_src = f"data:{ud.get('photo_mime','image/jpeg')};base64,{ud['photo_b64']}"
            result = result.replace(PHOTO_MARKER, photo_src)
            if photo_src not in result:
                result = re.sub(r'(<div[^>]*class="portrait-wrap"[^>]*>\s*<img[^>]*\ssrc=")[^"]*(")',
                    lambda m: m.group(1)+photo_src+m.group(2), result, flags=re.DOTALL)
            if photo_src not in result:
                result = re.sub(r'(<img[^>]*\ssrc=")(?!data:)[^"]*(")',
                    lambda m: m.group(1)+photo_src+m.group(2), result, count=1)
        else:
            result = result.replace(PHOTO_MARKER, "")
        fname = f"CV_{safe_name(company)}_{safe_name(role)}_{today()}.html"
        with open(os.path.join(user_cv_dir(u["id"]), fname), "w", encoding="utf-8") as f:
            f.write(result)
        return jsonify({"html": result, "filename": fname})
    except Exception as e:
        log.exception("adapt-cv-template failed")
        return jsonify({"error": str(e)}), 500

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES PHOTO / DOCS
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/photo", methods=["GET","POST"])
@require_auth
def route_photo():
    u = get_current_user()
    ud = get_user_data(u["id"])
    if request.method == "GET":
        return jsonify({"b64": ud.get("photo_b64",""), "mime": ud.get("photo_mime","image/jpeg")})
    data = request.json or {}
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO user_data(user_id) VALUES(?)", (u["id"],))
        db.execute("UPDATE user_data SET photo_b64=?, photo_mime=? WHERE user_id=?",
                   (data.get("b64",""), data.get("mime","image/jpeg"), u["id"]))
        db.commit()
    return jsonify({"ok": True})

@app.route("/api/docs", methods=["GET","POST"])
@require_auth
def route_docs():
    try:
        u  = get_current_user()
        ud = get_user_data(u["id"])
        if request.method == "GET":
            try:
                names = json.loads(ud.get("doc_names","[]") or "[]")
                if not isinstance(names, list): names = []
            except Exception:
                names = []
            return jsonify({
                "text":    ud.get("doc_text","") or "",
                "names":   names,
                "summary": ud.get("summary","") or "",
            })
        data = request.json or {}
        with get_db() as db:
            db.execute("INSERT OR IGNORE INTO user_data(user_id) VALUES(?)", (u["id"],))
            sets, vals = [], []
            if "text"    in data: sets.append("doc_text=?");  vals.append(data["text"] or "")
            if "names"   in data: sets.append("doc_names=?"); vals.append(json.dumps(data["names"] or []))
            if "summary" in data: sets.append("summary=?");   vals.append(data["summary"] or "")
            if sets:
                db.execute(f"UPDATE user_data SET {','.join(sets)} WHERE user_id=?", (*vals, u["id"]))
                db.commit()
        return jsonify({"ok": True})
    except Exception as e:
        log.exception(f"/api/docs FAILED: {e}")
        return jsonify({"error": f"Erreur serveur : {type(e).__name__}: {str(e)[:200]}"}), 500

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES IA (SEARCH, FETCH-URL, INTERVIEW-PREP, DOWNLOAD-PDF, EXTRACT-DOC)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/search", methods=["POST"])
@require_auth
@rate_limit(max_calls=30, window_sec=3600, key_fn=lambda: f"search:{session.get('user_id','?')}")
def route_search():
    data = request.json or {}
    query = (data.get("query","") or "").strip()
    location = (data.get("location","France") or "").strip() or "France"
    if not query:
        return jsonify({"jobs": [], "error": "Saisis un mot-clé"}), 400
    jobs, err, source = search_jobs(query, location)
    if not jobs and not err:
        # Pas de résultats mais pas d'erreur → message UX clair
        return jsonify({
            "jobs": [], "error": None, "source": source,
            "hint": "Aucun résultat. Essaie d'autres mots-clés ou une autre localisation."
        })
    return jsonify({"jobs": jobs or [], "error": err, "source": source})

@app.route("/api/fetch-url", methods=["POST"])
@require_auth
@rate_limit(max_calls=20, window_sec=3600, key_fn=lambda: f"ai:{session.get('user_id','?')}")
@require_quota
def route_fetch_url():
    u   = get_current_user()
    data = request.json or {}
    url  = (data.get("url","") or "").strip()
    if not url or not re.match(r"^https?://", url, re.I):
        return jsonify({"error": "URL invalide (http/https requis)"}), 400
    openai_key = OPENAI_API_KEY
    if not openai_key:
        return jsonify({"error": "Service indisponible (clé OpenAI non configurée)"}), 503
    try:
        from openai import OpenAI as _OAI
        _client = _OAI(api_key=openai_key)
        ws_prompt = (
            f"You must open and read this exact URL: {url}\n\n"
            "Do NOT search for similar jobs. Do NOT use any other URL. Open this specific page.\n\n"
            "Extract: title (job title), company (company name), description (full job description).\n\n"
            'Return ONLY this JSON with no markdown:\n{"title":"...","company":"...","description":"..."}'
        )
        resp = _client.responses.create(
            model="gpt-4o", tools=[{"type":"web_search_preview"}],
            tool_choice={"type":"web_search_preview"}, input=ws_prompt)
        raw = resp.output_text.strip()
        raw = re.sub(r"^```[\w]*\s*","",raw); raw = re.sub(r"\s*```$","",raw)
        m = re.search(r'\{[\s\S]*\}', raw)
        if not m: raise ValueError(f"Pas de JSON : {raw[:300]}")
        json_str = m.group(0)
        # Échappe les caractères de contrôle littéraux à l'intérieur des strings JSON
        json_str = re.sub(
            r'"(?:[^"\\]|\\.)*"',
            lambda x: x.group(0)
                .replace('\n','\\n').replace('\r','\\r')
                .replace('\t','\\t').replace('\x08','\\b').replace('\x0c','\\f'),
            json_str, flags=re.DOTALL
        )
        p = json.loads(json_str)
        return jsonify({"title":str(p.get("title",""))[:200],"company":str(p.get("company",""))[:200],"description":str(p.get("description",""))[:8000]})
    except Exception as e:
        return jsonify({"error": f"Échec : {e}"}), 500

@app.route("/api/interview-prep", methods=["POST"])
@require_auth
@rate_limit(max_calls=20, window_sec=3600, key_fn=lambda: f"ai:{session.get('user_id','?')}")
@require_quota
def route_interview_prep():
    u    = get_current_user()
    data = request.json or {}
    provider, api_key = get_ai_keys(u)
    if not api_key:
        return jsonify({"error": "Clé API manquante."}), 400
    docs_text = get_docs_context(u["id"])
    company = data.get("company",""); role = data.get("role","")
    job_desc = data.get("job_desc",""); cv_html = data.get("cv_html","")
    prompt = f"""Tu es un coach carrière expérimenté. Génère une fiche de préparation d'entretien complète en français.

Entreprise : {company}
Poste : {role}
Offre d'emploi :
{job_desc[:3000]}
{("CV du candidat :" + chr(10) + cv_html[:2000]) if cv_html else ""}
{("Documentation :" + chr(10) + docs_text[:2000]) if docs_text else ""}

Génère uniquement ce qui est fondé sur les informations disponibles.

Ton d'écriture — essentiel :
- Écris comme un humain qui parle à quelqu'un, pas comme un document RH.
- Pas de tirets à répétition ni de listes interminables. Quand c'est possible, formule en phrases courtes.
- Pas d'adjectifs creux : interdit d'écrire "dynamique", "motivé(e)", "passionné(e)", "excellent communicant", "force de proposition", "sens du résultat", "orienté business" et expressions similaires.
- Les réponses suggérées doivent sonner naturel, comme si le candidat les disait vraiment à voix haute. Pas de formules toutes faites.
- Les questions à poser au recruteur doivent être précises et montrer une vraie curiosité, pas des questions génériques.
- Sois direct. Une phrase courte qui dit quelque chose vaut mieux qu'un paragraphe qui tourne en rond.

🎯 MON PITCH EN 2 MINUTES
❓ QUESTIONS PROBABLES + RÉPONSES SUGGÉRÉES (8 questions)
💬 MES QUESTIONS À POSER AU RECRUTEUR (5 questions)
🏢 L'ENTREPRISE — CE QUE JE DOIS SAVOIR
⚡ COMPÉTENCES CLÉS À METTRE EN AVANT
⚠️ POINTS FAIBLES / RISQUES À PRÉPARER
📅 CHECKLIST LOGISTIQUE"""
    try:
        result = call_ai(provider, api_key, prompt, max_tokens=4096)
        app_id = data.get("app_id")
        if app_id:
            with get_db() as db:
                db.execute("UPDATE applications SET interview_prep=? WHERE id=? AND user_id=?",
                           (result, app_id, u["id"]))
                db.commit()
        return jsonify({"prep": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/extract-doc", methods=["POST"])
@require_auth
@rate_limit(max_calls=30, window_sec=3600, key_fn=lambda: f"extract:{session.get('user_id','?')}")
def route_extract_doc():
    data  = request.json or {}
    b64   = data.get("b64",""); mime = data.get("mime",""); fname = data.get("name","")
    if not b64: return jsonify({"text":""})
    raw = base64.b64decode(b64); text = ""
    try:
        if "pdf" in mime or fname.lower().endswith(".pdf"):
            try:
                import pypdf, io
                text = "\n".join(p.extract_text() or "" for p in pypdf.PdfReader(io.BytesIO(raw)).pages)
            except Exception:
                text = "[PDF reçu mais extraction impossible]"
        else:
            soup = BeautifulSoup(raw.decode("utf-8","replace"), "html.parser")
            text = soup.get_text("\n", strip=True)
    except Exception as e:
        text = f"[Erreur: {e}]"
    return jsonify({"text": text[:8000]})

@app.route("/api/download-pdf", methods=["POST"])
@require_auth
@rate_limit(max_calls=30, window_sec=3600, key_fn=lambda: f"pdf:{session.get('user_id','?')}")
def route_download_pdf():
    u        = get_current_user()
    data     = request.json or {}
    filename = (data.get("filename","") or "").strip()
    if not filename: return jsonify({"error": "Nom de fichier manquant"}), 400
    safe = os.path.basename(filename)
    if not safe or safe.startswith("."): return jsonify({"error": "Nom invalide"}), 400
    # Prio dossier user, fallback legacy avec check ownership via DB
    user_path   = os.path.join(user_cv_dir(u["id"]), safe)
    legacy_path = os.path.join(CV_DIR, safe)
    if os.path.isfile(user_path):
        path = user_path
    elif os.path.isfile(legacy_path):
        with get_db() as db:
            owned = db.execute(
                "SELECT 1 FROM applications WHERE user_id=? AND cv_filename=? LIMIT 1",
                (u["id"], safe)
            ).fetchone()
        if not owned:
            return jsonify({"error": "Accès refusé"}), 403
        path = legacy_path
    else:
        return jsonify({"error": "CV introuvable"}), 404
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return jsonify({"error": "Playwright non installé. Relancez le .bat."}), 500
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page    = browser.new_page(viewport={"width":1280,"height":900})
            page.goto(f"file:///{path.replace(chr(92),'/')}", wait_until="networkidle")
            page.wait_for_timeout(1500)
            # Mesure la largeur réelle du contenu (pas du viewport entier)
            dims = page.evaluate("""()=>{
                const children = [...document.body.children];
                let maxW = 0;
                for(const c of children){
                    const r = c.getBoundingClientRect();
                    if(r.width > maxW) maxW = r.width;
                }
                const w = maxW || document.body.scrollWidth;
                const h = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
                return {w, h};
            }""")
            PAD = 8  # marge minimum en mm sur chaque côté
            scale = round(min((210-PAD*2)/(dims['w']*0.264583), (297-PAD*2)/(dims['h']*0.264583), 1), 4)
            # Marges pour centrer horizontalement + padding vertical
            content_w_mm = dims['w'] * 0.264583 * scale
            hm = round(max(PAD, (210 - content_w_mm) / 2), 2)
            content_h_mm = dims['h'] * 0.264583 * scale
            vm = round(max(PAD, (297 - content_h_mm) / 2), 2) if content_h_mm < 297 - PAD*2 else PAD
            pdf_bytes = page.pdf(format="A4", print_background=True,
                margin={"top":f"{vm}mm","right":f"{hm}mm","bottom":f"{vm}mm","left":f"{hm}mm"}, scale=scale)
            browser.close()
        return Response(pdf_bytes, mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{safe.replace(".html",".pdf")}"'})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES TEMPLATES CV
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/cv-templates", methods=["GET"])
@require_auth
def route_get_templates():
    u = get_current_user()
    with get_db() as db:
        rows = db.execute(
            "SELECT id, name, style, color, created_at FROM cv_templates WHERE user_id=? ORDER BY created_at DESC",
            (u["id"],)
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/cv-templates", methods=["POST"])
@require_auth
def route_save_template():
    u    = get_current_user()
    data = request.json or {}
    html = data.get("html","")
    if not html:
        return jsonify({"error":"HTML manquant"}), 400
    name  = (data.get("name","Template") or "Template").strip()
    style = data.get("style","Moderne")
    color = data.get("color","#7c3aed")
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO cv_templates(user_id,name,style,color,html_content) VALUES(?,?,?,?,?)",
            (u["id"], name, style, color, html)
        )
        db.commit()
        row = db.execute("SELECT id,name,style,color,created_at FROM cv_templates WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify(dict(row))

@app.route("/api/cv-templates/<int:tpl_id>", methods=["GET","DELETE"])
@require_auth
def route_template(tpl_id):
    u = get_current_user()
    with get_db() as db:
        row = db.execute("SELECT * FROM cv_templates WHERE id=? AND user_id=?", (tpl_id, u["id"])).fetchone()
        if not row:
            return jsonify({"error":"Template non trouvé"}), 404
        if request.method == "DELETE":
            db.execute("DELETE FROM cv_templates WHERE id=?", (tpl_id,))
            db.commit()
            return jsonify({"ok":True})
    return jsonify(dict(row))

@app.route("/api/generate-template", methods=["POST"])
@require_auth
@rate_limit(max_calls=10, window_sec=3600, key_fn=lambda: f"ai:{session.get('user_id','?')}")
@require_quota
def route_generate_template():
    data = request.json or {}
    u    = get_current_user()
    ud   = get_user_data(u["id"])

    # ── Wizard answers ──────────────────────────────────────────────
    objectif        = data.get("objectif", "")
    secteur         = data.get("secteur", "")
    type_entreprise = data.get("typeEntreprise", "")
    experience      = data.get("experience", "")
    style           = data.get("style", "Moderne")
    ton_cv          = data.get("tonCV", "")
    couleur         = data.get("couleur", data.get("color", "#6d28d9"))
    couleurs_raw    = data.get("couleurs", [couleur])
    couleurs        = [c for c in couleurs_raw if c and len(c) == 7]
    if not couleurs: couleurs = [couleur]
    impressions     = data.get("impressions", [])
    name            = data.get("name", "Mon CV")
    imp_str         = ", ".join(impressions) if impressions else "Sérieux, Professionnel"

    # Palette couleurs lisible pour le prompt
    if len(couleurs) == 1:
        palette_str = f"{couleurs[0]} (principale)"
    elif len(couleurs) == 2:
        palette_str = f"{couleurs[0]} (principale), {couleurs[1]} (secondaire/accents)"
    else:
        palette_str = f"{couleurs[0]} (principale), {couleurs[1]} (secondaire), {couleurs[2]} (accent détails)"

    # ── Données réelles de l'utilisateur ────────────────────────────
    user_name    = u.get("name", "")
    user_email   = u.get("email", "")
    user_summary = ud.get("summary", "").strip()
    user_docs    = ud.get("doc_text", "").strip()
    has_photo    = bool(ud.get("photo_b64", ""))
    photo_mime   = ud.get("photo_mime", "image/jpeg")

    # Construit le bloc données utilisateur
    user_context = ""
    if user_summary:
        user_context += f"\n\n=== RÉSUMÉ PERSONNEL DE L'UTILISATEUR (utilise ces infos réelles) ===\n{user_summary[:3000]}"
    if user_docs:
        user_context += f"\n\n=== DOCUMENTS / ANCIENS CV IMPORTÉS (extrait infos pertinentes) ===\n{user_docs[:4000]}"

    # Placeholder photo : soit marqueur de remplacement, soit cercle initiales
    photo_placeholder = "PHOTO_PLACEHOLDER" if has_photo else None
    initiales = "".join([p[0].upper() for p in user_name.split()[:2]]) if user_name else "JD"
    nom_affiche = user_name if user_name else "Jean Dupont"
    email_affiche = user_email if user_email else "jean.dupont@email.com"

    # ── Guides de design par style ──────────────────────────────────
    design_guides = {
        "Sobre": """
STYLE SOBRE — Minimaliste haut de gamme :
- Palette : blanc pur + 1 couleur accent + gris anthracite texte
- Typographie : serif élégant (Cormorant Garamond, Playfair Display) pour nom, sans-serif (Lato, Source Sans Pro) pour corps
- Structure : 1 colonne, marges généreuses (40px), sections séparées par une fine ligne
- Accents design subtils : filet horizontal coloré sous le nom, point coloré avant chaque titre de section
- Aucune forme géométrique agressive, aucun fond coloré de section
- Impression : sobriété, confiance, expertise""",

        "Moderne": """
STYLE MODERNE — Design actuel et percutant :
- Structure : 2 colonnes (sidebar 34% colorée à gauche, contenu 66% blanc à droite)
- Sidebar : fond couleur principale, texte blanc, photo ronde en haut centrée, contact + compétences
- Header du contenu : nom en gros (28px bold), poste en couleur principale
- Formes géométriques : rectangle coloré en haut à droite (clip-path ou border), petits carrés colorés devant les titres de section
- Barres de compétences visuelles (div avec width% et fond couleur principale)
- Timeline expérience : ligne verticale colorée à gauche avec points/ronds
- Typographie : Inter, Nunito ou DM Sans
- Impression : moderne, dynamique, structuré""",

        "Créatif": """
STYLE CRÉATIF — Mémorable, audacieux :
- Header spectaculaire : fond couleur principale avec diagonale (clip-path: polygon(0 0, 100% 0, 100% 80%, 0 100%)), photo ronde border blanc 4px
- Formes géométriques : grands cercles transparents en arrière-plan, formes angulaires CSS
- Section compétences : tags/badges colorés avec border-radius:4px, fond semi-transparent
- Icônes : utilise Unicode ● ▸ ◆ ▪ pour ponctuer les sections
- Timeline avec alternance gauche/droite pour l'expérience
- Couleurs : couleur principale + version claire (opacity .15) pour fonds de section
- Typographie : Poppins ou Raleway, poids variés (300, 600, 800)
- Accents : band colorée verticale épaisse (6px) sur le bord gauche du contenu
- Impression : créativité, personnalité forte, originalité""",

        "Premium": """
STYLE PREMIUM — Luxe, raffinement :
- Fond page : blanc cassé (#fafaf8) ou crème très léger
- Header : 2 colonnes — gauche texte (nom huge 36px, trait fin doré/coloré, poste), droite photo dans cadre carré avec border colorée 3px
- Typographie : Cormorant Garamond ou Libre Baskerville pour titres, Montserrat light pour corps
- Séparateurs : ligne fine pleine largeur avec petit losange/carré centré dessus (CSS ::before/::after)
- Sections compétences : cercles/anneaux SVG inline ou progress rings CSS
- Palette : couleur principale + or (#c9a84c) ou argent pour accents secondaires, fond section alterné très léger
- Espacements généreux, jamais de surcharge visuelle
- Impression : excellence, haut de gamme, maturité""",

        "Impactant": """
STYLE IMPACTANT — Fort, ambitieux :
- Header pleine largeur : fond couleur principale, texte blanc, nom en majuscules 32px letter-spacing .1em
- Bande diagonale de transition header→corps (clip-path ou SVG en position absolute)
- Fond corps : blanc pur, contraste maximal
- Titres de section : fond couleur principale, texte blanc, padding 8px 16px, largeur auto, border-radius 4px
- Chiffres clés mis en valeur : grands chiffres colorés (nombre d'années, projets, etc.)
- Compétences : jauge horizontale épaisse (12px) avec animation CSS @keyframes fill
- Typographie : Oswald ou Barlow Condensed pour titres, Open Sans pour corps
- Impression : ambition, leadership, résultats"""
    }

    design_guide = design_guides.get(style, design_guides["Moderne"])

    # ── Règles selon type entreprise ────────────────────────────────
    entreprise_rules = {
        "Startup": "Layout innovant, badges de compétences tech, section 'Projets' mise en avant, pas trop corporate",
        "Grand groupe": "Structure classique optimisée, sections standards bien hiérarchisées, sérieux et lisibilité",
        "PME": "Équilibre polyvalence/spécialisation, ton accessible, mise en avant adaptabilité",
        "Cabinet conseil": "Précision, chiffres d'impact, bullet points STAR, dense mais aéré",
        "Luxe": "Raffinement maximal, typographie premium, aucun élément criard, élégance over everything",
        "Tech": "Compétences techniques en avant, stack tech visible, GitHub/portfolio, format épuré efficace",
        "Créatif/Com": "Portfolio/réalisations visuellement mis en avant, créativité démontrée par le design lui-même"
    }.get(type_entreprise, "Design professionnel, équilibre entre lisibilité et personnalité")

    # ── Prompt principal ─────────────────────────────────────────────
    prompt = f"""Tu es un designer UI/UX expert spécialisé en création de CV HTML visuellement exceptionnels. Ta mission : créer un CV HTML complet, moderne, et visuellement IMPRESSIONNANT.

=== PROFIL ET OBJECTIF ===
Nom affiché : {nom_affiche}
Email : {email_affiche}
Objectif candidature : {objectif}
Secteur : {secteur}
Type d'entreprise visée : {type_entreprise} → {entreprise_rules}
Niveau d'expérience : {experience}
Ton souhaité : {ton_cv}
Impression à laisser : {imp_str}
{user_context}

=== GUIDE DE DESIGN — Style "{style}" ===
{design_guide}

=== DONNÉES À UTILISER ===
{"→ PRIORITÉ ABSOLUE : utilise les informations du résumé personnel et des documents ci-dessus pour remplir les sections (expériences, compétences, formation). Si l'info est présente, utilise-la. Complète avec des données cohérentes si manquant." if user_summary or user_docs else "→ Génère des données placeholder réalistes et cohérentes avec le secteur '{secteur}' (postes, entreprises, formations typiques du domaine)."}
- Nom : {nom_affiche}
- Contact : {email_affiche}, +33 6 12 34 56 78, LinkedIn: linkedin.com/in/{nom_affiche.lower().replace(' ','-')}, {"GitHub / Portfolio si secteur tech/créatif" if secteur.lower() in ["tech","informatique","digital","développement","design","web"] else "Localisation Paris ou ville cohérente"}
- {"Photo : utilise exactement cette balise img : <img src='PHOTO_PLACEHOLDER' ...>" if has_photo else f"Photo : cercle CSS (width:90px;height:90px;border-radius:50%;background:{couleurs[0]};display:flex;align-items:center;justify-content:center;color:white;font-size:24px;font-weight:700) avec initiales '{initiales}'"}
- Palette de couleurs choisie : {palette_str}
- IMPORTANT : respecte EXACTEMENT ces couleurs dans tout le design (backgrounds, textes colorés, barres, bordures, etc.)
- Sections : Expérience (3 postes avec dates, entreprise, missions bullet points), Formation (2 diplômes), Compétences (adaptées secteur + level/barres), {"+ Projets si tech/créatif, + Langues, + Certifications si pertinent" if secteur else "+ Langues, + Centre d'intérêts si pertinent"}

=== TON ET STYLE D'ÉCRITURE DU TEXTE ===
- Chaque phrase de texte doit sonner humaine et directe. Pas de style RH robotique.
- Interdit : "dynamique", "passionné(e)", "motivé(e)", "orienté résultats", "force de proposition", "excellent communicant", "sens du travail en équipe", "maîtrise parfaite", "solides compétences en", et tout adjectif ou adverbe de remplissage similaire.
- Les descriptions de poste et missions : une action concrète par ligne, verbe fort, résultat si possible. Max 10 mots par bullet.
- Le résumé ou accroche : 2 phrases maximum, ton confiant mais sobre. Dire ce qu'on fait, pas ce qu'on "est".
- Les compétences : noms courts, pas de qualificatifs ("Python" pas "Excellente maîtrise de Python").
- Le texte doit donner l'impression que c'est le candidat qui a écrit, pas un générateur.

=== EXIGENCES TECHNIQUES OBLIGATOIRES ===
1. Fichier HTML UNIQUE et COMPLET — tout le CSS dans <style>, aucun JS
2. @import Google Font dans le CSS (choisir selon style : Poppins/Inter pour moderne, Cormorant pour premium, Raleway pour créatif, etc.)
3. Format A4 : body max-width 794px, centré (margin: 0 auto), background blanc pour zone CV
4. print-friendly : @media print {{ body {{ margin:0 }} .no-print {{ display:none }} }}
5. CSS avancé autorisé : clip-path, CSS Grid, Flexbox, ::before/::after, CSS variables, gradients, box-shadow
6. Aucune image externe sauf Google Fonts. SVG inline autorisé pour formes décoratives.
7. Chaque section doit être VISUELLEMENT distincte du reste
8. Le design doit être UNIQUE, mémorable, et refléter parfaitement le style "{style}"

=== CE QUI EST INTERDIT ===
- Template générique et ennuyeux (fond blanc, texte noir, aucune forme = REFUSÉ)
- Manque de hiérarchie visuelle
- Texte illisible (contraste insuffisant)
- Trop chargé au point d'être illisible

Retourne UNIQUEMENT le code HTML complet commençant par <!DOCTYPE html>. Aucun texte avant, aucune explication, aucun markdown, aucun bloc ```."""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Tu es un expert en design UI/UX et création de CV HTML premium. Tu génères UNIQUEMENT du code HTML pur, sans aucune explication ni markdown. Ton code est propre, moderne et visuellement impressionnant."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=12000,
            temperature=0.85
        )
        html = resp.choices[0].message.content.strip()
        # Nettoie markdown si présent
        html = re.sub(r"^```[\w]*\s*", "", html)
        html = re.sub(r"\s*```$", "", html)
        # Remplace PHOTO_PLACEHOLDER par la vraie photo si dispo
        if has_photo and ud.get("photo_b64"):
            photo_src = f"data:{photo_mime};base64,{ud['photo_b64']}"
            html = html.replace("PHOTO_PLACEHOLDER", photo_src)
        return jsonify({"html": html})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/template-pdf/<int:tpl_id>", methods=["GET"])
@require_auth
def route_template_pdf(tpl_id):
    u = get_current_user()
    with get_db() as db:
        row = db.execute("SELECT * FROM cv_templates WHERE id=? AND user_id=?", (tpl_id, u["id"])).fetchone()
        if not row: return jsonify({"error":"Template non trouvé"}), 404
    return _html_to_pdf_response(row["html_content"], row["name"])

@app.route("/api/preview-pdf", methods=["POST"])
@require_auth
def route_preview_pdf():
    data = request.json or {}
    html = data.get("html","")
    name = data.get("name","template")
    if not html: return jsonify({"error":"HTML manquant"}), 400
    return _html_to_pdf_response(html, name)

def _html_to_pdf_response(html_content, name):
    """Convertit du HTML en PDF via Playwright.
    Optimisé pour nos templates CV qui sont déjà au format A4
    (max-width: 794px / min-height: 1123px à 96dpi = exactement A4)."""
    import tempfile
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright non installé")
        return jsonify({"error":"Playwright non installé sur le serveur"}), 500

    # Injecte un override CSS pour le print (cache les body bg "écran" décoratifs,
    # force le contenu à occuper toute la page sans marges Playwright)
    print_css = """
<style id="__pdf_override">
@media print {
  html, body {
    background: #fff !important;
    margin: 0 !important;
    padding: 0 !important;
  }
  /* Force tout container max-width à occuper la page A4 sans marge */
  body > * {
    box-shadow: none !important;
    margin: 0 auto !important;
  }
  @page { size: A4; margin: 0; }
}
</style>
"""
    # Injecte avant </head> si possible, sinon en début de body
    if "</head>" in html_content:
        html_content = html_content.replace("</head>", print_css + "</head>", 1)
    elif "<body" in html_content:
        html_content = re.sub(r"(<body[^>]*>)", r"\1" + print_css, html_content, count=1)
    else:
        html_content = print_css + html_content

    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
            f.write(html_content)
            tmp = f.name
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                timeout=60000,  # 60s pour le cold start container
                args=[
                    "--no-sandbox",                # nécessaire en container Railway/Docker
                    "--disable-dev-shm-usage",     # mémoire partagée limitée en container
                    "--disable-setuid-sandbox",
                    "--disable-gpu",               # pas de GPU en container
                    "--disable-software-rasterizer",
                    "--single-process",            # évite les pb d'IPC sur certains containers
                    "--no-zygote",
                ],
            )
            try:
                # Viewport A4 réel : 794x1123 px à 96dpi
                ctx = browser.new_context(viewport={"width": 794, "height": 1123})
                page = ctx.new_page()
                page.goto(
                    f"file:///{tmp.replace(chr(92),'/')}",
                    wait_until="domcontentloaded",  # plus rapide que networkidle, qui hang sur les fonts CDN
                    timeout=15000,
                )
                # Attend les fonts (Google Fonts) et le layout
                try:
                    page.evaluate("document.fonts && document.fonts.ready")
                    page.wait_for_load_state("load", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(800)  # petit buffer pour les @import async

                # Rendu PDF en A4 strict, marges gérées par les @page CSS du template
                pdf_bytes = page.pdf(
                    format="A4",
                    print_background=True,
                    margin={"top":"0","right":"0","bottom":"0","left":"0"},
                    prefer_css_page_size=True,
                )
            finally:
                try: browser.close()
                except Exception: pass

        safe_name = re.sub(r'[^\w\-]','_', name or "CV")[:80] or "CV"
        log.info(f"pdf generated: {safe_name} ({len(pdf_bytes)} bytes)")
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.pdf"'}
        )
    except Exception as e:
        log.exception(f"PDF generation failed: {e}")
        return jsonify({"error": f"Erreur génération PDF : {type(e).__name__}: {str(e)[:200]}"}), 500
    finally:
        if tmp and os.path.exists(tmp):
            try: os.unlink(tmp)
            except Exception: pass

# ═══════════════════════════════════════════════════════════════════════════════
# ADAPTATION CV PDF — direct (sans passer par HTML)
# Pipeline : PDF → extraction blocs (fitz) → IA → ré-injection texte → PDF
# ═══════════════════════════════════════════════════════════════════════════════
import fitz  # PyMuPDF

PDF_MIN_BLOCK_CHARS = 6        # ignore mini-blocs (numéros de page, puces seules)
PDF_MIN_TOTAL_CHARS = 200      # en dessous = PDF probablement scanné/image
PDF_SHRINK_STEPS    = (1.0, 0.96, 0.92, 0.88, 0.84, 0.80)
MAX_PDF_SIZE_MB     = 10


def _pdf_int_to_rgb(c):
    """Couleur span PyMuPDF (int 0xRRGGBB) → tuple (r,g,b) en 0..1."""
    if isinstance(c, int):
        return (((c >> 16) & 0xFF)/255, ((c >> 8) & 0xFF)/255, (c & 0xFF)/255)
    return (0, 0, 0)


def _safe_fontname(font_name):
    """Mappe le nom de police embarqué vers une police builtin PyMuPDF
    (helv/tiro/cour + variantes bold/italic). Pas d'embedding de la police
    d'origine — choix MVP."""
    if not font_name:
        return "helv"
    f = font_name.lower()
    bold   = any(k in f for k in ("bold", "black", "heavy", "semibold", "demi"))
    italic = any(k in f for k in ("italic", "oblique"))
    serif  = (any(k in f for k in ("times", "serif", "roman", "cambria",
                                    "georgia", "garamond")) and "sans" not in f)
    mono   = any(k in f for k in ("mono", "cour", "consol"))
    if mono:
        return "cour"
    if serif:
        return "tibi" if (bold and italic) else "tibo" if bold else "tiit" if italic else "tiro"
    return "hebi" if (bold and italic) else "hebo" if bold else "heli" if italic else "helv"


def _extract_pdf_blocks(doc):
    """Extrait les blocs texte avec leurs propriétés. Returns (blocks, total_chars)."""
    blocks = []
    total = 0
    for page_idx, page in enumerate(doc):
        d = page.get_text("dict")
        for b in d.get("blocks", []):
            if b.get("type") != 0:   # 0 = texte, 1 = image
                continue
            lines = []
            for line in b.get("lines", []):
                spans = line.get("spans", [])
                if not spans:
                    continue
                txt = "".join(s.get("text", "") for s in spans)
                if not txt.strip():
                    continue
                first = spans[0]
                lines.append({
                    "text":  txt,
                    "font":  first.get("font", "helv"),
                    "size":  float(first.get("size", 11)),
                    "color": first.get("color", 0),
                })
            if not lines:
                continue
            block_text = "\n".join(l["text"] for l in lines).strip()
            if len(block_text) < PDF_MIN_BLOCK_CHARS:
                continue
            blocks.append({
                "page":  page_idx,
                "bbox":  tuple(b["bbox"]),
                "text":  block_text,
                "font":  lines[0]["font"],
                "size":  lines[0]["size"],
                "color": lines[0]["color"],
            })
            total += len(block_text)
    return blocks, total


def _ai_adapt_pdf_blocks(provider, api_key, blocks, job_offer_text, docs_text=""):
    """Envoie les blocs à l'IA en JSON, attend un JSON [{id, text}, ...]
    avec mêmes ids et longueur ±10%."""
    payload = [{"id": i, "text": b["text"]} for i, b in enumerate(blocks)]
    prompt = f"""You are a professional CV writer. You will receive CV text blocks extracted from a PDF and a target job offer. Rewrite each block to better match the offer.

Target job offer:
{job_offer_text[:4000]}

{("Candidate documentation (additional source of truth — do not invent beyond this):" + chr(10) + docs_text[:3000]) if docs_text else ""}

CV blocks (JSON array, in reading order):
{json.dumps(payload, ensure_ascii=False)}

CRITICAL constraints (the layout of the original PDF must not break):
- Return a JSON array with the EXACT same ids and the EXACT same number of items.
- Each adapted text MUST keep approximately the same character length (±10%) as the original. Going over WILL break the layout.
- Preserve line breaks: if the original text has N "\\n", the rewritten text should have a similar count.
- Do NOT invent any experience, qualification, date, name, company, school, certification or skill.
- Keep dates, proper nouns, contact info, section headings unchanged unless trivially generic.
- Highlight skills/experiences relevant to the offer; reorder words within a sentence if useful, do not add new bullets.
- Style: short, direct sentences. No filler ("dynamic", "motivated", "passionate", "team player", "synergy", "leverage", "results-driven", "proven track record").
- Output: ONLY a JSON array of objects {{"id": int, "text": str}}. No markdown, no fences, no commentary.

Output:"""
    raw = call_ai(provider, api_key, prompt, max_tokens=8000)
    raw = re.sub(r"^```[\w]*\s*", "", raw.strip())
    raw = re.sub(r"\s*```$",       "", raw.strip())
    m = re.search(r"\[[\s\S]*\]", raw)
    if not m:
        raise ValueError(f"Réponse IA non parsable : {raw[:200]}")
    arr = json.loads(m.group(0))
    return {int(it["id"]): str(it.get("text", "")) for it in arr if "id" in it}


def _insert_text_fit(page, bbox, text, fontname, fontsize, color):
    """Insère le texte dans bbox en réduisant la police progressivement.
    Tronque en dernier recours. Renvoie True si OK."""
    rect = fitz.Rect(bbox)
    col  = _pdf_int_to_rgb(color)
    sf   = _safe_fontname(fontname)
    for k in PDF_SHRINK_STEPS:
        size = max(fontsize * k, 6.0)
        rc = page.insert_textbox(
            rect, text, fontname=sf, fontsize=size, color=col,
            align=fitz.TEXT_ALIGN_LEFT,
        )
        if rc >= 0:   # rc = espace vertical restant ; négatif = débordement
            return True
    # Dernier recours : tronquer
    t = text
    while len(t) > 16:
        t = t[: int(len(t) * 0.85)].rstrip() + "…"
        rc = page.insert_textbox(
            rect, t, fontname=sf, fontsize=max(fontsize * 0.80, 6.0),
            color=col, align=fitz.TEXT_ALIGN_LEFT,
        )
        if rc >= 0:
            return True
    return False


def adapt_pdf_cv(input_pdf_path, job_offer_text, output_pdf_path,
                 provider=None, api_key=None, docs_text=""):
    """Adapte un CV PDF à une offre en conservant le design.
    Returns dict ok/error (cf. docstring de l'API)."""
    if not provider or not api_key:
        return {"error": "Clé API manquante."}
    try:
        doc = fitz.open(input_pdf_path)
    except Exception as e:
        return {"error": f"PDF illisible : {e}"}
    try:
        blocks, total_chars = _extract_pdf_blocks(doc)
        if not blocks or total_chars < PDF_MIN_TOTAL_CHARS:
            return {"error": "PDF non éditable proprement"}
        try:
            adapted = _ai_adapt_pdf_blocks(provider, api_key, blocks,
                                           job_offer_text, docs_text=docs_text)
        except Exception as e:
            log.warning(f"adapt_pdf_cv: IA échouée, fallback original — {e}")
            doc.save(output_pdf_path)
            return {"error": f"Adaptation IA échouée : {e}",
                    "fallback": output_pdf_path}
        # Préparation : redactions (efface l'ancien texte) avant ré-injection
        replacements   = []
        pages_touched  = set()
        for i, b in enumerate(blocks):
            new_t = (adapted.get(i) or "").strip()
            if not new_t or new_t == b["text"]:
                continue
            page = doc[b["page"]]
            page.add_redact_annot(fitz.Rect(b["bbox"]), fill=(1, 1, 1))
            pages_touched.add(b["page"])
            replacements.append((b, new_t))
        for pi in pages_touched:
            doc[pi].apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        # Ré-injection
        ok = 0
        for b, new_t in replacements:
            if _insert_text_fit(doc[b["page"]], b["bbox"], new_t,
                                b["font"], b["size"], b["color"]):
                ok += 1
            else:
                # Fallback bloc : remet l'original pour éviter une zone blanche
                _insert_text_fit(doc[b["page"]], b["bbox"], b["text"],
                                 b["font"], b["size"], b["color"])
        doc.save(output_pdf_path, garbage=4, deflate=True, clean=True)
        return {"ok": True, "path": output_pdf_path,
                "blocks_total": len(blocks), "blocks_replaced": ok}
    except Exception as e:
        return {"error": f"Erreur lors de l'adaptation : {e}"}
    finally:
        try: doc.close()
        except Exception: pass


# ── Routes : upload du CV PDF (Paramètres) + adaptation ────────────────────────
def _cv_pdf_disk_path(user_id):
    """Chemin disque du CV PDF source (1 par user)."""
    return os.path.join(user_cv_dir(user_id), "_source_cv.pdf")

def _user_has_pdf(ud, user_id):
    """True si un PDF source existe (disque OU legacy b64)."""
    return bool(ud.get("cv_pdf_path") and os.path.isfile(ud["cv_pdf_path"])) \
           or bool(ud.get("cv_pdf_b64", "")) \
           or os.path.isfile(_cv_pdf_disk_path(user_id))

def _read_user_pdf_bytes(ud, user_id):
    """Lit le PDF source (priorité disque, fallback b64). Retourne bytes ou None."""
    p = ud.get("cv_pdf_path") or _cv_pdf_disk_path(user_id)
    if os.path.isfile(p):
        with open(p, "rb") as f: return f.read()
    if ud.get("cv_pdf_b64"):
        try: return base64.b64decode(ud["cv_pdf_b64"])
        except Exception: return None
    return None

@app.route("/api/cv-pdf", methods=["GET", "POST", "DELETE"])
@require_auth
def route_cv_pdf():
    u  = get_current_user()
    ud = get_user_data(u["id"])
    if request.method == "GET":
        return jsonify({
            "has_pdf": _user_has_pdf(ud, u["id"]),
            "name":    ud.get("cv_pdf_name", ""),
        })
    if request.method == "DELETE":
        # Suppression DB + disque
        with get_db() as db:
            db.execute(
                "UPDATE user_data SET cv_pdf_b64='', cv_pdf_name='', cv_pdf_path='' WHERE user_id=?",
                (u["id"],)
            )
            db.commit()
        p = _cv_pdf_disk_path(u["id"])
        if os.path.isfile(p):
            try: os.unlink(p)
            except Exception as e: log.warning(f"unlink {p}: {e}")
        return jsonify({"ok": True})
    # POST : nouvelle source PDF → écrit sur disque (pas de b64 en DB)
    data = request.json or {}
    b64  = (data.get("b64") or "").strip()
    name = (data.get("name") or "cv.pdf").strip()[:200]
    if not b64:
        return jsonify({"error": "PDF manquant"}), 400
    try:
        raw = base64.b64decode(b64, validate=True)
    except Exception:
        return jsonify({"error": "Base64 invalide"}), 400
    if len(raw) > MAX_PDF_SIZE_MB * 1024 * 1024:
        return jsonify({"error": f"PDF trop volumineux (>{MAX_PDF_SIZE_MB} Mo)"}), 400
    if not raw.startswith(b"%PDF"):
        return jsonify({"error": "Le fichier ne semble pas être un PDF valide"}), 400
    disk = _cv_pdf_disk_path(u["id"])
    with open(disk, "wb") as f: f.write(raw)
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO user_data(user_id) VALUES(?)", (u["id"],))
        # On vide cv_pdf_b64 si présent (migration progressive vers disque)
        db.execute(
            "UPDATE user_data SET cv_pdf_b64='', cv_pdf_path=?, cv_pdf_name=? WHERE user_id=?",
            (disk, name, u["id"])
        )
        db.commit()
    return jsonify({"ok": True, "name": name})


@app.route("/api/adapt-cv-pdf", methods=["POST"])
@require_auth
@rate_limit(max_calls=20, window_sec=3600, key_fn=lambda: f"ai:{session.get('user_id','?')}")
@require_quota
def route_adapt_cv_pdf():
    """Adapte le CV PDF stocké en paramètres. Renvoie directement le PDF adapté."""
    u = get_current_user()
    provider, api_key = get_ai_keys(u)
    if not api_key:
        return jsonify({"error": "Clé API manquante. Configurez-la dans Paramètres."}), 400
    ud = get_user_data(u["id"])
    if not _user_has_pdf(ud, u["id"]):
        return jsonify({"error": "Aucun CV PDF dans vos paramètres. Importez-le d'abord."}), 400
    pdf_bytes = _read_user_pdf_bytes(ud, u["id"])
    if not pdf_bytes:
        return jsonify({"error": "PDF stocké illisible. Réimporte-le."}), 400
    data = request.json or {}
    job_desc = (data.get("job_desc", "") or "").strip()[:8000]
    company  = (data.get("company", "")  or "").strip()[:200]
    role     = (data.get("role", "")     or "").strip()[:200]
    if len(job_desc) < 30:
        return jsonify({"error": "Collez la description complète du poste"}), 400
    job_offer = f"Poste : {role}\nEntreprise : {company}\n\n{job_desc}"
    docs_text = get_docs_context(u["id"])

    user_dir = user_cv_dir(u["id"])
    in_name  = f"_in_{secrets.token_hex(8)}.pdf"
    out_name = f"CV_{safe_name(company)}_{safe_name(role)}_{today()}.pdf"
    in_path  = os.path.join(user_dir, in_name)
    out_path = os.path.join(user_dir, out_name)
    try:
        with open(in_path, "wb") as f:
            f.write(pdf_bytes)
    except Exception as e:
        log.exception("write input pdf")
        return jsonify({"error": f"Impossible de préparer le PDF : {e}"}), 500

    try:
        result = adapt_pdf_cv(in_path, job_offer, out_path,
                              provider=provider, api_key=api_key,
                              docs_text=docs_text)
    finally:
        try: os.unlink(in_path)
        except Exception: pass

    # Erreur dure (pas d'output produit)
    if "error" in result and not os.path.exists(out_path):
        return jsonify(result), 400
    # On renvoie le PDF en attachment ; on remonte aussi le filename via header custom
    resp = send_file(out_path, mimetype="application/pdf",
                     as_attachment=True, download_name=out_name)
    resp.headers["X-CV-Filename"] = out_name
    if result.get("fallback"):
        resp.headers["X-CV-Fallback"] = "1"
    return resp


# ═══════════════════════════════════════════════════════════════════════════════
# ✨ NOUVEAU SYSTÈME CV : data JSON + templates HTML statiques + render rapide
# ═══════════════════════════════════════════════════════════════════════════════
CV_TEMPLATES_DIR = os.path.join(BASE_DIR, "cv_templates_lib")

# Métadonnées par défaut pour les templates "core" (peuvent être surchargées
# par un commentaire <!-- meta: {...} --> dans le fichier HTML)
_TEMPLATES_META_DEFAULTS = {
    "modern":    {"name": "Moderne",   "category": "Moderne",  "preview": "Sidebar gradient, photo, timeline avec dropcap"},
    "editorial": {"name": "Éditorial", "category": "Premium",  "preview": "Magazine luxe, typo Fraunces, dropcap"},
    "bold":      {"name": "Bold",      "category": "Créatif",  "preview": "Hero asymétrique, Archivo Black, néobrutaliste"},
    "tech":      {"name": "Tech",      "category": "Tech",     "preview": "Dark mode, terminal-style, JetBrains Mono"},
    "premium":   {"name": "Premium",   "category": "Premium",  "preview": "Serif élégant, raffiné, dates en colonne"},
    "creative":  {"name": "Créatif",   "category": "Créatif",  "preview": "Header gradient, formes géométriques"},
}

# Cache (re-scanné au démarrage et toutes les 60 sec si fichiers ajoutés)
_templates_cache = {"data": None, "ts": 0}

def _parse_template_meta(html_head):
    """Cherche un commentaire <!-- meta: {...} --> en tête du HTML.
    Retourne dict ou {} si pas trouvé."""
    m = re.search(r"<!--\s*meta\s*:\s*(\{[\s\S]*?\})\s*-->", html_head[:2000])
    if not m: return {}
    try:
        return json.loads(m.group(1))
    except Exception:
        return {}

def _discover_templates(force=False):
    """Scanne CV_TEMPLATES_DIR et renvoie un dict {id: meta}."""
    if not force and _templates_cache["data"] is not None and (time.time() - _templates_cache["ts"]) < 60:
        return _templates_cache["data"]
    out = {}
    if not os.path.isdir(CV_TEMPLATES_DIR):
        _templates_cache["data"] = {}; _templates_cache["ts"] = time.time()
        return {}
    for fname in sorted(os.listdir(CV_TEMPLATES_DIR)):
        if not fname.endswith(".html"): continue
        if fname.startswith("_"): continue  # _partials cachés
        path = os.path.join(CV_TEMPLATES_DIR, fname)
        tid = re.sub(r"\.html$", "", fname)
        try:
            with open(path, encoding="utf-8") as f:
                head = f.read(3000)
        except Exception as e:
            log.warning(f"template read fail {fname}: {e}")
            continue
        meta_inline = _parse_template_meta(head)
        defaults = _TEMPLATES_META_DEFAULTS.get(tid, {})
        out[tid] = {
            "id":       tid,
            "name":     meta_inline.get("name")     or defaults.get("name") or tid.replace("_", " ").title(),
            "category": meta_inline.get("category") or defaults.get("category") or "Autres",
            "preview":  meta_inline.get("preview")  or defaults.get("preview") or "",
            "tags":     meta_inline.get("tags")     or [],
            "file":     fname,
        }
    _templates_cache["data"] = out
    _templates_cache["ts"] = time.time()
    return out

# Rétro-compat : on expose CV_TEMPLATES comme une vue dynamique
class _TemplatesProxy:
    def __getitem__(self, k): return _discover_templates()[k]
    def __contains__(self, k): return k in _discover_templates()
    def __iter__(self): return iter(_discover_templates())
    def get(self, k, default=None): return _discover_templates().get(k, default)
    def items(self): return _discover_templates().items()
    def keys(self): return _discover_templates().keys()
    def values(self): return _discover_templates().values()
CV_TEMPLATES = _TemplatesProxy()

def _hex_to_rgb(h):
    h = h.lstrip("#")
    if len(h) == 3: h = "".join(c*2 for c in h)
    try: return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    except Exception: return (47, 93, 168)  # default accent

def _adjust_color(h, factor):
    """Éclaircit (factor>1) ou assombrit (factor<1) une couleur hex."""
    r, g, b = _hex_to_rgb(h)
    if factor > 1:
        # éclaircit vers blanc
        r = int(r + (255 - r) * (factor - 1))
        g = int(g + (255 - g) * (factor - 1))
        b = int(b + (255 - b) * (factor - 1))
    else:
        r, g, b = int(r * factor), int(g * factor), int(b * factor)
    r, g, b = max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b))
    return f"#{r:02x}{g:02x}{b:02x}"

def _color_light(h):  return _adjust_color(h, 1.85)  # version très claire (fonds)
def _color_dark(h):   return _adjust_color(h, 0.7)   # version foncée (gradients)

# ── Mini moteur de templates "Mustache-like" (sections + variables) ────────────
import html as _html_mod

def _esc_html(s):
    """Échappe pour HTML. Refuse de stringifier dict/list (anti-leak de contexte
    si jamais une variable de template résolvait un objet complexe par erreur)."""
    if s is None: return ""
    if isinstance(s, (dict, list, tuple, set)): return ""
    return _html_mod.escape(str(s))

def _get_path(ctx, path):
    """Résout 'a.b.c' dans ctx (dict ou liste de dicts).
    Cas spécial : path "." retourne ctx["."] (item courant dans une boucle de scalars),
    sinon ctx lui-même si pas de clé "." (compat)."""
    if path == ".":
        if isinstance(ctx, dict) and "." in ctx:
            return ctx["."]
        return ctx if not isinstance(ctx, dict) else ""
    cur = ctx
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
        if cur is None:
            return None
    return cur

def _is_truthy(v):
    if v is None or v is False or v == "" or v == 0:
        return False
    if isinstance(v, (list, dict)) and len(v) == 0:
        return False
    return True

def _render_template(tpl, ctx):
    """Mini moteur Mustache-like :
    - {{path}}            → valeur escapée
    - {{#path}}...{{/path}}: section truthy ou boucle si liste
    - {{^path}}...{{/path}}: section INVERSÉE (rendue si falsy/vide)
    - {{.}}               → valeur courante dans une boucle de strings
    """
    section_re = re.compile(
        r"\{\{([#^])([\w\.]+)\}\}([\s\S]*?)\{\{/\2\}\}", re.MULTILINE
    )

    def render_sections(text, local_ctx):
        while True:
            m = section_re.search(text)
            if not m:
                break
            kind, path, inner = m.group(1), m.group(2), m.group(3)
            val = _get_path(local_ctx, path)
            if kind == "^":
                # Inversée : render uniquement si val est falsy
                if not _is_truthy(val):
                    text = text[:m.start()] + render_sections(inner, local_ctx) + text[m.end():]
                else:
                    text = text[:m.start()] + text[m.end():]
                continue
            # kind == "#" — section normale
            if isinstance(val, list):
                rendered = ""
                for item in val:
                    if isinstance(item, dict):
                        sub_ctx = {**local_ctx, **item}
                    else:
                        sub_ctx = {**local_ctx, ".": item}
                    rendered += render_sections(inner, sub_ctx)
                text = text[:m.start()] + rendered + text[m.end():]
            elif _is_truthy(val):
                text = text[:m.start()] + render_sections(inner, local_ctx) + text[m.end():]
            else:
                text = text[:m.start()] + text[m.end():]
        # Triple-brace {{{var}}} → valeur RAW (non échappée), pour HTML
        def var_sub_raw(mv):
            p = mv.group(1)
            v = _get_path(local_ctx, p)
            if v is None: return ""
            if isinstance(v, (dict, list, tuple, set)): return ""
            return str(v)
        text = re.sub(r"\{\{\{([\w\.]+)\}\}\}", var_sub_raw, text)
        # Double-brace {{var}} → valeur ÉCHAPPÉE (text-safe)
        def var_sub(mv):
            p = mv.group(1)
            v = _get_path(local_ctx, p)
            return _esc_html(v) if v is not None else ""
        text = re.sub(r"\{\{([\w\.]+)\}\}", var_sub, text)
        return text

    return render_sections(tpl, ctx)

# ── Schéma JSON CV (canonique) ─────────────────────────────────────────────────
def _empty_cv_data():
    return {
        "name": "", "title": "", "summary": "",
        "contact": {"email": "", "phone": "", "location": "", "linkedin": "", "website": ""},
        "experience": [],   # [{role, company, location, date, bullets:[]}]
        "education": [],    # [{degree, school, location, date}]
        "skills": [],       # [{name, level}]   level 1..5
        "languages": [],    # [{name, level}]
        "certifications": [], # [{name, date}]
        "interests": [],    # [str]
    }

def _strip_md_link(s):
    """Retire la syntaxe markdown [text](url) qu'l'IA ajoute parfois (email, URLs)."""
    if not isinstance(s, str): return s
    # [text](url) -> text  (préserve le 1er groupe)
    return re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s).strip()

def _pick(d, *keys, default=""):
    """Retourne la 1ère valeur non-vide trouvée pour les clés candidates."""
    for k in keys:
        if isinstance(d, dict) and d.get(k) not in (None, "", []):
            return d[k]
    return default

def _normalize_experience(items):
    """Normalise les variantes de l'IA → schéma canonique : role, company, location, date, bullets."""
    out = []
    for it in (items or []):
        if not isinstance(it, dict): continue
        role    = _pick(it, "role", "title", "position", "job_title")
        company = _pick(it, "company", "employer", "organization", "organisation")
        loc     = _pick(it, "location", "city", "place")
        date    = _pick(it, "date", "dates", "period", "duration")
        bullets = it.get("bullets") or it.get("achievements") or it.get("description") or []
        if isinstance(bullets, str):
            bullets = [b.strip() for b in re.split(r"\n+|•|·|-", bullets) if b.strip()]
        out.append({
            "role": str(role), "company": str(company), "location": str(loc),
            "date": str(date), "bullets": bullets,
        })
    return out

def _normalize_education(items):
    """Variantes : degree+school+date OU degree+institution+field+dates → canonique."""
    out = []
    for it in (items or []):
        if not isinstance(it, dict): continue
        degree = _pick(it, "degree", "diploma")
        field  = _pick(it, "field", "specialization", "major")
        if field and field.lower() not in degree.lower():
            degree = f"{degree} — {field}" if degree else field
        school = _pick(it, "school", "institution", "university", "establishment")
        loc    = _pick(it, "location", "city", "place")
        date   = _pick(it, "date", "dates", "period", "year", "years")
        out.append({"degree": str(degree), "school": str(school), "location": str(loc), "date": str(date)})
    return out

def _normalize_skills(items):
    out = []
    for it in (items or []):
        if isinstance(it, str):
            out.append({"name": it, "level": 4})
        elif isinstance(it, dict):
            name = _pick(it, "name", "skill", "label")
            try: lvl = int(_pick(it, "level", "value", "rating", default=4))
            except (TypeError, ValueError): lvl = 4
            out.append({"name": str(name), "level": lvl})
    return out

def _normalize_languages(items):
    out = []
    for it in (items or []):
        if isinstance(it, str):
            out.append({"name": it, "level": ""})
        elif isinstance(it, dict):
            name  = _pick(it, "name", "language", "lang")
            level = _pick(it, "level", "proficiency", "fluency", "rating")
            out.append({"name": str(name), "level": str(level)})
    return out

def _normalize_certifications(items):
    out = []
    for it in (items or []):
        if isinstance(it, str):
            out.append({"name": it, "date": ""})
        elif isinstance(it, dict):
            name = _pick(it, "name", "certification", "title", "label")
            date = _pick(it, "date", "year", "issued")
            out.append({"name": str(name), "date": str(date)})
    return out

def _normalize_cv_data(d):
    """Force le schéma + map les variantes de l'IA + dérive les flags has_X et level_pct."""
    base = _empty_cv_data()
    if isinstance(d, dict):
        for k in base:
            if k in d:
                base[k] = d[k]

    # Strings de premier niveau (avec map d'alias)
    if isinstance(d, dict):
        if not base.get("title"):
            base["title"] = _pick(d, "title", "headline", "current_role", "job_title")
        if not base.get("summary"):
            base["summary"] = _pick(d, "summary", "about", "bio", "profile", "objective")
        if not base.get("name"):
            base["name"] = _pick(d, "name", "full_name", "fullname")

    # contact safe + strip markdown
    contact = base.get("contact") or {}
    if isinstance(contact, dict):
        contact = {
            "email":    _strip_md_link(_pick(contact, "email", "mail")),
            "phone":    _pick(contact, "phone", "tel", "mobile"),
            "location": _pick(contact, "location", "city", "address"),
            "linkedin": _strip_md_link(_pick(contact, "linkedin", "linkedin_url")),
            "website":  _strip_md_link(_pick(contact, "website", "site", "portfolio", "url")),
        }
    else:
        contact = _empty_cv_data()["contact"]
    base["contact"] = contact

    # Strip markdown des champs text de premier niveau
    base["summary"] = _strip_md_link(base.get("summary", ""))

    # Listes safe + normalisation
    base["experience"]     = _normalize_experience(base.get("experience"))
    base["education"]      = _normalize_education(base.get("education"))
    base["skills"]         = _normalize_skills(base.get("skills"))
    base["languages"]      = _normalize_languages(base.get("languages"))
    base["certifications"] = _normalize_certifications(base.get("certifications"))
    interests = base.get("interests") or []
    if isinstance(interests, list):
        base["interests"] = [str(x) for x in interests if x]
    else:
        base["interests"] = []

    # Dérivés expérience
    for exp in base["experience"]:
        exp["has_bullets"] = bool(exp.get("bullets"))

    # Dérivés skills (level_pct)
    for s in base["skills"]:
        try: lvl = int(s.get("level", 4))
        except (TypeError, ValueError): lvl = 4
        s["level"] = max(1, min(5, lvl))
        s["level_pct"] = s["level"] * 20

    # Flags has_*
    base["has_experience"]     = bool(base["experience"])
    base["has_education"]      = bool(base["education"])
    base["has_skills"]         = bool(base["skills"])
    base["has_languages"]      = bool(base["languages"])
    base["has_certifications"] = bool(base["certifications"])
    base["has_interests"]      = bool(base["interests"])
    return base

def _initials(name):
    parts = (name or "").split()
    if not parts: return "?"
    if len(parts) == 1: return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()

def _render_cv(data, template_id="modern", color="#2F5DA8", photo_data_uri=""):
    """Render JSON CV → HTML via template choisi."""
    if template_id not in CV_TEMPLATES:
        template_id = "modern"
    tpl_path = os.path.join(CV_TEMPLATES_DIR, CV_TEMPLATES[template_id]["file"])
    with open(tpl_path, encoding="utf-8") as f:
        tpl = f.read()
    ctx = _normalize_cv_data(data)
    ctx["color"]       = color
    ctx["color_dark"]  = _color_dark(color)
    ctx["color_light"] = _color_light(color)
    if photo_data_uri:
        ctx["photo_or_initials"] = f'<img src="{photo_data_uri}" alt="">'
    else:
        ctx["photo_or_initials"] = _initials(ctx.get("name", ""))
    return _render_template(tpl, ctx)

# ── DB schema pour les CV utilisateurs ─────────────────────────────────────────
def _migrate_cv_documents():
    with get_db() as db:
        db.execute("""
        CREATE TABLE IF NOT EXISTS cv_documents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            name        TEXT DEFAULT 'Mon CV',
            data_json   TEXT NOT NULL,
            template_id TEXT DEFAULT 'modern',
            color       TEXT DEFAULT '#2F5DA8',
            created_at  TEXT DEFAULT (datetime('now')),
            updated_at  TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """)
        db.commit()
try:
    _migrate_cv_documents()
except Exception as e:
    log.error(f"_migrate_cv_documents FAILED at boot — continuing : {e}")

# ── Endpoints CV V2 ────────────────────────────────────────────────────────────
@app.route("/api/cv/templates", methods=["GET"])
@require_auth
def route_cv_templates_list():
    """Liste dynamique des templates (auto-découverte du dossier)."""
    cat_filter = (request.args.get("category") or "").strip()
    items = list(CV_TEMPLATES.values())
    if cat_filter and cat_filter.lower() != "tous":
        items = [t for t in items if t.get("category", "").lower() == cat_filter.lower()]
    # Ordre : core templates d'abord, puis alpha
    core_order = ["modern", "editorial", "bold", "tech", "premium", "creative"]
    items.sort(key=lambda t: (
        core_order.index(t["id"]) if t["id"] in core_order else 999,
        t.get("name", "")
    ))
    cats = sorted({t.get("category", "Autres") for t in CV_TEMPLATES.values()})
    return jsonify({
        "templates": items,
        "categories": ["Tous"] + cats,
        "total": len(items),
    })

@app.route("/api/cv/extract", methods=["POST"])
@require_auth
@rate_limit(max_calls=20, window_sec=3600, key_fn=lambda: f"ai:{session.get('user_id','?')}")
@require_quota
def route_cv_extract():
    """Extrait un JSON CV structuré depuis le résumé + docs de l'user via IA.
    À appeler 1 fois pour générer la base, puis l'user édite manuellement."""
    u    = get_current_user()
    ud   = get_user_data(u["id"])
    provider, api_key = get_ai_keys(u)
    if not api_key:
        return jsonify({"error": "Service IA non configuré"}), 503
    summary = (ud.get("summary","") or "").strip()
    docs    = (ud.get("doc_text","") or "").strip()
    if not summary and not docs:
        return jsonify({"error": "Ajoute d'abord ton résumé personnel ou tes documents dans Paramètres."}), 400

    # Exemple concret avec les BONS noms de clés (pas de schéma vide ambigu)
    example = {
        "name": "Marie Dupont",
        "title": "Développeuse Full Stack",
        "summary": "5 ans en JS/Python. Aime construire des produits qui marchent.",
        "contact": {
            "email": "marie@example.com", "phone": "+33 6 12 34 56 78",
            "location": "Paris", "linkedin": "marie-dupont", "website": ""
        },
        "experience": [
            {"role": "Senior Developer", "company": "Acme", "location": "Paris",
             "date": "2022 - Présent",
             "bullets": ["Refonte backend, -40% latence", "Tech lead équipe 4"]}
        ],
        "education": [
            {"degree": "Master Informatique", "school": "Paris-Saclay",
             "location": "Paris", "date": "2018 - 2020"}
        ],
        "skills": [
            {"name": "Python", "level": 5},
            {"name": "React", "level": 4}
        ],
        "languages": [
            {"name": "Français", "level": "Natif"},
            {"name": "Anglais", "level": "C1"}
        ],
        "certifications": [{"name": "AWS Solutions Architect", "date": "2023"}],
        "interests": ["Cyclisme", "Photographie"]
    }
    schema_hint = json.dumps(example, ensure_ascii=False, indent=2)
    prompt = f"""Tu es un expert RH. À partir des informations suivantes, génère un JSON CV.

Nom : {u.get("name","")}
Email : {u.get("email","")}

=== RÉSUMÉ PERSONNEL ===
{summary[:4000]}

=== DOCUMENTS / CV IMPORTÉS ===
{docs[:6000]}

=== STRUCTURE JSON EXACTE À UTILISER (exemple — RESPECTE LES NOMS DE CHAMPS) ===
{schema_hint}

NOMS DE CHAMPS OBLIGATOIRES (ne les renomme PAS, ne les traduis PAS) :
- experience : "role" (pas title/position), "company", "location", "date" (pas dates), "bullets"
- education  : "degree", "school" (pas institution/university), "location", "date"
- skills     : "name", "level" (entier 1-5)
- languages  : "name" (pas language), "level" (pas proficiency)
- certifications : "name", "date"
- interests  : tableau de strings simples ["Tennis", "Photo"]

CONSIGNES CONTENU :
- Remplis chaque champ basé UNIQUEMENT sur les informations ci-dessus. N'INVENTE RIEN.
- Si une info est absente : champ vide "" ou liste vide [].
- "title" = poste actuel ou souhaité (ex: "Bartender", "Développeur Full Stack").
- "summary" = 2 phrases max, accroche pro. Naturel, pas de RH-speak.
- "experience" antichronologique. Pour chaque poste, 2-4 "bullets" : verbe d'action concret, max 12 mots, ajoute un chiffre/résultat si dispo.
- "skills" : 6 à 12. "level" entier de 1 à 5 (5=expert, 3=bon, 1=base).
- "languages" : "level" en string lisible ("Natif", "C2", "B2", "Notions").
- ❌ INTERDITS : "dynamique", "passionné", "motivé", "force de proposition", "orienté résultats", adjectifs creux similaires.
- ❌ Pas de markdown dans les valeurs (pas de [text](url), pas de **gras**).
- Email : juste l'adresse en clair, sans crochets ni parenthèses.

Retourne UNIQUEMENT le JSON, sans ```json, sans commentaire."""

    try:
        raw = call_ai(provider, api_key, prompt, max_tokens=4000)
        raw = re.sub(r"^```[\w]*\s*", "", raw.strip())
        raw = re.sub(r"\s*```$", "", raw.strip())
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return jsonify({"error": "Réponse IA non parsable"}), 500
        data = json.loads(m.group(0))
        # Sanity : pré-remplit nom/email si vides
        if not data.get("name"):    data["name"]    = u.get("name", "")
        if not (data.get("contact") or {}).get("email"):
            data.setdefault("contact", {})["email"] = u.get("email", "")
        return jsonify({"data": _normalize_cv_data(data)})
    except Exception as e:
        log.exception("cv-extract failed")
        return jsonify({"error": str(e)}), 500

@app.route("/api/cv/render", methods=["POST"])
@require_auth
def route_cv_render():
    """Rendu instantané d'un JSON + template + couleur → HTML. Pas d'IA, pas de quota."""
    u    = get_current_user()
    data = request.json or {}
    cv_data     = data.get("data") or {}
    template_id = data.get("template_id", "modern")
    color       = (data.get("color", "#2F5DA8") or "#2F5DA8").strip()
    if not re.match(r"^#[0-9a-fA-F]{6}$", color):
        color = "#2F5DA8"
    ud = get_user_data(u["id"])
    photo_uri = ""
    if ud.get("photo_b64"):
        photo_uri = f"data:{ud.get('photo_mime','image/jpeg')};base64,{ud['photo_b64']}"
    html = _render_cv(cv_data, template_id, color, photo_uri)
    return jsonify({"html": html})

@app.route("/api/cv/pdf", methods=["POST"])
@require_auth
@rate_limit(max_calls=30, window_sec=3600, key_fn=lambda: f"pdf:{session.get('user_id','?')}")
def route_cv_pdf_v2():
    """Rendu CV → PDF directement (un seul appel pour le frontend).
    Body: {data, template_id, color, name}"""
    u    = get_current_user()
    data = request.json or {}
    cv_data     = data.get("data") or {}
    template_id = data.get("template_id", "modern")
    color       = (data.get("color", "#2F5DA8") or "#2F5DA8").strip()
    name        = (data.get("name", "CV") or "CV").strip()[:120]
    if not re.match(r"^#[0-9a-fA-F]{6}$", color):
        color = "#2F5DA8"
    ud = get_user_data(u["id"])
    photo_uri = ""
    if ud.get("photo_b64"):
        photo_uri = f"data:{ud.get('photo_mime','image/jpeg')};base64,{ud['photo_b64']}"
    html = _render_cv(cv_data, template_id, color, photo_uri)
    return _html_to_pdf_response(html, name)

@app.route("/api/cv/documents", methods=["GET"])
@require_auth
def route_cv_documents_list():
    u = get_current_user()
    with get_db() as db:
        rows = db.execute(
            "SELECT id,name,template_id,color,created_at,updated_at FROM cv_documents WHERE user_id=? ORDER BY updated_at DESC",
            (u["id"],)
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/cv/documents", methods=["POST"])
@require_auth
def route_cv_documents_create():
    u = get_current_user()
    data = request.json or {}
    name = (data.get("name") or "Mon CV")[:120]
    cv_data = data.get("data") or _empty_cv_data()
    template_id = data.get("template_id", "modern")
    color = data.get("color", "#2F5DA8")
    if template_id not in CV_TEMPLATES: template_id = "modern"
    if not re.match(r"^#[0-9a-fA-F]{6}$", color or ""): color = "#2F5DA8"
    with get_db() as db:
        cur = db.execute(
            "INSERT INTO cv_documents(user_id,name,data_json,template_id,color) VALUES(?,?,?,?,?)",
            (u["id"], name, json.dumps(cv_data, ensure_ascii=False), template_id, color)
        )
        cv_id = cur.lastrowid
        db.commit()
        row = db.execute("SELECT id,name,template_id,color,created_at,updated_at FROM cv_documents WHERE id=?", (cv_id,)).fetchone()
    return jsonify(dict(row))

@app.route("/api/cv/documents/<int:cv_id>", methods=["GET","PUT","DELETE"])
@require_auth
def route_cv_documents_one(cv_id):
    u = get_current_user()
    with get_db() as db:
        row = db.execute("SELECT * FROM cv_documents WHERE id=? AND user_id=?", (cv_id, u["id"])).fetchone()
        if not row: return jsonify({"error":"Non trouvé"}), 404
        if request.method == "DELETE":
            db.execute("DELETE FROM cv_documents WHERE id=?", (cv_id,))
            db.commit()
            return jsonify({"ok": True})
        if request.method == "PUT":
            data = request.json or {}
            sets, vals = [], []
            if "name" in data:        sets.append("name=?");        vals.append(data["name"][:120])
            if "data" in data:        sets.append("data_json=?");   vals.append(json.dumps(data["data"], ensure_ascii=False))
            if "template_id" in data and data["template_id"] in CV_TEMPLATES:
                sets.append("template_id=?"); vals.append(data["template_id"])
            if "color" in data and re.match(r"^#[0-9a-fA-F]{6}$", data.get("color","") or ""):
                sets.append("color=?"); vals.append(data["color"])
            if sets:
                sets.append("updated_at=datetime('now')")
                db.execute(f"UPDATE cv_documents SET {','.join(sets)} WHERE id=?", (*vals, cv_id))
                db.commit()
            row = db.execute("SELECT * FROM cv_documents WHERE id=?", (cv_id,)).fetchone()
    out = dict(row)
    out["data"] = json.loads(out.pop("data_json") or "{}")
    return jsonify(out)


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES ADMIN
# ═══════════════════════════════════════════════════════════════════════════════

# ── Rappels d'entretien (cron externe → /api/cron/run-reminders) ───────────────
CRON_TOKEN = os.environ.get("CRON_TOKEN", "").strip()

def _send_interview_reminders():
    """Scanne les stages prévus pour DEMAIN, envoie un mail si pas déjà fait.
    Returns dict {sent: N, skipped: N, errors: [...]}"""
    tomorrow = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    sent, skipped, errors = 0, 0, []
    with get_db() as db:
        rows = db.execute(
            """SELECT s.id, s.stage_type, s.scheduled_date, s.notes,
                      s.application_id, s.user_id,
                      u.email, u.name,
                      a.company, a.role_name, a.url
               FROM interview_stages s
               JOIN users u ON u.id = s.user_id
               JOIN applications a ON a.id = s.application_id
               WHERE s.scheduled_date = ?
                 AND (s.reminder_sent IS NULL OR NOT s.reminder_sent)
                 AND (s.result = 'En attente' OR s.result IS NULL OR s.result = '')
                 AND s.stage_type != 'Candidature envoyée'
                 AND (u.deleted_at = '' OR u.deleted_at IS NULL)""",
            (tomorrow,)
        ).fetchall()
        for r in rows:
            if not r["email"]:
                skipped += 1; continue
            stage = r["stage_type"] or "Entretien"
            company = r["company"] or "l'entreprise"
            role = r["role_name"] or "le poste"
            notes = (r["notes"] or "").strip()
            url = r["url"] or ""
            subj = f"📅 Rappel : {stage} demain — {company}"
            text = (
                f"Salut {r['name'] or ''},\n\n"
                f"Petit rappel : tu as un {stage.lower()} demain pour {role} chez {company}.\n\n"
                + (f"📌 Tes notes :\n{notes}\n\n" if notes else "")
                + (f"🔗 Offre : {url}\n\n" if url else "")
                + "Bonne chance !\n— JobFinder"
            )
            html = (
                f"<p>Salut <strong>{r['name'] or ''}</strong>,</p>"
                f"<p>Petit rappel : tu as un <strong>{stage.lower()}</strong> demain "
                f"pour <strong>{role}</strong> chez <strong>{company}</strong>.</p>"
                + (f"<p><strong>📌 Tes notes :</strong><br>{notes.replace(chr(10), '<br>')}</p>" if notes else "")
                + (f"<p>🔗 <a href='{url}'>Voir l'offre</a></p>" if url else "")
                + "<p>Bonne chance !<br>— JobFinder</p>"
            )
            ok = send_email(r["email"], subj, text, html)
            if ok:
                db.execute("UPDATE interview_stages SET reminder_sent=? WHERE id=?", (True, r["id"]))
                sent += 1
            else:
                errors.append({"stage_id": r["id"], "email": r["email"]})
        db.commit()
    return {"sent": sent, "skipped": skipped, "errors": errors,
            "tomorrow": tomorrow, "candidates": len(rows)}

@app.route("/api/cron/run-reminders", methods=["POST", "GET"])
def route_cron_reminders():
    """À hitter quotidiennement (matin) par un service de cron externe.
    Sécurité par token : Authorization: Bearer <CRON_TOKEN> ou ?token=<...>."""
    token = (
        request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        or request.args.get("token", "").strip()
    )
    if not CRON_TOKEN or token != CRON_TOKEN:
        return jsonify({"error": "Token cron invalide"}), 401
    result = _send_interview_reminders()
    log.info(f"cron reminders: {result}")
    return jsonify(result)

@app.route("/api/admin/run-reminders", methods=["POST"])
@require_role("admin")
def route_admin_run_reminders():
    """Trigger manuel pour l'admin (debug / test)."""
    result = _send_interview_reminders()
    log.info(f"admin run-reminders: {result} by={get_current_user()['id']}")
    return jsonify(result)

@app.route("/api/admin/backup-db", methods=["GET"])
@require_role("admin")
def route_admin_backup_db():
    """Backup snapshot de la DB.
    - SQLite : VACUUM INTO + send_file
    - Postgres : redirige vers Railway Backups (intégré, plus efficace)"""
    if USE_POSTGRES:
        return jsonify({
            "info": "DB Postgres détectée. Utilise les Backups Railway intégrés "
                    "(onglet Backups du service Postgres) → snapshots automatiques + restauration en 1 clic.",
            "use_railway_backups": True,
        })
    import tempfile
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
    tmp.close()
    try:
        os.unlink(tmp.name)
    except Exception: pass
    try:
        with get_db() as db:
            db.execute(f"VACUUM INTO ?", (tmp.name,))
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log.info(f"backup-db: by={get_current_user()['id']}")
        return send_file(
            tmp.name, mimetype="application/x-sqlite3",
            as_attachment=True, download_name=f"jobfinder_{ts}.db"
        )
    except Exception as e:
        log.exception("backup-db failed")
        try: os.unlink(tmp.name)
        except Exception: pass
        return jsonify({"error": f"Backup failed: {e}"}), 500

@app.route("/api/admin/stats", methods=["GET"])
@require_role("admin")
def route_admin_stats():
    """Stats globales pour le dashboard admin."""
    ym = _ym()
    with get_db() as db:
        total_users     = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        verified_users  = db.execute("SELECT COUNT(*) c FROM users WHERE email_verified").fetchone()["c"]
        new_users_week  = db.execute(
            "SELECT COUNT(*) c FROM users WHERE created_at >= datetime('now','-7 days')"
        ).fetchone()["c"]
        total_apps      = db.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"]
        total_stages    = db.execute("SELECT COUNT(*) c FROM interview_stages").fetchone()["c"]
        ai_calls_month  = db.execute(
            "SELECT COALESCE(SUM(count),0) s FROM usage_quotas WHERE ym=?", (ym,)
        ).fetchone()["s"]
        top_users = db.execute(
            """SELECT u.id, u.email, COALESCE(q.count,0) AS used
               FROM users u
               LEFT JOIN usage_quotas q ON q.user_id=u.id AND q.ym=?
               ORDER BY used DESC LIMIT 5""",
            (ym,)
        ).fetchall()
    return jsonify({
        "users": {"total": total_users, "verified": verified_users, "new_7d": new_users_week},
        "applications": total_apps,
        "interview_stages": total_stages,
        "ai_calls_this_month": ai_calls_month,
        "top_users_by_usage": [dict(r) for r in top_users],
        "ym": ym,
    })

@app.route("/api/admin/users", methods=["GET"])
@require_role("admin")
def route_admin_users():
    """Liste des users avec leur quota mensuel et leur consommation actuelle."""
    ym = _ym()
    with get_db() as db:
        rows = db.execute(
            """SELECT u.id, u.email, u.name, u.role, u.created_at,
                      u.email_verified, u.monthly_quota,
                      COALESCE(q.count, 0) AS used
               FROM users u
               LEFT JOIN usage_quotas q ON q.user_id = u.id AND q.ym = ?
               ORDER BY u.created_at DESC""",
            (ym,)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # 0 = défaut global (DEFAULT_MONTHLY_AI_QUOTA)
        d["effective_quota"] = d["monthly_quota"] or DEFAULT_MONTHLY_AI_QUOTA
        d["quota_is_default"] = (d["monthly_quota"] == 0 or d["monthly_quota"] is None)
        d["ym"] = ym
        out.append(d)
    return jsonify(out)

@app.route("/api/admin/users/<int:uid>", methods=["PUT","DELETE"])
@require_role("admin")
def route_admin_user(uid):
    me = get_current_user()
    with get_db() as db:
        if request.method == "DELETE":
            if uid == me["id"]:
                return jsonify({"error": "Impossible de supprimer votre propre compte"}), 400
            db.execute("DELETE FROM users WHERE id=?", (uid,)); db.commit()
            log.info(f"admin delete user: id={uid} by={me['id']}")
            return jsonify({"ok": True})
        data = request.json or {}
        sets, vals = [], []
        if "role" in data:
            if data["role"] not in ("membre","pro","admin"):
                return jsonify({"error": "Rôle invalide"}), 400
            sets.append("role=?"); vals.append(data["role"])
        if "monthly_quota" in data:
            try:
                q = int(data["monthly_quota"])
            except (TypeError, ValueError):
                return jsonify({"error": "Quota invalide (entier requis, 0 = défaut global)"}), 400
            if q < 0 or q > 1_000_000:
                return jsonify({"error": "Quota hors bornes (0 à 1 000 000)"}), 400
            sets.append("monthly_quota=?"); vals.append(q)
        if "reset_usage" in data and data["reset_usage"]:
            # Reset compteur du mois courant pour cet utilisateur
            db.execute("DELETE FROM usage_quotas WHERE user_id=? AND ym=?", (uid, _ym()))
        if sets:
            db.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", (*vals, uid))
        db.commit()
        log.info(f"admin update user: id={uid} fields={list(data.keys())} by={me['id']}")
        # Renvoie le row mis à jour avec le format de la liste
    # Réutilise la requête de listing pour cohérence
    ym = _ym()
    with get_db() as db:
        row = db.execute(
            """SELECT u.id, u.email, u.name, u.role, u.created_at,
                      u.email_verified, u.monthly_quota,
                      COALESCE(q.count, 0) AS used
               FROM users u
               LEFT JOIN usage_quotas q ON q.user_id = u.id AND q.ym = ?
               WHERE u.id = ?""",
            (ym, uid)
        ).fetchone()
    if not row:
        return jsonify({"error": "Utilisateur introuvable"}), 404
    d = dict(row)
    d["effective_quota"] = d["monthly_quota"] or DEFAULT_MONTHLY_AI_QUOTA
    d["quota_is_default"] = (d["monthly_quota"] == 0 or d["monthly_quota"] is None)
    d["ym"] = ym
    return jsonify(d)

# ═══════════════════════════════════════════════════════════════════════════════
# LANCEMENT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5151))
    # Auto-ouverture navigateur en local uniquement (pas en prod)
    if not IS_PROD:
        import webbrowser
        def _open_browser():
            time.sleep(1.2)
            try: webbrowser.open(f"http://localhost:{PORT}")
            except Exception: pass
        threading.Thread(target=_open_browser, daemon=True).start()
    log.info(f"JobFinder démarré → http://localhost:{PORT} (prod={IS_PROD})")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
