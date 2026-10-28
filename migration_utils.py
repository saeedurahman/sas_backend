"""Helpers to run raw SQL files from Alembic migrations."""
import sqlparse
from pathlib import Path
from sqlalchemy import text

SCHEMA_DIR = Path(__file__).parent / "schema"
SEEDS_DIR = Path(__file__).parent / "seeds"

INITIAL_SCHEMA_FILES = [
    "001_extensions_and_enums.sql",
    "002_group1_tenant_auth.sql",
    "003_group2_product_catalog.sql",
    "004_group3_inventory.sql",
    "005_group4_manufacturing.sql",
    "006_group5_sales.sql",
    "007_group6_restaurant.sql",
    "008_group7_expenses.sql",
    "009_group8_cash_register.sql",
    "010_group9_accounting.sql",
    "011_group10_notifications_settings.sql",
    "012_utilities_rls_views.sql",
    "013_fifo_functions.sql",
]


def _execute_sql(connection, sql: str):
    statements = sqlparse.split(sql)
    for stmt in statements:
        clean = stmt.strip()
        if not clean:
            continue
        parsed = sqlparse.parse(clean)[0]
        is_only_comments = all(
            token.ttype in (
                sqlparse.tokens.Comment.Single,
                sqlparse.tokens.Comment.Multiline,
            )
            or str(token).strip() == ""
            for token in parsed.flatten()
        )
        if is_only_comments:
            continue
        connection.execute(text(clean))


def run_sql_file(connection, filepath: Path):
    sql = filepath.read_text(encoding="utf-8")
    _execute_sql(connection, sql)


def run_schema_files(connection, files: list):
    for name in files:
        filepath = SCHEMA_DIR / name
        if filepath.exists():
            run_sql_file(connection, filepath)
        else:
            raise FileNotFoundError(f"Schema file not found: {filepath}")