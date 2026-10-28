from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SearchBaseSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class GlobalSearchResult(SearchBaseSchema):
    entity_type: str
    entity_id: UUID
    title: str
    subtitle: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class GlobalSearchResponse(SearchBaseSchema):
    query: str
    total: int
    results: list[GlobalSearchResult] = Field(default_factory=list)
