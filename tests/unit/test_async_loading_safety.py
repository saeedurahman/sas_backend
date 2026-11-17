"""Static guards against async lazy-load failures (MissingGreenlet) in response serialization.

The async session cannot lazy-load: a response schema that reads a relationship the service did
not eager-load fails *while FastAPI serializes the response* (HTTP 500). The DB-backed API smoke
tests (tests/integration/smoke) prove the real endpoints; these checks need no database and catch
the two structural causes early:

* an ORM relationship left on SQLAlchemy's default lazy="select", and
* a recursive (tree) schema embedded inside another response (e.g. a product's category with
  its ``children``), which no finite eager-load option list can fully cover.
"""

from __future__ import annotations

import typing

from pydantic import BaseModel
from sqlalchemy.orm import RelationshipProperty

import app.models  # noqa: F401  (register every mapper)
from app.models.base import Base


def _models_in(annotation: object) -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []
    if typing.get_origin(annotation) is not None:
        for arg in typing.get_args(annotation):
            found.extend(_models_in(arg))
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.append(annotation)
    return found


def _children(model: type[BaseModel]) -> list[type[BaseModel]]:
    out: list[type[BaseModel]] = []
    for field in model.model_fields.values():
        out.extend(_models_in(field.annotation))
    return out


def _reachable(model: type[BaseModel]) -> set[type[BaseModel]]:
    seen: set[type[BaseModel]] = set()
    stack = _children(model)
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(_children(current))
    return seen


def test_no_relationship_uses_implicit_lazy_loading():
    offenders = []
    for mapper in Base.registry.mappers:
        for prop in mapper.iterate_properties:
            if isinstance(prop, RelationshipProperty) and prop.lazy in ("select", True):
                offenders.append(f"{mapper.class_.__name__}.{prop.key}")
    assert not offenders, (
        "relationships on the default lazy='select' raise MissingGreenlet under AsyncSession; "
        f"declare lazy='selectin' or 'noload' explicitly: {offenders}"
    )


def test_recursive_schemas_are_only_top_level_response_models():
    """A tree schema (one that contains itself) may be an endpoint's own response, never nested."""
    from main import app

    offenders = []
    for route in app.routes:
        response_model = getattr(route, "response_model", None)
        if response_model is None:
            continue
        for top in _models_in(response_model):
            for nested in _reachable(top):
                if nested is not top and nested in _reachable(nested):
                    offenders.append(f"{route.path}: {top.__name__} embeds recursive {nested.__name__}")
    assert not offenders, (
        "embed a slim brief schema instead of a recursive tree (it needs unbounded eager loading): "
        + "; ".join(sorted(set(offenders)))
    )


def test_product_response_embeds_slim_category_brief():
    from app.schemas.product import CategoryBrief, ProductResponse

    assert set(CategoryBrief.model_fields) == {"id", "name", "parent_id"}
    assert CategoryBrief in _models_in(ProductResponse.model_fields["category"].annotation)
