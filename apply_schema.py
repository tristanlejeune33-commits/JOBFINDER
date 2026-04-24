#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Applique schema.sql sur une base PostgreSQL.

Usage :
    1. Récupère DATABASE_PUBLIC_URL dans Railway → service Postgres → onglet Variables
    2. Sous Windows PowerShell :
           $env:DATABASE_URL="postgresql://postgres:xxx@..."
           python apply_schema.py
       Sous Linux/Mac :
           export DATABASE_URL="postgresql://postgres:xxx@..."
           python apply_schema.py
"""
import os, sys

def main():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        sys.exit("❌ DATABASE_URL non défini. Colle l'URL Railway avant de lancer.")
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    if not os.path.exists(schema_path):
        sys.exit(f"❌ schema.sql introuvable à {schema_path}")

    with open(schema_path, encoding="utf-8") as f:
        sql = f.read()

    print(f"→ Connexion à {url.split('@')[-1].split('?')[0]}")
    import psycopg2
    conn = psycopg2.connect(url)
    conn.autocommit = True  # indispensable pour CREATE EXTENSION
    cur = conn.cursor()
    try:
        cur.execute(sql)
        print("✅ Schéma appliqué avec succès")
        # Liste les tables créées
        cur.execute("""
            SELECT tablename FROM pg_tables
            WHERE schemaname='public' ORDER BY tablename
        """)
        tables = [r[0] for r in cur.fetchall()]
        print(f"→ {len(tables)} tables présentes : {', '.join(tables)}")
    finally:
        cur.close(); conn.close()

if __name__ == "__main__":
    main()
