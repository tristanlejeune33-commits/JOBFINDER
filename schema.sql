-- ════════════════════════════════════════════════════════════════════════════
-- JobFinder — Schéma PostgreSQL production
-- ════════════════════════════════════════════════════════════════════════════
-- À exécuter une seule fois sur la base Railway PostgreSQL :
--   psql "$DATABASE_URL" -f schema.sql
-- Ou copier-coller dans l'onglet "Data" du plugin Postgres de Railway.
--
-- Idempotent : peut être relancé sans casser les données existantes.
-- ════════════════════════════════════════════════════════════════════════════

-- ── Extensions utiles ───────────────────────────────────────────────────────
CREATE EXTENSION IF NOT EXISTS citext;      -- email case-insensitive
CREATE EXTENSION IF NOT EXISTS pgcrypto;    -- gen_random_uuid() pour tokens


-- ════════════════════════════════════════════════════════════════════════════
-- Table : users
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS users (
    id               SERIAL PRIMARY KEY,
    email            CITEXT      NOT NULL UNIQUE,
    password_hash    TEXT        NOT NULL,
    name             TEXT        NOT NULL DEFAULT '',
    role             TEXT        NOT NULL DEFAULT 'membre'
                                 CHECK (role IN ('membre', 'pro', 'admin')),
    email_verified   BOOLEAN     NOT NULL DEFAULT FALSE,
    api_key_claude   TEXT        NOT NULL DEFAULT '',
    api_key_openai   TEXT        NOT NULL DEFAULT '',
    ai_provider      TEXT        NOT NULL DEFAULT 'Claude (Anthropic)',
    last_login_at    TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT users_email_format CHECK (email ~* '^[^@\s]+@[^@\s]+\.[^@\s]+$')
);

CREATE INDEX IF NOT EXISTS idx_users_email       ON users (email);
CREATE INDEX IF NOT EXISTS idx_users_created_at  ON users (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_users_role        ON users (role);


-- ════════════════════════════════════════════════════════════════════════════
-- Table : applications (candidatures)
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS applications (
    id               SERIAL PRIMARY KEY,
    user_id          INTEGER     NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company          TEXT        NOT NULL DEFAULT '',
    role_name        TEXT        NOT NULL DEFAULT '',
    job_desc         TEXT        NOT NULL DEFAULT '',
    status           TEXT        NOT NULL DEFAULT 'Envoyée',
    cv_filename      TEXT        NOT NULL DEFAULT '',
    notes            TEXT        NOT NULL DEFAULT '',
    url              TEXT        NOT NULL DEFAULT '',
    applied_date     DATE        NOT NULL DEFAULT CURRENT_DATE,
    interview_prep   TEXT        NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_applications_user_id        ON applications (user_id);
CREATE INDEX IF NOT EXISTS idx_applications_user_created   ON applications (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_applications_status         ON applications (user_id, status);
CREATE INDEX IF NOT EXISTS idx_applications_applied_date   ON applications (user_id, applied_date DESC);


-- ════════════════════════════════════════════════════════════════════════════
-- Table : interview_stages (étapes d'entretien / planning)
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS interview_stages (
    id               SERIAL PRIMARY KEY,
    application_id   INTEGER     NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    user_id          INTEGER     NOT NULL REFERENCES users(id)        ON DELETE CASCADE,
    stage_type       TEXT        NOT NULL DEFAULT 'Entretien',
    scheduled_date   TIMESTAMPTZ,
    notes            TEXT        NOT NULL DEFAULT '',
    result           TEXT        NOT NULL DEFAULT 'En attente',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stages_user_id        ON interview_stages (user_id);
CREATE INDEX IF NOT EXISTS idx_stages_application    ON interview_stages (application_id);
CREATE INDEX IF NOT EXISTS idx_stages_scheduled      ON interview_stages (user_id, scheduled_date);


-- ════════════════════════════════════════════════════════════════════════════
-- Table : user_data (profil étendu, 1:1 avec users)
-- ────────────────────────────────────────────────────────────────────────────
-- Contient : photo, résumé, documents importés, CV de base HTML, CV parsé JSON
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS user_data (
    user_id          INTEGER     PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    summary          TEXT        NOT NULL DEFAULT '',
    doc_text         TEXT        NOT NULL DEFAULT '',
    doc_names        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    photo_b64        TEXT        NOT NULL DEFAULT '',
    photo_mime       TEXT        NOT NULL DEFAULT 'image/jpeg',
    cv_html          TEXT        NOT NULL DEFAULT '',
    cv_name          TEXT        NOT NULL DEFAULT '',
    pdf_cv_json      JSONB,
    pdf_cv_raw       TEXT        NOT NULL DEFAULT '',
    pdf_cv_preview   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);


-- ════════════════════════════════════════════════════════════════════════════
-- Table : cv_templates (templates HTML enregistrés par l'utilisateur)
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cv_templates (
    id               SERIAL PRIMARY KEY,
    user_id          INTEGER     NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name             TEXT        NOT NULL DEFAULT 'Template',
    style            TEXT        NOT NULL DEFAULT 'Moderne',
    color            TEXT        NOT NULL DEFAULT '#7c3aed',
    html_content     TEXT        NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cv_templates_user ON cv_templates (user_id, created_at DESC);


-- ════════════════════════════════════════════════════════════════════════════
-- Table : cv_adaptes (NOUVEAU — historique des CV générés par l'IA)
-- ────────────────────────────────────────────────────────────────────────────
-- Remplace le stockage fichier dans /cv_adaptes/. Tout est en DB donc
-- persistant indépendamment des volumes Railway.
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS cv_adaptes (
    id               SERIAL PRIMARY KEY,
    user_id          INTEGER     NOT NULL REFERENCES users(id)        ON DELETE CASCADE,
    application_id   INTEGER              REFERENCES applications(id) ON DELETE SET NULL,
    filename         TEXT        NOT NULL,
    company          TEXT        NOT NULL DEFAULT '',
    role_name        TEXT        NOT NULL DEFAULT '',
    html_content     TEXT        NOT NULL,
    source           TEXT        NOT NULL DEFAULT 'adapt'
                                 CHECK (source IN ('adapt', 'template', 'pdf', 'generate')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cv_adaptes_user     ON cv_adaptes (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_cv_adaptes_app      ON cv_adaptes (application_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cv_adaptes_user_filename
                                                   ON cv_adaptes (user_id, filename);


-- ════════════════════════════════════════════════════════════════════════════
-- Table : password_reset_tokens (NOUVEAU — reset mot de passe par email)
-- ────────────────────────────────────────────────────────────────────────────
-- On stocke le hash du token, jamais le token clair. Le token clair n'existe
-- que dans le lien envoyé par email.
-- ════════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id               SERIAL PRIMARY KEY,
    user_id          INTEGER     NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash       TEXT        NOT NULL UNIQUE,
    expires_at       TIMESTAMPTZ NOT NULL,
    used_at           TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    request_ip       TEXT
);

CREATE INDEX IF NOT EXISTS idx_prt_user     ON password_reset_tokens (user_id);
CREATE INDEX IF NOT EXISTS idx_prt_expires  ON password_reset_tokens (expires_at);


-- ════════════════════════════════════════════════════════════════════════════
-- Triggers : maintien automatique de updated_at
-- ════════════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_users_updated        ON users;
CREATE TRIGGER trg_users_updated        BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_applications_updated ON applications;
CREATE TRIGGER trg_applications_updated BEFORE UPDATE ON applications
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

DROP TRIGGER IF EXISTS trg_user_data_updated    ON user_data;
CREATE TRIGGER trg_user_data_updated    BEFORE UPDATE ON user_data
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- ════════════════════════════════════════════════════════════════════════════
-- Vue : applications avec compteur de stages (pratique pour le dashboard)
-- ════════════════════════════════════════════════════════════════════════════
CREATE OR REPLACE VIEW v_applications_with_stages AS
SELECT
    a.*,
    COALESCE(s.stage_count, 0)        AS stage_count,
    s.next_stage_date
FROM applications a
LEFT JOIN (
    SELECT
        application_id,
        COUNT(*)                              AS stage_count,
        MIN(scheduled_date) FILTER (WHERE scheduled_date >= NOW()) AS next_stage_date
    FROM interview_stages
    GROUP BY application_id
) s ON s.application_id = a.id;


-- ════════════════════════════════════════════════════════════════════════════
-- Nettoyage automatique des tokens expirés (à lancer régulièrement)
-- Optionnel — exécuter manuellement ou via un cron Railway :
--   DELETE FROM password_reset_tokens WHERE expires_at < NOW() - INTERVAL '7 days';
-- ════════════════════════════════════════════════════════════════════════════
