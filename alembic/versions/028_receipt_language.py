"""Receipt layout language (config v2) and legacy receipt options -> layouts.

- Every stored layout config below version 2 is upgraded: ``items.name_language`` is dropped and
  ``style.language`` is set (``both`` when the layout had edited Urdu captions or an Urdu footer,
  otherwise ``en``).
- For each business, legacy receipt options (``app_settings`` ``receipt.footer_text`` /
  ``receipt.paper_size`` / ``receipt.show_tax`` / ``receipt.show_logo``, or the business config
  ``receipt_footer`` / ``receipt`` object) are copied ONCE into its seeded layouts, only where the owner had
  changed them from the defaults. The legacy keys are left in place (still readable) but rendering no
  longer uses them.

Idempotent: only layouts whose config is not yet version 2 are touched, so a second run changes nothing.
The logic lives in ``app.services.receipt_legacy`` (also covered by tests).

Revision ID: 028_receipt_language
Revises: 027_receipt_layouts
Create Date: 2026-11-17
"""

from typing import Sequence, Union

from alembic import op

revision: str = "028_receipt_language"
down_revision: Union[str, Sequence[str], None] = "027_receipt_layouts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from app.services.receipt_legacy import run_migration

    run_migration(op.get_bind())


def downgrade() -> None:
    # Version-2 configs stay valid data; the legacy settings were never deleted.
    pass
