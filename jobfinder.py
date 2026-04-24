#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JobFinder — Backend Flask + IA + Auth + SQLite"""

from flask import Flask, jsonify, request, send_file, Response, session
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import sqlite3, json, os, re, datetime, threading, webbrowser, base64, secrets
import requests as http_req
from bs4 import BeautifulSoup

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

# ── Clé OpenAI (variable d'env en prod, fallback hardcodé en local) ────────────
OPENAI_API_KEY = os.environ.get(
    "OPENAI_API_KEY",
    ""
)

# ── Chemins ─────────────────────────────────────────────────────────────────────
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))
DATA_DIR    = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
CV_DIR      = os.environ.get("CV_DIR",   os.path.join(BASE_DIR, "cv_adaptes"))
DB_PATH     = os.path.join(DATA_DIR, "jobfinder.db")
TEMPLATE_CV = os.path.join(BASE_DIR, "cv_vibe_modern_html (3).html")
for d in [DATA_DIR, CV_DIR]:
    os.makedirs(d, exist_ok=True)

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")
# SECRET_KEY MUST be set as env var in production — never use random here
_raw_secret = os.environ.get("SECRET_KEY", "")
if not _raw_secret:
    import warnings
    warnings.warn("SECRET_KEY non définie — sessions non persistantes entre redémarrages !", RuntimeWarning)
    _raw_secret = "dev-only-" + secrets.token_hex(16)   # stable pour la session courante
app.secret_key = _raw_secret
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# ── Adaptateur Base de Données (SQLite local / PostgreSQL Railway) ────────────
# Détecte automatiquement PostgreSQL via DATABASE_URL (fourni par Railway)
_DATABASE_URL = os.environ.get("DATABASE_URL", "")
# Railway génère parfois "postgres://" (déprécié), psycopg2 veut "postgresql://"
if _DATABASE_URL.startswith("postgres://"):
    _DATABASE_URL = _DATABASE_URL.replace("postgres://", "postgresql://", 1)
_DB_TYPE = "postgres" if _DATABASE_URL else "sqlite"

class _PgCursor:
    """Curseur psycopg2 avec interface compatible SQLite (dict rows + lastrowid)"""
    def __init__(self, cur, lastrowid=None):
        self._cur = cur
        self.lastrowid = lastrowid
    def fetchone(self):
        row = self._cur.fetchone()
        return dict(row) if row else None
    def fetchall(self):
        return [dict(r) for r in (self._cur.fetchall() or [])]
    def __iter__(self):
        return (dict(r) for r in self._cur)

class _PgConn:
    """Connexion PostgreSQL compatible avec les patterns SQLite du code existant"""
    import re as _re
    _OR_IGNORE = _re.compile(r'\bINSERT\s+OR\s+IGNORE\s+INTO\b', _re.IGNORECASE)
    _IS_INSERT = _re.compile(r'^\s*INSERT\s+INTO\b', _re.IGNORECASE | _re.MULTILINE)

    def __init__(self, url):
        import psycopg2, psycopg2.extras
        self._conn = psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)

    @classmethod
    def _translate(cls, q):
        """Convertit SQL SQLite → PostgreSQL"""
        is_ignore = bool(cls._OR_IGNORE.search(q))
        q = q.replace('?', '%s')
        q = cls._OR_IGNORE.sub('INSERT INTO', q)
        if is_ignore and 'ON CONFLICT' not in q.upper():
            q = q.rstrip().rstrip(';') + ' ON CONFLICT DO NOTHING'
        return q, is_ignore

    @staticmethod
    def _pg_schema(stmt):
        import re
        stmt = re.sub(r'INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT', 'SERIAL PRIMARY KEY', stmt, flags=re.IGNORECASE)
        stmt = re.sub(r"INTEGER\s+PRIMARY\s+KEY\b", 'INTEGER PRIMARY KEY', stmt, flags=re.IGNORECASE)
        stmt = re.sub(r"datetime\('now'\)", "to_char(NOW(),'YYYY-MM-DD HH24:MI:SS')", stmt)
        stmt = re.sub(r"date\('now'\)", "to_char(CURRENT_DATE,'YYYY-MM-DD')", stmt)
        return stmt

    def execute(self, query, params=()):
        q, is_ignore = self._translate(query)
        is_real_insert = bool(self._IS_INSERT.search(q)) and not is_ignore
        if is_real_insert and 'RETURNING' not in q.upper():
            q = q.rstrip().rstrip(';') + ' RETURNING id'
        cur = self._conn.cursor()
        cur.execute(q, params if params else ())
        lastrowid = None
        if is_real_insert:
            row = cur.fetchone()
            lastrowid = dict(row).get('id') if row else None
        return _PgCursor(cur, lastrowid)

    def executescript(self, script):
        cur = self._conn.cursor()
        for stmt in script.split(';'):
            stmt = stmt.strip()
            if not stmt:
                continue
            stmt = self._pg_schema(stmt)
            try:
                cur.execute(stmt)
            except Exception as e:
                self._conn.rollback()
                raise RuntimeError(f"Schema init error: {e}\nQuery: {stmt}") from e

    def commit(self):   self._conn.commit()
    def rollback(self): self._conn.rollback()
    def close(self):    self._conn.close()
    def __enter__(self): return self
    def __exit__(self, exc_type, *_):
        if exc_type: self._conn.rollback()
        else:        self._conn.commit()
        self._conn.close()

class _SqliteCursor:
    """Curseur SQLite avec fetchone() retournant des dicts (cohérent avec Postgres)"""
    def __init__(self, cur):
        self._cur = cur
        self.lastrowid = cur.lastrowid
    def fetchone(self):
        row = self._cur.fetchone()
        return dict(row) if row else None
    def fetchall(self):
        return [dict(r) for r in (self._cur.fetchall() or [])]
    def __iter__(self):
        return (dict(r) for r in self._cur)

class _SqliteConn:
    """Connexion SQLite avec interface cohérente (dict rows)"""
    def __init__(self, path):
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
    def execute(self, query, params=()):
        return _SqliteCursor(self._conn.execute(query, params))
    def executescript(self, script):
        return self._conn.executescript(script)
    def commit(self):   self._conn.commit()
    def rollback(self): self._conn.rollback()
    def close(self):    self._conn.close()
    def __enter__(self): return self
    def __exit__(self, exc_type, *_):
        if exc_type: self._conn.rollback()
        else:        self._conn.commit()
        self._conn.close()

def get_db():
    if _DB_TYPE == "postgres":
        return _PgConn(_DATABASE_URL)
    return _SqliteConn(DB_PATH)

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

        CREATE TABLE IF NOT EXISTS cv_adaptes (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id        INTEGER NOT NULL,
            application_id INTEGER,
            filename       TEXT NOT NULL,
            company        TEXT DEFAULT '',
            role_name      TEXT DEFAULT '',
            html_content   TEXT NOT NULL,
            source         TEXT DEFAULT 'adapt',
            created_at     TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            token_hash  TEXT UNIQUE NOT NULL,
            expires_at  TEXT NOT NULL,
            used_at     TEXT,
            created_at  TEXT DEFAULT (datetime('now')),
            request_ip  TEXT DEFAULT '',
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """)
    # Migration : ajout colonnes PDF CV (safe si déjà présentes)
    with get_db() as db:
        for col, dflt in [("pdf_cv_json","''"), ("pdf_cv_raw","''"), ("pdf_cv_preview","'{}' ")]:
            try:
                db.execute(f"ALTER TABLE user_data ADD COLUMN {col} TEXT DEFAULT {dflt}")
                db.commit()
            except Exception:
                pass  # colonne déjà présente

init_db()

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

def save_cv_adapte(user_id, filename, html_content, company="", role="", source="adapt", application_id=None):
    """Sauvegarde un CV adapté en base + sur disque (fallback local).
    Retourne le filename (peut être modifié pour éviter les collisions)."""
    # Écrit aussi sur disque si CV_DIR est dispo (pour route_download_pdf/Playwright)
    try:
        os.makedirs(CV_DIR, exist_ok=True)
        with open(os.path.join(CV_DIR, filename), "w", encoding="utf-8") as f:
            f.write(html_content)
    except Exception as e:
        print(f"[CV-DISK-WARN] {e}")
    with get_db() as db:
        # Si même filename existe pour ce user → on écrase (on garde une seule version)
        existing = db.execute(
            "SELECT id FROM cv_adaptes WHERE user_id=? AND filename=?",
            (user_id, filename)
        ).fetchone()
        if existing:
            db.execute(
                "UPDATE cv_adaptes SET html_content=?, company=?, role_name=?, source=?, application_id=? WHERE id=?",
                (html_content, company, role, source, application_id, existing["id"])
            )
        else:
            db.execute(
                "INSERT INTO cv_adaptes(user_id,application_id,filename,company,role_name,html_content,source) VALUES(?,?,?,?,?,?,?)",
                (user_id, application_id, filename, company, role, html_content, source)
            )
        db.commit()
    return filename

def load_cv_adapte(user_id, filename):
    """Relit un CV adapté depuis la DB. Retourne le HTML ou None."""
    with get_db() as db:
        row = db.execute(
            "SELECT html_content FROM cv_adaptes WHERE user_id=? AND filename=?",
            (user_id, filename)
        ).fetchone()
    return row["html_content"] if row else None

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

def get_ai_keys(user):
    claude_key = user.get("api_key_claude", "").strip()
    if claude_key:
        return "Claude (Anthropic)", claude_key
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

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES AUTH
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_file(os.path.join(BASE_DIR, "ui.html"))

@app.route("/api/auth/register", methods=["POST"])
def route_register():
    data  = request.json or {}
    email = data.get("email","").strip().lower()
    pwd   = data.get("password","").strip()
    name  = data.get("name","").strip()
    if not email or not pwd:
        return jsonify({"error": "Email et mot de passe requis"}), 400
    if len(pwd) < 6:
        return jsonify({"error": "Mot de passe trop court (min 6 caractères)"}), 400
    with get_db() as db:
        existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
        if existing:
            return jsonify({"error": "Email déjà utilisé"}), 400
        count = db.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        role  = "admin" if count == 0 else "membre"
        db.execute(
            "INSERT INTO users(email,password_hash,name,role) VALUES(?,?,?,?)",
            (email, generate_password_hash(pwd), name or email.split("@")[0], role)
        )
        db.commit()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    session["user_id"] = user["id"]
    return jsonify({"ok": True, "user": {"id": user["id"], "email": user["email"], "name": user["name"], "role": user["role"]}})

@app.route("/api/auth/login", methods=["POST"])
def route_login():
    data  = request.json or {}
    email = data.get("email","").strip().lower()
    pwd   = data.get("password","").strip()
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], pwd):
        return jsonify({"error": "Email ou mot de passe incorrect"}), 401
    session["user_id"] = row["id"]
    return jsonify({"ok": True, "user": {"id": row["id"], "email": row["email"], "name": row["name"], "role": row["role"]}})

@app.route("/api/auth/logout", methods=["POST"])
def route_logout():
    session.clear()
    return jsonify({"ok": True})

@app.route("/api/auth/me")
def route_me():
    u = get_current_user()
    if not u:
        return jsonify({"user": None})
    return jsonify({"user": {"id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"]}})

# ── Helpers email / reset password ──────────────────────────────────────────
import hashlib, smtplib
from email.mime.text import MIMEText

SMTP_HOST     = os.environ.get("SMTP_HOST", "")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER", "")
SMTP_PASS     = os.environ.get("SMTP_PASS", "")
SMTP_FROM     = os.environ.get("SMTP_FROM", SMTP_USER)
APP_BASE_URL  = os.environ.get("APP_BASE_URL", "http://localhost:5151")
RESET_TTL_MIN = 30  # minutes

def _hash_token(tok):
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()

def _send_email(to, subject, body_text, body_html=None):
    """Envoie un mail via SMTP. Silencieux si SMTP non configuré (dev)."""
    if not SMTP_HOST or not SMTP_USER:
        print(f"[EMAIL-DEV] to={to} subject={subject}\n{body_text}\n")
        return False
    msg = MIMEText(body_html or body_text, "html" if body_html else "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = SMTP_FROM
    msg["To"]      = to
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.sendmail(SMTP_FROM, [to], msg.as_string())
    return True

@app.route("/api/auth/forgot-password", methods=["POST"])
def route_forgot_password():
    data  = request.json or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Email requis"}), 400
    # Réponse neutre : on ne révèle pas si l'email existe (anti-enum)
    generic_ok = {"ok": True, "message": "Si un compte existe, un email a été envoyé."}
    with get_db() as db:
        row = db.execute("SELECT id,email FROM users WHERE email=?", (email,)).fetchone()
        if not row:
            return jsonify(generic_ok)
        token       = secrets.token_urlsafe(32)
        token_hash  = _hash_token(token)
        expires_at  = (datetime.datetime.utcnow() + datetime.timedelta(minutes=RESET_TTL_MIN)).strftime("%Y-%m-%d %H:%M:%S")
        ip          = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        db.execute(
            "INSERT INTO password_reset_tokens(user_id,token_hash,expires_at,request_ip) VALUES(?,?,?,?)",
            (row["id"], token_hash, expires_at, ip)
        )
        db.commit()
    reset_link = f"{APP_BASE_URL.rstrip('/')}/?reset_token={token}"
    subject    = "Réinitialisation de votre mot de passe — JobFinder"
    body_html  = f"""
    <div style="font-family:Inter,sans-serif;max-width:540px;margin:auto;padding:24px;">
      <h2 style="color:#7c3aed;">Réinitialisation de mot de passe</h2>
      <p>Vous avez demandé à réinitialiser votre mot de passe JobFinder.</p>
      <p>Cliquez sur ce lien (valable {RESET_TTL_MIN} minutes) :</p>
      <p><a href="{reset_link}" style="display:inline-block;background:#7c3aed;color:white;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:600;">Choisir un nouveau mot de passe</a></p>
      <p style="color:#666;font-size:13px;">Si vous n'êtes pas à l'origine de cette demande, ignorez cet email.</p>
      <p style="color:#999;font-size:12px;">Lien brut : {reset_link}</p>
    </div>
    """
    try:
        _send_email(email, subject, f"Lien de reset : {reset_link}", body_html)
    except Exception as e:
        # On ne remonte pas l'erreur SMTP au client pour éviter fuite d'info
        print(f"[EMAIL-ERROR] {e}")
    return jsonify(generic_ok)

@app.route("/api/auth/reset-password", methods=["POST"])
def route_reset_password():
    data  = request.json or {}
    token = (data.get("token") or "").strip()
    pwd   = (data.get("password") or "").strip()
    if not token or not pwd:
        return jsonify({"error": "Token et mot de passe requis"}), 400
    if len(pwd) < 6:
        return jsonify({"error": "Mot de passe trop court (min 6 caractères)"}), 400
    token_hash = _hash_token(token)
    with get_db() as db:
        row = db.execute(
            "SELECT id,user_id,expires_at,used_at FROM password_reset_tokens WHERE token_hash=?",
            (token_hash,)
        ).fetchone()
        if not row:
            return jsonify({"error": "Lien invalide"}), 400
        if row.get("used_at"):
            return jsonify({"error": "Lien déjà utilisé"}), 400
        # Convertit expires_at en datetime naïf UTC pour comparer proprement
        exp = row["expires_at"]
        if isinstance(exp, str):
            try:
                exp_dt = datetime.datetime.strptime(exp[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                exp_dt = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        else:
            # Postgres renvoie un datetime (timezone-aware) → on normalise en UTC naïf
            exp_dt = exp.replace(tzinfo=None) if exp.tzinfo else exp
        now = datetime.datetime.utcnow()
        if exp_dt < now:
            return jsonify({"error": "Lien expiré"}), 400
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        db.execute("UPDATE users SET password_hash=? WHERE id=?",
                   (generate_password_hash(pwd), row["user_id"]))
        db.execute("UPDATE password_reset_tokens SET used_at=? WHERE id=?",
                   (now_str, row["id"]))
        db.commit()
    return jsonify({"ok": True, "message": "Mot de passe mis à jour"})

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES CONFIG (par utilisateur)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/config", methods=["GET","POST"])
@require_auth
def route_config():
    u = get_current_user()
    if request.method == "GET":
        ud = get_user_data(u["id"])
        return jsonify({
            "provider":       u.get("ai_provider","Claude (Anthropic)"),
            "has_claude_key": bool(u.get("api_key_claude","")),
            "has_openai_key": True,
            "has_summary":    bool(ud.get("summary","").strip()),
            "has_docs":       bool(ud.get("doc_text","").strip()),
            "has_photo":      bool(ud.get("photo_b64","").strip()),
        })
    data = request.json or {}
    sets = {}
    if "provider" in data:       sets["ai_provider"]    = data["provider"]
    if data.get("api_key"):      sets["api_key_claude"] = data["api_key"]
    if data.get("api_key_openai"): sets["api_key_openai"] = data["api_key_openai"]
    if sets:
        cols = ", ".join(f"{k}=?" for k in sets)
        with get_db() as db:
            db.execute(f"UPDATE users SET {cols} WHERE id=?", (*sets.values(), u["id"]))
            db.commit()
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
            (u["id"], data.get("company",""), data.get("role",""),
             data.get("job_desc",""), data.get("status","Envoyée"),
             data.get("cv_filename",""), data.get("notes",""),
             data.get("url",""), applied)
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
        for k, col in mapping.items():
            if k in data:
                sets.append(f"{col}=?")
                vals.append(data[k])
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
    u    = get_current_user()
    safe = os.path.basename(filename)
    # 1. Disque si présent
    path = os.path.join(CV_DIR, safe)
    if os.path.exists(path):
        return send_file(path, mimetype="text/html")
    # 2. Fallback base de données (prod Railway, volume non configuré)
    html = load_cv_adapte(u["id"], safe)
    if html is None:
        return "CV introuvable", 404
    return Response(html, mimetype="text/html")

@app.route("/api/cv-adaptes", methods=["GET"])
@require_auth
def route_list_cv_adaptes():
    u = get_current_user()
    with get_db() as db:
        rows = db.execute(
            "SELECT id, filename, company, role_name, source, application_id, created_at "
            "FROM cv_adaptes WHERE user_id=? ORDER BY created_at DESC",
            (u["id"],)
        ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/cv-adaptes/<int:cv_id>", methods=["DELETE"])
@require_auth
def route_delete_cv_adapte(cv_id):
    u = get_current_user()
    with get_db() as db:
        row = db.execute("SELECT filename FROM cv_adaptes WHERE id=? AND user_id=?",
                         (cv_id, u["id"])).fetchone()
        if not row:
            return jsonify({"error": "Non trouvé"}), 404
        db.execute("DELETE FROM cv_adaptes WHERE id=?", (cv_id,))
        db.commit()
    # Supprime aussi le fichier si présent (best-effort)
    try:
        p = os.path.join(CV_DIR, os.path.basename(row["filename"]))
        if os.path.exists(p): os.remove(p)
    except Exception:
        pass
    return jsonify({"ok": True})

@app.route("/api/adapt-cv", methods=["POST"])
@require_auth
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
        save_cv_adapte(u["id"], fname, result, company=company, role=role, source="adapt")
        return jsonify({"html": result, "filename": fname})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/adapt-cv-template", methods=["POST"])
@require_auth
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
        save_cv_adapte(u["id"], fname, result, company=company, role=role, source="template")
        return jsonify({"html": result, "filename": fname})
    except Exception as e:
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
    u  = get_current_user()
    ud = get_user_data(u["id"])
    if request.method == "GET":
        return jsonify({"text": ud.get("doc_text",""), "names": json.loads(ud.get("doc_names","[]")), "summary": ud.get("summary","")})
    data = request.json or {}
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO user_data(user_id) VALUES(?)", (u["id"],))
        sets, vals = [], []
        if "text"    in data: sets.append("doc_text=?");  vals.append(data["text"])
        if "names"   in data: sets.append("doc_names=?"); vals.append(json.dumps(data["names"]))
        if "summary" in data: sets.append("summary=?");   vals.append(data["summary"])
        if sets:
            db.execute(f"UPDATE user_data SET {','.join(sets)} WHERE user_id=?", (*vals, u["id"]))
            db.commit()
    return jsonify({"ok": True})

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES IA (SEARCH, FETCH-URL, INTERVIEW-PREP, DOWNLOAD-PDF, EXTRACT-DOC)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/search", methods=["POST"])
@require_auth
def route_search():
    data = request.json or {}
    jobs, err = search_indeed(data.get("query",""), data.get("location","France"))
    return jsonify({"jobs": jobs, "error": err})

@app.route("/api/fetch-url", methods=["POST"])
@require_auth
def route_fetch_url():
    u   = get_current_user()
    data = request.json or {}
    url  = data.get("url","").strip()
    if not url:
        return jsonify({"error": "URL manquante"}), 400
    openai_key = OPENAI_API_KEY
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
def route_download_pdf():
    u        = get_current_user()
    data     = request.json or {}
    filename = data.get("filename","").strip()
    if not filename: return jsonify({"error": "Nom de fichier manquant"}), 400
    safe = os.path.basename(filename)
    path = os.path.join(CV_DIR, safe)
    # Si le fichier n'est plus sur disque (ex: redeploy Railway sans volume),
    # on le recrée depuis la DB.
    if not os.path.exists(path):
        html = load_cv_adapte(u["id"], safe)
        if html is None:
            return jsonify({"error": "CV introuvable"}), 404
        try:
            os.makedirs(CV_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception as e:
            return jsonify({"error": f"Impossible d'écrire le CV : {e}"}), 500
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
    import tempfile
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return jsonify({"error":"Playwright non installé"}), 500
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False, encoding='utf-8') as f:
            f.write(html_content); tmp = f.name
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page    = browser.new_page(viewport={"width":1280,"height":900})
            page.goto(f"file:///{tmp.replace(chr(92),'/')}", wait_until="networkidle")
            page.wait_for_timeout(1500)
            dims = page.evaluate("""()=>{
                const children=[...document.body.children];
                let maxW=0;
                for(const c of children){const r=c.getBoundingClientRect();if(r.width>maxW)maxW=r.width;}
                return {w:maxW||document.body.scrollWidth, h:Math.max(document.body.scrollHeight,document.documentElement.scrollHeight)};
            }""")
            PAD=8
            scale=round(min((210-PAD*2)/(dims['w']*0.264583),(297-PAD*2)/(dims['h']*0.264583),1),4)
            content_w_mm=dims['w']*0.264583*scale
            hm=round(max(PAD,(210-content_w_mm)/2),2)
            content_h_mm=dims['h']*0.264583*scale
            vm=round(max(PAD,(297-content_h_mm)/2),2) if content_h_mm<297-PAD*2 else PAD
            pdf_bytes=page.pdf(format="A4",print_background=True,
                margin={"top":f"{vm}mm","right":f"{hm}mm","bottom":f"{vm}mm","left":f"{hm}mm"},scale=scale)
            browser.close()
        safe_name = re.sub(r'[^\w\-]','_', name)
        return Response(pdf_bytes, mimetype="application/pdf",
            headers={"Content-Disposition":f'attachment; filename="{safe_name}.pdf"'})
    finally:
        if tmp and os.path.exists(tmp): os.unlink(tmp)

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES ADMIN
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/admin/users", methods=["GET"])
@require_role("admin")
def route_admin_users():
    with get_db() as db:
        rows = db.execute("SELECT id,email,name,role,created_at FROM users ORDER BY created_at DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/admin/users/<int:uid>", methods=["PUT","DELETE"])
@require_role("admin")
def route_admin_user(uid):
    me = get_current_user()
    with get_db() as db:
        if request.method == "DELETE":
            if uid == me["id"]:
                return jsonify({"error": "Impossible de supprimer votre propre compte"}), 400
            db.execute("DELETE FROM users WHERE id=?", (uid,)); db.commit()
            return jsonify({"ok": True})
        data = request.json or {}
        if "role" in data:
            if data["role"] not in ("membre","pro","admin"):
                return jsonify({"error": "Rôle invalide"}), 400
            db.execute("UPDATE users SET role=? WHERE id=?", (data["role"], uid)); db.commit()
        row = db.execute("SELECT id,email,name,role,created_at FROM users WHERE id=?", (uid,)).fetchone()
    return jsonify(dict(row))

# ═══════════════════════════════════════════════════════════════════════════════
# ROUTES PDF IMPORT
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_pdf_text(raw_bytes):
    """Extracts text from PDF bytes. Tries pymupdf first, falls back to pypdf."""
    # Try pymupdf (better extraction with layout awareness)
    try:
        import fitz  # pymupdf
        doc = fitz.open(stream=raw_bytes, filetype="pdf")
        pages = []
        for page in doc:
            pages.append(page.get_text("text"))
        return "\n".join(pages)
    except ImportError:
        pass
    except Exception:
        pass
    # Fallback: pypdf (already in requirements)
    import pypdf, io
    reader = pypdf.PdfReader(io.BytesIO(raw_bytes))
    return "\n".join(p.extract_text() or "" for p in reader.pages)


@app.route("/api/pdf-import", methods=["POST"])
@require_auth
def route_pdf_import():
    """
    Accepts: JSON { b64: "<base64 PDF>", name: "cv.pdf" }
    Returns: { cv_json, raw_text, preview }
    Extracts text from PDF, then asks LLM to structure it as JSON.
    """
    u    = get_current_user()
    data = request.json or {}
    b64  = data.get("b64", "")
    if not b64:
        return jsonify({"error": "Aucun fichier reçu."}), 400
    try:
        raw_bytes = base64.b64decode(b64)
    except Exception:
        return jsonify({"error": "Fichier invalide."}), 400

    # ── Extraction texte ────────────────────────────────────────────────────
    try:
        raw_text = _extract_pdf_text(raw_bytes)
    except Exception as e:
        return jsonify({"error": f"Impossible d'extraire le PDF : {e}"}), 500

    raw_text = raw_text.strip()
    if len(raw_text) < 40:
        return jsonify({"error": "PDF illisible ou scanné. Utilisez un PDF avec texte natif."}), 400

    # ── Structuration LLM ───────────────────────────────────────────────────
    provider, api_key = get_ai_keys(u)
    if not api_key:
        # Return raw text without structure (user can still adapt later)
        return jsonify({
            "cv_json": None,
            "raw_text": raw_text[:8000],
            "preview": {"name": "CV importé", "title": "", "n_exp": 0, "n_skills": 0}
        })

    extract_prompt = f"""Extract this CV into a structured JSON object.
Return ONLY valid JSON — no markdown, no code fences, no explanation.

CV text:
{raw_text[:5500]}

Required JSON schema:
{{
  "header": {{
    "full_name": "string",
    "title": "string or null",
    "email": "string or null",
    "phone": "string or null",
    "location": "string or null",
    "linkedin": "string or null"
  }},
  "summary": "string or null",
  "experiences": [
    {{
      "company": "string",
      "title": "string",
      "start_date": "string",
      "end_date": "string or null",
      "current": false,
      "bullets": ["string"]
    }}
  ],
  "education": [
    {{
      "institution": "string",
      "degree": "string",
      "field": "string or null",
      "start_date": "string or null",
      "end_date": "string or null"
    }}
  ],
  "skills": {{
    "technical": ["string"],
    "tools": ["string"],
    "soft": ["string"]
  }},
  "languages": [{{"language": "string", "level": "string"}}],
  "certifications": [{{"name": "string", "issuer": "string or null", "date": "string or null"}}]
}}

Strict rules:
- Never invent any information not present in the text
- Preserve exact company names, dates, institutions and degrees
- If a field is missing, use null or []
- Detect language from CV and respond in that language
- Return ONLY the JSON object, nothing else"""

    try:
        raw_result = call_ai(provider, api_key, extract_prompt, max_tokens=3000)
        clean = raw_result.strip()
        clean = re.sub(r'^```[a-z]*\n?', '', clean)
        clean = re.sub(r'\n?```$', '', clean)
        cv_json = json.loads(clean)
    except (json.JSONDecodeError, Exception):
        # Still useful: return raw text, UI can continue without cv_json
        cv_json = None

    # ── Build preview summary ───────────────────────────────────────────────
    if cv_json:
        h = cv_json.get("header", {}) or {}
        skills = cv_json.get("skills", {}) or {}
        n_skills = len(skills.get("technical", []) or []) + len(skills.get("tools", []) or [])
        preview = {
            "name":     h.get("full_name") or "CV importé",
            "title":    h.get("title") or "",
            "n_exp":    len(cv_json.get("experiences", []) or []),
            "n_edu":    len(cv_json.get("education", []) or []),
            "n_skills": n_skills,
        }
    else:
        preview = {"name": "CV importé", "title": "", "n_exp": 0, "n_skills": 0}

    return jsonify({"cv_json": cv_json, "raw_text": raw_text[:8000], "preview": preview})


@app.route("/api/pdf-adapt", methods=["POST"])
@require_auth
def route_pdf_adapt():
    """
    Accepts: { cv_json, raw_text, job_desc, company, role, template_id }
    Returns: { html, filename }
    Reuses the template rendering pipeline with CV extracted from PDF as context.
    """
    u    = get_current_user()
    data = request.json or {}
    provider, api_key = get_ai_keys(u)
    if not api_key:
        return jsonify({"error": "Clé API manquante. Configurez-la dans Paramètres."}), 400

    cv_json  = data.get("cv_json")
    raw_text = data.get("raw_text", "")
    job_desc = data.get("job_desc", "")
    company  = data.get("company", "")
    role     = data.get("role", "")
    tpl_id   = data.get("template_id")

    if not cv_json and not raw_text:
        return jsonify({"error": "Aucun CV fourni."}), 400
    if not job_desc:
        return jsonify({"error": "Description de l'offre manquante."}), 400

    # ── Build CV context ────────────────────────────────────────────────────
    if cv_json:
        cv_context = ("=== CV STRUCTURÉ (source de vérité — ne rien inventer) ===\n"
                      + json.dumps(cv_json, ensure_ascii=False, indent=2))
    else:
        cv_context = "=== TEXTE DU CV ===\n" + raw_text

    # Merge with user personal summary if available
    extra = get_docs_context(u["id"])

    # ── Load template ───────────────────────────────────────────────────────
    template_html = None
    if tpl_id:
        with get_db() as db:
            row = db.execute("SELECT html_content FROM cv_templates WHERE id=? AND user_id=?",
                             (tpl_id, u["id"])).fetchone()
        if row:
            template_html = row["html_content"]
    if not template_html:
        if not os.path.exists(TEMPLATE_CV):
            return jsonify({"error": "Template CV introuvable."}), 400
        with open(TEMPLATE_CV, encoding="utf-8") as f:
            template_html = f.read()

    # ── Split head / body ───────────────────────────────────────────────────
    ud           = get_user_data(u["id"])
    PHOTO_MARKER = "PORTRAIT_SRC_PLACEHOLDER"
    head_match   = re.search(r'^([\s\S]*?<body[^>]*>)', template_html, re.IGNORECASE)
    body_match   = re.search(r'<body[^>]*>([\s\S]*)</body>', template_html, re.IGNORECASE)
    head_part    = head_match.group(1) if head_match else ""
    body_part    = body_match.group(1) if body_match else template_html
    body_for_ai  = re.sub(
        r'(<div[^>]*class="portrait-wrap"[^>]*>\s*<img[^>]*\ssrc=")[^"]*(")',
        lambda m: m.group(1) + PHOTO_MARKER + m.group(2), body_part, flags=re.DOTALL)

    # ── LLM adaptation prompt ───────────────────────────────────────────────
    extra_block = ("Additional candidate notes:\n" + extra[:2000]) if extra else ""
    prompt = f"""You are a professional CV writer. Rewrite this CV template using the candidate's real information, targeted for a specific job.

Target position: {role}
Company: {company}
Job description:
{job_desc[:3000]}

{cv_context[:5000]}

{extra_block}

HTML body to rewrite (preserve all tags/classes/IDs/attributes exactly):
{body_for_ai}

Instructions:
- Keep every HTML tag, class, ID and attribute exactly as-is
- Replace text content ONLY with information from the CV and docs above
- NEVER invent experiences, companies, degrees, dates, numbers or skills not present in the source
- Adapt and prioritise skills/experiences most relevant to the target role
- Rewrite the summary to target this specific position
- Keep src="{PHOTO_MARKER}" exactly as-is

Writing style — critical:
- Short, direct sentences. No "highly motivated", "passionate", "dynamic", "results-driven".
- Action verb + concrete result per bullet. Max 12 words per bullet.
- Sounds like the candidate wrote it — confident, grounded, not inflated.
- Vary sentence structure. Do not start every bullet the same way.

Rewritten HTML body:"""

    try:
        result = call_ai(provider, api_key, prompt, max_tokens=8000)
        result = re.sub(r'^```[\w]*\n?', '', result.strip())
        result = re.sub(r'\n?```$', '', result.strip())
        if head_part:
            result = head_part + result + "\n</body>\n</html>"
        # Inject photo
        if ud.get("photo_b64"):
            photo_src = f"data:{ud.get('photo_mime','image/jpeg')};base64,{ud['photo_b64']}"
            result = result.replace(PHOTO_MARKER, photo_src)
        else:
            result = result.replace(PHOTO_MARKER, "")
        fname = f"CV_{safe_name(company)}_{safe_name(role)}_{today()}.html"
        save_cv_adapte(u["id"], fname, result, company=company, role=role, source="pdf")
        return jsonify({"html": result, "filename": fname})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTE CV PDF (stockage par compte)
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/api/pdf-cv", methods=["GET", "POST"])
@require_auth
def route_pdf_cv():
    u = get_current_user()
    if request.method == "GET":
        ud = get_user_data(u["id"])
        cv_json_raw = ud.get("pdf_cv_json", "") or ""
        raw_text    = ud.get("pdf_cv_raw", "")  or ""
        preview_raw = ud.get("pdf_cv_preview", "{}") or "{}"
        try:
            cv_json = json.loads(cv_json_raw) if cv_json_raw else None
        except Exception:
            cv_json = None
        try:
            preview = json.loads(preview_raw)
        except Exception:
            preview = {}
        return jsonify({"cv_json": cv_json, "raw_text": raw_text, "preview": preview})
    # POST : sauvegarde ou effacement
    data = request.json or {}
    cv_json  = data.get("cv_json")
    raw_text = data.get("raw_text", "")
    preview  = data.get("preview", {})
    with get_db() as db:
        db.execute("INSERT OR IGNORE INTO user_data(user_id) VALUES(?)", (u["id"],))
        db.execute(
            "UPDATE user_data SET pdf_cv_json=?, pdf_cv_raw=?, pdf_cv_preview=? WHERE user_id=?",
            (json.dumps(cv_json) if cv_json else "", raw_text or "", json.dumps(preview), u["id"])
        )
        db.commit()
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════════
# LANCEMENT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    PORT = int(os.environ.get("PORT", 5151))
    # N'ouvre le navigateur que si on tourne en local (pas en prod)
    if not os.environ.get("RAILWAY_ENVIRONMENT") and not os.environ.get("DYNO"):
        def open_browser():
            import time; time.sleep(1.2)
            webbrowser.open(f"http://localhost:{PORT}")
        threading.Thread(target=open_browser, daemon=True).start()
    print(f"\n  ⚡ JobFinder demarré  →  http://localhost:{PORT}\n")
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)
