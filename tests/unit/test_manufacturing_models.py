"""Model import smoke tests for manufacturing foundation."""

from __future__ import annotations


def test_manufacturing_models_import_cleanly() -> None:
    from app.models import (  # noqa: F401
        BomHeader,
        BomLine,
        ProductionLine,
        ProductionOrder,
        PurchaseLine,
    )
    from app.models.enums import (  # noqa: F401
        ProductionOrderStatusEnum,
    )

    assert BomHeader.__tablename__ == "bom_headers"
    assert BomLine.__tablename__ == "bom_lines"
    assert ProductionOrder.__tablename__ == "production_orders"
    assert ProductionLine.__tablename__ == "production_lines"

    assert ProductionOrderStatusEnum.draft.value == "draft"
    assert ProductionOrderStatusEnum.in_progress.value == "in_progress"
    assert ProductionOrderStatusEnum.completed.value == "completed"
    assert ProductionOrderStatusEnum.cancelled.value == "cancelled"


def test_purchase_line_accepts_production_order_source() -> None:
    from app.models.inventory import PurchaseLine

    assert PurchaseLine.__table__.c.purchase_order_id.nullable is True
    assert PurchaseLine.__table__.c.production_order_id.nullable is True
