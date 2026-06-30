"""Model import smoke tests for restaurant foundation."""

from __future__ import annotations


def test_restaurant_models_import_cleanly() -> None:
    from app.models import (  # noqa: F401
        DiningTable,
        FloorPlan,
        KotOrder,
        KotOrderLine,
        Modifier,
        ModifierGroup,
        ProductModifierGroup,
        Sale,
    )
    from app.models.enums import (  # noqa: F401
        KotStatusEnum,
        ModifierSelectionTypeEnum,
        TableStatusEnum,
    )

    assert Sale.__tablename__ == "sales"
    assert DiningTable.__tablename__ == "tables"
    assert KotOrder.__tablename__ == "kot_orders"
