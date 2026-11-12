"""Default price list per business: backfill + at most one default.

* Businesses with duplicate live defaults keep the oldest; the rest are demoted.
* Businesses with no live default get one: an existing live, active retail list is
  promoted, otherwise a "Retail" list is created (name made unique if taken -
  (business_id, name) is unique even across soft-deleted rows).
* Partial unique index guarantees at most one live default per business.
* Idempotent: safe to re-run. The change_seq triggers from 019 stamp every touched row,
  so backfilled lists reach devices through the sync feed.

Revision ID: 020_default_price_list
Revises: 019_offline_sync
Create Date: 2026-11-12

"""

from typing import Sequence, Union

from alembic import op

revision: str = "020_default_price_list"
down_revision: Union[str, Sequence[str], None] = "019_offline_sync"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Demote duplicate defaults (keep the oldest) so the unique index can be built.
    op.execute(
        """
        UPDATE price_lists p SET is_default = FALSE, updated_at = NOW()
        FROM (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY business_id ORDER BY created_at, id
            ) AS rn
            FROM price_lists
            WHERE is_default AND deleted_at IS NULL
        ) d
        WHERE p.id = d.id AND d.rn > 1
        """
    )

    # 2a. Promote the oldest live, active retail list for businesses with no default.
    op.execute(
        """
        UPDATE price_lists p SET is_default = TRUE, updated_at = NOW()
        FROM (
            SELECT DISTINCT ON (business_id) id
            FROM price_lists pl
            WHERE deleted_at IS NULL AND is_active AND list_type = 'retail'
              AND NOT EXISTS (
                  SELECT 1 FROM price_lists x
                  WHERE x.business_id = pl.business_id
                    AND x.is_default AND x.deleted_at IS NULL
              )
            ORDER BY business_id, created_at, id
        ) c
        WHERE p.id = c.id
        """
    )

    # 2b. Create "Retail" for every business still without a live default.
    op.execute(
        """
        INSERT INTO price_lists (id, business_id, name, list_type, is_default, is_active,
                                 created_at, updated_at)
        SELECT gen_random_uuid(), b.id,
               CASE
                   WHEN NOT EXISTS (SELECT 1 FROM price_lists n
                                    WHERE n.business_id = b.id AND n.name = 'Retail')
                       THEN 'Retail'
                   WHEN NOT EXISTS (SELECT 1 FROM price_lists n
                                    WHERE n.business_id = b.id AND n.name = 'Retail Default')
                       THEN 'Retail Default'
                   ELSE 'Retail Default ' || LEFT(b.id::text, 8)
               END,
               'retail', TRUE, TRUE, NOW(), NOW()
        FROM businesses b
        WHERE NOT EXISTS (
            SELECT 1 FROM price_lists x
            WHERE x.business_id = b.id AND x.is_default AND x.deleted_at IS NULL
        )
        """
    )

    # 3. At most one live default per business. The 019 change_seq triggers are deferred
    #    to commit; fire them now, since CREATE INDEX refuses with pending trigger events.
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute("SET CONSTRAINTS ALL DEFERRED")
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_price_lists_one_default
        ON price_lists (business_id)
        WHERE is_default AND deleted_at IS NULL
        """
    )


def downgrade() -> None:
    # Backfilled rows are ordinary data and are left in place.
    op.execute("DROP INDEX IF EXISTS uq_price_lists_one_default")
