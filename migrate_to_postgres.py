#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Migration SQLite local → PostgreSQL Railway

Usage :
    1. Assure-toi que schema.sql a été exécuté sur la base Railway
    2. Exporte DATABASE_URL (copie depuis Railway → Postgres → Variables → DATABASE_URL)
          export DATABASE_URL="postgresql://postgres:xxx@containers-us-west-X.railway.app:PORT/railway"
          (sous Windows PowerShell : $env:DATABASE_URL="postgresql://...")
    3. Lance : python migrate_to_postgres.py

Options :
    --dry-run   simule sans écrire
    --db PATH   chemin SQLite (défaut : data/jobfinder.db)
    --cv-dir P  dossier CV HTML (défaut : cv_adaptes)
"""
import os, sys, json, sqlite3, argparse, datetime, glob

def log(*a, **k): print("[migrate]", *a, **k)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db",     default=os.path.join(os.path.dirname(__file__), "data", "jobfinder.db"))
    ap.add_argument("--cv-dir", default=os.path.join(os.path.dirname(__file__), "cv_adaptes"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db_path  = args.db
    cv_dir   = args.cv_dir
    dry      = args.dry_run

    pg_url = os.environ.get("DATABASE_URL", "").strip()
    if not pg_url:
        sys.exit("❌ DATABASE_URL non défini. Exporte l'URL Railway Postgres avant de lancer.")
    if pg_url.startswith("postgres://"):
        pg_url = pg_url.replace("postgres://", "postgresql://", 1)

    if not os.path.exists(db_path):
        sys.exit(f"❌ Base SQLite introuvable : {db_path}")

    log(f"Source SQLite : {db_path}")
    log(f"Cible         : {pg_url.split('@')[-1]}")
    log(f"CV dir        : {cv_dir}")
    if dry: log("Mode DRY-RUN — rien ne sera écrit")

    import psycopg2, psycopg2.extras
    sq = sqlite3.connect(db_path)
    sq.row_factory = sqlite3.Row
    pg = psycopg2.connect(pg_url)
    pg_cur = pg.cursor()

    # ── users ───────────────────────────────────────────────────────────────
    users = sq.execute("SELECT * FROM users").fetchall()
    log(f"{len(users)} utilisateurs à migrer")
    id_map_users = {}
    for u in users:
        old_id = u["id"]
        pg_cur.execute("""
            INSERT INTO users (email, password_hash, name, role,
                               api_key_claude, api_key_openai, ai_provider, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,COALESCE(%s::timestamptz, NOW()))
            ON CONFLICT (email) DO UPDATE SET
                name = EXCLUDED.name
            RETURNING id
        """, (u["email"], u["password_hash"], u["name"] or "", u["role"] or "membre",
              u["api_key_claude"] or "", u["api_key_openai"] or "",
              u["ai_provider"] or "Claude (Anthropic)", u["created_at"]))
        new_id = pg_cur.fetchone()[0]
        id_map_users[old_id] = new_id
        log(f"  user {old_id} → {new_id}  ({u['email']})")

    # ── applications ────────────────────────────────────────────────────────
    apps = sq.execute("SELECT * FROM applications").fetchall()
    log(f"{len(apps)} candidatures à migrer")
    id_map_apps = {}
    for a in apps:
        new_uid = id_map_users.get(a["user_id"])
        if not new_uid: continue
        pg_cur.execute("""
            INSERT INTO applications (user_id, company, role_name, job_desc, status,
                                      cv_filename, notes, url, applied_date,
                                      interview_prep, created_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,
                    COALESCE(%s::date, CURRENT_DATE),
                    %s, COALESCE(%s::timestamptz, NOW()))
            RETURNING id
        """, (new_uid, a["company"] or "", a["role_name"] or "", a["job_desc"] or "",
              a["status"] or "Envoyée", a["cv_filename"] or "", a["notes"] or "",
              a["url"] or "", a["applied_date"], a["interview_prep"] or "", a["created_at"]))
        id_map_apps[a["id"]] = pg_cur.fetchone()[0]

    # ── interview_stages ────────────────────────────────────────────────────
    stages = sq.execute("SELECT * FROM interview_stages").fetchall()
    log(f"{len(stages)} étapes d'entretien à migrer")
    for s in stages:
        new_uid = id_map_users.get(s["user_id"])
        new_aid = id_map_apps.get(s["application_id"])
        if not new_uid or not new_aid: continue
        pg_cur.execute("""
            INSERT INTO interview_stages (application_id, user_id, stage_type,
                                          scheduled_date, notes, result, created_at)
            VALUES (%s,%s,%s,%s::timestamptz,%s,%s,COALESCE(%s::timestamptz, NOW()))
        """, (new_aid, new_uid, s["stage_type"] or "Entretien",
              s["scheduled_date"], s["notes"] or "", s["result"] or "En attente",
              s["created_at"]))

    # ── user_data ───────────────────────────────────────────────────────────
    user_datas = sq.execute("SELECT * FROM user_data").fetchall()
    log(f"{len(user_datas)} profils user_data à migrer")
    for ud in user_datas:
        new_uid = id_map_users.get(ud["user_id"])
        if not new_uid: continue
        doc_names  = ud["doc_names"]  if ud["doc_names"]  else "[]"
        preview    = ud["pdf_cv_preview"] if "pdf_cv_preview" in ud.keys() and ud["pdf_cv_preview"] else "{}"
        pdf_json   = ud["pdf_cv_json"]    if "pdf_cv_json"    in ud.keys() else None
        pdf_raw    = ud["pdf_cv_raw"]     if "pdf_cv_raw"     in ud.keys() else ""
        pg_cur.execute("""
            INSERT INTO user_data (user_id, summary, doc_text, doc_names, photo_b64,
                                   photo_mime, cv_html, cv_name,
                                   pdf_cv_json, pdf_cv_raw, pdf_cv_preview)
            VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s,
                    NULLIF(%s,'')::jsonb, %s, COALESCE(NULLIF(%s,'')::jsonb, '{}'::jsonb))
            ON CONFLICT (user_id) DO UPDATE SET
                summary    = EXCLUDED.summary,
                doc_text   = EXCLUDED.doc_text,
                doc_names  = EXCLUDED.doc_names,
                photo_b64  = EXCLUDED.photo_b64,
                photo_mime = EXCLUDED.photo_mime,
                cv_html    = EXCLUDED.cv_html,
                cv_name    = EXCLUDED.cv_name
        """, (new_uid, ud["summary"] or "", ud["doc_text"] or "", doc_names,
              ud["photo_b64"] or "", ud["photo_mime"] or "image/jpeg",
              ud["cv_html"] or "", ud["cv_name"] or "",
              pdf_json or "", pdf_raw or "", preview or "{}"))

    # ── cv_templates ────────────────────────────────────────────────────────
    tpls = sq.execute("SELECT * FROM cv_templates").fetchall()
    log(f"{len(tpls)} templates CV à migrer")
    for t in tpls:
        new_uid = id_map_users.get(t["user_id"])
        if not new_uid: continue
        pg_cur.execute("""
            INSERT INTO cv_templates (user_id, name, style, color, html_content, created_at)
            VALUES (%s,%s,%s,%s,%s, COALESCE(%s::timestamptz, NOW()))
        """, (new_uid, t["name"] or "Template", t["style"] or "Moderne",
              t["color"] or "#7c3aed", t["html_content"] or "", t["created_at"]))

    # ── CV adaptés depuis le dossier cv_adaptes/ ────────────────────────────
    if os.path.isdir(cv_dir):
        files = sorted(glob.glob(os.path.join(cv_dir, "*.html")))
        log(f"{len(files)} fichiers CV HTML dans {cv_dir}")
        # On essaie de rattacher à un user via son email stocké ? Non :
        # on ne peut pas deviner l'owner. On les importe sous l'admin (le plus ancien user).
        if files:
            if not id_map_users:
                log("  ⚠ Aucun user migré, on saute les CV du dossier")
            else:
                admin_uid = min(id_map_users.values())
                log(f"  → rattachés à user_id={admin_uid} (le plus ancien)")
                for fp in files:
                    fname = os.path.basename(fp)
                    try:
                        with open(fp, encoding="utf-8") as f: html = f.read()
                    except Exception as e:
                        log(f"  ✗ {fname} : {e}"); continue
                    # Heuristique pour company/role depuis filename : CV_<company>_<role>_<date>.html
                    parts = fname.replace("CV_","").replace(".html","").split("_")
                    date = parts[-1] if parts and len(parts[-1]) == 10 else ""
                    mid  = parts[:-1] if date else parts
                    company = mid[0] if mid else ""
                    role    = "_".join(mid[1:]) if len(mid) > 1 else ""
                    pg_cur.execute("""
                        INSERT INTO cv_adaptes (user_id, filename, company, role_name,
                                                html_content, source)
                        VALUES (%s,%s,%s,%s,%s,'adapt')
                        ON CONFLICT (user_id, filename) DO NOTHING
                    """, (admin_uid, fname, company.replace("_"," "), role.replace("_"," "), html))

    if dry:
        pg.rollback(); log("DRY-RUN : rollback effectué, rien n'a été committé")
    else:
        pg.commit(); log("✅ Migration terminée, commit effectué")

    sq.close(); pg.close()

if __name__ == "__main__":
    main()
