"""Delete test businesses (and every row that belongs to them) from DATABASE_URL.

    python scripts/cleanup_test_data.py            # DRY RUN: per-table counts, deletes nothing
    python scripts/cleanup_test_data.py --apply    # delete, in one transaction

    # KEEP-LIST mode: delete every business EXCEPT the named ones
    python scripts/cleanup_test_data.py --keep-names "Acme,Beta Shop"
    python scripts/cleanup_test_data.py --keep-names "Acme,Beta Shop" --apply [--i-understand]

Default mode touches only businesses matching the name patterns below. Their rows are found through
``business_id`` on every tenant table (discovered from the live schema) and deleted child
tables first. Nothing is committed unless every delete succeeds.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Names created by tests/helpers/tenants.py: build_pos_tenant ("POSTest <n>"; any name
# starting with POSTest), build_void_race_tenant, build_cancel_race_tenant, build_fifo_tenant.
# Extend here if more helpers get their own prefix.
NAME_PREFIXES = ("POSTest",)
NAME_REGEXES = (r"^(VoidRace|CancelRace|FIFO) [0-9]+$",)

MATCH_SQL = "(" + " OR ".join(
    [f"left(name, :plen{i}) = :prefix{i}" for i in range(len(NAME_PREFIXES))]
    + [f"name ~ :regex{i}" for i in range(len(NAME_REGEXES))]
) + ")"
MATCH_PARAMS = {
    **{f"plen{i}": len(p) for i, p in enumerate(NAME_PREFIXES)},
    **{f"prefix{i}": p for i, p in enumerate(NAME_PREFIXES)},
    **{f"regex{i}": r for i, r in enumerate(NAME_REGEXES)},
}
# Never touched, even if one of them ever gains a business_id column.
PROTECTED_TABLES = ("plans", "licenses", "alembic_version")
PROTECTED_PREFIXES = ("platform_",)
KEEP_MODE_MAX_DELETE_FRACTION = 0.95
IDS = "business_id = ANY(CAST(:ids AS uuid[]))"


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _plan(inspector):
    """Return (delete_order, nullify) for tables carrying business_id.

    ``delete_order`` lists tables children-first. ``nullify`` is [(table, column)] nullable FK
    columns on a dependency cycle (or pointing at their own table), set to NULL before deleting.
    """
    tables = [
        t for t in inspector.get_table_names()
        if t != "businesses"
        and t not in PROTECTED_TABLES
        and not t.startswith(PROTECTED_PREFIXES)
        and any(c["name"] == "business_id" for c in inspector.get_columns(t))
    ]
    scoped = set(tables)
    columns = {t: {c["name"]: c for c in inspector.get_columns(t)} for t in tables}
    fks = []  # (child, parent, constrained columns)
    for t in tables:
        for fk in inspector.get_foreign_keys(t):
            if fk["referred_table"] in scoped:
                fks.append((t, fk["referred_table"], fk["constrained_columns"]))

    graph = defaultdict(set)
    for child, parent, _ in fks:
        if child != parent:
            graph[child].add(parent)

    # Tarjan strongly connected components over child -> parent edges.
    index, low, stack, on_stack, comp_of = {}, {}, [], set(), {}
    counter = [0]

    def visit(v):
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on_stack.add(v)
        for w in graph[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in on_stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp_of[w] = v
                if w == v:
                    break

    sys.setrecursionlimit(10000)
    for t in tables:
        if t not in index:
            visit(t)

    nullify, edges = [], defaultdict(set)
    for child, parent, cols in fks:
        if child == parent or comp_of[child] == comp_of[parent]:
            if all(columns[child][c]["nullable"] for c in cols):
                nullify.extend((child, c) for c in cols)
            elif child != parent:
                raise SystemExit(f"Cannot break FK cycle: {child}({', '.join(cols)}) -> {parent} is NOT NULL.")
        else:
            edges[child].add(parent)

    # A table can be deleted once no remaining scoped table references it.
    referenced_by = defaultdict(set)
    for child, parents in edges.items():
        for p in parents:
            referenced_by[p].add(child)
    order, remaining = [], set(tables)
    while remaining:
        ready = sorted(t for t in remaining if not (referenced_by[t] & remaining))
        if not ready:
            raise SystemExit("Unresolvable FK ordering among: " + ", ".join(sorted(remaining)))
        order.extend(ready)
        remaining -= set(ready)
    return order, sorted(set(nullify))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    parser.add_argument(
        "--keep-names",
        help="comma-separated exact business names (case-insensitive) to KEEP; "
        "every other business becomes a deletion target",
    )
    parser.add_argument(
        "--i-understand",
        action="store_true",
        help="allow --keep-names --apply to delete more than 95%% of all businesses",
    )
    args = parser.parse_args()
    keep = list(dict.fromkeys(n.strip() for n in (args.keep_names or "").split(",") if n.strip()))
    if args.keep_names is not None and not keep:
        print("Error: --keep-names is empty", file=sys.stderr)
        return 1

    from sqlalchemy import create_engine, inspect, text

    from app.core.config import settings

    url = settings.database_url
    for prefix in ("postgresql+asyncpg://", "postgresql://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg2://" + url[len(prefix):]
    engine = create_engine(url)
    print(f"Database: {engine.url.host}:{engine.url.port or 5432}/{engine.url.database}")
    print(f"Mode: {'APPLY (will delete)' if args.apply else 'DRY RUN (nothing is deleted)'}\n")

    order, nullify = _plan(inspect(engine))

    with engine.connect() as conn:
        if not args.apply:
            conn.execute(text("SET TRANSACTION READ ONLY"))
        total = conn.execute(text("SELECT count(*) FROM businesses")).scalar()
        over_limit = False
        if keep:
            keep_lower = [n.lower() for n in keep]
            kept = conn.execute(
                text("SELECT id, name, created_at FROM businesses WHERE lower(name) = ANY(:keep) ORDER BY name"),
                {"keep": keep_lower},
            ).all()
            missing = [n for n in keep if n.lower() not in {r.name.lower() for r in kept}]
            if missing:
                print(f"Error: keep name(s) match no business: {', '.join(missing)}", file=sys.stderr)
                return 1
            print(f"Kept businesses ({len(kept)}):")
            for r in kept:
                print(f"  - {r.name}  id={r.id}  created_at={r.created_at}")
            rows = conn.execute(
                text("SELECT id, name FROM businesses WHERE lower(name) <> ALL(:keep) ORDER BY name"),
                {"keep": keep_lower},
            ).all()
            ids = [str(r.id) for r in rows]
            print(f"\nBusinesses total: {total}; to delete: {len(ids)}; kept: {total - len(ids)}")
            over_limit = len(ids) > KEEP_MODE_MAX_DELETE_FRACTION * total
            if over_limit:
                print(
                    f"WARNING: this deletes {len(ids) / total:.1%} of all businesses; "
                    "--apply additionally requires --i-understand."
                )
            if args.apply and over_limit and not args.i_understand:
                print("Error: refusing to apply without --i-understand", file=sys.stderr)
                return 1
        else:
            rows = conn.execute(
                text(f"SELECT id, name FROM businesses WHERE {MATCH_SQL} ORDER BY name"), MATCH_PARAMS
            ).all()
            ids = [str(r.id) for r in rows]
            print(f"Businesses total: {total}; matching test patterns: {len(ids)}; untouched: {total - len(ids)}")
            for r in rows[:10]:
                print(f"  - {r.name}")
            if len(rows) > 10:
                print(f"  ... and {len(rows) - 10} more")
        if not ids:
            print("\nNothing to delete.")
            return 0

        counts = {
            t: conn.execute(text(f"SELECT count(*) FROM {_quote(t)} WHERE {IDS}"), {"ids": ids}).scalar()
            for t in order
        }
        counts["businesses"] = len(ids)

        print(f"\n{'table':40} {'rows':>10}")
        for t in [*order, "businesses"]:
            if counts[t]:
                print(f"{t:40} {counts[t]:>10}")
        print(f"{'TOTAL':40} {sum(counts.values()):>10}")

        if not args.apply:
            print("\nDry run only. Re-run with --apply to delete these rows.")
            return 0

        # Single transaction: an exception skips commit() and the connection rolls back.
        for t, c in nullify:
            conn.execute(
                text(f"UPDATE {_quote(t)} SET {_quote(c)} = NULL WHERE {IDS} AND {_quote(c)} IS NOT NULL"),
                {"ids": ids},
            )
        deleted = {}
        for t in order:
            if counts[t]:
                deleted[t] = conn.execute(text(f"DELETE FROM {_quote(t)} WHERE {IDS}"), {"ids": ids}).rowcount
        deleted["businesses"] = conn.execute(
            text("DELETE FROM businesses WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": ids}
        ).rowcount
        conn.commit()

        print("\nDeleted:")
        for t, n in deleted.items():
            print(f"{t:40} {n:>10}")
        print(f"{'TOTAL':40} {sum(deleted.values()):>10}")
        left = conn.execute(text(f"SELECT count(*) FROM businesses WHERE {MATCH_SQL}"), MATCH_PARAMS).scalar()
        after = conn.execute(text("SELECT count(*) FROM businesses")).scalar()
        print(f"\nFinal: businesses total {after}; matching test patterns remaining {left}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
