"""One-off schema introspection for model generation."""
import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

import psycopg2

url = os.getenv("DATABASE_URL", "")
m = re.match(r"postgresql://([^:]+):([^@]+)@([^:]+):(\d+)/(.+)", url)
user, pw, host, port, db = m.groups()

tables = [
    "business_types",
    "businesses",
    "business_configs",
    "branches",
    "users",
    "roles",
    "permissions",
    "role_permissions",
    "user_roles",
    "refresh_tokens",
]

conn = psycopg2.connect(host=host, port=port, user=user, password=pw, dbname=db)
cur = conn.cursor()

for t in tables:
    cur.execute(
        """
        SELECT column_name, data_type, udt_name, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s
        ORDER BY ordinal_position
        """,
        (t,),
    )
    print(f"=== {t} ===")
    for r in cur.fetchall():
        print(f"  {r[0]:30} {r[2]:25} nullable={r[3]:3} default={r[4]}")
    print()

for en in [
    "subscription_plan_enum",
    "subscription_status_enum",
    "sync_status_enum",
    "user_status_enum",
]:
    cur.execute(
        """
        SELECT enumlabel FROM pg_enum e
        JOIN pg_type t ON e.enumtypid = t.oid
        WHERE t.typname = %s ORDER BY e.enumsortorder
        """,
        (en,),
    )
    print(en, ":", [r[0] for r in cur.fetchall()])

conn.close()
