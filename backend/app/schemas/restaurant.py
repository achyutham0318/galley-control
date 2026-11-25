import uuid

from pydantic import BaseModel, Field


class RestaurantSummaryResponse(BaseModel):
    id: uuid.UUID = Field(description="Restaurant UUID")
    name: str = Field(description="Restaurant display name")
    slug: str = Field(description="Unique URL slug")
    description: str | None = Field(default=None, description="Short description")
    location_tag: str = Field(description="Campus location label")
    active_items_count: int = Field(default=0, description="Available non-archived items")
    active_surplus_deals_count: int = Field(
        default=0, description="Active surplus deals with stock > 0"
    )
    current_wait_minutes: int = Field(
        default=0, description="Estimated kitchen wait time in minutes"
    )
